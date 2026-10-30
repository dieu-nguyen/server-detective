# System Architecture — Windows Server Multi-Service Telegram Debug Agent

## 1. Overview & Goals

The **Windows Server Multi-Service Telegram Debug Agent** is a Python daemon deployed directly on a Windows Server that hosts multiple Python services, a Node.js gateway, PM2, ngrok, n8n, and a MySQL database.

It allows an engineering team to investigate outages, inspect logs, check tunnel/workflow health, run read-only database diagnostics, and safely trigger service restarts from mobile or desktop via **Telegram**—without needing to RDP or SSH into the Windows Server.

### Core Architectural Principles
1. **Single Switch Linking Auth Mode & Execution Engine (`cli_subscription` vs. `sdk_apikey`):**
   - `cli_subscription`: Uses a flat-rate monthly subscription (Cursor default, or Claude Code / OpenAI Codex / Gemini CLI) by delegating the agentic reasoning loop to the official CLI binary (`agent -p`, `claude -p`, `codex exec`).
   - `sdk_apikey`: Uses a pay-as-you-go API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`) by running a fast, in-memory Python async tool-calling loop.
2. **Write Tools Once (`ops_toolkit.py`):**
   - Both engines share the exact same operational toolkit (`ops_toolkit.py`).
   - `sdk_apikey` imports `OpsToolkit` in-process as Python methods.
   - `cli_subscription` invokes `python ops_toolkit.py <subcommand>` via CLI (while also having read-only access to service source code directories).
3. **Defense-in-Depth Safety:**
   - **Team RBAC:** Telegram group allowlist + `admins` and `viewers` roles.
   - **Zero Direct Destructive Execution:** Neither engine can restart a service or kill a query directly. Both call `propose_action`, which posts an interactive `[✅ Approve (Admins Only)]` / `[❌ Reject]` button in Telegram.
   - **Two-Layer Read-Only MySQL:** Database-level read-only user + Python SQL AST/keyword validator (`SELECT`, `SHOW`, `DESCRIBE`, `EXPLAIN` only, single-statement, auto `LIMIT`, session read-only transaction, and query timeout).
   - **Secret Redaction & Audit Trail:** All tool outputs and LLM responses pass through `redact_secrets()` before leaving the server, and every team question or button click is recorded in `audit.jsonl`.

---

## 2. High-Level Component Diagram

```text
Teammate / Admin on Mobile (Telegram DM or Shared Ops Group)
        │
        │  Outbound Long-Polling (HTTPS 443 — Zero Inbound Firewall Ports Needed)
        ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  1. Telegram Gateway & RBAC Layer (`agent/telegram_bot.py`)               │
│     • Validates `allowed_group_ids`, `admins`, and `viewers`              │
│     • Logs every request & approval click to `audit.jsonl`                │
│     • Handles `/status`, `/services`, `/mode`, `/cli`, `/sdk`, `/clear`   │
│     • Renders interactive `[Approve]` / `[Reject]` InlineKeyboards        │
└─────────────────────────────────────┬─────────────────────────────────────┘
                                      │
                                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  2. Unified Engine Router & Conversation Memory (`agent/engines/router.py`)│
│     • Maintains bounded per-chat/topic history (`ConversationMemory`)     │
│     • Routes turn based on `config.yaml -> engine.mode` (or `/cli` `/sdk`)│
│     • Optional automatic fallback from CLI Subscription to SDK API Key    │
└──────────────────┬─────────────────────────────────────┬──────────────────┘
                   │                                     │
     mode: "cli_subscription"                  mode: "sdk_apikey"
     (Subscription OAuth / CLI)                (Pay-as-you-go HTTP API Key)
                   │                                     │
                   ▼                                     ▼
┌─────────────────────────────────────┐   ┌─────────────────────────────────┐
│ 3A. CLI Subscription Engine         │   │ 3B. SDK API Key Engine          │
│ (`agent/engines/cli_subscription.py`│   │ (`agent/engines/sdk_apikey.py`) │
├─────────────────────────────────────┤   ├─────────────────────────────────┤
│ • Default: Cursor CLI (`agent -p`)  │   │ • Providers: Anthropic, OpenAI, │
│ • Presets: `cursor`, `claude`,      │   │   Gemini, OpenRouter, Custom    │
│   `codex`, `gemini`, `custom`       │   │ • Runs async tool loop in RAM   │
│ • Guarded by `asyncio.Semaphore`    │   │ • Calls `OpsToolkit` methods    │
│ • Can read/grep service source code │   │   directly in Python process    │
│ • Calls `python ops_toolkit.py ...` │   │ • Low latency (~1s first token) │
└──────────────────┬──────────────────┘   └──────────────┬──────────────────┘
                   │                                     │
                   └──────────────────┬──────────────────┘
                                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  4. Shared Windows Server Operations Toolkit (`ops_toolkit.py`)           │
├───────────────────────────────────────────────────────────────────────────┤
│  Read-Only Diagnostic Tools (Auto-Approved for Viewers & Admins):         │
│  • `get_system_resources()`           (psutil CPU, RAM, Disks, Top PIDs)  │
│  • `list_registered_services_status()`(All services in `config.yaml`)     │
│  • `get_pm2_status()` / `get_pm2_logs()` (`pm2 jlist` & `~/.pm2/logs`)    │
│  • `get_service_logs()`               (Newest `*.log` tail + filter)      │
│  • `get_windows_event_logs()`         (PowerShell `Get-WinEvent`)         │
│  • `check_ngrok_status()`             (`127.0.0.1:4040/api/tunnels`+reqs) │
│  • `check_n8n_status()`               (`127.0.0.1:5678/healthz` + logs)   │
│  • `query_mysql_readonly()`           (Validated read-only `pymysql`)     │
│                                                                           │
│  Gated State-Changing Tools (Requires Admin Click in Telegram):           │
│  • `propose_action()`  ──► Writes to `.pending_actions.json`              │
│  • `execute_approved_action()` (Only called by Telegram callback handler) │
└───────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Execution Flows

### 3.1 Mode A: `cli_subscription` Flow (Cursor CLI Default)
1. A teammate sends *"Why is `realtime-gateway-node` returning 502 on ngrok webhooks?"* in the Telegram Ops group.
2. `TelegramOpsBot` verifies the user's ID is in `admins` or `viewers` and the group ID is in `allowed_group_ids`.
3. `EngineRouter` selects `CLISubscriptionEngine` and acquires the `asyncio.Semaphore` (`max_concurrent_processes: 1`).
4. `CLISubscriptionEngine` builds the system prompt (containing the service registry from `config.yaml` and the exact `python ops_toolkit.py` commands) + recent chat history, and invokes:
   ```powershell
   agent -p "<composed_prompt>" --output-format text
   ```
5. Inside its turn, Cursor CLI runs:
   - `python ops_toolkit.py ngrok-status`
   - `python ops_toolkit.py pm2-status`
   - `python ops_toolkit.py pm2-logs --app realtime-gateway-node --lines 100`
   - Reads the relevant source file in `C:\Services\realtime-gateway-node` if a stack trace appears.
   - If the service is wedged, runs `python ops_toolkit.py propose-action --type pm2_restart --target realtime-gateway-node --reason "Process hung on unhandled rejection"`.
6. `CLISubscriptionEngine` captures the output, redacts secrets, detects the newly created pending action in `.pending_actions.json`, and returns `AgentTurnResult` to `TelegramOpsBot`.
7. `TelegramOpsBot` posts the root-cause analysis and renders the `[✅ Approve (Admins Only)]` `[❌ Reject]` inline keyboard beneath it.

### 3.2 Mode B: `sdk_apikey` Flow
1. Same Telegram entry and RBAC check.
2. `EngineRouter` selects `SDKApiKeyEngine`.
3. `SDKApiKeyEngine` sends the system prompt, conversation history, and JSON tool definitions (`TOOL_DEFINITIONS`) to the configured provider (`anthropic`, `openai`, `gemini`, or `openrouter`).
4. When the LLM returns `tool_calls` (e.g., `check_ngrok_status`, `get_pm2_logs`, `query_mysql_readonly`), Python executes the corresponding `OpsToolkit` methods in-process via `asyncio.to_thread` and feeds the redacted JSON back to the LLM.
5. If the LLM calls `propose_action`, the action is added to `.pending_actions.json` and rendered as a Telegram approval button.

---

## 4. Directory Structure

```text
/workspace
├── main.py                         # Entrypoint (`--config config.yaml`, `--check` preflight)
├── ops_toolkit.py                  # Shared Windows/PM2/ngrok/n8n/MySQL toolkit (Python + CLI)
├── config.yaml                     # Full annotated Windows Server configuration
├── config.example.yaml             # Template copy of config.yaml
├── .env.example                    # Environment variable template for secrets
├── requirements.txt                # Python dependencies
├── agent/
│   ├── __init__.py
│   ├── config.py                   # Typed YAML + .env configuration loader & mode persistence
│   ├── security.py                 # Secret redactor, read-only SQL validator, JSONL audit logger
│   ├── telegram_bot.py             # Telegram bot handlers, RBAC, mode switcher, approval buttons
│   └── engines/
│       ├── __init__.py
│       ├── base.py                 # Shared prompt builder, turn result types, conversation memory
│       ├── cli_subscription.py     # Mode 1: Cursor / Claude / Codex / Gemini CLI subscription engine
│       ├── sdk_apikey.py           # Mode 2: Anthropic & OpenAI-compatible API key SDK engine
│       └── router.py               # Engine router, live mode switcher, and optional fallback
├── docs/
│   ├── ARCHITECTURE.md             # This architecture specification
│   └── adr/
│       ├── ADR-001-dual-engine-auth-architecture.md
│       ├── ADR-002-multi-provider-abstraction-cursor-default.md
│       └── ADR-003-defense-in-depth-rbac-and-readonly-mysql.md
└── tests/
    └── test_agent.py               # Automated test suite verifying toolkit, SQL guardrails, & engines
```

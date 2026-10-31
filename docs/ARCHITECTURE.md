# System Architecture — Windows Server Multi-Service Telegram Debug Agent

## 1. Overview & Goals

The **Windows Server Multi-Service Telegram Debug Agent** is a Python daemon deployed directly on a Windows Server that hosts multiple Python services, a Node.js gateway, PM2, ngrok, n8n, and a MySQL database.

It allows an engineering team to investigate outages, inspect **multiple action-specific log files**, search and read **service source code**, check tunnel/workflow health, run read-only database diagnostics, and safely trigger service restarts from mobile or desktop via **Telegram**—without needing to RDP or SSH into the Windows Server.

### Core Architectural Principles
1. **Single Switch Linking Auth Mode & Execution Engine (`cli_subscription` vs. `sdk_apikey`):**
   - `cli_subscription`: Uses a flat-rate monthly subscription (**Cursor CLI** default, or Claude Code / OpenAI Codex / Gemini CLI) by delegating the agentic reasoning loop to the official CLI binary (`agent -p --output-format json [--resume <id>]`).
   - `sdk_apikey`: Uses a pay-as-you-go API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`) powered by the **OpenCode CLI harness** (`opencode run --format json [--session <id>]`), **Pi CLI** (`pi -p`), or the built-in in-memory Python async SDK loop.
2. **Native Session Continuation & Automatic Session Rotation (`agent/session_manager.py`):**
   - Because Telegram is a single continuous chat stream, the bot tracks the native CLI `session_id` per Telegram `(chat_id, topic_id, provider)` in `.sessions.json`.
   - Follow-up questions during an active investigation pass `--resume <session_id>` (Cursor/Claude) or `--session <session_id>` (OpenCode/Pi) so the agent preserves its exact native loop, file reads, and tool history.
   - **When a New Session Starts:** The bot automatically rotates to a clean session when:
     1. The chat has been quiet for > `idle_timeout_minutes` (default **30 minutes**).
     2. The current session reaches `max_turns_per_session` (default **15 turns**).
     3. Any teammate runs `/new` (or `/clear`) or taps the inline `[🔄 Start New Session]` button.
3. **Multi-File Action Logs & Source Code Inspection (`ops_toolkit.py`):**
   - Services that log different actions to different files (`payment.log`, `webhook.log`, `auth.log`, `scheduler.log`, `error.log`) are mapped via `logs_dir` and `log_files` in `config.yaml`.
   - Both engines can list all log files (`list_service_log_files`), tail/filter across one or all action logs (`get_service_logs`), and search/read source code jailed inside each service's `working_dir` (`search_service_code`, `read_service_code_file`).
4. **Defense-in-Depth Safety:**
   - **Team RBAC:** Telegram group allowlist + `admins` and `viewers` roles.
   - **Zero Direct Destructive Execution:** Neither engine can restart a service or kill a query directly. Both call `propose_action`, which posts an interactive `[✅ Approve (Admins Only)]` / `[❌ Reject]` button in Telegram.
   - **Two-Layer Read-Only MySQL:** Database-level read-only user + Python SQL validator (`SELECT`, `SHOW`, `DESCRIBE`, `EXPLAIN` only, single-statement, auto `LIMIT`, session read-only transaction, and query timeout).

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
│     • Commands: `/status`, `/services`, `/mode`, `/new`, `/session`,      │
│       `/cli`, `/sdk`                                                      │
│     • Renders `[🔄 Start New Session]` + Admin `[Approve]` / `[Reject]`   │
└─────────────────────────────────────┬─────────────────────────────────────┘
                                      │
                                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  2. Unified Engine Router & Native Session Manager                        │
│     (`agent/engines/router.py` + `agent/session_manager.py`)              │
│     • Resolves native `session_id` for `(chat_id, topic_id, provider)`    │
│     • Auto-rotates session if idle > 30m or turns >= 15 (or `/new`)       │
│     • Captures new `session_id` from CLI JSON output (`parse_cli_json`)   │
└──────────────────┬─────────────────────────────────────┬──────────────────┘
                   │                                     │
     mode: "cli_subscription"                  mode: "sdk_apikey"
     (Subscription OAuth / CLI)                (Pay-as-you-go API Key)
                   │                                     │
                   ▼                                     ▼
┌─────────────────────────────────────┐   ┌─────────────────────────────────┐
│ 3A. CLI Subscription Engine         │   │ 3B. SDK / API Key Engine        │
│ (`agent/engines/cli_subscription.py`│   │ (`agent/engines/sdk_apikey.py`) │
├─────────────────────────────────────┤   ├─────────────────────────────────┤
│ • Default: Cursor CLI               │   │ • Default Harness: **OpenCode** │
│   `agent -p --output-format json`   │   │   `opencode run --format json`  │
│   `[--resume <session_id>]`         │   │   `[--session <session_id>]`    │
│ • Presets: `cursor`, `claude`,      │   │ • Alt Harness: `pi` or          │
│   `codex`, `gemini`                 │   │   `python_sdk` (auto-fallback)  │
└──────────────────┬──────────────────┘   └──────────────┬──────────────────┘
                   │                                     │
                   └──────────────────┬──────────────────┘
                                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  4. Shared Windows Server Operations Toolkit (`ops_toolkit.py`)           │
├───────────────────────────────────────────────────────────────────────────┤
│  Read-Only Diagnostic, Multi-Log, & Source Code Tools:                    │
│  • `get_system_resources()`           (psutil CPU, RAM, Disks, Top PIDs)  │
│  • `list_registered_services_status()`(All services & log categories)     │
│  • `list_service_log_files()`         (All action logs, sizes, mtimes)    │
│  • `get_service_logs()`               (Tail/filter 1 or all action logs)  │
│  • `search_service_code()`            (Grep inside jailed `working_dir`)  │
│  • `read_service_code_file()`         (Read numbered source code lines)   │
│  • `get_pm2_status()` / `get_pm2_logs()` (`pm2 jlist` & `~/.pm2/logs`)    │
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

## 3. Native Session Lifecycle in a Single Telegram Chat

Because a Telegram DM or group topic is a single never-ending thread, `SessionManager` (`agent/session_manager.py`) manages native CLI session boundaries automatically:

| Scenario in Telegram | What `SessionManager` Does | What Appears in Telegram Reply Header |
| :--- | :--- | :--- |
| **First question** on an incident | Runs CLI without `--resume`/`--session`, injects full system prompt, captures `session_id` from JSON stdout, saves to `.sessions.json`. | `🆕 Session ses_8f9a1b (Turn 1/15)` |
| **Follow-up question 3 mins later** (*"Check the payment log and trace line 84 in code"*) | Sees `idle < 30m` and `turns < 15`. Passes `--resume ses_8f9a1b` (Cursor/Claude) or `--session ses_8f9a1b` (OpenCode) so the CLI continues its exact native agent loop. | `🔗 Resumed ses_8f9a1b (Turn 2/15)` |
| **Next message 2 hours later** (New incident) | Sees `idle > 30m`. Discards old `session_id` and starts a clean native session automatically. | `🆕 Session ses_4c2d9e (Turn 1/15 — auto-rotated: idle_timeout)` |
| **Teammate types `/new` or clicks `[🔄 Start New Session]`** | Immediately clears active `session_id` for that chat/topic so the next question starts fresh. | `🆕 Started a fresh debugging session` |

# System Architecture — Windows Server Multi-Service Telegram Debug Agent

## 1. Overview & Goals

The **Windows Server Multi-Service Telegram Debug Agent** is a Python daemon deployed directly on a Windows Server that hosts multiple Python services, a Node.js gateway, PM2, ngrok, n8n, and a MySQL database.

It allows an engineering team to investigate outages, inspect **multiple action-specific log files**, search and read **service source code**, check tunnel/workflow health, run read-only database diagnostics, and safely trigger service restarts from mobile or desktop via **Telegram**—without needing to RDP or SSH into the Windows Server.

### Core Architectural Principles
1. **Single Switch Linking Auth Mode & Execution Engine (`cli_subscription` vs. `sdk_apikey`):**
   - `cli_subscription`: Uses a flat-rate monthly subscription (**Cursor CLI** default, or Claude Code / OpenAI Codex / Gemini CLI) by delegating the agentic reasoning loop to the official CLI binary (`agent -p --output-format json [--resume <id>]`).
   - `sdk_apikey`: Uses a pay-as-you-go API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`) powered by the **OpenCode CLI harness** (`opencode run --format json [--session <id>]`), **Pi CLI** (`pi -p`), or the built-in in-memory Python async SDK loop.
2. **SQLite-Backed Session Manager & Cross-Tool Session Porting (`agent/session_manager.py`):**
   - Stores all sessions, per-tool native CLI IDs (`cursor`, `claude`, `opencode`, `pi`), turn-by-turn message transcripts, pending approvals, and audit logs in a single ACID/WAL SQLite database (`agent_state.db`).
   - **Cross-Tool Porting:** Each logical session (`s_...`) maps multiple provider native IDs (`session_native_ids`) AND stores the conversation transcript (`session_messages`). If a session starts in Cursor CLI (`cur_123`) and you switch to OpenCode API (`/mode sdk` or `/sdk`), the prior transcript from SQLite is automatically seeded into OpenCode's first turn, and OpenCode's new `ses_456` ID is linked to the **same** logical session!
   - **Automatic Rotation & Historical Reuse (`/session <id>`):**
     - Auto-rotates to a clean session after `idle_timeout_minutes` (**30 minutes**) or `max_turns_per_session` (**15 turns**), or via `/new`.
     - Any historical session can be reactivated anytime via `/session <session_id>` (accepts either the logical `s_...` ID or the native Cursor/OpenCode ID, and **resets `turn_count = 0` and `idle_time = 0m`**).
3. **Multi-File Action Logs & Source Code Inspection (`ops_toolkit.py`):**
   - Services that log different actions to different files (`payment.log`, `webhook.log`, `auth.log`, `scheduler.log`, `error.log`) are mapped via `logs_dir` and `log_files` in `config.yaml`.
   - Both engines can list all log files (`list_service_log_files`), tail/filter across one or all action logs (`get_service_logs`), and search/read source code jailed inside each service's `working_dir` (`search_service_code`, `read_service_code_file`).
4. **Defense-in-Depth Safety:**
   - **Team RBAC:** Telegram group allowlist + `admins` and `viewers` roles.
   - **Zero Direct Destructive Execution:** Neither engine can restart a service or kill a query directly. Both call `propose_action`, which posts an interactive `[✅ Approve (Admins Only)]` / `[❌ Reject]` button in Telegram.
   - **Two-Layer Read-Only MySQL:** Database-level read-only user + Python SQL validator (`SELECT`, `SHOW`, `DESCRIBE`, `EXPLAIN` only, single-statement, auto `LIMIT`, session read-only transaction, and query timeout).

---

## 2. SQLite Schema (`agent_state.db`)

```text
agent_state.db (SQLite WAL Mode)
├── sessions            (session_id PK, telegram_key, title, turn_count, total_turns_ever, is_active, created_at, last_active_at)
├── session_native_ids  ((session_id, provider_key) PK, native_session_id, updated_at)
├── session_messages    (id PK, session_id FK, role, content, provider_key, created_at)
├── pending_actions     (action_id PK, action_type, target, reason, status, created_at, requested_by_user_id, requested_by_username)
└── audit_logs          (id PK, timestamp_utc, timestamp_epoch, event_type, user_id, username, chat_id, engine_mode, details_json)
```

---

## 3. Session Lifecycle & Cross-Tool Porting in Telegram

| Scenario in Telegram | What `SessionManager` (`agent_state.db`) Does | What Appears in Telegram Reply Header |
| :--- | :--- | :--- |
| **First question** on an incident (e.g. in Cursor CLI) | Creates logical session `s_a1b2c3d4`, runs `agent -p --output-format json`, captures native ID `cur_991`, links `(s_a1b2c3d4, cli_subscription:cursor) -> cur_991`. | `🆕 Session s_a1b2c3d4 (native: cur_991 • Turn 1/15)` |
| **Follow-up question 3 mins later** in same mode | Sees `idle < 30m` and `turns < 15`. Passes `--resume cur_991` so Cursor CLI continues its exact native loop. | `🔗 Resumed s_a1b2c3d4 (native: cur_991 • Turn 2/15)` |
| **Switch to `/mode sdk` (OpenCode) mid-incident** | Sees `s_a1b2c3d4` has no OpenCode native ID yet. Seeds the prior SQLite transcript into `opencode run --format json`, captures `ses_open88`, and links `(s_a1b2c3d4, sdk_apikey:opencode) -> ses_open88`. | `🔀 Ported Session s_a1b2c3d4 ➔ ses_open88 (Turn 3/15)` |
| **Reuse an old session days later (`/session s_a1b2c3d4` or `/session cur_991`)** | Marks `s_a1b2c3d4` active again, **resets `turn_count = 0/15` and `idle_timer = 0.0m`**, and resumes the matching native ID for the current mode! | `🔁 Session s_a1b2c3d4 Reactivated! Turn Counter Reset: 0/15, Idle Timer Reset: 0.0m` |

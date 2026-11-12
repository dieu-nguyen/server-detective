# server-detective

**server-detective** is a unified, session-aware Python debugging agent for **Windows Server** that lets you and your teammates investigate services, inspect **multiple action-specific log files**, read and search **service source code**, check **PM2 / ngrok / n8n**, and run **read-only MySQL diagnostics** directly from **Telegram**.

---

## Key Features

1. **Dual-Mode Engine Switch (`config.yaml` or live via `/mode` in Telegram):**
   - **`cli_subscription` (Default — Cursor Subscription):** Uses your existing flat-rate **Cursor** subscription via Cursor CLI (`agent -p --output-format json --resume <id>`), or switch to `claude`, `codex`, or `gemini` CLI with one line in `config.yaml`.
   - **`sdk_apikey` (API Key Mode — Powered by OpenCode + Python SDK Fallback):** Uses **OpenCode** (`opencode run --format json --session <id>`) or **Pi** (`pi -p`) with your API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`), with automatic fallback to the built-in in-memory Python SDK loop.
2. **SQLite Session Manager, Historical Session Reuse (`/session <id>`), & Cross-Tool Porting:**
   - Stores all sessions, per-tool native CLI IDs (`cursor`, `claude`, `opencode`, `pi`), turn transcripts, pending approvals, and audit logs in a single SQLite database (`agent_state.db`).
   - **Continues the exact native session** (`--resume <id>` for Cursor/Claude, `--session <id>` for OpenCode/Pi) while you are actively debugging back-and-forth.
   - **Cross-Tool Session Porting:** If you start debugging in Cursor CLI (`cli_subscription`) and switch to OpenCode (`sdk_apikey`), the bot automatically seeds the prior SQLite transcript into OpenCode and links both native IDs under the same session!
   - **Reuse Any Old Session (`/session <session_id>`):**
     - Run `/session` to see the active session + recent historical sessions with one-click **`[🔁 Resume]`** buttons.
     - Run `/session <session_id>` (using either the `s_...` ID or native Cursor/OpenCode ID) to reactivate an old session and **reset its turn counter to `0/15` and idle timer to `0m`**.
   - **Automatically starts a new session** when:
     - The chat has been quiet for **> 30 minutes** (`idle_timeout_minutes: 30`), OR
     - The session reaches **15 turns** (`max_turns_per_session: 15`), OR
     - You type `/new` (or tap the inline **`[🔄 Start New Session]`** button on any reply).
3. **Multi-File Action Logs & Source Code Inspection:**
   - Supports services that log different actions to separate files (`payment.log`, `webhook.log`, `auth.log`, `scheduler.log`, `error.log`) via `logs_dir` and `log_files` in `config.yaml`.
   - Built-in tools to list all log files (`list-service-logs`), tail/filter specific or all action logs (`service-logs`), search source code (`search-code`), and read source code with line numbers (`read-code`) jailed inside each service's `working_dir`.
4. **Built-In Windows Server Stack Integrations:**
   - **PM2:** Parses `pm2 jlist` and `~/.pm2/logs`.
   - **ngrok:** Queries `http://127.0.0.1:4040/api/tunnels` and `/api/requests/http`.
   - **n8n:** Checks `/healthz`, PM2 status, and n8n logs.
   - **MySQL (Strictly Read-Only):** Runs `SELECT`, `SHOW`, `DESCRIBE`, `EXPLAIN` with automatic row limits, read-only session transactions, and query timeouts.
5. **Team RBAC & Interactive Telegram Approval Buttons:**
   - Supports shared Telegram Ops groups + role separation (`admins` vs `viewers`).
   - State-changing actions (`pm2_restart`, `pm2_reload`, `windows_service_restart`, `mysql_kill_query`) **never execute automatically**—the bot posts an `[✅ Approve (Admins Only)]` / `[❌ Reject]` button in Telegram.

---

## Architecture & ADR Documentation

- **[System Architecture Document](docs/ARCHITECTURE.md)**
- **Architecture Decision Records (ADRs):**
  - **[ADR-001: Unifying Agent Architecture with Authentication Mode, OpenCode Harness, and SQLite Cross-Tool Session Lifecycle](docs/adr/ADR-001-dual-engine-auth-architecture.md)**
  - **[ADR-002: Multi-Provider Abstraction with Cursor CLI as Default Subscription Engine](docs/adr/ADR-002-multi-provider-abstraction-cursor-default.md)**
  - **[ADR-003: Defense-in-Depth Team RBAC, Read-Only MySQL, and Gated Telegram Approvals](docs/adr/ADR-003-defense-in-depth-rbac-and-readonly-mysql.md)**
  - **[ADR-004: SQLite Session Lifecycle, 30-Minute Post-Response Idle Timer, `/session <id>` Reuse, and Cross-Tool Session Porting](docs/adr/ADR-004-sqlite-session-lifecycle-and-cross-tool-porting.md)**
  - **[ADR-005: Multi-File Action Log Discovery and Path-Jailed Source Code Inspection](docs/adr/ADR-005-multi-file-action-logs-and-jailed-code-inspection.md)**
  - **[ADR-006: Removing In-Memory Python SDK Loop in Favor of CLI Harnesses (`opencode` / `pi`) and Configurable Hard CLI Restrictions](docs/adr/ADR-006-cli-harnesses-only-and-configurable-restrictions.md)**
  - **[ADR-007: Telegram as the Operator Interface, Headless Cursor CLI, and Server-Side Turn Logs](docs/adr/ADR-007-telegram-primary-interface-headless-cursor-cli.md)**

---

## Step-by-Step Deployment Guide for Windows Server

### Step 1: Create Your Telegram Bot & Get Team IDs

1. Open Telegram and message **[@BotFather](https://t.me/BotFather)** → send `/newbot` → copy your `TELEGRAM_BOT_TOKEN`.
2. *(Optional for Group Chat)* Create a private Telegram group for your team, add the bot to the group, and in **@BotFather** disable Group Privacy (`/setprivacy` → `Disable`) if you want the bot to read messages without requiring `@bot_name` mentions.
3. Find your Telegram User IDs (and Group Chat ID) by messaging `@userinfobot` or `@RawDataBot` on Telegram.

---

### Step 2: Create a Read-Only MySQL User on Your Server

Open MySQL on your Windows Server and create a read-only user for the agent:

```sql
CREATE USER 'ops_agent_ro'@'127.0.0.1' IDENTIFIED BY 'your_strong_readonly_password';
CREATE USER 'ops_agent_ro'@'localhost' IDENTIFIED BY 'your_strong_readonly_password';
GRANT SELECT, PROCESS, SHOW VIEW ON *.* TO 'ops_agent_ro'@'127.0.0.1';
GRANT SELECT, PROCESS, SHOW VIEW ON *.* TO 'ops_agent_ro'@'localhost';
FLUSH PRIVILEGES;
```

---

### Step 3: Set Up Cursor CLI (`cli_subscription`) & OpenCode (`sdk_apikey`)

1. **For `cli_subscription` Mode (Cursor Subscription — Default):**
   - Ensure Cursor CLI (`agent`) is installed and on your `PATH` (or set `binaries.cursor: "wsl agent"` in `config.yaml` if installed in WSL).
   - Run `agent login` once under the Windows user account that will run the bot (or set `CURSOR_API_KEY` in `.env`).
2. **For `sdk_apikey` Mode (OpenCode CLI Harness + API Key):**
   - Install OpenCode globally via Node.js (which you already have for PM2):
     ```powershell
     npm install -g opencode-ai
     ```
   - Set your API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, or `OPENROUTER_API_KEY`) in `.env`.
3. **Optional Hard CLI Tool/Command Restrictions (`engine.cli_restrictions` in `config.yaml`):**
   - Disabled by default (`enabled: false`) so you can review behavior first.
   - When you flip `cli_restrictions.enabled: true` in `config.yaml`, the bot automatically enforces CLI-level read-only file access and restricts shell execution to `allowed_shell_commands` across Cursor CLI, Claude CLI, OpenCode, Pi, and Codex.

---

### Step 4: Clone Project & Install Python Dependencies on Windows Server

Open **PowerShell** on your Windows Server:

```powershell
cd C:\Services
git clone <your-repo-url> server-detective
cd C:\Services\server-detective

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

### Step 5: Configure `.env` and `config.yaml`

1. Copy `.env.example` to `.env` and fill in your secrets:
   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```
2. Edit `config.yaml` to map your Windows Server's services (`working_dir`, `logs_dir`, `log_files`), Telegram User IDs, PM2 paths, and MySQL database names:
   ```powershell
   notepad config.yaml
   ```
3. Run the built-in preflight check to verify your `config.yaml` and Windows system inspection:
   ```powershell
   python main.py --config config.yaml --check
   ```
   You can also test the multi-log and code inspection tools directly from PowerShell:
   ```powershell
   python ops_toolkit.py --config config.yaml list-service-logs --service order-api-py
   python ops_toolkit.py --config config.yaml service-logs --service order-api-py --category payment --lines 80
   python ops_toolkit.py --config config.yaml search-code --service order-api-py --query "webhook"
   python ops_toolkit.py --config config.yaml read-code --service order-api-py --file "main.py" --start-line 1 --end-line 100
   ```

---

### Step 6: Run the Bot 24/7 with PM2

Run the Telegram agent under your logged-in Windows user account via PM2:

```powershell
pm2 start .\main.py --name "server-detective" --interpreter "C:\Services\server-detective\.venv\Scripts\python.exe" -- --config config.yaml
pm2 save
```

---

## Telegram Commands & Session Controls

- `/start` — Check your role (`Admin` vs `Viewer`), active engine mode, session policy, and registered services.
- `/session` — View the active session + recent saved sessions from SQLite (with one-click **`[🔁 Resume]`** buttons).
- `/session <session_id>` — Reuse any old session (accepts logical `s_...` ID or native Cursor/OpenCode session ID), **resetting its turn counter (`0/15`) and idle timer (`0.0m/30m`)**, and porting context across tools if you switched modes.
- `/new` (or `/clear` or click **`[🔄 Start New Session]`**) — Force a fresh debugging session immediately when switching topics.
- `/status` — Instant CPU, RAM, Disk, and multi-service health summary.
- `/services` — List all configured services, action log categories, and code directories.
- `/mode` — View or switch engine mode (`cli_subscription` | `sdk_apikey`).
- `/cli <question>` — Force a single turn through `cli_subscription` mode.
- `/sdk <question>` — Force a single turn through `sdk_apikey` mode.

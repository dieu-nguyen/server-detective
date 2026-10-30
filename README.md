# Windows Server Multi-Service Telegram Debug Agent

A unified Python debugging agent for **Windows Server** that lets you and your teammates investigate services, inspect logs, check **PM2 / ngrok / n8n**, and run **read-only MySQL diagnostics** directly from **Telegram**—with a single switch between **CLI Subscription Mode** (Cursor default, or Claude / Codex / Gemini CLI) and **SDK API Key Mode** (Anthropic, OpenAI, Gemini, OpenRouter).

---

## Key Features

- **Dual-Mode Engine Switch (`config.yaml` or live via `/mode` in Telegram):**
  - **`cli_subscription` (Default — Cursor Subscription):** Uses your existing flat-rate **Cursor** subscription via Cursor CLI (`agent -p`), or switch to `claude`, `codex`, or `gemini` CLI with one line in `config.yaml`. Can inspect source code across your service folders + invoke `ops_toolkit.py`.
  - **`sdk_apikey` (Pay-as-you-go API Key):** Runs a fast in-memory Python async tool-calling loop with Anthropic, OpenAI, Gemini, or OpenRouter API keys.
- **Multi-Service Windows Server Registry:**
  - Inspects multiple **Python** and **Node.js** services managed by **PM2** or **Windows Services (NSSM / `services.msc`)**.
  - Tails application log files (`*.log`), PM2 logs (`~/.pm2/logs`), and **Windows Event Viewer** (`Get-WinEvent`).
- **Built-In Stack Integrations:**
  - **PM2:** Parses `pm2 jlist` for status, restart count, uptime, CPU, and RAM.
  - **ngrok:** Queries `http://127.0.0.1:4040/api/tunnels` and `/api/requests/http` to check public tunnel health and recent `500`/`502` webhook failures.
  - **n8n:** Checks `/healthz`, PM2 status, and n8n logs.
  - **MySQL (Strictly Read-Only):** Runs `SELECT`, `SHOW`, `DESCRIBE`, `EXPLAIN` with automatic row limits, read-only session transactions, and query timeouts.
- **Team RBAC & Interactive Telegram Approval Buttons:**
  - Supports shared Telegram Ops groups + role separation (`admins` vs `viewers`).
  - State-changing actions (`pm2_restart`, `pm2_reload`, `windows_service_restart`, `mysql_kill_query`) **never execute automatically**—the bot posts an `[✅ Approve (Admins Only)]` / `[❌ Reject]` button in Telegram.

---

## Architecture & ADR Documentation

- **[System Architecture Document](docs/ARCHITECTURE.md)**
- **Architecture Decision Records (ADRs):**
  - **[ADR-001: Unifying Agent Architecture with Authentication Mode (`cli_subscription` vs. `sdk_apikey`)](docs/adr/ADR-001-dual-engine-auth-architecture.md)**
  - **[ADR-002: Multi-Provider Abstraction with Cursor CLI as Default Subscription Engine](docs/adr/ADR-002-multi-provider-abstraction-cursor-default.md)**
  - **[ADR-003: Defense-in-Depth Team RBAC, Read-Only MySQL, and Gated Telegram Approvals](docs/adr/ADR-003-defense-in-depth-rbac-and-readonly-mysql.md)**

---

## Step-by-Step Deployment Guide for Windows Server

### Step 1: Create Your Telegram Bot & Get Team IDs

1. Open Telegram and message **[@BotFather](https://t.me/BotFather)** → send `/newbot` → copy your `TELEGRAM_BOT_TOKEN`.
2. *(Optional for Group Chat)* Create a private Telegram group for your team, add the bot to the group, and in **@BotFather** disable Group Privacy (`/setprivacy` → `Disable`) if you want the bot to read messages without requiring `@bot_name` mentions (or keep `require_mention_in_groups: true` in `config.yaml`).
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

### Step 3: Set Up Cursor CLI (for `cli_subscription` Mode)

You can authenticate Cursor CLI on your Windows Server using your Cursor subscription:

1. **Install Cursor CLI (`agent`):**
   - **Native Windows / PowerShell or WSL:** Install Cursor CLI from Cursor (or run `cursor --install-extension` / install the `agent` CLI binary so `agent --version` works in PowerShell).
   - *Note:* If you run Cursor CLI inside WSL on Windows Server, set `binaries.cursor: "wsl agent"` in `config.yaml`.
2. **Authenticate with your Cursor Subscription:**
   - Run `agent login` once under the Windows user account that will run the bot (e.g., `Administrator`), **OR**
   - Generate a `CURSOR_API_KEY` from your Cursor account settings and place it in `.env`.

*(If you ever want to switch to Claude Code CLI or OpenAI Codex CLI later, just run `npm install -g @anthropic-ai/claude-code` or `npm install -g @openai/codex` and change `cli_subscription.provider` in `config.yaml`.)*

---

### Step 4: Clone Project & Install Python Dependencies on Windows Server

Open **PowerShell** on your Windows Server:

```powershell
cd C:\Services
git clone <your-repo-url> telegram-debug-agent
cd C:\Services\telegram-debug-agent

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
2. Edit `config.yaml` to match your Windows Server's services, Telegram User IDs, PM2 paths, and MySQL database names:
   ```powershell
   notepad config.yaml
   ```
3. Run the built-in preflight check to verify your `config.yaml` and Windows system inspection before starting the bot:
   ```powershell
   python main.py --config config.yaml --check
   ```
   You can also test any individual diagnostic tool directly from PowerShell:
   ```powershell
   python ops_toolkit.py --config config.yaml system-resources
   python ops_toolkit.py --config config.yaml services-status
   python ops_toolkit.py --config config.yaml pm2-status
   python ops_toolkit.py --config config.yaml ngrok-status
   python ops_toolkit.py --config config.yaml n8n-status
   ```

---

### Step 6: Run the Bot Permanently with PM2 (or NSSM)

Because you already use **PM2** on your Windows Server, the easiest way to keep the Telegram agent running 24/7 under your logged-in Windows user account is via PM2:

```powershell
pm2 start .\main.py --name "telegram-debug-agent" --interpreter "C:\Services\telegram-debug-agent\.venv\Scripts\python.exe" -- --config config.yaml
pm2 save
```

*(Important: Running under PM2 as your logged-in Windows user ensures the bot automatically sees both your `%USERPROFILE%\.pm2` process list and your `agent login` subscription session.)*

---

## Using the Bot in Telegram

Once running, open your Telegram chat or group and try:

- `/start` — Check your role (`Admin` vs `Viewer`), active engine mode, and registered services.
- `/status` — Instant CPU, RAM, Disk, and multi-service health summary.
- `/mode` — View current engine mode, or switch live:
  - `/mode cli_subscription` (uses Cursor CLI subscription)
  - `/mode sdk_apikey` (uses API key SDK loop)
- `/cli Why is sync-worker-py throwing an exception in the last 50 log lines?` — Force a turn through CLI Subscription mode.
- `/sdk Check MySQL processlist and tell me if any queries are stuck.` — Force a turn through SDK API Key mode.
- **Natural Language:** Just ask *"Why are webhooks failing on ngrok right now?"* or *"Check n8n health and recent error logs."*

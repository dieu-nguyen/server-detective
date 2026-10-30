# ADR-003: Defense-in-Depth Team RBAC, Read-Only MySQL, and Gated Telegram Approvals

- **Status:** Accepted
- **Date:** 2026-10-30

## Context

The agent runs on a production Windows Server and is shared by multiple teammates in Telegram. It needs access to PM2, Windows Services, application logs, ngrok, n8n, and a live MySQL database. Allowing an LLM—or any non-admin user—to execute arbitrary write queries or service restarts directly without oversight poses an unacceptable production risk.

## Decision

1. **Two-Tier Telegram Role-Based Access Control (RBAC):**
   - `telegram.allowed_group_ids`: Restricts which group chats the bot can operate in.
   - `telegram.viewers`: Teammates who can run read-only investigations and ask the agent to diagnose issues.
   - `telegram.admins`: Senior/on-call engineers who can additionally switch engine modes (`/mode`) and click `[✅ Approve]` on state-changing actions.
2. **Two-Bounce Gated Action Pattern (`propose_action`):**
   - State-changing actions (`pm2_restart`, `pm2_reload`, `windows_service_restart`, `mysql_kill_query`) are never executed inside the LLM turn.
   - Calling `propose_action` writes a time-limited pending record to `.pending_actions.json` and causes the Telegram bot to render an InlineKeyboard (`approve:<action_id>` / `reject:<action_id>`).
   - Only a Telegram callback from a user ID in `telegram.admins` triggers `execute_approved_action()`.
3. **Two-Layer Read-Only MySQL Protection:**
   - **Database Layer:** Connect using a dedicated MySQL user granted only `SELECT, PROCESS, SHOW VIEW`.
   - **Application Layer (`validate_readonly_sql`):** Strip comments, reject multi-statement queries (`;`), require prefix in `('SELECT', 'SHOW', 'DESCRIBE', 'DESC', 'EXPLAIN')`, block write/DDL/admin keywords (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`, `GRANT`, `KILL`, `INTO OUTFILE`), run `SET SESSION TRANSACTION READ ONLY;`, and enforce `LIMIT` and `query_timeout_seconds`.
4. **Mandatory Secret Redaction & Audit Logging:**
   - All subprocess, file, HTTP, and SQL outputs pass through `redact_secrets()` before reaching the LLM or Telegram.
   - All questions, mode switches, and approval/rejection clicks are logged to `audit.jsonl`.

## Consequences

- **Positive:** Teammates can freely collaborate and debug production issues in a shared Telegram group without risk of accidental data mutation or unapproved service restarts.
- **Positive:** Full accountability via `audit.jsonl`.

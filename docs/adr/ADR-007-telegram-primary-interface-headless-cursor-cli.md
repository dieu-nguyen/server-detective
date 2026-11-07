# ADR-007: Telegram as the Operator Interface, Headless Cursor CLI, and Server-Side Turn Logs

- **Status:** Accepted
- **Date:** 2026-11-07

## Context

The agent is deployed on a **Windows Server** under **PM2** and is invoked from **Telegram** (`/cli`, `/sdk`, or natural-language messages). Early production use showed that a working Telegram bot is not the same as a working Cursor CLI turn:

1. **PM2 PATH ≠ interactive PowerShell PATH.** `where.exe agent` in a login shell found `C:\Users\admin\AppData\Local\cursor-agent\agent.cmd`, but the PM2 process often could not. Telegram then returned `CLI binary agent was not found on PATH`.
2. **`agent.cmd` and `shlex.split` on Windows paths.** `asyncio.create_subprocess_exec` cannot run `.cmd` without `cmd.exe`. POSIX `shlex.split` eats backslashes in `C:\Users\...`, so a full path in `config.yaml` can be mangled unless quoted/forward-slashed and pointed at `agent.exe`.
3. **Workspace trust and y/n prompts have no TTY.** Headless print mode (`agent -p`) stops with `Workspace Trust Required` or waits forever on command/MCP approval. Telegram cannot answer those prompts, so the chat froze on `Investigating...`.
4. **CLI flag order.** `-p` / `--print` is a **boolean**. The prompt is a **trailing argument**. Flags placed *after* the (large) prompt are ignored by Cursor CLI and can be truncated by Windows `cmd.exe`’s 8191-character limit. `--trust` after the prompt therefore never applied.
5. **Server-local `config.yaml`.** Telegram IDs, `binaries.cursor`, working dirs, and timeouts differ per machine. Tracking `config.yaml` in git caused deploys to overwrite server edits.
6. **Telegram was not a complete interface.** The bot posted Investigating and then waited up to `timeout_seconds` (default **180s**) with no further chat update. Cursor stdout/stderr were buffered until exit. A failed `edit_text`, a CLI crash, or an unhandled handler exception could leave Investigating forever while the detail existed only in PM2 logs.
7. **Operators asked for “thinking” like the Cursor desktop app.** Official Cursor CLI print-mode docs state that **`thinking` events are suppressed** in every `--output-format`. The closest substitute is `stream-json` assistant messages + tool-call events, written to a file for development/tuning—not as a substitute for the Telegram answer.
8. **Long answers.** Telegram needs ~3900-character chunks. Unbounded splitting could spam many messages. Operators want a usable mobile reply of **at most 3 chunks**.

## Decision

### 1. Telegram is the operator interface; files are for tuning

- Every **successful** turn and every **error** (timeout, missing binary, non-zero CLI exit, unhandled handler exception) **must** produce a Telegram message.
- Investigating is edited every **20s** with elapsed time vs `timeout_seconds`, then replaced by the answer or `Agent Error`.
- If Investigating cannot be edited, send a **new reply**.
- A global Telegram `error_handler` posts `Bot Error` for uncaught command/button failures.
- Disk logs (`pm2 logs`, `./logs/agent-turns.log`, `audit.jsonl`, `agent_state.db`) are **development/tuning**, not the user-facing result.

The only intentional silent skip: group messages when `telegram.require_mention_in_groups` is true and the bot is not mentioned/replied to.

### 2. Headless Cursor CLI must skip interactive gates

Default `cli_subscription` argv (flags **before** the prompt):

```text
agent -p --output-format stream-json --trust --force --approve-mcps [--resume <id>] [--model <m>] "<prompt>"
```

| Flag | Config (defaults on) | Skips |
| :--- | :--- | :--- |
| `--trust` | `engine.cli_subscription.trust_workspace` | Workspace trust dialog |
| `--force` (`--yolo`) | `engine.cli_subscription.force_allow_commands` | Shell y/n approval |
| `--approve-mcps` | `engine.cli_subscription.approve_mcps` | MCP connect prompts |

This does **not** bypass the bot’s own Telegram `[Approve]` / `[Reject]` cards for `pm2 restart` / query kill (ADR-003).

On Windows, `binaries.cursor` should be the **`.exe`** full path (forward slashes), configured on the server, not assumed from PATH.

### 3. Hard timeout; no silent hang

- `engine.cli_subscription.timeout_seconds` (default **180**). Raise this on the server if turns need longer thinking.
- When the timeout fires, **kill** the CLI process and post the timeout error to Telegram.
- Partial output already written to `agent-turns.log` is kept; Telegram does not invent a successful answer after a kill.

### 4. Live turn log for development (not hidden CoT)

- Switch Cursor print output to `--output-format stream-json`.
- Stream stdout/stderr line-by-line into `engine.cli_subscription.turn_log_file` (default `./logs/agent-turns.log`): `[system]`, `[assistant]`, `[tool:started|completed]`, `[thinking]` **if** Cursor emits it, `[stderr]`, `[result]`.
- Cursor print mode still **omits** desktop-app chain-of-thought. Operators tail the file with `Get-Content .\logs\agent-turns.log -Wait -Tail 50`.

### 5. Cap Telegram answers at 3 chunks

- Chunk size remains **3900** characters (`split_telegram_message`).
- System prompt: write for ~3 Telegram messages (root cause, a few file:line cites, next steps; no log dumps).
- Hard cap: `limit_telegram_chunks` sends at most **3** messages and notes truncation on the last chunk.

### 6. Do not track machine-local `config.yaml`

- Gitignore `config.yaml`. Keep `config.example.yaml` as the template; tests load the example.
- Before `git pull` on the server, copy `config.yaml` aside so a tree deletion does not wipe Telegram IDs and CLI paths.

## Consequences

- **Positive:** Operators can stay in Telegram for success and failure; they do not need RDP to learn that a turn died.
- **Positive:** Headless Cursor no longer depends on a human answering trust/y/n in PM2.
- **Positive:** Server config (PATH, user IDs, timeouts) can change without git fights.
- **Negative / residual:** Hidden model reasoning is still unavailable in print mode. Tune from `agent-turns.log` + heartbeat, not a desktop-style thinking pane.
- **Negative / residual:** `--force` auto-approves Cursor shell tools. Destructive production actions remain gated by Telegram admin buttons, not by Cursor’s y/n.
- **Negative / residual:** A turn longer than `timeout_seconds` is killed; increase the config value when 3 minutes is too short.
- **Negative / residual:** PM2 vs login-shell PATH and `.cmd` vs `.exe` remain an ops concern; document the full `agent.exe` path in server `config.yaml`.

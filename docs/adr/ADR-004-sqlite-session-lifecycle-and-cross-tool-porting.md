# ADR-004: SQLite Session Lifecycle, 30-Minute Post-Response Idle Timer, `/session <id>` Reuse, and Cross-Tool Session Porting

- **Status:** Accepted
- **Date:** 2026-10-31

## Context

1. **Single-Chat Stream in Telegram:**
   - Unlike desktop coding IDEs where users explicitly open a new tab per bug, a Telegram DM or group topic is a single continuous chat stream.
   - During an active incident, teammates send follow-up questions every few minutes and expect the agent to continue its native CLI agent loop (`agent --resume <id>` in Cursor CLI, `opencode run --session <id>` in OpenCode, `claude --resume <id>` in Claude Code CLI).
   - However, when an incident ends and a new incident happens hours or days later in the same Telegram chat, resuming the old session wastes tokens and pollutes context.
2. **Need to Reuse Historical Sessions (`/session <id>`):**
   - Sometimes a recurring bug reappears days later and an engineer wants to pick up an earlier investigation session where it left off, with a fresh turn budget and idle timer.
   - Flat `.sessions.json` files are brittle for historical lookup, counter resets, and multi-user concurrency on Windows Server.
3. **Different Native Session Formats Across Tools:**
   - Cursor CLI (`~/.cursor/chats/`) and OpenCode (`~/.local/share/opencode/storage/session/`) use separate on-disk storage and incompatible native session IDs, so switching from `/mode cli` (Cursor) to `/mode sdk` (OpenCode) mid-investigation cannot pass a Cursor ID directly to `opencode --session`.

## Decision

1. **Use SQLite (`agent_state.db` with WAL mode) for All Session, Action, and Audit State:**
   - Store `sessions`, `session_native_ids`, `session_messages`, `pending_actions`, and `audit_logs` in a single ACID SQLite database using Python's built-in `sqlite3`.
2. **Count the 30-Minute Idle Timeout from the Agent's Last Response (`last_active_at`):**
   - `last_active_at` is updated in `record_turn()` at the exact moment the agent completes its investigation and sends the response back to Telegram (or when a user explicitly reactivates a session via `/session <id>`).
   - When the next message arrives, `resolve_session()` checks `(now - last_active_at) > idle_timeout_seconds` (default 30 minutes) and `turn_count >= max_turns_per_session` (default 15 turns). If either threshold is exceeded, the old session is marked `is_active = 0` (preserved in SQLite history) and a brand-new session is created automatically.
3. **Historical Session Reuse via `/session <id>` and `[🔁 Resume]` Buttons:**
   - `/session` displays the active session plus the most recent saved sessions with one-click `[🔁 Resume]` buttons.
   - `/session <id>` matches either the logical session ID (`s_<hex>`) or any linked native CLI ID (`cur_...`, `ses_...`), sets `is_active = 1`, and **resets `turn_count = 0` and `last_active_at = now`** (while preserving `total_turns_ever` for historical visibility).
4. **Cross-Tool Session Porting (`session_native_ids` + `session_messages`):**
   - Each logical session (`s_<hex>`) maps `(session_id, provider_key) -> native_session_id` in `session_native_ids` and stores the turn-by-turn Q&A transcript in `session_messages`.
   - When continuing in the same tool, the engine passes `--resume <native_id>` or `--session <native_id>` directly.
   - When switching tools mid-session (e.g. Cursor CLI ➔ OpenCode API), `SessionManager` detects `total_turns_ever > 0` with no native ID for the new provider, seeds the prior SQLite transcript into the new tool's prompt, captures the new tool's native session ID, and links both native IDs under the same logical session.

## Consequences

- **Positive:** Teammates never have to manually manage sessions during routine use (30m post-response idle timer and 15-turn cap handle rotation automatically), yet have full power to `/new` or `/session <id>` anytime.
- **Positive:** Seamless switching between Cursor CLI subscription mode and OpenCode API key mode mid-investigation without losing context.

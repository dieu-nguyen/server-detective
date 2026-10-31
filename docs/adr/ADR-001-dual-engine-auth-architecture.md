# ADR-001: Unifying Agent Architecture with Authentication Mode, OpenCode Harness, and SQLite Cross-Tool Session Lifecycle

- **Status:** Accepted (Updated)
- **Date:** 2026-10-31

## Context

1. **Auth Mode vs. Engine Architecture:**
   - Flat-rate monthly subscriptions (Cursor Pro/Business, Claude Pro/Max, ChatGPT Plus/Pro) must run through the official vendor CLI (`agent`, `claude`, `codex`).
   - Pay-as-you-go API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`) can run through **OpenCode** (`opencode run`), **Pi** (`pi -p`), or an in-memory Python SDK loop.
2. **Session Persistence, Historical Reuse (`/session <id>`), and Cross-Tool Porting:**
   - Cursor CLI, OpenCode, and Claude CLI persist their native sessions on local disk (`~/.cursor/chats/`, `~/.local/share/opencode/storage/session/`, `~/.claude/projects/`).
   - However, native CLI session IDs are tool-specific (a Cursor `chatId` cannot be passed directly to `opencode --session`).
   - Furthermore, flat `.json` state files are brittle when storing historical sessions for lookup, resetting turn/idle counters, or handling concurrent teammates on Windows Server.

## Decision

1. **Use SQLite (`agent_state.db` in WAL mode) for All Local State (`agent/session_manager.py`):**
   - Store `sessions`, `session_native_ids`, `session_messages`, `pending_actions`, and `audit_logs` in a single ACID SQLite database using Python's standard-library `sqlite3`.
2. **Separate Per-Tool Native IDs While Bridging Context Across Tools:**
   - Each logical session (`s_<hex>`) maintains a `1:N` mapping in `session_native_ids` (`(session_id, provider_key) -> native_session_id`) plus the full turn transcript in `session_messages`.
   - When continuing within the same tool, pass its native `--resume` / `--session` ID directly.
   - When switching tools mid-session (e.g. Cursor CLI ➔ OpenCode API), automatically seed the SQLite transcript into the new tool's first turn and bind the new tool's native session ID to the same logical session.
3. **Support Historical Session Reactivation (`/session <id>` and `[🔁 Resume]` Buttons):**
   - Running `/session` lists the active session plus recent saved sessions from SQLite with one-click `[🔁 Resume]` inline buttons.
   - Running `/session <id>` (accepting either the logical `s_...` ID or any native CLI ID) reactivates that session and **resets `turn_count = 0` and `last_active_at = now`**, granting a full fresh turn and idle window.

## Consequences

- **Positive:** Safe concurrent access across threads/subprocesses on Windows Server via SQLite WAL mode.
- **Positive:** Teammates can reuse any past investigation days later with `/session <id>`, and seamlessly switch between Cursor CLI subscription mode and OpenCode API key mode without losing session context.

# ADR-001: Unifying Agent Architecture with Authentication Mode, OpenCode Harness, and Native Session Lifecycle

- **Status:** Accepted (Updated)
- **Date:** 2026-10-31

## Context

1. **Auth Mode vs. Engine Architecture:**
   - Flat-rate monthly subscriptions (Cursor Pro/Business, Claude Pro/Max, ChatGPT Plus/Pro) must run through the official vendor CLI (`agent`, `claude`, `codex`).
   - Pay-as-you-go API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`) can either run through an open-source CLI coding harness (**OpenCode** `opencode run` or **Pi** `pi -p`) or an in-memory Python SDK loop.
2. **Native Session Continuation in Telegram:**
   - In Telegram, all messages happen inside a single continuous chat thread.
   - We need follow-up messages during an incident to resume the CLI's **native agent loop and context** (`agent --resume <id>`, `opencode run --session <id>`, `claude --resume <id>`), while preventing stale sessions from yesterday's incident from polluting today's debugging.

## Decision

1. **Two Modes in `config.yaml` (`cli_subscription` and `sdk_apikey`):**
   - `cli_subscription`: Defaults to **Cursor CLI** (`agent -p --output-format json [--resume <id>]`), with presets for `claude`, `codex`, and `gemini`.
   - `sdk_apikey`: Defaults to the **OpenCode CLI harness** (`opencode run --format json [--session <id>]`), with `pi` and the built-in in-memory `python_sdk` loop (including `auto_fallback_to_python_sdk: true` if OpenCode is not yet installed on the machine).
2. **Automatic 3-Trigger Native Session Lifecycle (`agent/session_manager.py`):**
   - Capture the native `session_id` (`sessionID` / `chatId`) from CLI JSON output and store it per `(telegram_chat_topic, provider)` in `.sessions.json`.
   - Resume that native session on subsequent messages until any of three rotation triggers fire:
     1. **Idle Timeout (`idle_timeout_minutes: 30`):** If the chat is silent for > 30 minutes, automatically start a fresh session on the next message.
     2. **Turn Cap (`max_turns_per_session: 15`):** Automatically rotate after 15 turns to bound context size and token usage.
     3. **Explicit Reset (`/new`, `/clear`, or `[🔄 Start New Session]` button):** Allow any teammate to reset the session immediately when switching topics.
3. **Shared `ops_toolkit.py` with Multi-Log & Code Inspection:**
   - Extend `ops_toolkit.py` with `list_service_log_files`, multi-category `get_service_logs`, `search_service_code`, and path-jailed `read_service_code_file` so every mode and harness has complete visibility into action-specific logs and service source code.

## Consequences

- **Positive:** Both `cli_subscription` (Cursor) and `sdk_apikey` (OpenCode) enjoy full agentic capabilities (code search, multi-file logs, native session memory, context compaction) plus our Windows/PM2/ngrok/n8n/MySQL guardrails.
- **Positive:** Teammates get seamless multi-turn conversations during an outage, and automatic clean sessions when a new outage happens hours later.

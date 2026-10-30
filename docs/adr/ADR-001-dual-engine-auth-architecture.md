# ADR-001: Unifying Agent Architecture with Authentication Mode (`cli_subscription` vs. `sdk_apikey`)

- **Status:** Accepted
- **Date:** 2026-10-30

## Context

We initially evaluated two ways to build a server debugging agent on Windows Server:
1. Building a custom Python tool-calling loop using an LLM SDK.
2. Wrapping an autonomous CLI coding agent (`agent` / `claude` / `codex`).

We also evaluated two ways to pay for and authenticate LLM usage:
1. Using an existing flat-rate monthly subscription (such as Cursor Pro/Business, Claude Pro/Max, or ChatGPT Plus/Pro).
2. Using a pay-as-you-go API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`).

During analysis, we identified that **LLM providers enforce a strict technical boundary between consumer/seat subscriptions and raw HTTP APIs**:
- Consumer/seat OAuth subscriptions are only permitted to execute inside the vendor's official CLI harness (`cursor-agent`/`agent`, `claude`, `codex`).
- Direct Python HTTP SDK loops (`anthropic`, `openai`) require a pay-as-you-go API key.

Additionally, maintaining two completely separate agent implementations would risk duplicating every Windows Server, PM2, ngrok, n8n, and MySQL tool definition.

## Decision

1. **Collapse the Architecture Switch and Authentication Switch into a Single Engine Mode (`engine.mode` in `config.yaml`):**
   - `cli_subscription`: Uses the logged-in subscription CLI binary as the reasoning engine.
   - `sdk_apikey`: Uses the in-memory Python async SDK tool-calling loop with an API key.
2. **Share a Single Operational Toolkit (`ops_toolkit.py`):**
   - All Windows Server, PM2, ngrok, n8n, read-only MySQL, and gated action functions are implemented **once** in `ops_toolkit.py`.
   - `sdk_apikey` imports `OpsToolkit` directly as Python methods.
   - `cli_subscription` invokes `python ops_toolkit.py <subcommand>` via CLI instructions injected into its prompt, while also leveraging the CLI's native ability to read/grep service source code directories.
3. **Share Conversation Memory & Gated Approvals in Python:**
   - Python (`EngineRouter` and `PendingActionStore`) owns the conversation history and the `.pending_actions.json` approval queue, ensuring seamless live switching between `cli_subscription` and `sdk_apikey` without losing context or bypassing safety controls.

## Consequences

- **Positive:** Zero duplication of Windows/PM2/ngrok/n8n/MySQL diagnostic logic.
- **Positive:** $0 incremental token cost when running day-to-day in `cli_subscription` mode on an existing Cursor subscription.
- **Positive:** Instant fallback to `sdk_apikey` mode (via `config.yaml` or `/mode sdk` in Telegram) if the subscription hits a rolling quota cap or requires low-latency concurrent execution.
- **Trade-off:** `cli_subscription` spawns an OS subprocess per turn (~2–3s cold start on Windows), mitigated by an `asyncio.Semaphore` (`max_concurrent_processes`) so multiple teammates do not overwhelm the server with concurrent CLI processes.

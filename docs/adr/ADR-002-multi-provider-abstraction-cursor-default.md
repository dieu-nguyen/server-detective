# ADR-002: Multi-Provider Abstraction with Cursor CLI as Default Subscription Engine

- **Status:** Accepted
- **Date:** 2026-10-30

## Context

The team already holds an active **Cursor** subscription and wants to use it immediately as the primary engine (`cli_subscription`), while avoiding lock-in if they later switch to Claude Code CLI, OpenAI Codex CLI, Gemini CLI, or various API key providers (Anthropic, OpenAI, Gemini, OpenRouter).

## Decision

1. **CLI Subscription Provider Presets (`agent/engines/cli_subscription.py`):**
   - Set `cursor` (`agent -p "<prompt>" --output-format text`) as the default `cli_subscription.provider` in `config.yaml`.
   - Include built-in command builders for `cursor`, `claude`, `codex`, `gemini`, and a `custom` argv template so switching CLI providers requires only changing `cli_subscription.provider` in `config.yaml`.
   - Automatically detect `agent` or `cursor-agent` on `PATH` (and support multi-token binary strings like `wsl agent` if Cursor CLI is installed inside WSL on Windows Server).
2. **SDK API Key Provider Abstraction (`agent/engines/sdk_apikey.py`):**
   - Support native `anthropic` (`AsyncAnthropic`) and `openai` (`AsyncOpenAI` with configurable `base_url`) clients using a single provider-agnostic `TOOL_DEFINITIONS` schema.
   - Pre-configure OpenAI-compatible base URLs for `openai`, `gemini`, and `openrouter`, plus arbitrary `custom_openai` endpoints.

## Consequences

- **Positive:** Works out of the box with the user's existing Cursor subscription, giving access to multiple frontier models (`sonnet-4`, `gpt-5`, `gemini`) under one subscription.
- **Positive:** Switching to another CLI or API provider in the future requires zero code changes—only a 1-line edit in `config.yaml`.

# ADR-006: Removing In-Memory Python SDK Loop in Favor of CLI Harnesses (`opencode` / `pi`) and Configurable Hard CLI Restrictions

- **Status:** Accepted
- **Date:** 2026-10-31

## Context

1. **Unifying Both Modes on Agent CLI Harnesses:**
   - Once `sdk_apikey` mode adopted **OpenCode** (`opencode run --format json --session <id>`) and **Pi** (`pi -p --session <id>`) as its execution harnesses, maintaining a separate in-memory Python SDK tool-calling loop (`anthropic` / `openai` Python packages + duplicate JSON tool schemas) became redundant.
   - Removing the `python_sdk` loop means **both** modes (`cli_subscription` and `sdk_apikey`) operate with the exact same architecture: headless CLI coding harnesses + native `--resume` / `--session` continuation + SQLite `SessionManager` + `ops_toolkit.py`.
2. **Optional Hard CLI Tool & Command Restrictions (`engine.cli_restrictions`):**
   - Because CLI harnesses (`agent`, `claude`, `opencode`, `pi`, `codex`) include built-in file-edit and shell tools, we want a clean switch in `config.yaml` (`engine.cli_restrictions.enabled`) that starts **disabled (`false`) by default** for initial review, and when enabled (`true`), enforces native CLI permission flags/configs to block file modifications and restrict shell execution to `allowed_shell_commands`.

## Decision

1. **Remove In-Memory `python_sdk` Loop:**
   - `sdk_apikey` mode now exclusively uses **OpenCode (`opencode`)** (default) or **Pi (`pi`)** with the configured API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`).
   - Removed unused `anthropic` and `openai` Python SDK dependencies from `requirements.txt`.
2. **Add `engine.cli_restrictions` to `config.yaml` (`enabled: false` by default):**
   - When `enabled: false` (default): CLI harnesses run with standard flags and follow the system prompt safety rules.
   - When `enabled: true`:
     - **OpenCode (`opencode`):** Injects `OPENCODE_CONFIG_CONTENT` with `permission.edit = "deny"` and `permission.bash` allow/deny rules from `config.yaml`.
     - **Cursor CLI (`agent`):** Writes `.cursor/cli.json` with `permissions.allow` (`Read(**)` + `Shell(<allowed>)`) and `permissions.deny` (`Write(**)` + `Shell(<denied>)`).
     - **Claude Code CLI (`claude`):** Passes `--disallowedTools Edit Write NotebookEdit` and `--allowedTools Read Grep Glob Bash(<allowed>)`.
     - **Codex (`codex`) & Pi (`pi`):** Passes `--sandbox read-only` (Codex) and `--tools read,grep,find,ls,bash` (Pi).

## Consequences

- **Positive:** Simpler, lighter Python codebase with zero duplicate tool-schema maintenance.
- **Positive:** You can test and review the agent with `cli_restrictions.enabled: false` first, and flip `enabled: true` in `config.yaml` whenever you want hard technical CLI sandboxing.

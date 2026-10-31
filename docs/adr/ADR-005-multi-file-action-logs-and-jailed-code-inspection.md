# ADR-005: Multi-File Action Log Discovery and Path-Jailed Source Code Inspection

- **Status:** Accepted
- **Date:** 2026-10-31

## Context

1. **Multiple Action-Specific Log Files per Service:**
   - On the target Windows Server, a single service does not merely write to one rolling `app.log`. Instead, it writes different functional actions into separate log files (e.g., `payment.log`, `webhook.log`, `auth.log`, `scheduler.log`, `mysql_sync.log`, `error.log`).
   - Only reading the newest 1–2 files matching `*.log` would miss errors in action-specific log files that were modified slightly earlier.
2. **Source Code Inspection Across Both Modes:**
   - Effective debugging requires correlating log stack traces and error messages with the actual Python (`.py`) and Node.js (`.js`/`.ts`) source code on disk.
   - While CLI harnesses (`agent`, `opencode`) have built-in file tools, our shared `ops_toolkit.py` also needs structured, path-jailed source code search and read tools so every engine (including the Python SDK fallback) can inspect service code safely without exposing `.env` secrets or allowing directory traversal outside the service folders.

## Decision

1. **Multi-File Action Log Mapping & Discovery (`logs_dir` + `log_files` in `config.yaml`):**
   - Each service in `config.yaml` defines `logs_dir` and a named `log_files` dictionary mapping action categories (`payment`, `webhook`, `auth`, etc.) to file globs.
   - Added `list_service_log_files(service_name)` (`ops_toolkit.py list-service-logs`) to enumerate all discovered log files with their action category, size in KB, and UTC modification timestamp.
   - Enhanced `get_service_logs(service_name, category_or_file, lines, filter_keyword)` (`ops_toolkit.py service-logs --category ...`) so the agent can tail/filter a specific action category or search across all active log files of a service simultaneously.
2. **Path-Jailed Source Code Search & Read Tools (`search_service_code` & `read_service_code_file`):**
   - Added `search_service_code` (`ops_toolkit.py search-code`) to grep inside a service's `working_dir` while automatically pruning heavy/irrelevant folders (`node_modules`, `.venv`, `__pycache__`, `.git`, `dist`, `build`).
   - Added `read_service_code_file` (`ops_toolkit.py read-code`) to return line-numbered source code slices while enforcing `_resolve_jailed_service_path()` (blocking `../` path traversal outside `working_dir`) and explicitly blocking `.env` secret files.

## Consequences

- **Positive:** The agent can accurately investigate domain-specific failures (e.g., checking `payment.log` vs `webhook.log`) and trace stack traces to exact source lines in `working_dir`.
- **Positive:** `.env` files and paths outside registered service directories remain protected.

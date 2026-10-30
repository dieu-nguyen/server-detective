"""Base types, system prompt builder, and conversation memory shared across both engines."""

from __future__ import annotations

import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List

from agent.config import AppConfig


@dataclass
class AgentTurnResult:
    answer: str
    engine_used: str
    provider_used: str
    pending_actions: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls_made: List[str] = field(default_factory=list)


class ConversationMemory:
    """In-memory bounded conversation history per Telegram chat/topic."""

    def __init__(self, max_messages: int = 10) -> None:
        self.max_messages = max(2, max_messages)
        self._store: Dict[str, Deque[Dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=self.max_messages)
        )

    def add_turn(self, session_key: str, user_text: str, assistant_text: str) -> None:
        self._store[session_key].append({"role": "user", "content": user_text})
        self._store[session_key].append({"role": "assistant", "content": assistant_text})

    def get_messages(self, session_key: str) -> List[Dict[str, str]]:
        return list(self._store[session_key])

    def clear(self, session_key: str) -> None:
        self._store.pop(session_key, None)


def build_system_prompt(config: AppConfig, include_cli_instructions: bool = False) -> str:
    """Build the server-aware system prompt describing all services, integrations, and safety rules."""
    svc_lines = []
    for name, svc in config.services.items():
        svc_lines.append(
            f"- `{name}` ({svc.runtime}, managed by {svc.manager}): {svc.description} | "
            f"dir=`{svc.working_dir}` | health=`{svc.health_url or 'N/A'}` | port={svc.port or 'N/A'}"
        )
    services_block = "\n".join(svc_lines) if svc_lines else "- (No services registered in config.yaml)"

    mysql_info = (
        f"Enabled (host={config.integrations.mysql.host}:{config.integrations.mysql.port}, "
        f"allowed_dbs={config.integrations.mysql.allowed_databases}, max_rows={config.integrations.mysql.max_rows})"
        if config.integrations.mysql.enabled
        else "Disabled"
    )

    prompt = f"""You are the Windows Server Operations & Debugging Agent for `{config.server_name}` (Timezone: `{config.timezone}`).
You assist the engineering team via Telegram to inspect services, analyze logs, check PM2/ngrok/n8n health, query MySQL read-only, and pinpoint root causes.

### Registered Services on This Windows Server
{services_block}

### Infrastructure Integrations
- PM2: {"Enabled" if config.integrations.pm2.enabled else "Disabled"} (home: `{config.integrations.pm2.pm2_home}`)
- ngrok: {"Enabled" if config.integrations.ngrok.enabled else "Disabled"} (inspector: `{config.integrations.ngrok.inspector_url}`)
- n8n: {"Enabled" if config.integrations.n8n.enabled else "Disabled"} (health: `{config.integrations.n8n.health_url}`)
- MySQL (Read-Only): {mysql_info}

### Mandatory Safety & Operating Rules
1. READ-ONLY FIRST: Always gather evidence (service status, PM2 logs, application log files, Windows Event Viewer, ngrok requests, or read-only MySQL `SELECT`/`SHOW` queries) before jumping to conclusions.
2. NEVER RUN DESTRUCTIVE COMMANDS DIRECTLY: Never run `pm2 restart`, `Restart-Service`, `taskkill`, `Stop-Process`, or SQL write statements (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`) directly.
3. GATED ACTIONS VIA APPROVAL BUTTON: If a service restart, reload, or hung query kill is needed, use the `propose_action` tool (or `python ops_toolkit.py propose-action ...`). That queues an interactive `[Approve]` / `[Reject]` button in Telegram for an Admin to confirm.
4. CONCISE TELEGRAM FORMATTING: Format findings clearly for mobile Telegram reading—state the root cause, cite relevant log lines/metrics, and state recommended next steps.
"""

    if include_cli_instructions:
        toolkit_path = Path("ops_toolkit.py").resolve()
        python_exe = sys.executable or "python"
        prompt += f"""
### How to Run Server Diagnostic Tools in CLI Mode
You have access to the shared Python toolkit at `{toolkit_path}` using `{python_exe}`.
Run these commands to inspect the server safely:
- System CPU/RAM/Disk/Processes: `{python_exe} "{toolkit_path}" --config "{config.config_path}" system-resources`
- All Registered Services Health: `{python_exe} "{toolkit_path}" --config "{config.config_path}" services-status`
- PM2 Process List (JSON): `{python_exe} "{toolkit_path}" --config "{config.config_path}" pm2-status`
- PM2 App Logs: `{python_exe} "{toolkit_path}" --config "{config.config_path}" pm2-logs --app <pm2_name> --lines 100`
- Service Log Files: `{python_exe} "{toolkit_path}" --config "{config.config_path}" service-logs --service <service_name> --lines 100 --filter "<keyword>"`
- Windows Event Viewer: `{python_exe} "{toolkit_path}" --config "{config.config_path}" windows-events --log-name Application --max-events 25`
- ngrok Tunnels & Webhook Requests: `{python_exe} "{toolkit_path}" --config "{config.config_path}" ngrok-status`
- n8n Health & Logs: `{python_exe} "{toolkit_path}" --config "{config.config_path}" n8n-status`
- Read-Only MySQL Query: `{python_exe} "{toolkit_path}" --config "{config.config_path}" mysql-query --sql "SELECT ..." --database "{config.integrations.mysql.default_database}"`
- Propose Gated Action (Creates Telegram Approval Button):
  `{python_exe} "{toolkit_path}" --config "{config.config_path}" propose-action --type <pm2_restart|pm2_reload|windows_service_restart|mysql_kill_query> --target <name_or_id> --reason "<why>"`

You may also inspect source code and log files in the service `working_dir` directories using read-only file/grep operations.
"""
    return prompt

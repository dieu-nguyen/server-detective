"""Configuration loader and runtime mode management."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv


@dataclass
class SessionConfig:
    """Controls SQLite-backed native CLI session continuation, cross-tool porting, and auto-rotation."""

    enabled: bool = True
    state_db_file: str = "./agent_state.db"
    sessions_file: str = "./agent_state.db"  # Alias for backwards compatibility
    idle_timeout_minutes: int = 30  # Auto-start a fresh native session if chat was quiet > 30 mins
    max_turns_per_session: int = 15  # Auto-rotate session after N turns to prevent context bloat
    show_new_session_button: bool = True  # Show inline [🔄 New Session] button on replies


@dataclass
class CLIRestrictionsConfig:
    """
    Optional hard CLI tool/command restrictions for both `cli_subscription` and `sdk_apikey` (OpenCode/Pi).
    Disabled (`enabled: false`) by default so you can review and enable it when ready.
    """

    enabled: bool = False
    allow_file_edits: bool = False
    allowed_shell_commands: List[str] = field(
        default_factory=lambda: [
            "python *ops_toolkit.py*",
            "python3 *ops_toolkit.py*",
            "*ops_toolkit.py*",
            "pm2 jlist*",
            "pm2 status*",
            "pm2 logs*",
            "pm2 describe*",
        ]
    )
    denied_shell_commands: List[str] = field(
        default_factory=lambda: [
            "pm2 restart*",
            "pm2 reload*",
            "pm2 stop*",
            "pm2 delete*",
            "pm2 kill*",
            "*Restart-Service*",
            "*Stop-Service*",
            "*Stop-Process*",
            "taskkill*",
            "del *",
            "rm -rf*",
            "format *",
        ]
    )


@dataclass
class CLISubscriptionConfig:
    provider: str = "cursor"  # "cursor" | "claude" | "codex" | "gemini" | "custom"
    model: str = ""
    timeout_seconds: int = 180
    max_concurrent_processes: int = 1
    default_working_dir: str = "."
    binaries: Dict[str, str] = field(
        default_factory=lambda: {
            "cursor": "agent",
            "claude": "claude",
            "codex": "codex",
            "gemini": "gemini",
        }
    )
    custom_command_template: List[str] = field(default_factory=list)
    # Headless Telegram runs cannot answer Cursor CLI's workspace-trust prompt.
    trust_workspace: bool = True
    # Headless Telegram cannot answer Cursor's y/n command-approval prompts.
    force_allow_commands: bool = True
    # Auto-approve MCP server prompts (`--approve-mcps`).
    approve_mcps: bool = True
    # Human-readable live log of assistant text + tool calls (empty string disables).
    # Cursor print mode does not emit hidden chain-of-thought; this is the visible stream.
    turn_log_file: str = "./logs/agent-turns.log"


@dataclass
class SDKApiKeyConfig:
    """
    Configuration for `sdk_apikey` mode (API Key mode).
    Uses a CLI coding agent harness (`opencode` default, or `pi`) powered by your API key
    (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`).
    """

    harness: str = "opencode"  # "opencode" | "pi"
    opencode_bin: str = "opencode"
    pi_bin: str = "pi"
    provider: str = "anthropic"  # "openai" | "anthropic" | "gemini" | "openrouter" | etc.
    model: str = "claude-3-5-haiku-latest"  # e.g. "claude-sonnet-4-5", "gpt-4.1-mini", "gemini-2.5-flash"
    api_key_env: str = "ANTHROPIC_API_KEY"
    timeout_seconds: int = 180
    max_concurrent_processes: int = 2


@dataclass
class EngineConfig:
    mode: str = "cli_subscription"  # "cli_subscription" | "sdk_apikey"
    allow_telegram_mode_switch: bool = True
    auto_fallback_to_sdk: bool = False
    max_history_messages: int = 10
    audit_log_file: str = "./audit.jsonl"
    pending_actions_file: str = "./agent_state.db"
    session: SessionConfig = field(default_factory=SessionConfig)
    cli_restrictions: CLIRestrictionsConfig = field(default_factory=CLIRestrictionsConfig)
    cli_subscription: CLISubscriptionConfig = field(default_factory=CLISubscriptionConfig)
    sdk_apikey: SDKApiKeyConfig = field(default_factory=SDKApiKeyConfig)


@dataclass
class TelegramConfig:
    allowed_group_ids: List[int] = field(default_factory=list)
    admins: List[int] = field(default_factory=list)
    viewers: List[int] = field(default_factory=list)
    require_mention_in_groups: bool = False
    action_expiry_seconds: int = 600


@dataclass
class PM2IntegrationConfig:
    enabled: bool = True
    pm2_bin: str = "pm2"
    pm2_home: str = ""
    default_log_lines: int = 100


@dataclass
class NgrokIntegrationConfig:
    enabled: bool = True
    inspector_url: str = "http://127.0.0.1:4040"
    win_service_name: str = ""
    pm2_name: str = ""


@dataclass
class N8nIntegrationConfig:
    enabled: bool = True
    health_url: str = "http://127.0.0.1:5678/healthz"
    pm2_name: str = "n8n"
    win_service_name: str = ""
    working_dir: str = ""
    log_paths: List[str] = field(default_factory=list)


@dataclass
class MySQLIntegrationConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "ops_agent_ro"
    password_env: str = "MYSQL_PASSWORD"
    default_database: str = ""
    allowed_databases: List[str] = field(default_factory=list)
    max_rows: int = 50
    query_timeout_seconds: int = 5


@dataclass
class IntegrationsConfig:
    pm2: PM2IntegrationConfig = field(default_factory=PM2IntegrationConfig)
    ngrok: NgrokIntegrationConfig = field(default_factory=NgrokIntegrationConfig)
    n8n: N8nIntegrationConfig = field(default_factory=N8nIntegrationConfig)
    mysql: MySQLIntegrationConfig = field(default_factory=MySQLIntegrationConfig)


@dataclass
class ServiceDefinition:
    name: str
    description: str = ""
    runtime: str = "python"  # "python" | "nodejs" | "dotnet" | "other"
    manager: str = "pm2"  # "pm2" | "windows_service" | "process"
    pm2_name: str = ""
    win_service_name: str = ""
    process_match: str = ""
    working_dir: str = ""
    logs_dir: str = ""  # Directory containing multiple action log files
    log_files: Dict[str, str] = field(default_factory=dict)  # Action name -> path/glob
    log_paths: List[str] = field(default_factory=list)
    event_log_source: str = ""
    health_url: str = ""
    port: Optional[int] = None


@dataclass
class AppConfig:
    server_name: str = "windows-server"
    timezone: str = "Asia/Saigon"
    engine: EngineConfig = field(default_factory=EngineConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    integrations: IntegrationsConfig = field(default_factory=IntegrationsConfig)
    services: Dict[str, ServiceDefinition] = field(default_factory=dict)
    config_path: Path = field(default_factory=lambda: Path("config.yaml"))

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.telegram.admins

    def is_viewer(self, user_id: int) -> bool:
        return user_id in self.telegram.viewers or user_id in self.telegram.admins

    def is_authorized(self, user_id: int, chat_id: int) -> bool:
        if user_id in self.telegram.admins or user_id in self.telegram.viewers:
            if chat_id < 0 and self.telegram.allowed_group_ids:
                return chat_id in self.telegram.allowed_group_ids
            return True
        return False


def load_config(config_path: str | Path = "config.yaml", env_path: Optional[str | Path] = None) -> AppConfig:
    """Load `.env` and `config.yaml` into a typed `AppConfig` instance."""
    if env_path:
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        load_dotenv(override=False)

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path.resolve()}")

    with path.open("r", encoding="utf-8") as f:
        raw: Dict[str, Any] = yaml.safe_load(f) or {}

    eng_raw = raw.get("engine", {}) or {}
    sess_raw = eng_raw.get("session", {}) or {}
    rest_raw = eng_raw.get("cli_restrictions", {}) or {}
    cli_raw = eng_raw.get("cli_subscription", {}) or {}
    sdk_raw = eng_raw.get("sdk_apikey", {}) or {}

    db_file = str(
        sess_raw.get("state_db_file")
        or sess_raw.get("sessions_file")
        or eng_raw.get("state_db_file")
        or "./agent_state.db"
    )
    session_cfg = SessionConfig(
        enabled=bool(sess_raw.get("enabled", True)),
        state_db_file=db_file,
        sessions_file=db_file,
        idle_timeout_minutes=int(sess_raw.get("idle_timeout_minutes", 30)),
        max_turns_per_session=int(sess_raw.get("max_turns_per_session", 15)),
        show_new_session_button=bool(sess_raw.get("show_new_session_button", True)),
    )

    default_allowed_cmds = CLIRestrictionsConfig().allowed_shell_commands
    default_denied_cmds = CLIRestrictionsConfig().denied_shell_commands
    restrictions_cfg = CLIRestrictionsConfig(
        enabled=bool(rest_raw.get("enabled", False)),
        allow_file_edits=bool(rest_raw.get("allow_file_edits", False)),
        allowed_shell_commands=list(rest_raw.get("allowed_shell_commands", default_allowed_cmds) or []),
        denied_shell_commands=list(rest_raw.get("denied_shell_commands", default_denied_cmds) or []),
    )

    default_binaries = {
        "cursor": "agent",
        "claude": "claude",
        "codex": "codex",
        "gemini": "gemini",
    }
    default_binaries.update(cli_raw.get("binaries", {}) or {})

    cli_cfg = CLISubscriptionConfig(
        provider=str(cli_raw.get("provider", "cursor")).lower(),
        model=str(cli_raw.get("model", "") or ""),
        timeout_seconds=int(cli_raw.get("timeout_seconds", 180)),
        max_concurrent_processes=int(cli_raw.get("max_concurrent_processes", 1)),
        default_working_dir=str(cli_raw.get("default_working_dir", ".")),
        binaries=default_binaries,
        custom_command_template=list(cli_raw.get("custom_command_template", []) or []),
        trust_workspace=bool(cli_raw.get("trust_workspace", True)),
        force_allow_commands=bool(cli_raw.get("force_allow_commands", True)),
        approve_mcps=bool(cli_raw.get("approve_mcps", True)),
        turn_log_file=str(cli_raw.get("turn_log_file", "./logs/agent-turns.log") or ""),
    )

    sdk_cfg = SDKApiKeyConfig(
        harness=str(sdk_raw.get("harness", "opencode")).lower(),
        opencode_bin=str(sdk_raw.get("opencode_bin", "opencode")),
        pi_bin=str(sdk_raw.get("pi_bin", "pi")),
        provider=str(sdk_raw.get("provider", "anthropic")).lower(),
        model=str(sdk_raw.get("model", "claude-3-5-haiku-latest")),
        api_key_env=str(sdk_raw.get("api_key_env", "ANTHROPIC_API_KEY")),
        timeout_seconds=int(sdk_raw.get("timeout_seconds", 180)),
        max_concurrent_processes=int(sdk_raw.get("max_concurrent_processes", 2)),
    )

    mode = str(eng_raw.get("mode", "cli_subscription")).lower()
    if mode not in ("cli_subscription", "sdk_apikey"):
        raise ValueError(f"Invalid engine.mode '{mode}'. Must be 'cli_subscription' or 'sdk_apikey'.")

    engine_cfg = EngineConfig(
        mode=mode,
        allow_telegram_mode_switch=bool(eng_raw.get("allow_telegram_mode_switch", True)),
        auto_fallback_to_sdk=bool(eng_raw.get("auto_fallback_to_sdk", False)),
        max_history_messages=int(eng_raw.get("max_history_messages", 10)),
        audit_log_file=str(eng_raw.get("audit_log_file", "./audit.jsonl")),
        pending_actions_file=str(eng_raw.get("pending_actions_file", db_file)),
        session=session_cfg,
        cli_restrictions=restrictions_cfg,
        cli_subscription=cli_cfg,
        sdk_apikey=sdk_cfg,
    )

    tg_raw = raw.get("telegram", {}) or {}
    telegram_cfg = TelegramConfig(
        allowed_group_ids=[int(x) for x in (tg_raw.get("allowed_group_ids") or [])],
        admins=[int(x) for x in (tg_raw.get("admins") or [])],
        viewers=[int(x) for x in (tg_raw.get("viewers") or [])],
        require_mention_in_groups=bool(tg_raw.get("require_mention_in_groups", False)),
        action_expiry_seconds=int(tg_raw.get("action_expiry_seconds", 600)),
    )

    int_raw = raw.get("integrations", {}) or {}
    pm2_raw = int_raw.get("pm2", {}) or {}
    ngrok_raw = int_raw.get("ngrok", {}) or {}
    n8n_raw = int_raw.get("n8n", {}) or {}
    mysql_raw = int_raw.get("mysql", {}) or {}

    integrations_cfg = IntegrationsConfig(
        pm2=PM2IntegrationConfig(
            enabled=bool(pm2_raw.get("enabled", True)),
            pm2_bin=str(pm2_raw.get("pm2_bin", "pm2")),
            pm2_home=str(pm2_raw.get("pm2_home", "") or ""),
            default_log_lines=int(pm2_raw.get("default_log_lines", 100)),
        ),
        ngrok=NgrokIntegrationConfig(
            enabled=bool(ngrok_raw.get("enabled", True)),
            inspector_url=str(ngrok_raw.get("inspector_url", "http://127.0.0.1:4040")),
            win_service_name=str(ngrok_raw.get("win_service_name", "") or ""),
            pm2_name=str(ngrok_raw.get("pm2_name", "") or ""),
        ),
        n8n=N8nIntegrationConfig(
            enabled=bool(n8n_raw.get("enabled", True)),
            health_url=str(n8n_raw.get("health_url", "http://127.0.0.1:5678/healthz")),
            pm2_name=str(n8n_raw.get("pm2_name", "n8n") or ""),
            win_service_name=str(n8n_raw.get("win_service_name", "") or ""),
            working_dir=str(n8n_raw.get("working_dir", "") or ""),
            log_paths=list(n8n_raw.get("log_paths", []) or []),
        ),
        mysql=MySQLIntegrationConfig(
            enabled=bool(mysql_raw.get("enabled", True)),
            host=str(mysql_raw.get("host", "127.0.0.1")),
            port=int(mysql_raw.get("port", 3306)),
            user=str(mysql_raw.get("user", "ops_agent_ro")),
            password_env=str(mysql_raw.get("password_env", "MYSQL_PASSWORD")),
            default_database=str(mysql_raw.get("default_database", "") or ""),
            allowed_databases=list(mysql_raw.get("allowed_databases", []) or []),
            max_rows=int(mysql_raw.get("max_rows", 50)),
            query_timeout_seconds=int(mysql_raw.get("query_timeout_seconds", 5)),
        ),
    )

    services_raw = raw.get("services", {}) or {}
    services: Dict[str, ServiceDefinition] = {}
    for svc_name, svc_data in services_raw.items():
        if not isinstance(svc_data, dict):
            continue
        port_val = svc_data.get("port")
        raw_log_files = svc_data.get("log_files") or {}
        log_files_dict = {str(k): str(v) for k, v in raw_log_files.items()} if isinstance(raw_log_files, dict) else {}
        services[svc_name] = ServiceDefinition(
            name=svc_name,
            description=str(svc_data.get("description", "") or ""),
            runtime=str(svc_data.get("runtime", "python")),
            manager=str(svc_data.get("manager", "pm2")),
            pm2_name=str(svc_data.get("pm2_name", svc_name) or ""),
            win_service_name=str(svc_data.get("win_service_name", "") or ""),
            process_match=str(svc_data.get("process_match", "") or ""),
            working_dir=str(svc_data.get("working_dir", "") or ""),
            logs_dir=str(svc_data.get("logs_dir", "") or ""),
            log_files=log_files_dict,
            log_paths=list(svc_data.get("log_paths", []) or []),
            event_log_source=str(svc_data.get("event_log_source", "") or ""),
            health_url=str(svc_data.get("health_url", "") or ""),
            port=int(port_val) if port_val is not None else None,
        )

    return AppConfig(
        server_name=str(raw.get("server_name", "windows-server")),
        timezone=str(raw.get("timezone", "Asia/Saigon")),
        engine=engine_cfg,
        telegram=telegram_cfg,
        integrations=integrations_cfg,
        services=services,
        config_path=path,
    )


def save_engine_mode_to_config(config_path: str | Path, new_mode: str) -> None:
    """Persist a mode switch (`cli_subscription` or `sdk_apikey`) into `config.yaml` while preserving comments."""
    if new_mode not in ("cli_subscription", "sdk_apikey"):
        raise ValueError(f"Unsupported mode: {new_mode}")
    path = Path(config_path)
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    import re

    updated = re.sub(
        r'^(\s*mode:\s*)["\']?(cli_subscription|sdk_apikey)["\']?',
        rf'\1"{new_mode}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    path.write_text(updated, encoding="utf-8")

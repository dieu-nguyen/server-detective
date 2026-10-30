"""Configuration loader and runtime mode management."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv


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


@dataclass
class SDKApiKeyConfig:
    provider: str = "anthropic"  # "openai" | "anthropic" | "gemini" | "openrouter" | "custom_openai"
    model: str = "claude-3-5-haiku-latest"
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: str = ""
    max_tool_turns: int = 8
    temperature: float = 0.1


@dataclass
class EngineConfig:
    mode: str = "cli_subscription"  # "cli_subscription" | "sdk_apikey"
    allow_telegram_mode_switch: bool = True
    auto_fallback_to_sdk: bool = False
    max_history_messages: int = 10
    audit_log_file: str = "./audit.jsonl"
    pending_actions_file: str = "./.pending_actions.json"
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
    cli_raw = eng_raw.get("cli_subscription", {}) or {}
    sdk_raw = eng_raw.get("sdk_apikey", {}) or {}

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
    )

    sdk_cfg = SDKApiKeyConfig(
        provider=str(sdk_raw.get("provider", "anthropic")).lower(),
        model=str(sdk_raw.get("model", "claude-3-5-haiku-latest")),
        api_key_env=str(sdk_raw.get("api_key_env", "ANTHROPIC_API_KEY")),
        base_url=str(sdk_raw.get("base_url", "") or ""),
        max_tool_turns=int(sdk_raw.get("max_tool_turns", 8)),
        temperature=float(sdk_raw.get("temperature", 0.1)),
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
        pending_actions_file=str(eng_raw.get("pending_actions_file", "./.pending_actions.json")),
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
        services[svc_name] = ServiceDefinition(
            name=svc_name,
            description=str(svc_data.get("description", "") or ""),
            runtime=str(svc_data.get("runtime", "python")),
            manager=str(svc_data.get("manager", "pm2")),
            pm2_name=str(svc_data.get("pm2_name", svc_name) or ""),
            win_service_name=str(svc_data.get("win_service_name", "") or ""),
            process_match=str(svc_data.get("process_match", "") or ""),
            working_dir=str(svc_data.get("working_dir", "") or ""),
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

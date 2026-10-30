"""Unit and integration tests for Windows Server Multi-Service Telegram Debug Agent."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent.config import load_config, save_engine_mode_to_config
from agent.engines.cli_subscription import CLISubscriptionEngine
from agent.engines.router import EngineRouter
from agent.security import redact_secrets, validate_readonly_sql
from ops_toolkit import OpsToolkit


def test_config_loads_and_validates() -> None:
    cfg = load_config("config.yaml")
    assert cfg.server_name == "prod-win-server-01"
    assert cfg.engine.mode == "cli_subscription"
    assert cfg.engine.cli_subscription.provider == "cursor"
    assert "order-api-py" in cfg.services
    assert "realtime-gateway-node" in cfg.services
    assert cfg.is_admin(111111111)
    assert not cfg.is_admin(333333333)
    assert cfg.is_viewer(333333333)
    assert cfg.is_authorized(333333333, -1001234567890)
    assert not cfg.is_authorized(999999999, -1001234567890)


def test_sql_readonly_guardrail() -> None:
    ok, sql, err = validate_readonly_sql("SELECT id, status FROM orders WHERE id = 42", max_rows=25)
    assert ok
    assert sql == "SELECT id, status FROM orders WHERE id = 42 LIMIT 25"
    assert err == ""

    ok2, sql2, _ = validate_readonly_sql("SHOW PROCESSLIST;")
    assert ok2
    assert sql2 == "SHOW PROCESSLIST"

    for bad_query in [
        "DELETE FROM orders WHERE id = 1",
        "UPDATE users SET role='admin'",
        "DROP TABLE orders",
        "SELECT * FROM orders; DROP TABLE users",
        "KILL 1042",
    ]:
        valid, _, err_msg = validate_readonly_sql(bad_query)
        assert not valid
        assert err_msg


def test_secret_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYSQL_PASSWORD", "SuperSecretMySQLPass99!")
    raw = "Connecting with password=SuperSecretMySQLPass99! and key sk-ant-api03-1234567890abcdefghij"
    cleaned = redact_secrets(raw)
    assert "SuperSecretMySQLPass99!" not in cleaned
    assert "sk-ant-api03-1234567890abcdefghij" not in cleaned
    assert "[REDACTED" in cleaned


def test_cli_subscription_command_builders(tmp_path: Path) -> None:
    cfg = load_config("config.yaml")
    cfg.engine.pending_actions_file = str(tmp_path / "pending.json")
    tk = OpsToolkit(cfg)
    engine = CLISubscriptionEngine(cfg, tk)

    # Default Cursor CLI preset
    cmd_cursor = engine.build_command("check order-api-py")
    assert "-p" in cmd_cursor
    assert "check order-api-py" in cmd_cursor

    # Switch to Claude CLI preset
    cfg.engine.cli_subscription.provider = "claude"
    cfg.engine.cli_subscription.model = "sonnet"
    cmd_claude = engine.build_command("check logs")
    assert cmd_claude[0] == "claude"
    assert "--model" in cmd_claude

    # Switch to Codex CLI preset
    cfg.engine.cli_subscription.provider = "codex"
    cmd_codex = engine.build_command("inspect db")
    assert cmd_codex[:2] == ["codex", "exec"]


def test_ops_toolkit_and_gated_action_queue(tmp_path: Path) -> None:
    cfg = load_config("config.yaml")
    cfg.engine.pending_actions_file = str(tmp_path / "pending.json")
    tk = OpsToolkit(cfg)

    sys_res = tk.get_system_resources()
    assert "cpu_percent" in sys_res
    assert "memory" in sys_res

    proposed = tk.propose_action(
        action_type="pm2_restart",
        target="order-api-py",
        reason="High memory usage",
        requested_by_user_id=333333333,
        requested_by_username="dev_user",
    )
    assert proposed["ok"] is True
    action_id = proposed["action"]["action_id"]

    pending_list = tk.action_store.list_pending()
    assert len(pending_list) == 1
    assert pending_list[0]["action_id"] == action_id


def test_router_mode_switch_persistence(tmp_path: Path) -> None:
    temp_cfg = tmp_path / "config.yaml"
    temp_cfg.write_text(Path("config.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    cfg = load_config(temp_cfg)
    router = EngineRouter(cfg)
    assert router.current_mode == "cli_subscription"

    router.switch_mode("sdk", persist=True)
    assert router.current_mode == "sdk_apikey"

    reloaded = load_config(temp_cfg)
    assert reloaded.engine.mode == "sdk_apikey"
    save_engine_mode_to_config(temp_cfg, "cli_subscription")

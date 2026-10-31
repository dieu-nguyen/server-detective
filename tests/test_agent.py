"""Unit and integration tests for Windows Server Multi-Service Telegram Debug Agent."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent.config import ServiceDefinition, load_config, save_engine_mode_to_config
from agent.engines.cli_subscription import CLISubscriptionEngine
from agent.engines.router import EngineRouter
from agent.engines.sdk_apikey import SDKApiKeyEngine
from agent.security import redact_secrets, validate_readonly_sql
from agent.session_manager import SessionManager, parse_cli_json_output
from ops_toolkit import OpsToolkit


def test_config_loads_and_validates() -> None:
    cfg = load_config("config.yaml")
    assert cfg.server_name == "prod-win-server-01"
    assert cfg.engine.mode == "cli_subscription"
    assert cfg.engine.cli_subscription.provider == "cursor"
    assert cfg.engine.sdk_apikey.harness == "opencode"
    assert cfg.engine.session.idle_timeout_minutes == 30
    assert cfg.engine.session.max_turns_per_session == 15
    assert "order-api-py" in cfg.services
    assert "payment" in cfg.services["order-api-py"].log_files
    assert "webhook" in cfg.services["order-api-py"].log_files
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


def test_cli_subscription_and_opencode_command_builders_with_sessions(tmp_path: Path) -> None:
    cfg = load_config("config.yaml")
    cfg.engine.pending_actions_file = str(tmp_path / "pending.json")
    tk = OpsToolkit(cfg)
    cli_engine = CLISubscriptionEngine(cfg, tk)
    sdk_engine = SDKApiKeyEngine(cfg, tk)

    # 1. Cursor CLI fresh vs resumed session
    cmd_cursor_new = cli_engine.build_command("check order-api-py", resume_session_id=None)
    assert "--output-format" in cmd_cursor_new
    assert "json" in cmd_cursor_new
    assert "--resume" not in cmd_cursor_new

    cmd_cursor_resume = cli_engine.build_command("check payment log", resume_session_id="chat_abc123")
    assert "--resume" in cmd_cursor_resume
    assert "chat_abc123" in cmd_cursor_resume

    # 2. OpenCode CLI (`sdk_apikey` mode) fresh vs resumed session
    cmd_oc_new = sdk_engine.build_opencode_command("inspect logs", resume_session_id=None)
    assert cmd_oc_new[:2] == ["opencode", "run"]
    assert "--format" in cmd_oc_new
    assert "--session" not in cmd_oc_new

    cmd_oc_resume = sdk_engine.build_opencode_command("follow up", resume_session_id="ses_xyz789")
    assert "--session" in cmd_oc_resume
    assert "ses_xyz789" in cmd_oc_resume


def test_session_manager_lifecycle_and_auto_rotation(tmp_path: Path) -> None:
    sess_file = tmp_path / "sessions.json"
    mgr = SessionManager(sessions_file=sess_file, idle_timeout_minutes=30, max_turns_per_session=3)

    # Turn 1: New session
    st1 = mgr.resolve_session("-100123:main", "cli_subscription:cursor")
    assert st1.is_new_session is True
    assert st1.native_session_id is None
    st1_saved = mgr.record_turn(st1, "ses_native_001")
    assert st1_saved.turn_count == 1
    assert st1_saved.native_session_id == "ses_native_001"

    # Turn 2: Active follow-up resumes `ses_native_001`
    st2 = mgr.resolve_session("-100123:main", "cli_subscription:cursor")
    assert st2.is_new_session is False
    assert st2.native_session_id == "ses_native_001"
    mgr.record_turn(st2, "ses_native_001")

    # Turn 3: Reaches max_turns=3
    st3 = mgr.resolve_session("-100123:main", "cli_subscription:cursor")
    mgr.record_turn(st3, "ses_native_001")

    # Turn 4: Exceeds max_turns_per_session (3) -> Auto-rotates to a brand new session!
    st4 = mgr.resolve_session("-100123:main", "cli_subscription:cursor")
    assert st4.is_new_session is True
    assert st4.native_session_id is None
    assert "max_turns_reached" in st4.rotation_reason

    # Test idle timeout auto-rotation
    mgr.record_turn(st4, "ses_native_002")
    raw = json.loads(sess_file.read_text(encoding="utf-8"))
    raw["-100123:main"]["cli_subscription:cursor"]["last_active_at"] = time.time() - (45 * 60)
    sess_file.write_text(json.dumps(raw), encoding="utf-8")

    st_idle = mgr.resolve_session("-100123:main", "cli_subscription:cursor")
    assert st_idle.is_new_session is True
    assert st_idle.native_session_id is None
    assert "idle_timeout" in st_idle.rotation_reason


def test_parse_cli_json_output_formats() -> None:
    # 1. Cursor / Claude single JSON output
    cursor_raw = json.dumps({"type": "result", "result": "Root cause is DB timeout", "session_id": "cur_999"})
    ans1, sid1 = parse_cli_json_output(cursor_raw)
    assert ans1 == "Root cause is DB timeout"
    assert sid1 == "cur_999"

    # 2. OpenCode JSONL streaming event output
    opencode_raw = "\n".join(
        [
            json.dumps({"type": "step_start", "sessionID": "ses_open_42"}),
            json.dumps({"type": "text", "sessionID": "ses_open_42", "part": {"text": "Checked payment.log. "}}),
            json.dumps({"type": "text", "sessionID": "ses_open_42", "part": {"text": "Found 502 on line 88."}}),
        ]
    )
    ans2, sid2 = parse_cli_json_output(opencode_raw)
    assert "Checked payment.log. Found 502 on line 88." == ans2
    assert sid2 == "ses_open_42"


def test_multi_file_action_logs_and_source_code_tools(tmp_path: Path) -> None:
    svc_dir = tmp_path / "order-service"
    logs_dir = svc_dir / "logs"
    logs_dir.mkdir(parents=True)

    # Create multiple action log files
    (logs_dir / "payment.log").write_text("INFO Payment started\nERROR Payment gateway timeout order=77\n")
    (logs_dir / "webhook.log").write_text("INFO Webhook received event=invoice.paid\n")
    (logs_dir / "auth.log").write_text("WARN Invalid JWT token from 10.0.0.4\n")

    # Create source code file and a secret .env file
    (svc_dir / "handler.py").write_text(
        "def process_payment(order_id):\n"
        "    if not order_id:\n"
        "        raise ValueError('Missing order_id')\n"
        "    return True\n"
    )
    (svc_dir / ".env").write_text("SECRET_KEY=do_not_read_me\n")

    cfg = load_config("config.yaml")
    cfg.services["test-svc"] = ServiceDefinition(
        name="test-svc",
        runtime="python",
        manager="pm2",
        working_dir=str(svc_dir),
        logs_dir=str(logs_dir),
        log_files={
            "payment": str(logs_dir / "payment*.log"),
            "webhook": str(logs_dir / "webhook*.log"),
            "auth": str(logs_dir / "auth*.log"),
        },
    )
    tk = OpsToolkit(cfg)

    # 1. List all log files
    listed = tk.list_service_log_files("test-svc")
    assert listed["ok"] is True
    assert listed["total_files_found"] == 3

    # 2. Read specific action log (`payment`)
    pay_logs = tk.get_service_logs("test-svc", category_or_file="payment", filter_keyword="ERROR")
    assert pay_logs["ok"] is True
    assert pay_logs["matched_files_count"] == 1
    only_log = next(iter(pay_logs["logs"].values()))
    assert "Payment gateway timeout order=77" in only_log["tail"]

    # 3. Search source code
    code_search = tk.search_service_code("test-svc", query="process_payment")
    assert code_search["ok"] is True
    assert len(code_search["matches"]) == 1
    assert code_search["matches"][0]["file"] == "handler.py"

    # 4. Read source code file with line numbers
    code_read = tk.read_service_code_file("test-svc", relative_path="handler.py", start_line=1, end_line=4)
    assert code_read["ok"] is True
    assert "process_payment" in code_read["content"]

    # 5. Ensure reading `.env` or traversing outside `working_dir` is blocked
    blocked_env = tk.read_service_code_file("test-svc", relative_path=".env")
    assert blocked_env["ok"] is False
    blocked_traversal = tk.read_service_code_file("test-svc", relative_path="../../etc/passwd")
    assert blocked_traversal["ok"] is False


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

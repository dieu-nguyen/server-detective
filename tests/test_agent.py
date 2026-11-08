"""Unit and integration tests for Windows Server Multi-Service Telegram Debug Agent."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent.config import ServiceDefinition, load_config, save_engine_mode_to_config
from agent.engines.cli_subscription import (
    CLISubscriptionEngine,
    compose_cursor_turn_prompt,
    format_cli_stream_line,
    resolve_cursor_bin_parts,
)
from agent.telegram_bot import (
    TELEGRAM_MAX_CHUNKS,
    TELEGRAM_MAX_LEN,
    limit_telegram_chunks,
    split_telegram_message,
    telegram_html,
)
from agent.engines.router import EngineRouter
from agent.engines.sdk_apikey import SDKApiKeyEngine
from agent.security import redact_secrets, validate_readonly_sql
from agent.session_manager import SessionManager, init_sqlite_db, parse_cli_json_output
from ops_toolkit import OpsToolkit


def test_config_loads_and_validates() -> None:
    cfg = load_config("config.example.yaml")
    assert cfg.server_name == "prod-win-server-01"
    assert cfg.engine.mode == "cli_subscription"
    assert cfg.engine.cli_subscription.provider == "cursor"
    assert cfg.engine.sdk_apikey.harness == "opencode"
    assert cfg.engine.cli_restrictions.enabled is False
    assert cfg.engine.session.state_db_file == "./agent_state.db"
    assert cfg.engine.session.idle_timeout_minutes == 30
    assert cfg.engine.session.max_turns_per_session == 15
    assert cfg.engine.cli_subscription.turn_log_file == "./logs/agent-turns.log"
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
    cfg = load_config("config.example.yaml")
    cfg.engine.session.state_db_file = str(tmp_path / "state.db")
    tk = OpsToolkit(cfg)
    cli_engine = CLISubscriptionEngine(cfg, tk)
    sdk_engine = SDKApiKeyEngine(cfg, tk)

    # 1. Cursor CLI fresh vs resumed session
    cmd_cursor_new = cli_engine.build_command("check order-api-py", resume_session_id=None)
    assert "--output-format" in cmd_cursor_new
    assert "stream-json" in cmd_cursor_new
    assert "--trust" in cmd_cursor_new
    assert "--resume" not in cmd_cursor_new
    assert cmd_cursor_new[-1] == "check order-api-py"
    assert cmd_cursor_new.index("--trust") < cmd_cursor_new.index("check order-api-py")
    assert cmd_cursor_new.index("--force") < cmd_cursor_new.index("check order-api-py")
    assert cmd_cursor_new.index("--approve-mcps") < cmd_cursor_new.index("check order-api-py")
    assert cmd_cursor_new.index("-p") < cmd_cursor_new.index("check order-api-py")

    cfg.engine.cli_subscription.trust_workspace = False
    cmd_cursor_untrusted = cli_engine.build_command("check order-api-py", resume_session_id=None)
    assert "--trust" not in cmd_cursor_untrusted
    cfg.engine.cli_subscription.trust_workspace = True

    cfg.engine.cli_subscription.force_allow_commands = False
    cmd_cursor_no_force = cli_engine.build_command("check order-api-py", resume_session_id=None)
    assert "--force" not in cmd_cursor_no_force
    cfg.engine.cli_subscription.force_allow_commands = True

    cmd_cursor_resume = cli_engine.build_command("check payment log", resume_session_id="chat_abc123")
    assert "--resume" in cmd_cursor_resume
    assert "chat_abc123" in cmd_cursor_resume
    assert cmd_cursor_resume.index("--resume") < cmd_cursor_resume.index("check payment log")
    assert cmd_cursor_resume[-1] == "check payment log"

    fake_root = tmp_path / "cursor-agent"
    old_ver = fake_root / "versions" / "2026.01.01-aaa"
    new_ver = fake_root / "versions" / "2026.11.08-bbb"
    old_ver.mkdir(parents=True)
    new_ver.mkdir(parents=True)
    (fake_root / "agent.cmd").write_text("@echo off\n", encoding="utf-8")
    for folder in (old_ver, new_ver):
        (folder / "node.exe").write_bytes(b"")
        (folder / "index.js").write_text("console.log('ok')\n", encoding="utf-8")
    resolved = resolve_cursor_bin_parts(str(fake_root / "agent.cmd"))
    assert resolved[0] == str(new_ver / "node.exe")
    assert resolved[1] == str(new_ver / "index.js")
    cfg.engine.cli_subscription.binaries["cursor"] = str(fake_root / "agent.cmd")
    cmd_unwrapped = cli_engine.build_command("pong", resume_session_id=None)
    assert cmd_unwrapped[0] == str(new_ver / "node.exe")
    assert cmd_unwrapped[1] == str(new_ver / "index.js")
    assert cmd_unwrapped[-1] == "pong"

    first = compose_cursor_turn_prompt(
        "answer with pong please",
        is_resume=False,
        requester="NamDCbi",
        system_prompt="You are the ops agent.",
        history_block="",
    )
    assert first.startswith("answer with pong please\n")
    follow = compose_cursor_turn_prompt(
        "look at\nudemy-run-tool",
        is_resume=True,
        requester="NamDCbi",
    )
    assert follow.startswith("look at udemy-run-tool\n")
    assert "\nlook at" not in follow

    # 2. OpenCode CLI (`sdk_apikey` mode) fresh vs resumed session
    cmd_oc_new = sdk_engine.build_opencode_command("inspect logs", resume_session_id=None)
    assert cmd_oc_new[:2] == ["opencode", "run"]
    assert "--format" in cmd_oc_new
    assert "--session" not in cmd_oc_new

    cmd_oc_resume = sdk_engine.build_opencode_command("follow up", resume_session_id="ses_xyz789")
    assert "--session" in cmd_oc_resume
    assert "ses_xyz789" in cmd_oc_resume

    # 3. Verify optional hard CLI restrictions (disabled by default, enabled when flipped)
    cfg.engine.cli_restrictions.enabled = True
    oc_perms = sdk_engine.build_opencode_permissions_json()
    assert oc_perms["permission"]["edit"] == "deny"
    assert oc_perms["permission"]["bash"]["*"] == "deny"
    assert oc_perms["permission"]["bash"]["python *ops_toolkit.py*"] == "allow"

    cursor_perms = cli_engine.build_cursor_permissions_dict()
    assert "Write(**)" in cursor_perms["permissions"]["deny"]
    assert "Shell(python *ops_toolkit.py*)" in cursor_perms["permissions"]["allow"]

    cfg.engine.cli_subscription.provider = "claude"
    cmd_claude_restricted = cli_engine.build_command("check status")
    assert "--disallowedTools" in cmd_claude_restricted
    assert "--allowedTools" in cmd_claude_restricted


def test_sqlite_session_lifecycle_cross_tool_porting_and_session_reuse(tmp_path: Path) -> None:
    db_file = tmp_path / "agent_state.db"
    mgr = SessionManager(db_path=db_file, idle_timeout_minutes=30, max_turns_per_session=3)

    # 1. Turn 1 in Cursor CLI: creates logical session `s_...` and records native ID `cur_001`
    st1 = mgr.resolve_session("-100123:main", "cli_subscription:cursor")
    assert st1.is_new_session is True
    assert st1.native_session_id is None
    st1_saved = mgr.record_turn(st1, "Why is order-api failing?", "Found DB timeout.", "cur_001")
    first_logical_sid = st1_saved.session_id
    assert st1_saved.turn_count == 1
    assert st1_saved.native_session_id == "cur_001"

    # 2. Turn 2 in Cursor CLI: resumes `cur_001` natively
    st2 = mgr.resolve_session("-100123:main", "cli_subscription:cursor")
    assert st2.session_id == first_logical_sid
    assert st2.is_new_session is False
    assert st2.native_session_id == "cur_001"
    mgr.record_turn(st2, "Check payment.log", "Payment log shows 502.", "cur_001")

    # 3. Cross-Tool Porting: User switches to OpenCode API (`sdk_apikey:opencode:anthropic`) on Turn 3!
    #    The logical session is the SAME, `is_ported_across_tools` is True, and prior transcript is available!
    st_port = mgr.resolve_session("-100123:main", "sdk_apikey:opencode:anthropic")
    assert st_port.session_id == first_logical_sid
    assert st_port.is_ported_across_tools is True
    assert st_port.native_session_id is None  # OpenCode doesn't have a native ID yet on this session
    assert len(st_port.transcript_messages) == 4  # 2 prior user+assistant turns ready to seed OpenCode!
    st_port_saved = mgr.record_turn(st_port, "Trace line 42 in handler.py", "Bug is null check.", "ses_opencode_99")
    assert st_port_saved.all_native_ids["cli_subscription:cursor"] == "cur_001"
    assert st_port_saved.all_native_ids["sdk_apikey:opencode:anthropic"] == "ses_opencode_99"

    # 4. Turn 4 exceeds max_turns_per_session (3) -> Auto-rotates to a brand-new logical session!
    st4 = mgr.resolve_session("-100123:main", "sdk_apikey:opencode:anthropic")
    assert st4.is_new_session is True
    assert st4.session_id != first_logical_sid
    assert "max_turns_reached" in st4.rotation_reason
    mgr.record_turn(st4, "New unrelated question", "Answer", "ses_opencode_100")

    # 5. Reuse Old Session via `/session <id>` (works with logical ID `first_logical_sid` OR native ID `cur_001`!)
    reused = mgr.reuse_old_session("-100123:main", "cur_001", current_provider_key="sdk_apikey:opencode:anthropic")
    assert reused is not None
    assert reused.session_id == first_logical_sid
    assert reused.turn_count == 0  # Reset to 0!
    assert reused.total_turns_ever == 3  # Historical turns preserved!
    assert reused.native_session_id == "ses_opencode_99"  # Already linked to OpenCode too!

    # 6. Test idle timeout auto-rotation in SQLite
    with init_sqlite_db(db_file) as conn:
        conn.execute(
            "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
            (time.time() - (45 * 60), first_logical_sid),
        )
        conn.commit()

    st_idle = mgr.resolve_session("-100123:main", "cli_subscription:cursor")
    assert st_idle.is_new_session is True
    assert st_idle.session_id != first_logical_sid
    assert "idle_timeout" in st_idle.rotation_reason


def test_parse_cli_json_output_formats() -> None:
    cursor_raw = json.dumps({"type": "result", "result": "Root cause is DB timeout", "session_id": "cur_999"})
    ans1, sid1 = parse_cli_json_output(cursor_raw)
    assert ans1 == "Root cause is DB timeout"
    assert sid1 == "cur_999"

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


def test_telegram_chunks_split_and_cap_at_three() -> None:
    short = split_telegram_message("hello")
    assert short == ["hello"]
    long_text = ("paragraph\n" * 2000)
    chunks = split_telegram_message(long_text)
    assert len(chunks) > TELEGRAM_MAX_CHUNKS
    assert all(len(c) <= TELEGRAM_MAX_LEN for c in chunks)
    limited = limit_telegram_chunks(chunks)
    assert len(limited) == TELEGRAM_MAX_CHUNKS
    assert "Truncated to 3 Telegram messages" in limited[-1]
    assert all(len(c) <= TELEGRAM_MAX_LEN for c in limited)


def test_telegram_html_converts_markdown_asterisks() -> None:
    header = "🤖 **[cli_subscription • cursor]**\nSession `s_410f3fbb`\n\npong"
    rendered = telegram_html(header)
    assert "**" not in rendered
    assert "<b>[cli_subscription • cursor]</b>" in rendered
    assert "<code>s_410f3fbb</code>" in rendered
    assert "pong" in rendered
    assert telegram_html("a < b") == "a &lt; b"


def test_format_cli_stream_line_assistant_tools_and_thinking() -> None:
    assert format_cli_stream_line("") is None
    assert format_cli_stream_line(
        json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "huge"}]}})
    ) is None

    thinking = format_cli_stream_line(json.dumps({"type": "thinking", "text": "Need to check payment.log"}))
    assert thinking == "[thinking] Need to check payment.log"

    assistant = format_cli_stream_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "I'll inspect logs"}]},
            }
        )
    )
    assert assistant == "[assistant] I'll inspect logs"
    assert format_cli_stream_line(json.dumps({"type": "assistant", "model_call_id": "dup", "message": {"content": [{"type": "text", "text": "x"}]}})) is None

    started = format_cli_stream_line(
        json.dumps(
            {
                "type": "tool_call",
                "subtype": "started",
                "tool_call": {"readToolCall": {"args": {"path": "payment.log"}}},
            }
        )
    )
    assert started == "[tool:started] read payment.log"


def test_multi_file_action_logs_source_code_and_sqlite_actions(tmp_path: Path) -> None:
    svc_dir = tmp_path / "order-service"
    logs_dir = svc_dir / "logs"
    logs_dir.mkdir(parents=True)

    (logs_dir / "payment.log").write_text("INFO Payment started\nERROR Payment gateway timeout order=77\n")
    (logs_dir / "webhook.log").write_text("INFO Webhook received event=invoice.paid\n")
    (logs_dir / "auth.log").write_text("WARN Invalid JWT token from 10.0.0.4\n")

    (svc_dir / "handler.py").write_text(
        "def process_payment(order_id):\n"
        "    if not order_id:\n"
        "        raise ValueError('Missing order_id')\n"
        "    return True\n"
    )
    (svc_dir / ".env").write_text("SECRET_KEY=do_not_read_me\n")

    cfg = load_config("config.example.yaml")
    cfg.engine.session.state_db_file = str(tmp_path / "state.db")
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

    listed = tk.list_service_log_files("test-svc")
    assert listed["ok"] is True
    assert listed["total_files_found"] == 3

    pay_logs = tk.get_service_logs("test-svc", category_or_file="payment", filter_keyword="ERROR")
    assert pay_logs["ok"] is True
    assert pay_logs["matched_files_count"] == 1
    only_log = next(iter(pay_logs["logs"].values()))
    assert "Payment gateway timeout order=77" in only_log["tail"]

    code_search = tk.search_service_code("test-svc", query="process_payment")
    assert code_search["ok"] is True
    assert len(code_search["matches"]) == 1

    code_read = tk.read_service_code_file("test-svc", relative_path="handler.py", start_line=1, end_line=4)
    assert code_read["ok"] is True
    assert "process_payment" in code_read["content"]

    blocked_env = tk.read_service_code_file("test-svc", relative_path=".env")
    assert blocked_env["ok"] is False

    # Verify SQLite PendingActionStore
    proposed = tk.propose_action("pm2_restart", "order-api-py", "Memory spike", 333333333, "dev1")
    assert proposed["ok"] is True
    aid = proposed["action"]["action_id"]
    assert len(tk.action_store.list_pending()) == 1
    popped = tk.action_store.pop_action(aid)
    assert popped is not None
    assert len(tk.action_store.list_pending()) == 0


def test_router_mode_switch_persistence(tmp_path: Path) -> None:
    temp_cfg = tmp_path / "config.yaml"
    temp_cfg.write_text(Path("config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    cfg = load_config(temp_cfg)
    cfg.engine.session.state_db_file = str(tmp_path / "state.db")
    router = EngineRouter(cfg)
    assert router.current_mode == "cli_subscription"

    router.switch_mode("sdk", persist=True)
    assert router.current_mode == "sdk_apikey"

    reloaded = load_config(temp_cfg)
    assert reloaded.engine.mode == "sdk_apikey"
    save_engine_mode_to_config(temp_cfg, "cli_subscription")

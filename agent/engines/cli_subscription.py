"""
Mode 1: CLI Subscription Engine (`cli_subscription`).

Uses your existing monthly subscription (Cursor default, or Claude Code / OpenAI Codex / Gemini CLI).
Session-aware (`--resume <id>`) and supports optional hard CLI tool/command restrictions
(`engine.cli_restrictions.enabled: true` in `config.yaml`, disabled by default).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from agent.config import AppConfig
from agent.engines.base import AgentTurnResult, build_system_prompt
from agent.security import redact_secrets
from agent.session_manager import parse_cli_json_output
from ops_toolkit import OpsToolkit


def _assistant_text_from_event(obj: Dict[str, Any]) -> str:
    message = obj.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if content is None:
        content = obj.get("content") or obj.get("text") or obj.get("delta")
    parts: List[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("thinking") or ""))
            else:
                parts.append(str(item))
    elif isinstance(content, str):
        parts.append(content)
    return "".join(parts)


def _tool_call_summary(obj: Dict[str, Any]) -> str:
    tool_call = obj.get("tool_call") or {}
    if not isinstance(tool_call, dict):
        return str(tool_call)[:300]
    chunks: List[str] = []
    for key, val in tool_call.items():
        name = str(key).replace("ToolCall", "")
        if isinstance(val, dict):
            args = val.get("args") if isinstance(val.get("args"), dict) else {}
            detail = (
                args.get("path")
                or args.get("command")
                or args.get("query")
                or args.get("pattern")
                or ""
            )
            if key == "function":
                name = str(val.get("name") or name)
                detail = str(val.get("arguments") or detail)[:200]
            chunks.append(f"{name} {detail}".strip())
        else:
            chunks.append(name)
    return "; ".join(chunks) or "tool"


def format_cli_stream_line(raw_line: str) -> Optional[str]:
    """Turn one Cursor CLI stream-json line into a human-readable log line."""
    line = (raw_line or "").strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except Exception:
        return f"[stdout] {redact_secrets(line)[:800]}"
    if not isinstance(obj, dict):
        return None

    event_type = str(obj.get("type") or "")
    if event_type == "system":
        return (
            f"[system] model={obj.get('model') or ''} session={obj.get('session_id') or ''} "
            f"cwd={obj.get('cwd') or ''}"
        ).strip()
    if event_type == "user":
        return None
    if event_type in ("thinking", "reasoning"):
        text = redact_secrets(_assistant_text_from_event(obj) or str(obj.get("text") or "")).strip()
        return f"[thinking] {text}" if text else None
    if event_type == "assistant":
        if obj.get("model_call_id"):
            return None
        text = redact_secrets(_assistant_text_from_event(obj)).strip()
        return f"[assistant] {text}" if text else None
    if event_type == "tool_call":
        subtype = obj.get("subtype") or "event"
        return f"[tool:{subtype}] {redact_secrets(_tool_call_summary(obj))}"
    if event_type == "result":
        return (
            f"[result] duration_ms={obj.get('duration_ms')} session={obj.get('session_id') or ''} "
            f"error={obj.get('is_error')}"
        ).strip()
    return f"[{event_type or 'event'}] {redact_secrets(line)[:400]}"


class _TurnLog:
    """Append-only, line-flushed turn log so `Get-Content -Wait` shows live output."""

    def __init__(self, path: str) -> None:
        self.path = Path(path) if path.strip() else None
        self._fh = None

    def __enter__(self) -> "_TurnLog":
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def write(self, line: str) -> None:
        text = (line or "").rstrip()
        if not text:
            return
        logger.info("CLI turn %s", text[:800])
        if self._fh:
            self._fh.write(text + "\n")
            self._fh.flush()


class CLISubscriptionEngine:
    """Session-aware headless CLI bridge supporting Cursor (`agent` / `cursor-agent`), Claude (`claude`), Codex (`codex`), and Gemini (`gemini`)."""

    def __init__(self, config: AppConfig, toolkit: OpsToolkit) -> None:
        self.config = config
        self.toolkit = toolkit
        self._semaphore = asyncio.Semaphore(
            max(1, config.engine.cli_subscription.max_concurrent_processes)
        )

    def build_cursor_permissions_dict(self) -> Dict[str, Any]:
        """
        Build `.cursor/cli.json` permissions payload when `cli_restrictions.enabled` is True.
        """
        rest = self.config.engine.cli_restrictions
        allow_list = ["Read(**)"]
        if rest.allow_file_edits:
            allow_list.append("Write(**)")
        for pattern in rest.allowed_shell_commands:
            allow_list.append(f"Shell({pattern})")

        deny_list = []
        if not rest.allow_file_edits:
            deny_list.append("Write(**)")
        for pattern in rest.denied_shell_commands:
            deny_list.append(f"Shell({pattern})")

        return {"permissions": {"allow": allow_list, "deny": deny_list}}

    def build_command(
        self,
        prompt: str,
        resume_session_id: Optional[str] = None,
    ) -> List[str]:
        """Build the exact argv list for the configured CLI subscription provider, including `--resume` and optional restrictions."""
        cli_cfg = self.config.engine.cli_subscription
        rest_cfg = self.config.engine.cli_restrictions
        provider = cli_cfg.provider.lower()
        model = cli_cfg.model.strip()

        if provider == "custom" and cli_cfg.custom_command_template:
            return [
                part.format(prompt=prompt, model=model, session_id=resume_session_id or "")
                for part in cli_cfg.custom_command_template
            ]

        raw_bin = cli_cfg.binaries.get(provider, provider)
        bin_parts = shlex.split(raw_bin)

        if provider == "cursor":
            binary_name = bin_parts[0]
            if len(bin_parts) == 1 and not shutil.which(binary_name):
                for alt in ("cursor-agent", "agent"):
                    if shutil.which(alt):
                        bin_parts = [alt]
                        break
            # `-p` / `--print` is a boolean flag; the prompt is a trailing argument.
            # Flags after the prompt are ignored by Cursor CLI, so `--trust` never
            # applied and PM2 still hit "Workspace Trust Required". On Windows
            # `agent.cmd` they can also be truncated past cmd.exe's 8191-char limit.
            cmd = [*bin_parts, "-p", "--output-format", "stream-json"]
            if cli_cfg.trust_workspace:
                cmd.append("--trust")
            if cli_cfg.force_allow_commands:
                cmd.append("--force")
            if cli_cfg.approve_mcps:
                cmd.append("--approve-mcps")
            if resume_session_id:
                cmd.extend(["--resume", resume_session_id])
            if model:
                cmd.extend(["--model", model])
            cmd.append(prompt)
            return cmd

        if provider == "claude":
            cmd = [*bin_parts, "-p", prompt, "--output-format", "json"]
            if resume_session_id:
                cmd.extend(["--resume", resume_session_id])
            if model:
                cmd.extend(["--model", model])
            if rest_cfg.enabled:
                if not rest_cfg.allow_file_edits:
                    cmd.extend(["--disallowedTools", "Edit", "Write", "NotebookEdit"])
                allowed_tools = ["Read", "Grep", "Glob"]
                for pat in rest_cfg.allowed_shell_commands:
                    allowed_tools.append(f"Bash({pat})")
                cmd.extend(["--allowedTools", *allowed_tools])
            return cmd

        if provider == "codex":
            cmd = [*bin_parts, "exec", prompt]
            if rest_cfg.enabled and not rest_cfg.allow_file_edits:
                cmd.extend(["--sandbox", "read-only"])
            if model:
                cmd.extend(["--model", model])
            return cmd

        if provider == "gemini":
            cmd = [*bin_parts, "-p", prompt]
            if model:
                cmd.extend(["--model", model])
            return cmd

        raise ValueError(
            f"Unsupported cli_subscription.provider '{provider}'. Choose from: cursor, claude, codex, gemini, custom."
        )

    def _prepare_cursor_cli_restrictions(self, cwd: str) -> None:
        """If `cli_restrictions.enabled` is True and provider is `cursor`, write `.cursor/cli.json` in `cwd`."""
        rest_cfg = self.config.engine.cli_restrictions
        if not rest_cfg.enabled or self.config.engine.cli_subscription.provider.lower() != "cursor":
            return
        try:
            cursor_dir = Path(cwd) / ".cursor"
            cursor_dir.mkdir(parents=True, exist_ok=True)
            cli_json_path = cursor_dir / "cli.json"
            cli_json_path.write_text(
                json.dumps(self.build_cursor_permissions_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    async def run_turn(
        self,
        user_message: str,
        history: List[Dict[str, str]],
        resume_session_id: Optional[str] = None,
        requester_user_id: Optional[int] = None,
        requester_username: str = "",
    ) -> AgentTurnResult:
        cli_cfg = self.config.engine.cli_subscription

        if resume_session_id:
            composed_prompt = (
                f"[Follow-up from @{requester_username or requester_user_id or 'teammate'}]:\n"
                f"{user_message}"
            )
        else:
            sys_prompt = build_system_prompt(self.config, include_cli_instructions=True)
            history_lines = []
            for msg in history[-6:]:
                role = "User" if msg["role"] == "user" else "Agent"
                history_lines.append(f"{role}: {msg['content']}")
            history_block = "\n".join(history_lines)

            composed_prompt = (
                f"{sys_prompt}\n\n"
                f"--- Recent Conversation Context ---\n{history_block or '(New debugging session)'}\n\n"
                f"--- Request from @{requester_username or requester_user_id or 'teammate'} ---\n"
                f"{user_message}"
            )

        before_ids = {a["action_id"] for a in self.toolkit.action_store.list_pending()}
        argv = self.build_command(composed_prompt, resume_session_id=resume_session_id)

        cwd = cli_cfg.default_working_dir
        if not cwd or not Path(cwd).exists():
            cwd = str(Path.cwd())

        self._prepare_cursor_cli_restrictions(cwd)

        public_argv = argv[:-1] if argv else argv
        logger.info(
            "Starting CLI subscription provider=%s cwd=%s timeout=%ss argv=%s prompt_chars=%s",
            cli_cfg.provider,
            cwd,
            cli_cfg.timeout_seconds,
            public_argv,
            len(argv[-1]) if argv else 0,
        )

        stdout_parts: List[bytes] = []
        stderr_parts: List[bytes] = []

        async def _pump_stdout(proc: asyncio.subprocess.Process, turn_log: _TurnLog) -> None:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                stdout_parts.append(line)
                formatted = format_cli_stream_line(line.decode("utf-8", errors="replace"))
                if formatted:
                    turn_log.write(formatted)

        async def _pump_stderr(proc: asyncio.subprocess.Process, turn_log: _TurnLog) -> None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                stderr_parts.append(line)
                text = redact_secrets(line.decode("utf-8", errors="replace")).rstrip()
                if text:
                    turn_log.write(f"[stderr] {text}")

        with _TurnLog(cli_cfg.turn_log_file) as turn_log:
            turn_log.write(
                f"======== TURN start provider={cli_cfg.provider} cwd={cwd} "
                f"user=@{requester_username or requester_user_id or 'unknown'} "
                f"question={redact_secrets(user_message)[:400]} ========"
            )
            async with self._semaphore:
                proc = None
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=cwd,
                        env=os.environ.copy(),
                    )
                    logger.info("CLI process started pid=%s binary=%s", proc.pid, argv[0])
                    turn_log.write(f"[proc] pid={proc.pid} binary={argv[0]}")
                    await asyncio.wait_for(
                        asyncio.gather(
                            _pump_stdout(proc, turn_log),
                            _pump_stderr(proc, turn_log),
                            proc.wait(),
                        ),
                        timeout=cli_cfg.timeout_seconds,
                    )
                except FileNotFoundError:
                    raise RuntimeError(
                        f"CLI binary `{argv[0]}` was not found on PATH. "
                        f"Verify that `{cli_cfg.provider}` CLI is installed and logged in on the server, "
                        f"or switch `engine.mode` to `sdk_apikey` in `config.yaml`."
                    )
                except asyncio.TimeoutError:
                    if proc and proc.returncode is None:
                        logger.error(
                            "CLI timed out after %ss; killing pid=%s",
                            cli_cfg.timeout_seconds,
                            proc.pid,
                        )
                        turn_log.write(f"[timeout] killing pid={proc.pid} after {cli_cfg.timeout_seconds}s")
                        proc.kill()
                        try:
                            await proc.wait()
                        except Exception:
                            pass
                    raise RuntimeError(
                        f"CLI Subscription engine (`{cli_cfg.provider}`) timed out after {cli_cfg.timeout_seconds}s. "
                        f"Watch `{cli_cfg.turn_log_file or 'pm2 logs'}` while it runs."
                    )

            turn_log.write(f"======== TURN end exit={proc.returncode if proc else 'n/a'} ========")

        stdout_bytes = b"".join(stdout_parts)
        stderr_bytes = b"".join(stderr_parts)
        logger.info(
            "CLI finished provider=%s exit=%s stdout_chars=%s stderr_chars=%s log=%s",
            cli_cfg.provider,
            proc.returncode if proc else None,
            len(stdout_bytes),
            len(stderr_bytes),
            cli_cfg.turn_log_file,
        )

        raw_stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        raw_stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        parsed_text, extracted_session_id = parse_cli_json_output(raw_stdout)
        answer = redact_secrets(parsed_text or raw_stderr or "Investigation completed with no output.")
        if proc.returncode != 0:
            err = redact_secrets(raw_stderr)[:1500] or "Unknown CLI error"
            if not raw_stdout:
                raise RuntimeError(
                    f"CLI Subscription engine (`{cli_cfg.provider}`) exited with code {proc.returncode}: {err}"
                )
            answer = (
                f"⚠️ Cursor CLI exited with code {proc.returncode}.\n{err}\n\n{answer}"
            )

        after_actions = self.toolkit.action_store.list_pending()
        new_actions = [a for a in after_actions if a["action_id"] not in before_ids]
        for action in new_actions:
            if not action.get("requested_by_user_id") and requester_user_id:
                action["requested_by_user_id"] = requester_user_id
                action["requested_by_username"] = requester_username

        return AgentTurnResult(
            answer=answer,
            engine_used="cli_subscription",
            provider_used=cli_cfg.provider,
            pending_actions=new_actions,
            tool_calls_made=[f"cli:{cli_cfg.provider}"],
            native_session_id=extracted_session_id or resume_session_id,
        )

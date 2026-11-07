"""
Mode 2: SDK / API Key Engine (`sdk_apikey`).

Powered by **OpenCode (`opencode run`)** (default) or **Pi (`pi -p`)** using your API key
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`).

Features:
- Full coding agent harness capabilities (built-in file read, ripgrep, glob, context compaction)
- Access to `ops_toolkit.py` (PM2, ngrok, n8n, multi-file logs, source code, read-only MySQL, gated approvals)
- Native session continuation (`opencode run --session <id>` / `pi --session <id>`)
- Optional hard CLI tool/command restrictions (`engine.cli_restrictions.enabled: true` in `config.yaml`,
  disabled by default): injects `OPENCODE_CONFIG_CONTENT` permission rules (`edit: "deny"`, bash allow/deny patterns)
  for OpenCode or `--tools read,grep,find,ls,bash` for Pi.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.config import AppConfig
from agent.engines.base import AgentTurnResult, build_system_prompt
from agent.security import redact_secrets
from agent.session_manager import parse_cli_json_output
from ops_toolkit import OpsToolkit


class SDKApiKeyEngine:
    """
    API Key Engine powered by OpenCode CLI (`opencode run --format json --session <id>`)
    or Pi CLI (`pi -p --session <id>`).
    """

    def __init__(self, config: AppConfig, toolkit: OpsToolkit) -> None:
        self.config = config
        self.toolkit = toolkit
        self._semaphore = asyncio.Semaphore(
            max(1, config.engine.sdk_apikey.max_concurrent_processes)
        )

    def build_opencode_permissions_json(self) -> Dict[str, Any]:
        """
        Build the `OPENCODE_CONFIG_CONTENT` permission object when `cli_restrictions.enabled` is True.
        Denies file edits (unless `allow_file_edits` is True) and restricts bash commands to `allowed_shell_commands`.
        """
        rest = self.config.engine.cli_restrictions
        bash_rules: Dict[str, str] = {"*": "deny"}
        for pat in rest.allowed_shell_commands:
            bash_rules[pat] = "allow"
        for pat in rest.denied_shell_commands:
            bash_rules[pat] = "deny"

        return {
            "permission": {
                "edit": "allow" if rest.allow_file_edits else "deny",
                "bash": bash_rules,
            }
        }

    def build_opencode_command(
        self,
        prompt: str,
        resume_session_id: Optional[str] = None,
    ) -> List[str]:
        """Build the `opencode run` argv with `--format json`, `--model provider/model`, and `--session <id>`."""
        sdk_cfg = self.config.engine.sdk_apikey
        bin_parts = shlex.split(sdk_cfg.opencode_bin or "opencode")
        cmd = [*bin_parts, "run", prompt, "--format", "json"]
        if resume_session_id:
            cmd.extend(["--session", resume_session_id])
        if sdk_cfg.model:
            model_arg = sdk_cfg.model if "/" in sdk_cfg.model else f"{sdk_cfg.provider}/{sdk_cfg.model}"
            cmd.extend(["--model", model_arg])
        return cmd

    def build_pi_command(
        self,
        prompt: str,
        resume_session_id: Optional[str] = None,
    ) -> List[str]:
        """Build the `pi` coding agent CLI argv (with optional read-only tool restriction)."""
        sdk_cfg = self.config.engine.sdk_apikey
        rest_cfg = self.config.engine.cli_restrictions
        bin_parts = shlex.split(sdk_cfg.pi_bin or "pi")
        cmd = [*bin_parts, "-p", prompt]
        if resume_session_id:
            cmd.extend(["--session", resume_session_id])
        if sdk_cfg.provider:
            cmd.extend(["--provider", sdk_cfg.provider])
        if sdk_cfg.model:
            cmd.extend(["--model", sdk_cfg.model])
        if rest_cfg.enabled and not rest_cfg.allow_file_edits:
            cmd.extend(["--tools", "read,grep,find,ls,bash"])
        return cmd

    async def run_turn(
        self,
        user_message: str,
        history: List[Dict[str, str]],
        resume_session_id: Optional[str] = None,
        requester_user_id: Optional[int] = None,
        requester_username: str = "",
    ) -> AgentTurnResult:
        sdk_cfg = self.config.engine.sdk_apikey
        rest_cfg = self.config.engine.cli_restrictions
        provider = sdk_cfg.provider.lower()
        harness = sdk_cfg.harness.lower()
        if harness not in ("opencode", "pi"):
            harness = "opencode"

        api_key = os.environ.get(sdk_cfg.api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(
                f"Missing API key in environment variable `{sdk_cfg.api_key_env}` for `sdk_apikey` mode "
                f"(harness=`{harness}`, provider=`{provider}`). Set `{sdk_cfg.api_key_env}` in `.env` or switch to `cli_subscription`."
            )

        if resume_session_id:
            composed_prompt = (
                f"[Follow-up from @{requester_username or requester_user_id or 'teammate'}]:\n"
                f"{user_message}"
            )
        else:
            sys_prompt = build_system_prompt(self.config, include_cli_instructions=True)
            history_lines = [
                f"{'User' if m['role'] == 'user' else 'Agent'}: {m['content']}" for m in history[-6:]
            ]
            composed_prompt = (
                f"{sys_prompt}\n\n"
                f"--- Recent Conversation Context ---\n{chr(10).join(history_lines) or '(New debugging session)'}\n\n"
                f"--- Request from @{requester_username or requester_user_id or 'teammate'} ---\n"
                f"{user_message}"
            )

        before_ids = {a["action_id"] for a in self.toolkit.action_store.list_pending()}
        argv = (
            self.build_opencode_command(composed_prompt, resume_session_id=resume_session_id)
            if harness == "opencode"
            else self.build_pi_command(composed_prompt, resume_session_id=resume_session_id)
        )

        cwd = self.config.engine.cli_subscription.default_working_dir
        if not cwd or not Path(cwd).exists():
            cwd = str(Path.cwd())

        env = os.environ.copy()
        if rest_cfg.enabled and harness == "opencode":
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(self.build_opencode_permissions_json())

        async with self._semaphore:
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=sdk_cfg.timeout_seconds,
                )
            except FileNotFoundError:
                raise RuntimeError(
                    f"API Key harness binary `{argv[0]}` was not found on PATH. "
                    f"Install OpenCode (`npm install -g opencode-ai`) on the server or switch `engine.mode` to `cli_subscription`."
                )
            except asyncio.TimeoutError:
                if proc is not None and proc.returncode is None:
                    proc.kill()
                    try:
                        await proc.wait()
                    except Exception:
                        pass
                raise RuntimeError(f"API Key harness (`{harness}`) timed out after {sdk_cfg.timeout_seconds}s.")

        raw_stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        raw_stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        parsed_text, extracted_session_id = parse_cli_json_output(raw_stdout)
        answer = redact_secrets(parsed_text or raw_stderr or "Completed.")
        if proc.returncode != 0:
            err = redact_secrets(raw_stderr)[:1500] or "Unknown error"
            if not raw_stdout:
                raise RuntimeError(
                    f"API Key harness (`{harness}`) exited with code {proc.returncode}: {err}"
                )
            answer = f"⚠️ API Key harness exited with code {proc.returncode}.\n{err}\n\n{answer}"

        after_actions = self.toolkit.action_store.list_pending()
        new_actions = [a for a in after_actions if a["action_id"] not in before_ids]
        for action in new_actions:
            if not action.get("requested_by_user_id") and requester_user_id:
                action["requested_by_user_id"] = requester_user_id
                action["requested_by_username"] = requester_username

        return AgentTurnResult(
            answer=answer,
            engine_used="sdk_apikey",
            provider_used=f"{harness}:{sdk_cfg.provider}/{sdk_cfg.model}",
            pending_actions=new_actions,
            tool_calls_made=[f"harness:{harness}"],
            native_session_id=extracted_session_id or resume_session_id,
        )

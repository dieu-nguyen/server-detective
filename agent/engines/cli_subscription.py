"""
Mode 1: CLI Subscription Engine (`cli_subscription`).

Uses your existing monthly subscription (Cursor default, or Claude Code / OpenAI Codex / Gemini CLI)
by invoking the headless CLI agent with an asyncio concurrency semaphore, injecting the server
context & `ops_toolkit.py` instructions, and collecting any gated actions proposed during the run.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.config import AppConfig
from agent.engines.base import AgentTurnResult, build_system_prompt
from agent.security import redact_secrets
from ops_toolkit import OpsToolkit


class CLISubscriptionEngine:
    """Headless CLI bridge supporting Cursor (`agent` / `cursor-agent`), Claude (`claude`), Codex (`codex`), and Gemini (`gemini`)."""

    def __init__(self, config: AppConfig, toolkit: OpsToolkit) -> None:
        self.config = config
        self.toolkit = toolkit
        self._semaphore = asyncio.Semaphore(
            max(1, config.engine.cli_subscription.max_concurrent_processes)
        )

    def build_command(self, full_prompt: str) -> List[str]:
        """Build the exact argv list for the configured CLI subscription provider."""
        cli_cfg = self.config.engine.cli_subscription
        provider = cli_cfg.provider.lower()
        model = cli_cfg.model.strip()

        if provider == "custom" and cli_cfg.custom_command_template:
            return [part.format(prompt=full_prompt, model=model) for part in cli_cfg.custom_command_template]

        raw_bin = cli_cfg.binaries.get(provider, provider)
        # Support multi-token binaries like `wsl agent` or `wsl cursor-agent`
        bin_parts = shlex.split(raw_bin)

        if provider == "cursor":
            # Cursor CLI (`agent` or `cursor-agent`):
            # Supports `-p` / `--print` for headless execution and `--output-format text`
            binary_name = bin_parts[0]
            if len(bin_parts) == 1 and not shutil.which(binary_name):
                # Fallback between `agent` and `cursor-agent` if one is on PATH
                for alt in ("cursor-agent", "agent"):
                    if shutil.which(alt):
                        bin_parts = [alt]
                        break
            cmd = [*bin_parts, "-p", full_prompt, "--output-format", "text"]
            if model:
                cmd.extend(["--model", model])
            return cmd

        if provider == "claude":
            # Claude Code CLI (`claude -p "<prompt>" --output-format text`)
            cmd = [*bin_parts, "-p", full_prompt, "--output-format", "text"]
            if model:
                cmd.extend(["--model", model])
            return cmd

        if provider == "codex":
            # OpenAI Codex CLI (`codex exec "<prompt>"`)
            cmd = [*bin_parts, "exec", full_prompt]
            if model:
                cmd.extend(["--model", model])
            return cmd

        if provider == "gemini":
            # Google Gemini CLI (`gemini -p "<prompt>"`)
            cmd = [*bin_parts, "-p", full_prompt]
            if model:
                cmd.extend(["--model", model])
            return cmd

        raise ValueError(
            f"Unsupported cli_subscription.provider '{provider}'. Choose from: cursor, claude, codex, gemini, custom."
        )

    async def run_turn(
        self,
        user_message: str,
        history: List[Dict[str, str]],
        requester_user_id: Optional[int] = None,
        requester_username: str = "",
    ) -> AgentTurnResult:
        cli_cfg = self.config.engine.cli_subscription
        sys_prompt = build_system_prompt(self.config, include_cli_instructions=True)

        history_lines = []
        for msg in history[-6:]:
            role = "User" if msg["role"] == "user" else "Agent"
            history_lines.append(f"{role}: {msg['content']}")
        history_block = "\n".join(history_lines)

        composed_prompt = (
            f"{sys_prompt}\n\n"
            f"--- Recent Conversation History ---\n{history_block or '(New conversation)'}\n\n"
            f"--- Current Request from @{requester_username or requester_user_id or 'teammate'} ---\n"
            f"{user_message}"
        )

        # Snapshot existing pending action IDs before running CLI so we can detect newly proposed actions
        before_ids = {a["action_id"] for a in self.toolkit.action_store.list_pending()}

        argv = self.build_command(composed_prompt)
        cwd = cli_cfg.default_working_dir
        if not cwd or not Path(cwd).exists():
            cwd = str(Path.cwd())

        async with self._semaphore:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=os.environ.copy(),
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=cli_cfg.timeout_seconds,
                )
            except FileNotFoundError:
                raise RuntimeError(
                    f"CLI binary `{argv[0]}` was not found on PATH. "
                    f"Verify that `{cli_cfg.provider}` CLI is installed and logged in on the server, "
                    f"or switch `engine.mode` to `sdk_apikey` in `config.yaml`."
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"CLI Subscription engine (`{cli_cfg.provider}`) timed out after {cli_cfg.timeout_seconds}s."
                )

        stdout_text = redact_secrets(stdout_bytes.decode("utf-8", errors="replace").strip())
        stderr_text = redact_secrets(stderr_bytes.decode("utf-8", errors="replace").strip())

        if proc.returncode != 0 and not stdout_text:
            raise RuntimeError(
                f"CLI Subscription engine (`{cli_cfg.provider}`) exited with code {proc.returncode}: "
                f"{stderr_text or 'Unknown CLI error'}"
            )

        answer = stdout_text or stderr_text or "Investigation completed with no output."

        # Detect any new pending actions queued during the CLI turn
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
        )

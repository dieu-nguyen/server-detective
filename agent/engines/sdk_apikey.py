"""
Mode 2: SDK / API Key Engine (`sdk_apikey`).

Supports two execution harnesses powered by your API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, etc.):
1. **OpenCode CLI Harness (`harness: "opencode"`, default) or Pi CLI (`harness: "pi"`):**
   - Runs `opencode run "<prompt>" --format json [--session <session_id>] [--model <provider/model>]`
   - Gives API key mode full built-in coding agent capabilities (file read, ripgrep, glob, context compaction)
     PLUS native `--session <id>` continuation and access to `ops_toolkit.py`.
2. **In-Memory Python SDK Loop (`harness: "python_sdk"` or automatic fallback):**
   - Calls `OpsToolkit` methods (including multi-file logs `list_service_log_files` / `get_service_logs`
     and source code inspection `search_service_code` / `read_service_code_file`) directly in-memory.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.config import AppConfig
from agent.engines.base import AgentTurnResult, build_system_prompt
from agent.security import redact_secrets
from agent.session_manager import parse_cli_json_output
from ops_toolkit import OpsToolkit

# Provider-agnostic tool schemas for the in-memory Python SDK loop
TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "get_system_resources",
        "description": "Inspect Windows Server CPU, RAM, disk space across drives, uptime, and top processes by CPU/Memory.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_registered_services_status",
        "description": "Check status, PM2/Windows Service state, log categories, port listener, and HTTP health check for all services in config.yaml.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_pm2_status",
        "description": "Get structured JSON status (`pm2 jlist`) of all Python and Node.js apps managed by PM2.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_pm2_logs",
        "description": "Read recent PM2 stdout and error log lines for a specific PM2 app.",
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "Name of the PM2 process"},
                "lines": {"type": "integer", "description": "Number of recent lines (default 100)", "default": 100},
                "log_type": {
                    "type": "string",
                    "enum": ["all", "error", "out"],
                    "description": "Which log stream to read",
                },
            },
            "required": ["app_name"],
        },
    },
    {
        "name": "list_service_log_files",
        "description": "List all available log files and action log categories (e.g. payment, webhook, auth, error) for a service, with sizes and timestamps.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service key from config.yaml"},
            },
            "required": ["service_name"],
        },
    },
    {
        "name": "get_service_logs",
        "description": "Read recent lines across a service's multiple action log files. Can filter by action category/filename and keyword.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service key from config.yaml"},
                "category_or_file": {
                    "type": "string",
                    "description": "Optional action log category (e.g. 'payment', 'webhook', 'auth') or filename substring. Leave empty to read across all log files.",
                },
                "lines": {"type": "integer", "description": "Number of tail lines per file (default 100)", "default": 100},
                "filter_keyword": {
                    "type": "string",
                    "description": "Optional substring filter (e.g. 'ERROR', 'Traceback', '502')",
                },
            },
            "required": ["service_name"],
        },
    },
    {
        "name": "search_service_code",
        "description": "Search (grep) across a service's source code files inside its working_dir for an error message, function, class, or route.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service key from config.yaml"},
                "query": {"type": "string", "description": "Substring or symbol to search for in code"},
                "file_pattern": {
                    "type": "string",
                    "description": "Optional filename glob filter (e.g. '*.py', '*.js', '*.ts')",
                },
            },
            "required": ["service_name", "query"],
        },
    },
    {
        "name": "read_service_code_file",
        "description": "Read source code lines with line numbers from a file inside a service's working_dir.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service key from config.yaml"},
                "relative_path": {"type": "string", "description": "Relative file path inside working_dir"},
                "start_line": {"type": "integer", "description": "1-based start line (default 1)", "default": 1},
                "end_line": {"type": "integer", "description": "1-based end line (default 160)", "default": 160},
            },
            "required": ["service_name", "relative_path"],
        },
    },
    {
        "name": "get_windows_event_logs",
        "description": "Query Windows Event Viewer (Application or System log) for recent errors/warnings.",
        "parameters": {
            "type": "object",
            "properties": {
                "log_name": {"type": "string", "description": "Log name, e.g. 'Application' or 'System'"},
                "source_filter": {"type": "string", "description": "Optional provider/source filter"},
                "max_events": {"type": "integer", "description": "Max events to return (default 25)"},
            },
            "required": [],
        },
    },
    {
        "name": "check_ngrok_status",
        "description": "Inspect active ngrok tunnels (`http://127.0.0.1:4040/api/tunnels`) and recent webhook HTTP status codes.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "check_n8n_status",
        "description": "Check n8n healthz endpoint, PM2 state, and recent n8n log lines.",
        "parameters": {
            "type": "object",
            "properties": {
                "log_lines": {"type": "integer", "description": "Number of n8n log lines to tail (default 80)"}
            },
            "required": [],
        },
    },
    {
        "name": "query_mysql_readonly",
        "description": "Execute a strictly READ-ONLY MySQL query (SELECT, SHOW, DESCRIBE, EXPLAIN) with automatic LIMIT and timeout.",
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "Read-only SQL statement (SELECT, SHOW, DESCRIBE, EXPLAIN)"},
                "database": {"type": "string", "description": "Optional database name override"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "propose_action",
        "description": "Queue a state-changing action (PM2 restart/reload, Windows service restart, or MySQL query kill) for Admin approval via Telegram button.",
        "parameters": {
            "type": "object",
            "properties": {
                "action_type": {
                    "type": "string",
                    "enum": ["pm2_restart", "pm2_reload", "windows_service_restart", "mysql_kill_query"],
                },
                "target": {
                    "type": "string",
                    "description": "PM2 app name, Windows service name, or numeric MySQL query ID",
                },
                "reason": {"type": "string", "description": "Reason shown on the Telegram approval card"},
            },
            "required": ["action_type", "target", "reason"],
        },
    },
]

OPENAI_COMPAT_BASE_URLS = {
    "openai": None,
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openrouter": "https://openrouter.ai/api/v1",
}


class SDKApiKeyEngine:
    """
    API Key Engine supporting:
    - `opencode` CLI harness (`opencode run --format json --session <id>`)
    - `pi` CLI harness (`pi -p`)
    - `python_sdk` in-memory async loop (direct or automatic fallback)
    """

    def __init__(self, config: AppConfig, toolkit: OpsToolkit) -> None:
        self.config = config
        self.toolkit = toolkit

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
        """Build the `pi` coding agent CLI argv."""
        sdk_cfg = self.config.engine.sdk_apikey
        bin_parts = shlex.split(sdk_cfg.pi_bin or "pi")
        cmd = [*bin_parts, "-p", prompt]
        if resume_session_id:
            cmd.extend(["--session", resume_session_id])
        if sdk_cfg.provider:
            cmd.extend(["--provider", sdk_cfg.provider])
        if sdk_cfg.model:
            cmd.extend(["--model", sdk_cfg.model])
        return cmd

    def _execute_tool(
        self,
        name: str,
        args: Dict[str, Any],
        requester_user_id: Optional[int],
        requester_username: str,
        new_actions: List[Dict[str, Any]],
    ) -> str:
        """Dispatch a tool call to `OpsToolkit` and return JSON string result."""
        try:
            if name == "get_system_resources":
                res = self.toolkit.get_system_resources()
            elif name == "list_registered_services_status":
                res = self.toolkit.list_registered_services_status()
            elif name == "get_pm2_status":
                res = self.toolkit.get_pm2_status()
            elif name == "get_pm2_logs":
                res = self.toolkit.get_pm2_logs(
                    app_name=str(args.get("app_name", "")),
                    lines=int(args.get("lines", 100)),
                    log_type=str(args.get("log_type", "all")),
                )
            elif name == "list_service_log_files":
                res = self.toolkit.list_service_log_files(service_name=str(args.get("service_name", "")))
            elif name == "get_service_logs":
                res = self.toolkit.get_service_logs(
                    service_name=str(args.get("service_name", "")),
                    lines=int(args.get("lines", 100)),
                    filter_keyword=str(args.get("filter_keyword", "")),
                    category_or_file=str(args.get("category_or_file", "")),
                )
            elif name == "search_service_code":
                res = self.toolkit.search_service_code(
                    service_name=str(args.get("service_name", "")),
                    query=str(args.get("query", "")),
                    file_pattern=str(args.get("file_pattern", "*")),
                )
            elif name == "read_service_code_file":
                res = self.toolkit.read_service_code_file(
                    service_name=str(args.get("service_name", "")),
                    relative_path=str(args.get("relative_path", "")),
                    start_line=int(args.get("start_line", 1)),
                    end_line=int(args.get("end_line", 160)),
                )
            elif name == "get_windows_event_logs":
                res = self.toolkit.get_windows_event_logs(
                    log_name=str(args.get("log_name", "Application")),
                    source_filter=str(args.get("source_filter", "")),
                    max_events=int(args.get("max_events", 25)),
                )
            elif name == "check_ngrok_status":
                res = self.toolkit.check_ngrok_status()
            elif name == "check_n8n_status":
                res = self.toolkit.check_n8n_status(log_lines=int(args.get("log_lines", 80)))
            elif name == "query_mysql_readonly":
                res = self.toolkit.query_mysql_readonly(
                    sql=str(args.get("sql", "")),
                    database=str(args.get("database", "")),
                )
            elif name == "propose_action":
                res = self.toolkit.propose_action(
                    action_type=str(args.get("action_type", "")),
                    target=str(args.get("target", "")),
                    reason=str(args.get("reason", "")),
                    requested_by_user_id=requester_user_id,
                    requested_by_username=requester_username,
                )
                if res.get("ok") and res.get("action"):
                    new_actions.append(res["action"])
            else:
                res = {"ok": False, "error": f"Unknown tool: {name}"}
        except Exception as exc:
            res = {"ok": False, "error": f"Exception running {name}: {exc}"}

        return redact_secrets(json.dumps(res, ensure_ascii=False, default=str))

    async def run_turn(
        self,
        user_message: str,
        history: List[Dict[str, str]],
        resume_session_id: Optional[str] = None,
        requester_user_id: Optional[int] = None,
        requester_username: str = "",
    ) -> AgentTurnResult:
        sdk_cfg = self.config.engine.sdk_apikey
        provider = sdk_cfg.provider.lower()
        harness = sdk_cfg.harness.lower()

        api_key = os.environ.get(sdk_cfg.api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(
                f"Missing API key in environment variable `{sdk_cfg.api_key_env}` for `sdk_apikey` mode "
                f"(harness=`{harness}`, provider=`{provider}`). Set `{sdk_cfg.api_key_env}` in `.env` or switch to `cli_subscription`."
            )

        if harness in ("opencode", "pi"):
            bin_to_check = shlex.split(sdk_cfg.opencode_bin if harness == "opencode" else sdk_cfg.pi_bin)[0]
            if shutil.which(bin_to_check):
                return await self._run_cli_harness_turn(
                    harness=harness,
                    user_message=user_message,
                    history=history,
                    resume_session_id=resume_session_id,
                    requester_user_id=requester_user_id,
                    requester_username=requester_username,
                )
            if not sdk_cfg.auto_fallback_to_python_sdk:
                raise RuntimeError(
                    f"API Key harness `{harness}` binary (`{bin_to_check}`) was not found on PATH. "
                    f"Install it (`npm i -g opencode-ai`) or set `auto_fallback_to_python_sdk: true` in `config.yaml`."
                )

        # In-memory Python SDK loop (either selected directly or via auto_fallback_to_python_sdk)
        if provider == "anthropic":
            return await self._run_anthropic_loop(
                api_key=api_key,
                user_message=user_message,
                history=history,
                requester_user_id=requester_user_id,
                requester_username=requester_username,
            )
        return await self._run_openai_compat_loop(
            api_key=api_key,
            user_message=user_message,
            history=history,
            requester_user_id=requester_user_id,
            requester_username=requester_username,
        )

    async def _run_cli_harness_turn(
        self,
        harness: str,
        user_message: str,
        history: List[Dict[str, str]],
        resume_session_id: Optional[str],
        requester_user_id: Optional[int],
        requester_username: str,
    ) -> AgentTurnResult:
        """Execute `opencode run` or `pi -p` with native session continuation and `ops_toolkit.py` access."""
        sdk_cfg = self.config.engine.sdk_apikey

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
                f"--- Recent Conversation Context ---\n{chr(10).join(history_lines) or '(New session)'}\n\n"
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

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=os.environ.copy(),
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=sdk_cfg.timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"API Key harness (`{harness}`) timed out after {sdk_cfg.timeout_seconds}s.")

        raw_stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        raw_stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0 and not raw_stdout:
            raise RuntimeError(
                f"API Key harness (`{harness}`) exited with code {proc.returncode}: "
                f"{redact_secrets(raw_stderr) or 'Unknown error'}"
            )

        parsed_text, extracted_session_id = parse_cli_json_output(raw_stdout)
        answer = redact_secrets(parsed_text or raw_stderr or "Completed.")

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

    async def _run_anthropic_loop(
        self,
        api_key: str,
        user_message: str,
        history: List[Dict[str, str]],
        requester_user_id: Optional[int],
        requester_username: str,
    ) -> AgentTurnResult:
        import anthropic

        sdk_cfg = self.config.engine.sdk_apikey
        client = anthropic.AsyncAnthropic(api_key=api_key)
        sys_prompt = build_system_prompt(self.config, include_cli_instructions=False)

        anthropic_tools = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in TOOL_DEFINITIONS
        ]

        messages: List[Dict[str, Any]] = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": user_message})

        new_actions: List[Dict[str, Any]] = []
        tools_called: List[str] = []

        for _ in range(sdk_cfg.max_tool_turns):
            resp = await client.messages.create(
                model=sdk_cfg.model,
                max_tokens=2048,
                temperature=sdk_cfg.temperature,
                system=sys_prompt,
                tools=anthropic_tools,
                messages=messages,
            )

            messages.append({"role": "assistant", "content": resp.content})

            tool_use_blocks = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if not tool_use_blocks:
                text_parts = [getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"]
                final_text = redact_secrets("\n".join(p for p in text_parts if p).strip())
                return AgentTurnResult(
                    answer=final_text or "Completed.",
                    engine_used="sdk_apikey",
                    provider_used=f"python_sdk:anthropic/{sdk_cfg.model}",
                    pending_actions=new_actions,
                    tool_calls_made=tools_called,
                )

            tool_results = []
            for block in tool_use_blocks:
                t_name = block.name
                t_args = dict(block.input or {})
                tools_called.append(t_name)
                output_str = await asyncio.to_thread(
                    self._execute_tool,
                    t_name,
                    t_args,
                    requester_user_id,
                    requester_username,
                    new_actions,
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output_str,
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        return AgentTurnResult(
            answer="Reached maximum diagnostic tool turns. Please review the findings above or refine your question.",
            engine_used="sdk_apikey",
            provider_used=f"python_sdk:anthropic/{sdk_cfg.model}",
            pending_actions=new_actions,
            tool_calls_made=tools_called,
        )

    async def _run_openai_compat_loop(
        self,
        api_key: str,
        user_message: str,
        history: List[Dict[str, str]],
        requester_user_id: Optional[int],
        requester_username: str,
    ) -> AgentTurnResult:
        from openai import AsyncOpenAI

        sdk_cfg = self.config.engine.sdk_apikey
        provider = sdk_cfg.provider.lower()
        base_url = sdk_cfg.base_url.strip() or OPENAI_COMPAT_BASE_URLS.get(provider)
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)

        sys_prompt = build_system_prompt(self.config, include_cli_instructions=False)
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in TOOL_DEFINITIONS
        ]

        messages: List[Dict[str, Any]] = [{"role": "system", "content": sys_prompt}]
        for m in history:
            messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": user_message})

        new_actions: List[Dict[str, Any]] = []
        tools_called: List[str] = []

        for _ in range(sdk_cfg.max_tool_turns):
            resp = await client.chat.completions.create(
                model=sdk_cfg.model,
                temperature=sdk_cfg.temperature,
                messages=messages,
                tools=openai_tools,
            )
            choice = resp.choices[0].message
            messages.append(choice)

            if not choice.tool_calls:
                final_text = redact_secrets((choice.content or "").strip())
                return AgentTurnResult(
                    answer=final_text or "Completed.",
                    engine_used="sdk_apikey",
                    provider_used=f"python_sdk:{provider}/{sdk_cfg.model}",
                    pending_actions=new_actions,
                    tool_calls_made=tools_called,
                )

            for tc in choice.tool_calls:
                t_name = tc.function.name
                try:
                    t_args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    t_args = {}
                tools_called.append(t_name)
                output_str = await asyncio.to_thread(
                    self._execute_tool,
                    t_name,
                    t_args,
                    requester_user_id,
                    requester_username,
                    new_actions,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": output_str,
                    }
                )

        return AgentTurnResult(
            answer="Reached maximum diagnostic tool turns. Please review the findings above or refine your question.",
            engine_used="sdk_apikey",
            provider_used=f"python_sdk:{provider}/{sdk_cfg.model}",
            pending_actions=new_actions,
            tool_calls_made=tools_called,
        )

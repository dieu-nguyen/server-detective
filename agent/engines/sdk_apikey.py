"""
Mode 2: SDK API Key Engine (`sdk_apikey`).

Runs an in-memory Python async tool-calling loop directly against an LLM HTTP API
using an API key (`anthropic`, `openai`, `gemini`, `openrouter`, or `custom_openai`),
calling `OpsToolkit` methods directly in-process.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from agent.config import AppConfig
from agent.engines.base import AgentTurnResult, build_system_prompt
from agent.security import redact_secrets
from ops_toolkit import OpsToolkit

# Provider-agnostic tool schemas (converted automatically for Anthropic or OpenAI-compatible APIs)
TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "get_system_resources",
        "description": "Inspect Windows Server CPU, RAM, disk space across drives, uptime, and top processes by CPU/Memory.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_registered_services_status",
        "description": "Check status, PM2/Windows Service state, port listener, and HTTP health check for all services in config.yaml.",
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
        "name": "get_service_logs",
        "description": "Read latest application log files for a registered service in config.yaml, with optional keyword filter.",
        "parameters": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service key from config.yaml"},
                "lines": {"type": "integer", "description": "Number of tail lines (default 100)", "default": 100},
                "filter_keyword": {
                    "type": "string",
                    "description": "Optional substring filter (e.g. 'ERROR', 'Traceback', '502')",
                },
            },
            "required": ["service_name"],
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

# Default OpenAI-compatible base URLs for multi-provider flexibility
OPENAI_COMPAT_BASE_URLS = {
    "openai": None,
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openrouter": "https://openrouter.ai/api/v1",
}


class SDKApiKeyEngine:
    """In-memory Python tool-calling loop supporting Anthropic and OpenAI-compatible providers."""

    def __init__(self, config: AppConfig, toolkit: OpsToolkit) -> None:
        self.config = config
        self.toolkit = toolkit

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
            elif name == "get_service_logs":
                res = self.toolkit.get_service_logs(
                    service_name=str(args.get("service_name", "")),
                    lines=int(args.get("lines", 100)),
                    filter_keyword=str(args.get("filter_keyword", "")),
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
        requester_user_id: Optional[int] = None,
        requester_username: str = "",
    ) -> AgentTurnResult:
        sdk_cfg = self.config.engine.sdk_apikey
        provider = sdk_cfg.provider.lower()
        api_key = os.environ.get(sdk_cfg.api_key_env, "").strip()
        if not api_key:
            raise RuntimeError(
                f"Missing API key in environment variable `{sdk_cfg.api_key_env}` for `sdk_apikey` mode "
                f"(provider=`{provider}`). Set `{sdk_cfg.api_key_env}` in `.env` or switch to `cli_subscription`."
            )

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
                    provider_used=f"anthropic:{sdk_cfg.model}",
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
            provider_used=f"anthropic:{sdk_cfg.model}",
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
                    provider_used=f"{provider}:{sdk_cfg.model}",
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
            provider_used=f"{provider}:{sdk_cfg.model}",
            pending_actions=new_actions,
            tool_calls_made=tools_called,
        )

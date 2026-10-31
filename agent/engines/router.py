"""
Unified Engine Router with Native Session Lifecycle Management.

Routes each Telegram debugging turn to either:
- `cli_subscription` (Cursor CLI default, or Claude / Codex / Gemini CLI)
- `sdk_apikey` (OpenCode CLI harness default, Pi CLI, or in-memory Python SDK loop)

Manages when to resume an existing native session vs. start a fresh session:
- Automatically continues the active native session (`--resume <id>` / `--session <id>`)
  while teammates are actively conversing.
- Automatically rotates to a new session when idle > `idle_timeout_minutes` (30m)
  or when `max_turns_per_session` (15) is reached, or when `/new` is triggered.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.config import AppConfig, save_engine_mode_to_config
from agent.engines.base import AgentTurnResult, ConversationMemory
from agent.engines.cli_subscription import CLISubscriptionEngine
from agent.engines.sdk_apikey import SDKApiKeyEngine
from agent.session_manager import SessionManager
from ops_toolkit import OpsToolkit


class EngineRouter:
    """Routes requests to the active engine mode and coordinates native session continuation."""

    def __init__(self, config: AppConfig, toolkit: Optional[OpsToolkit] = None) -> None:
        self.config = config
        self.toolkit = toolkit or OpsToolkit(config)
        self.memory = ConversationMemory(max_messages=config.engine.max_history_messages)
        self.sessions = SessionManager(
            sessions_file=config.engine.session.sessions_file,
            idle_timeout_minutes=config.engine.session.idle_timeout_minutes,
            max_turns_per_session=config.engine.session.max_turns_per_session,
            enabled=config.engine.session.enabled,
        )
        self.cli_engine = CLISubscriptionEngine(config, self.toolkit)
        self.sdk_engine = SDKApiKeyEngine(config, self.toolkit)

    @property
    def current_mode(self) -> str:
        return self.config.engine.mode

    def _provider_key(self, mode: str) -> str:
        if mode == "cli_subscription":
            return f"cli_subscription:{self.config.engine.cli_subscription.provider}"
        return f"sdk_apikey:{self.config.engine.sdk_apikey.harness}:{self.config.engine.sdk_apikey.provider}"

    def switch_mode(self, new_mode: str, persist: bool = True) -> str:
        normalized = new_mode.strip().lower()
        if normalized in ("cli", "subscription", "cli_subscription"):
            normalized = "cli_subscription"
        elif normalized in ("sdk", "apikey", "api_key", "sdk_apikey", "opencode"):
            normalized = "sdk_apikey"
        else:
            raise ValueError("Mode must be `cli_subscription` (or `cli`) or `sdk_apikey` (or `sdk`).")

        self.config.engine.mode = normalized
        if persist:
            save_engine_mode_to_config(self.config.config_path, normalized)
        return normalized

    def reset_session(self, session_key: str) -> None:
        """Start a brand-new session for `session_key` (clears both native session IDs and memory)."""
        self.memory.clear(session_key)
        self.sessions.reset_chat_sessions(session_key)

    def get_session_status(self, session_key: str) -> Dict[str, Any]:
        return self.sessions.get_chat_info(session_key)

    async def handle_message(
        self,
        session_key: str,
        user_message: str,
        requester_user_id: Optional[int] = None,
        requester_username: str = "",
        override_mode: Optional[str] = None,
    ) -> AgentTurnResult:
        mode = (override_mode or self.current_mode).lower()
        prov_key = self._provider_key(mode)

        # Check whether to resume existing native session or rotate to a new one
        session_state = self.sessions.resolve_session(session_key, prov_key)
        if session_state.is_new_session and session_state.rotation_reason not in ("", "first_turn"):
            # Clear fallback memory when a session auto-rotates due to idle timeout or max turns
            self.memory.clear(session_key)

        history = self.memory.get_messages(session_key)

        if mode == "cli_subscription":
            try:
                result = await self.cli_engine.run_turn(
                    user_message=user_message,
                    history=history,
                    resume_session_id=session_state.native_session_id,
                    requester_user_id=requester_user_id,
                    requester_username=requester_username,
                )
            except Exception as cli_exc:
                if self.config.engine.auto_fallback_to_sdk:
                    sdk_state = self.sessions.resolve_session(session_key, self._provider_key("sdk_apikey"))
                    result = await self.sdk_engine.run_turn(
                        user_message=user_message,
                        history=history,
                        resume_session_id=sdk_state.native_session_id,
                        requester_user_id=requester_user_id,
                        requester_username=requester_username,
                    )
                    result.answer = (
                        f"⚠️ *[Auto-Fallback to SDK API Key mode after CLI error: {cli_exc}]*\n\n"
                        + result.answer
                    )
                    session_state = sdk_state
                else:
                    raise
        else:
            result = await self.sdk_engine.run_turn(
                user_message=user_message,
                history=history,
                resume_session_id=session_state.native_session_id,
                requester_user_id=requester_user_id,
                requester_username=requester_username,
            )

        updated_state = self.sessions.record_turn(session_state, result.native_session_id)
        result.native_session_id = updated_state.native_session_id
        result.session_turn = updated_state.turn_count
        result.is_new_session = session_state.is_new_session
        result.session_rotation_reason = session_state.rotation_reason

        self.memory.add_turn(session_key, user_message, result.answer)
        return result

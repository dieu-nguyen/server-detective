"""
Unified Engine Router with SQLite-Backed Cross-Tool Portable Session Management.

Routes each Telegram debugging turn to either:
- `cli_subscription` (Cursor CLI default, or Claude / Codex / Gemini CLI)
- `sdk_apikey` (OpenCode CLI harness default, Pi CLI, or in-memory Python SDK loop)

Manages:
1. Native CLI session continuation (`--resume <id>` for Cursor/Claude, `--session <id>` for OpenCode/Pi).
2. Automatic session rotation when idle > `idle_timeout_minutes` (30m) or `max_turns_per_session` (15) is reached.
3. **Cross-Tool Session Porting**: If a session started in Cursor CLI is switched to OpenCode API (or vice versa),
   the prior SQLite turn transcript is automatically seeded into the new tool and both native session IDs
   are linked under the same logical `session_id` in `agent_state.db`.
4. **Reusing Old Sessions (`/session <id>`)**: Reactivates any historical session (by logical ID or native tool ID)
   and resets `turn_count = 0` and `last_active_at = now`.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.config import AppConfig, save_engine_mode_to_config
from agent.engines.base import AgentTurnResult
from agent.engines.cli_subscription import CLISubscriptionEngine
from agent.engines.sdk_apikey import SDKApiKeyEngine
from agent.session_manager import ActiveSessionState, SessionManager
from ops_toolkit import OpsToolkit


class EngineRouter:
    """Routes requests to the active engine mode and coordinates SQLite-backed session continuation & cross-tool porting."""

    def __init__(self, config: AppConfig, toolkit: Optional[OpsToolkit] = None) -> None:
        self.config = config
        self.toolkit = toolkit or OpsToolkit(config)
        db_file = config.engine.session.state_db_file or config.engine.session.sessions_file
        self.sessions = SessionManager(
            db_path=db_file,
            idle_timeout_minutes=config.engine.session.idle_timeout_minutes,
            max_turns_per_session=config.engine.session.max_turns_per_session,
            max_history_messages=config.engine.max_history_messages,
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
        """Deactivate the current session for `session_key` so the next message starts fresh (keeps history in SQLite)."""
        self.sessions.reset_chat_sessions(session_key)

    def reuse_session(self, session_key: str, target_session_id: str) -> Optional[ActiveSessionState]:
        """
        Reactivate a historical session (by logical `s_...` ID or native Cursor/OpenCode/Claude session ID)
        and reset its turn count to `0` and idle timer to `0m`.
        """
        prov_key = self._provider_key(self.current_mode)
        return self.sessions.reuse_old_session(
            telegram_key=session_key,
            target_id=target_session_id,
            current_provider_key=prov_key,
        )

    def get_session_status(self, session_key: str, limit: int = 8) -> Dict[str, Any]:
        return self.sessions.get_chat_sessions_overview(session_key, limit=limit)

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

        session_state = self.sessions.resolve_session(session_key, prov_key)
        history = session_state.transcript_messages

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
                        history=sdk_state.transcript_messages,
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

        updated_state = self.sessions.record_turn(
            state=session_state,
            user_message=user_message,
            assistant_answer=result.answer,
            native_session_id=result.native_session_id,
        )
        result.logical_session_id = updated_state.session_id
        result.native_session_id = updated_state.native_session_id
        result.all_native_ids = updated_state.all_native_ids
        result.session_turn = updated_state.turn_count
        result.is_new_session = session_state.is_new_session
        result.is_ported_across_tools = session_state.is_ported_across_tools
        result.session_rotation_reason = session_state.rotation_reason
        return result

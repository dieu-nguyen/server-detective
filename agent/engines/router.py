"""
Unified Engine Router.

Routes each Telegram debugging turn to either:
- `cli_subscription` (Cursor CLI default, or Claude / Codex / Gemini CLI)
- `sdk_apikey` (Anthropic, OpenAI, Gemini, OpenRouter, custom OpenAI-compatible API)

Supports live runtime mode switching (persisted to `config.yaml`) and optional automatic fallback.
"""

from __future__ import annotations

from typing import Optional

from agent.config import AppConfig, save_engine_mode_to_config
from agent.engines.base import AgentTurnResult, ConversationMemory
from agent.engines.cli_subscription import CLISubscriptionEngine
from agent.engines.sdk_apikey import SDKApiKeyEngine
from ops_toolkit import OpsToolkit


class EngineRouter:
    """Routes requests to the active engine mode and manages shared conversation memory."""

    def __init__(self, config: AppConfig, toolkit: Optional[OpsToolkit] = None) -> None:
        self.config = config
        self.toolkit = toolkit or OpsToolkit(config)
        self.memory = ConversationMemory(max_messages=config.engine.max_history_messages)
        self.cli_engine = CLISubscriptionEngine(config, self.toolkit)
        self.sdk_engine = SDKApiKeyEngine(config, self.toolkit)

    @property
    def current_mode(self) -> str:
        return self.config.engine.mode

    def switch_mode(self, new_mode: str, persist: bool = True) -> str:
        normalized = new_mode.strip().lower()
        if normalized in ("cli", "subscription", "cli_subscription"):
            normalized = "cli_subscription"
        elif normalized in ("sdk", "apikey", "api_key", "sdk_apikey"):
            normalized = "sdk_apikey"
        else:
            raise ValueError("Mode must be `cli_subscription` (or `cli`) or `sdk_apikey` (or `sdk`).")

        self.config.engine.mode = normalized
        if persist:
            save_engine_mode_to_config(self.config.config_path, normalized)
        return normalized

    async def handle_message(
        self,
        session_key: str,
        user_message: str,
        requester_user_id: Optional[int] = None,
        requester_username: str = "",
        override_mode: Optional[str] = None,
    ) -> AgentTurnResult:
        mode = (override_mode or self.current_mode).lower()
        history = self.memory.get_messages(session_key)

        if mode == "cli_subscription":
            try:
                result = await self.cli_engine.run_turn(
                    user_message=user_message,
                    history=history,
                    requester_user_id=requester_user_id,
                    requester_username=requester_username,
                )
            except Exception as cli_exc:
                if self.config.engine.auto_fallback_to_sdk:
                    result = await self.sdk_engine.run_turn(
                        user_message=user_message,
                        history=history,
                        requester_user_id=requester_user_id,
                        requester_username=requester_username,
                    )
                    result.answer = (
                        f"⚠️ *[Auto-Fallback to SDK API Key mode after CLI error: {cli_exc}]*\n\n"
                        + result.answer
                    )
                else:
                    raise
        else:
            result = await self.sdk_engine.run_turn(
                user_message=user_message,
                history=history,
                requester_user_id=requester_user_id,
                requester_username=requester_username,
            )

        self.memory.add_turn(session_key, user_message, result.answer)
        return result

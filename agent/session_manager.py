"""
Native Session Manager (`agent/session_manager.py`).

Solves the "When to Resume vs. Start a New Session in Telegram" problem:
- Tracks native CLI session IDs per Telegram chat/topic AND per provider
  (e.g., Cursor `agent --resume <id>`, OpenCode `opencode run --session <id>`,
  Claude `claude --resume <id>`).
- Automatically rotates to a fresh session when:
  1. The Telegram chat/topic has been idle for > `idle_timeout_minutes` (default 30m).
  2. The session has reached `max_turns_per_session` (default 15 turns).
  3. A user explicitly requests `/new` (or `/clear`) or taps `[🔄 New Session]`.
- Also parses structured JSON / JSONL stdout from Cursor CLI, OpenCode CLI, and Claude CLI
  to extract both the `session_id` (`sessionID` / `chatId`) and the assistant's text response.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


@dataclass
class ActiveSessionState:
    telegram_key: str
    provider_key: str  # e.g., "cli_subscription:cursor" or "sdk_apikey:opencode"
    native_session_id: Optional[str]
    turn_count: int
    created_at: float
    last_active_at: float
    is_new_session: bool
    rotation_reason: str = ""  # "", "first_turn", "idle_timeout", "max_turns_reached", "manual_reset"


class SessionManager:
    """Persistent session state manager with automatic idle-timeout and max-turn rotation."""

    def __init__(
        self,
        sessions_file: str | Path = "./.sessions.json",
        idle_timeout_minutes: int = 30,
        max_turns_per_session: int = 15,
        enabled: bool = True,
    ) -> None:
        self.sessions_file = Path(sessions_file)
        self.idle_timeout_seconds = max(60, int(idle_timeout_minutes) * 60)
        self.max_turns_per_session = max(1, int(max_turns_per_session))
        self.enabled = enabled

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.sessions_file.exists():
            return {}
        try:
            data = json.loads(self.sessions_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, data: Dict[str, Dict[str, Any]]) -> None:
        try:
            self.sessions_file.parent.mkdir(parents=True, exist_ok=True)
            self.sessions_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def resolve_session(self, telegram_key: str, provider_key: str) -> ActiveSessionState:
        """
        Determine whether to resume the existing native session or start a new one
        for `(telegram_key, provider_key)`.
        """
        now = time.time()
        if not self.enabled:
            return ActiveSessionState(
                telegram_key=telegram_key,
                provider_key=provider_key,
                native_session_id=None,
                turn_count=0,
                created_at=now,
                last_active_at=now,
                is_new_session=True,
                rotation_reason="sessions_disabled",
            )

        all_sessions = self._load()
        chat_entry = all_sessions.get(telegram_key, {})
        record = chat_entry.get(provider_key)

        if not record or not isinstance(record, dict):
            return ActiveSessionState(
                telegram_key=telegram_key,
                provider_key=provider_key,
                native_session_id=None,
                turn_count=0,
                created_at=now,
                last_active_at=now,
                is_new_session=True,
                rotation_reason="first_turn",
            )

        last_active = float(record.get("last_active_at", 0))
        turns = int(record.get("turn_count", 0))
        native_id = record.get("native_session_id")

        idle_seconds = now - last_active
        if idle_seconds > self.idle_timeout_seconds:
            idle_mins = int(idle_seconds // 60)
            return ActiveSessionState(
                telegram_key=telegram_key,
                provider_key=provider_key,
                native_session_id=None,
                turn_count=0,
                created_at=now,
                last_active_at=now,
                is_new_session=True,
                rotation_reason=f"idle_timeout ({idle_mins}m quiet > {self.idle_timeout_seconds // 60}m limit)",
            )

        if turns >= self.max_turns_per_session:
            return ActiveSessionState(
                telegram_key=telegram_key,
                provider_key=provider_key,
                native_session_id=None,
                turn_count=0,
                created_at=now,
                last_active_at=now,
                is_new_session=True,
                rotation_reason=f"max_turns_reached ({turns}/{self.max_turns_per_session} turns)",
            )

        return ActiveSessionState(
            telegram_key=telegram_key,
            provider_key=provider_key,
            native_session_id=str(native_id) if native_id else None,
            turn_count=turns,
            created_at=float(record.get("created_at", now)),
            last_active_at=last_active,
            is_new_session=not bool(native_id),
            rotation_reason="",
        )

    def record_turn(
        self,
        state: ActiveSessionState,
        native_session_id: Optional[str],
    ) -> ActiveSessionState:
        """Persist updated turn count, timestamp, and captured native `session_id`."""
        now = time.time()
        effective_id = native_session_id or state.native_session_id
        new_turns = state.turn_count + 1

        all_sessions = self._load()
        if state.telegram_key not in all_sessions or not isinstance(all_sessions[state.telegram_key], dict):
            all_sessions[state.telegram_key] = {}

        all_sessions[state.telegram_key][state.provider_key] = {
            "native_session_id": effective_id,
            "turn_count": new_turns,
            "created_at": state.created_at,
            "last_active_at": now,
        }
        self._save(all_sessions)

        return ActiveSessionState(
            telegram_key=state.telegram_key,
            provider_key=state.provider_key,
            native_session_id=effective_id,
            turn_count=new_turns,
            created_at=state.created_at,
            last_active_at=now,
            is_new_session=state.is_new_session,
            rotation_reason=state.rotation_reason,
        )

    def reset_chat_sessions(self, telegram_key: str) -> None:
        """Clear all native sessions for a Telegram chat/topic (`/new` or `[🔄 New Session]`)."""
        all_sessions = self._load()
        if telegram_key in all_sessions:
            all_sessions.pop(telegram_key, None)
            self._save(all_sessions)

    def get_chat_info(self, telegram_key: str) -> Dict[str, Any]:
        """Return active session metadata for `/session` status inspection."""
        all_sessions = self._load()
        return all_sessions.get(telegram_key, {})


def parse_cli_json_output(raw_stdout: str) -> Tuple[str, Optional[str]]:
    """
    Parse stdout from Cursor CLI (`--output-format json`), Claude CLI (`--output-format json`),
    or OpenCode CLI (`--format json` JSONL stream) to extract `(assistant_text, session_id)`.
    Gracefully falls back to plain text if stdout is not JSON.
    """
    text = (raw_stdout or "").strip()
    if not text:
        return "", None

    # 1. Try single JSON object (Cursor CLI / Claude CLI `--output-format json`)
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                session_id = (
                    obj.get("session_id")
                    or obj.get("sessionId")
                    or obj.get("sessionID")
                    or obj.get("chat_id")
                    or obj.get("chatId")
                )
                result_text = (
                    obj.get("result")
                    or obj.get("text")
                    or obj.get("content")
                    or obj.get("message")
                    or ""
                )
                if isinstance(result_text, list):
                    result_text = "\n".join(
                        str(item.get("text", item)) if isinstance(item, dict) else str(item)
                        for item in result_text
                    )
                if result_text or session_id:
                    return str(result_text).strip() or text, (str(session_id) if session_id else None)
        except Exception:
            pass

    # 2. Try JSONL / streaming JSON lines (OpenCode `opencode run --format json` or stream-json)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    json_objects = []
    for ln in lines:
        if ln.startswith("{") and ln.endswith("}"):
            try:
                json_objects.append(json.loads(ln))
            except Exception:
                continue

    if json_objects:
        extracted_session_id: Optional[str] = None
        text_fragments: list[str] = []
        final_result: Optional[str] = None

        for obj in json_objects:
            if not isinstance(obj, dict):
                continue
            sid = (
                obj.get("sessionID")
                or obj.get("session_id")
                or obj.get("sessionId")
                or obj.get("chatId")
                or (obj.get("part") or {}).get("sessionID")
                if isinstance(obj.get("part"), dict)
                else None
            )
            if sid and not extracted_session_id:
                extracted_session_id = str(sid)

            # OpenCode JSON event format: `{"type": "text", "part": {"text": "..."}}`
            if obj.get("type") == "text" and isinstance(obj.get("part"), dict):
                part_text = obj["part"].get("text")
                if part_text:
                    text_fragments.append(str(part_text))
            elif "result" in obj and obj["result"]:
                final_result = str(obj["result"])
            elif "text" in obj and isinstance(obj["text"], str):
                text_fragments.append(obj["text"])

        combined = (final_result or "".join(text_fragments)).strip()
        if combined or extracted_session_id:
            return combined or text, extracted_session_id

    # 3. Fallback: plain text stdout
    return text, None

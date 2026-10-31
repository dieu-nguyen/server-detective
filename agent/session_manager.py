"""
SQLite-Backed State & Cross-Tool Portable Session Manager (`agent/session_manager.py`).

Why SQLite (`agent_state.db`):
1. Stores all sessions, per-tool native CLI session IDs (`cursor`, `claude`, `opencode`, `pi`),
   and turn-by-turn message transcripts in a single ACID/WAL SQLite database (`agent_state.db`).
2. Supports **Cross-Tool Session Porting**:
   - A logical session (`session_id`) can hold native CLI IDs for multiple tools simultaneously
     (e.g. `cli_subscription:cursor` -> `cur_abc` AND `sdk_apikey:opencode:anthropic` -> `ses_xyz`).
   - If you start a session in Cursor CLI and then switch to OpenCode (or run `/sdk`), or resume a
     Cursor CLI session while in OpenCode mode, `SessionManager` automatically bridges/seeds the
     prior SQLite turn transcript into OpenCode, captures OpenCode's native `ses_...` ID, and
     links both native IDs to the same session!
3. Supports `/session <id>` (by logical ID, prefix, or any tool's native CLI ID):
   - Reactivates that historical session for the current Telegram chat/topic.
   - Resets `turn_count = 0` and `last_active_at = now` so you get a full fresh turn budget and
     idle timeout window on top of the resumed session.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ActiveSessionState:
    session_id: str  # Unified logical session ID (e.g. "s_8f9a1b2c")
    telegram_key: str
    provider_key: str  # e.g. "cli_subscription:cursor" or "sdk_apikey:opencode:anthropic"
    native_session_id: Optional[str]  # Native CLI session ID for THIS specific `provider_key` (if already established)
    all_native_ids: Dict[str, str] = field(default_factory=dict)  # Map of provider_key -> native_session_id
    turn_count: int = 0
    total_turns_ever: int = 0
    title: str = ""
    created_at: float = 0.0
    last_active_at: float = 0.0
    is_new_session: bool = True
    is_ported_across_tools: bool = False  # True when switching tools on an existing session that has prior turns
    rotation_reason: str = ""  # "", "first_turn", "idle_timeout", "max_turns_reached", "resumed_by_user"
    transcript_messages: List[Dict[str, str]] = field(default_factory=list)


def init_sqlite_db(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode enabled and ensure all schema tables exist."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            telegram_key TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            turn_count INTEGER NOT NULL DEFAULT 0,
            total_turns_ever INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_provider_key TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            last_active_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_tg_active
            ON sessions(telegram_key, is_active, last_active_at DESC);

        CREATE TABLE IF NOT EXISTS session_native_ids (
            session_id TEXT NOT NULL,
            provider_key TEXT NOT NULL,
            native_session_id TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (session_id, provider_key),
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_native_id_lookup
            ON session_native_ids(native_session_id);

        CREATE TABLE IF NOT EXISTS session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            provider_key TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_session_messages_sid
            ON session_messages(session_id, id ASC);

        CREATE TABLE IF NOT EXISTS pending_actions (
            action_id TEXT PRIMARY KEY,
            action_type TEXT NOT NULL,
            target TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            requested_by_user_id INTEGER,
            requested_by_username TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_utc TEXT NOT NULL,
            timestamp_epoch REAL NOT NULL,
            event_type TEXT NOT NULL,
            user_id INTEGER,
            username TEXT NOT NULL DEFAULT '',
            chat_id INTEGER,
            engine_mode TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    conn.commit()
    return conn


class SessionManager:
    """
    SQLite-backed session manager supporting:
    - Automatic idle-timeout & max-turn rotation
    - Cross-tool session porting (e.g. Cursor CLI <-> OpenCode API <-> Python SDK)
    - Reusing any historical session via `/session <id>` (resets turn count & idle timer)
    """

    def __init__(
        self,
        db_path: str | Path = "./agent_state.db",
        idle_timeout_minutes: int = 30,
        max_turns_per_session: int = 15,
        max_history_messages: int = 12,
        enabled: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self.idle_timeout_seconds = max(60, int(idle_timeout_minutes) * 60)
        self.max_turns_per_session = max(1, int(max_turns_per_session))
        self.max_history_messages = max(2, int(max_history_messages))
        self.enabled = enabled
        with self._conn() as conn:
            pass

    def _conn(self) -> sqlite3.Connection:
        return init_sqlite_db(self.db_path)

    def _get_native_ids_map(self, conn: sqlite3.Connection, session_id: str) -> Dict[str, str]:
        rows = conn.execute(
            "SELECT provider_key, native_session_id FROM session_native_ids WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        return {str(r["provider_key"]): str(r["native_session_id"]) for r in rows}

    def _get_transcript(self, conn: sqlite3.Connection, session_id: str) -> List[Dict[str, str]]:
        rows = conn.execute(
            """
            SELECT role, content, provider_key FROM (
                SELECT id, role, content, provider_key FROM session_messages
                WHERE session_id = ?
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (session_id, self.max_history_messages),
        ).fetchall()
        return [
            {"role": str(r["role"]), "content": str(r["content"]), "provider_key": str(r["provider_key"])}
            for r in rows
        ]

    def _create_new_session_row(
        self,
        conn: sqlite3.Connection,
        telegram_key: str,
        provider_key: str,
        rotation_reason: str,
    ) -> ActiveSessionState:
        now = time.time()
        # Mark any previous active sessions for this telegram_key as inactive (kept in history!)
        conn.execute(
            "UPDATE sessions SET is_active = 0 WHERE telegram_key = ? AND is_active = 1",
            (telegram_key,),
        )
        new_sid = f"s_{uuid.uuid4().hex[:8]}"
        conn.execute(
            """
            INSERT INTO sessions (
                session_id, telegram_key, title, turn_count, total_turns_ever,
                is_active, last_provider_key, created_at, last_active_at
            ) VALUES (?, ?, '', 0, 0, 1, ?, ?, ?)
            """,
            (new_sid, telegram_key, provider_key, now, now),
        )
        conn.commit()
        return ActiveSessionState(
            session_id=new_sid,
            telegram_key=telegram_key,
            provider_key=provider_key,
            native_session_id=None,
            all_native_ids={},
            turn_count=0,
            total_turns_ever=0,
            title="",
            created_at=now,
            last_active_at=now,
            is_new_session=True,
            is_ported_across_tools=False,
            rotation_reason=rotation_reason,
            transcript_messages=[],
        )

    def resolve_session(self, telegram_key: str, provider_key: str) -> ActiveSessionState:
        """
        Resolve the active session for `telegram_key`, rotating to a new session if idle > limit
        or turns >= max_turns, and seamlessly porting context if `provider_key` changed!
        """
        now = time.time()
        with self._conn() as conn:
            if not self.enabled:
                return self._create_new_session_row(conn, telegram_key, provider_key, "sessions_disabled")

            row = conn.execute(
                """
                SELECT * FROM sessions
                WHERE telegram_key = ? AND is_active = 1
                ORDER BY last_active_at DESC LIMIT 1
                """,
                (telegram_key,),
            ).fetchone()

            if not row:
                return self._create_new_session_row(conn, telegram_key, provider_key, "first_turn")

            sid = str(row["session_id"])
            last_active = float(row["last_active_at"])
            turns = int(row["turn_count"])
            total_turns = int(row["total_turns_ever"])
            idle_seconds = now - last_active

            # Rule 1: Idle timeout exceeded -> Auto-rotate to a brand new session
            if idle_seconds > self.idle_timeout_seconds and total_turns > 0:
                idle_mins = int(idle_seconds // 60)
                return self._create_new_session_row(
                    conn,
                    telegram_key,
                    provider_key,
                    f"idle_timeout ({idle_mins}m quiet > {self.idle_timeout_seconds // 60}m limit)",
                )

            # Rule 2: Turn limit reached -> Auto-rotate to a brand new session
            if turns >= self.max_turns_per_session:
                return self._create_new_session_row(
                    conn,
                    telegram_key,
                    provider_key,
                    f"max_turns_reached ({turns}/{self.max_turns_per_session} turns)",
                )

            native_map = self._get_native_ids_map(conn, sid)
            transcript = self._get_transcript(conn, sid)
            native_for_provider = native_map.get(provider_key)

            # Detect if this session already has turns in another tool (e.g. Cursor CLI)
            # and is now being used with a new tool (e.g. OpenCode API) for the first time!
            ported = bool(total_turns > 0 and not native_for_provider)

            return ActiveSessionState(
                session_id=sid,
                telegram_key=telegram_key,
                provider_key=provider_key,
                native_session_id=native_for_provider,
                all_native_ids=native_map,
                turn_count=turns,
                total_turns_ever=total_turns,
                title=str(row["title"] or ""),
                created_at=float(row["created_at"]),
                last_active_at=last_active,
                is_new_session=(total_turns == 0),
                is_ported_across_tools=ported,
                rotation_reason="ported_across_tools" if ported else "",
                transcript_messages=transcript,
            )

    def record_turn(
        self,
        state: ActiveSessionState,
        user_message: str,
        assistant_answer: str,
        native_session_id: Optional[str],
    ) -> ActiveSessionState:
        """
        Save the completed turn into SQLite (`sessions`, `session_native_ids`, and `session_messages`).
        """
        now = time.time()
        effective_native_id = native_session_id or state.native_session_id
        new_turns = state.turn_count + 1
        new_total = state.total_turns_ever + 1
        title = state.title or user_message.strip().replace("\n", " ")[:70]

        with self._conn() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET title = ?,
                    turn_count = ?,
                    total_turns_ever = ?,
                    last_provider_key = ?,
                    last_active_at = ?,
                    is_active = 1
                WHERE session_id = ?
                """,
                (title, new_turns, new_total, state.provider_key, now, state.session_id),
            )

            if effective_native_id:
                conn.execute(
                    """
                    INSERT INTO session_native_ids (session_id, provider_key, native_session_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(session_id, provider_key) DO UPDATE SET
                        native_session_id = excluded.native_session_id,
                        updated_at = excluded.updated_at
                    """,
                    (state.session_id, state.provider_key, str(effective_native_id), now),
                )

            conn.execute(
                """
                INSERT INTO session_messages (session_id, role, content, provider_key, created_at)
                VALUES (?, 'user', ?, ?, ?)
                """,
                (state.session_id, user_message, state.provider_key, now),
            )
            conn.execute(
                """
                INSERT INTO session_messages (session_id, role, content, provider_key, created_at)
                VALUES (?, 'assistant', ?, ?, ?)
                """,
                (state.session_id, assistant_answer, state.provider_key, now),
            )
            conn.commit()
            native_map = self._get_native_ids_map(conn, state.session_id)
            transcript = self._get_transcript(conn, state.session_id)

        return ActiveSessionState(
            session_id=state.session_id,
            telegram_key=state.telegram_key,
            provider_key=state.provider_key,
            native_session_id=effective_native_id,
            all_native_ids=native_map,
            turn_count=new_turns,
            total_turns_ever=new_total,
            title=title,
            created_at=state.created_at,
            last_active_at=now,
            is_new_session=state.is_new_session,
            is_ported_across_tools=state.is_ported_across_tools,
            rotation_reason=state.rotation_reason,
            transcript_messages=transcript,
        )

    def reuse_old_session(
        self,
        telegram_key: str,
        target_id: str,
        current_provider_key: str = "",
    ) -> Optional[ActiveSessionState]:
        """
        Reactivate any historical session by:
        1. Exact or prefix match on logical `session_id` (e.g. `s_8f9a1b2c`), OR
        2. Exact or prefix match on any tool's `native_session_id` (e.g. Cursor or OpenCode ID), OR
        3. If `target_id` is an external CLI session ID not yet in SQLite, import & register it!

        Resets `turn_count = 0` and `last_active_at = now` so the user gets a full fresh
        15-turn and 30-minute idle window on the resumed session.
        """
        needle = target_id.strip()
        if not needle:
            return None

        now = time.time()
        with self._conn() as conn:
            # 1. Search logical `sessions.session_id`
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ? OR session_id LIKE ? ORDER BY last_active_at DESC LIMIT 1",
                (needle, f"{needle}%"),
            ).fetchone()

            # 2. Search `session_native_ids.native_session_id` across all tools
            if not row:
                native_row = conn.execute(
                    """
                    SELECT session_id FROM session_native_ids
                    WHERE native_session_id = ? OR native_session_id LIKE ?
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (needle, f"{needle}%"),
                ).fetchone()
                if native_row:
                    row = conn.execute(
                        "SELECT * FROM sessions WHERE session_id = ?",
                        (str(native_row["session_id"]),),
                    ).fetchone()

            # 3. If not found in SQLite at all, allow attaching an external native CLI session ID directly
            if not row and current_provider_key:
                conn.execute(
                    "UPDATE sessions SET is_active = 0 WHERE telegram_key = ? AND is_active = 1",
                    (telegram_key,),
                )
                new_sid = f"s_{uuid.uuid4().hex[:8]}"
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_id, telegram_key, title, turn_count, total_turns_ever,
                        is_active, last_provider_key, created_at, last_active_at
                    ) VALUES (?, ?, ?, 0, 1, 1, ?, ?, ?)
                    """,
                    (new_sid, telegram_key, f"Imported native session {needle[:16]}", current_provider_key, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO session_native_ids (session_id, provider_key, native_session_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (new_sid, current_provider_key, needle, now),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (new_sid,)).fetchone()

            if not row:
                return None

            sid = str(row["session_id"])
            # Deactivate any other active session on this chat, then activate `sid` with reset turn_count & idle timer
            conn.execute(
                "UPDATE sessions SET is_active = 0 WHERE telegram_key = ? AND is_active = 1",
                (telegram_key,),
            )
            conn.execute(
                """
                UPDATE sessions
                SET telegram_key = ?,
                    is_active = 1,
                    turn_count = 0,
                    last_active_at = ?
                WHERE session_id = ?
                """,
                (telegram_key, now, sid),
            )
            conn.commit()

            updated_row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (sid,)).fetchone()
            native_map = self._get_native_ids_map(conn, sid)
            transcript = self._get_transcript(conn, sid)
            prov = current_provider_key or str(updated_row["last_provider_key"])
            native_for_prov = native_map.get(prov)

            return ActiveSessionState(
                session_id=sid,
                telegram_key=telegram_key,
                provider_key=prov,
                native_session_id=native_for_prov,
                all_native_ids=native_map,
                turn_count=0,
                total_turns_ever=int(updated_row["total_turns_ever"]),
                title=str(updated_row["title"] or ""),
                created_at=float(updated_row["created_at"]),
                last_active_at=now,
                is_new_session=False,
                is_ported_across_tools=bool(int(updated_row["total_turns_ever"]) > 0 and not native_for_prov),
                rotation_reason="resumed_by_user",
                transcript_messages=transcript,
            )

    def reset_chat_sessions(self, telegram_key: str) -> None:
        """Deactivate current session for `telegram_key` (keeps it in SQLite history for `/session <id>`)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET is_active = 0 WHERE telegram_key = ? AND is_active = 1",
                (telegram_key,),
            )
            conn.commit()

    def get_chat_sessions_overview(self, telegram_key: str, limit: int = 8) -> Dict[str, Any]:
        """Return the currently active session plus recent historical sessions for `telegram_key`."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                WHERE telegram_key = ?
                ORDER BY is_active DESC, last_active_at DESC
                LIMIT ?
                """,
                (telegram_key, max(1, int(limit))),
            ).fetchall()

            active_session: Optional[Dict[str, Any]] = None
            recent_sessions: List[Dict[str, Any]] = []

            for r in rows:
                sid = str(r["session_id"])
                native_map = self._get_native_ids_map(conn, sid)
                item = {
                    "session_id": sid,
                    "title": str(r["title"] or "(Untitled session)"),
                    "is_active": bool(r["is_active"]),
                    "turn_count": int(r["turn_count"]),
                    "total_turns_ever": int(r["total_turns_ever"]),
                    "last_provider_key": str(r["last_provider_key"]),
                    "native_ids": native_map,
                    "created_at": float(r["created_at"]),
                    "last_active_at": float(r["last_active_at"]),
                }
                if item["is_active"] and active_session is None:
                    active_session = item
                else:
                    recent_sessions.append(item)

            return {
                "telegram_key": telegram_key,
                "active_session": active_session,
                "recent_sessions": recent_sessions,
            }


def parse_cli_json_output(raw_stdout: str) -> Tuple[str, Optional[str]]:
    """
    Parse stdout from Cursor CLI (`--output-format json`), Claude CLI (`--output-format json`),
    or OpenCode CLI (`--format json` JSONL stream) to extract `(assistant_text, session_id)`.
    Gracefully falls back to plain text if stdout is not JSON.
    """
    text = (raw_stdout or "").strip()
    if not text:
        return "", None

    # 1. Single JSON object (Cursor CLI / Claude CLI `--output-format json`)
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

    # 2. JSONL / streaming JSON lines (OpenCode `opencode run --format json` or stream-json)
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
                or ((obj.get("part") or {}).get("sessionID") if isinstance(obj.get("part"), dict) else None)
            )
            if sid and not extracted_session_id:
                extracted_session_id = str(sid)

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

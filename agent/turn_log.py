"""Shared rolling turn log used by Cursor CLI streaming and Telegram delivery."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable, List
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ANSWER_LOG_MAX_CHARS = 80_000


def format_turn_log_time(tz_name: str) -> str:
    """Local wall time for turn-log lines, e.g. 2026-11-16 22:03:15 +0700."""
    try:
        tz = ZoneInfo(tz_name) if tz_name else datetime.now().astimezone().tzinfo
        now = datetime.now(tz)
    except Exception:
        now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M:%S %z")


def rotate_log_file(path: Path, backup_count: int) -> None:
    """Rename path -> path.1, path.1 -> path.2, ... dropping the oldest."""
    if backup_count <= 0:
        path.write_text("", encoding="utf-8")
        return
    oldest = Path(f"{path}.{backup_count}")
    if oldest.exists():
        oldest.unlink()
    for index in range(backup_count - 1, 0, -1):
        src = Path(f"{path}.{index}")
        if src.exists():
            src.replace(Path(f"{path}.{index + 1}"))
    if path.exists():
        path.replace(Path(f"{path}.1"))


def answer_log_lines(text: str, kind: str = "answer", max_chars: int = ANSWER_LOG_MAX_CHARS) -> List[str]:
    """Prefix a Telegram answer/error body so it is grep-able in agent-turns.log."""
    raw = text or ""
    lines = [f"[telegram-{kind}] begin chars={len(raw)}"]
    clipped = raw
    if len(raw) > max_chars:
        clipped = raw[:max_chars]
        lines.append(f"[telegram-{kind}] truncated_for_log kept={max_chars}")
    if not clipped:
        lines.append(f"[{kind}] (empty)")
    else:
        for line in clipped.splitlines():
            lines.append(f"[{kind}] {line}")
    lines.append(f"[telegram-{kind}] end")
    return lines


class TurnLog:
    """Line-flushed turn log with timestamps and size-based rotation."""

    def __init__(
        self,
        path: str,
        timezone: str = "Asia/Saigon",
        max_bytes: int = 5_242_880,
        backup_count: int = 7,
    ) -> None:
        self.path = Path(path) if path.strip() else None
        self.timezone = timezone
        self.max_bytes = max(0, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        self._fh = None

    def __enter__(self) -> "TurnLog":
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
        stamped = f"{format_turn_log_time(self.timezone)} {text}"
        logger.debug("CLI turn %s", stamped[:800])
        if not self._fh:
            return
        try:
            self._fh.write(stamped + "\n")
            self._fh.flush()
            self._maybe_rotate()
        except Exception:
            logger.exception("Failed to write turn log line")

    def write_lines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def _maybe_rotate(self) -> None:
        if not self.path or not self._fh or self.max_bytes <= 0:
            return
        try:
            size = self._fh.tell()
        except Exception:
            size = self.path.stat().st_size
        if size < self.max_bytes:
            return
        self._fh.close()
        self._fh = None
        rotate_log_file(self.path, self.backup_count)
        self._fh = self.path.open("a", encoding="utf-8")


def write_turn_log(
    path: str,
    timezone: str,
    lines: Iterable[str],
    max_bytes: int = 5_242_880,
    backup_count: int = 7,
) -> None:
    if not (path or "").strip():
        return
    with TurnLog(path, timezone=timezone, max_bytes=max_bytes, backup_count=backup_count) as log:
        log.write_lines(lines)

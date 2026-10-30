"""Security utilities: secret redaction, SQL read-only validation, and audit logging."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Patterns matching common tokens, passwords, connection strings, and API keys
SECRET_PATTERNS = [
    re.compile(r"(sk-[a-zA-Z0-9_\-]{16,})"),
    re.compile(r"(\b\d{8,12}:[A-Za-z0-9_-]{30,}\b)"),  # Telegram bot token
    re.compile(r"(AIza[0-9A-Za-z\-_]{30,})"),  # Google / Gemini API key
    re.compile(
        r"((?:password|passwd|pwd|secret|api_key|apikey|token|authorization|mysql_pwd)\s*[=:]\s*)([^\s\"',;]+)",
        re.IGNORECASE,
    ),
    re.compile(r"(mysql(?:\+\w+)?://[^:\s]+:)([^@\s]+)(@)", re.IGNORECASE),
    re.compile(r"(Bearer\s+)([A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE),
]

ALLOWED_SQL_PREFIXES = ("SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN")

FORBIDDEN_SQL_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|GRANT|REVOKE|RENAME|CALL|LOAD\s+DATA|INTO\s+OUTFILE|INTO\s+DUMPFILE|LOCK\s+TABLES|SET\s+GLOBAL|SET\s+PASSWORD|KILL)\b",
    re.IGNORECASE,
)


def redact_secrets(text: str, extra_env_vars: Optional[List[str]] = None) -> str:
    """Redact API keys, passwords, connection strings, and configured secret env values from text."""
    if not text:
        return text

    redacted = text
    # Redact literal values of sensitive environment variables if present in environment
    env_names = extra_env_vars or [
        "TELEGRAM_BOT_TOKEN",
        "MYSQL_PASSWORD",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "CURSOR_API_KEY",
        "OPENROUTER_API_KEY",
    ]
    for env_name in env_names:
        val = os.environ.get(env_name, "").strip()
        if val and len(val) >= 6 and val not in ("your_telegram_bot_token_here", "your_readonly_mysql_password"):
            redacted = redacted.replace(val, f"[REDACTED:{env_name}]")

    # Apply regex patterns
    redacted = SECRET_PATTERNS[0].sub("[REDACTED_API_KEY]", redacted)
    redacted = SECRET_PATTERNS[1].sub("[REDACTED_TELEGRAM_TOKEN]", redacted)
    redacted = SECRET_PATTERNS[2].sub("[REDACTED_GOOGLE_KEY]", redacted)
    redacted = SECRET_PATTERNS[3].sub(r"\1[REDACTED]", redacted)
    redacted = SECRET_PATTERNS[4].sub(r"\1[REDACTED]\3", redacted)
    redacted = SECRET_PATTERNS[5].sub(r"\1[REDACTED]", redacted)
    return redacted


def validate_readonly_sql(sql: str, max_rows: int = 50) -> Tuple[bool, str, str]:
    """
    Validate that a SQL query is strictly read-only and single-statement.
    Returns (is_valid, normalized_sql_or_empty, error_message).
    """
    if not sql or not sql.strip():
        return False, "", "SQL query cannot be empty."

    # Strip SQL comments
    cleaned = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*[\s\S]*?\*/", "", cleaned).strip()
    # Strip trailing semicolon
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()

    if not cleaned:
        return False, "", "SQL query contained only comments."

    # Reject multi-statement queries
    if ";" in cleaned:
        return False, "", "Multi-statement SQL queries (containing ';') are blocked for safety."

    first_word = cleaned.split(None, 1)[0].upper()
    if first_word not in ALLOWED_SQL_PREFIXES:
        return (
            False,
            "",
            f"Only read-only SQL queries starting with {ALLOWED_SQL_PREFIXES} are allowed (got '{first_word}').",
        )

    forbidden_match = FORBIDDEN_SQL_KEYWORDS.search(cleaned)
    if forbidden_match:
        return (
            False,
            "",
            f"Forbidden SQL keyword '{forbidden_match.group(1).upper()}' detected in read-only query.",
        )

    # Automatically enforce LIMIT on SELECT queries if not already limited
    if first_word == "SELECT" and not re.search(r"\bLIMIT\s+\d+", cleaned, re.IGNORECASE):
        cleaned = f"{cleaned} LIMIT {int(max_rows)}"

    return True, cleaned, ""


class AuditLogger:
    """Append-only JSONL audit logger for team accountability."""

    def __init__(self, file_path: str | Path = "./audit.jsonl") -> None:
        self.file_path = Path(file_path)

    def log(
        self,
        event_type: str,
        user_id: Optional[int] = None,
        username: str = "",
        chat_id: Optional[int] = None,
        engine_mode: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "timestamp_epoch": time.time(),
            "event_type": event_type,
            "user_id": user_id,
            "username": username,
            "chat_id": chat_id,
            "engine_mode": engine_mode,
            "details": details or {},
        }
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            with self.file_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

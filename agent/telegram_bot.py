"""
Telegram Gateway with Team Role-Based Access Control (RBAC), Mode Switching,
SQLite-Backed Session Reuse (`/session`, `/session <id>`, `[🔁 Resume]` buttons),
Cross-Tool Session Porting, Audit Logging, and Interactive Admin Approval Buttons.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent.config import AppConfig
from agent.engines.router import EngineRouter
from agent.security import AuditLogger, redact_secrets
from ops_toolkit import OpsToolkit

TELEGRAM_MAX_LEN = 3500
TELEGRAM_MAX_CHUNKS = 3
TELEGRAM_MAX_ENTITY = 180
TELEGRAM_TRUNCATION_NOTE = (
    "\n\n_(Truncated to 3 Telegram messages. Ask a follow-up for the rest.)_"
)


def telegram_html(text: str) -> str:
    """Escape HTML, then turn short Markdown **bold** / `code` into Telegram HTML.

    Telegram rejects format entities that are too long ("Separator is found, but
    chunk is longer than limit"). Only wrap short, single-line spans.
    """
    escaped = html.escape(text or "", quote=False)

    def _short_span(match: re.Match[str], tag: str) -> str:
        inner = match.group(1)
        if len(inner) > TELEGRAM_MAX_ENTITY or "\n" in inner:
            return match.group(0)
        return f"<{tag}>{inner}</{tag}>"

    max_ent = TELEGRAM_MAX_ENTITY
    escaped = re.sub(
        rf"\*\*([^*\n]{{1,{max_ent}}})\*\*",
        lambda m: _short_span(m, "b"),
        escaped,
    )
    escaped = re.sub(
        rf"`([^`\n]{{1,{max_ent}}})`",
        lambda m: _short_span(m, "code"),
        escaped,
    )
    escaped = re.sub(
        rf"(?<!\*)\*([^*\n]{{1,{max_ent}}})\*(?!\*)",
        lambda m: _short_span(m, "i"),
        escaped,
    )
    escaped = re.sub(
        rf"(?<![A-Za-z0-9])_([^_\n]{{1,{max_ent}}})_(?![A-Za-z0-9])",
        lambda m: _short_span(m, "i"),
        escaped,
    )
    return escaped


def html_to_plain(text: str) -> str:
    """Strip tags for a parse_mode=None retry after Telegram HTML parse errors."""
    plain = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.IGNORECASE)
    plain = re.sub(r"<[^>]+>", "", plain)
    return html.unescape(plain)


def split_telegram_message(text: str, max_len: int = TELEGRAM_MAX_LEN) -> List[str]:
    """Split long agent responses into safe Telegram-sized chunks."""
    if not text:
        return ["(Empty response)"]
    chunks: List[str] = []
    remaining = text
    while len(remaining) > max_len:
        split_idx = remaining.rfind("\n", 0, max_len)
        if split_idx <= 0:
            split_idx = max_len
        chunks.append(remaining[:split_idx])
        remaining = remaining[split_idx:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def limit_telegram_chunks(
    chunks: List[str],
    max_chunks: int = TELEGRAM_MAX_CHUNKS,
    max_len: int = TELEGRAM_MAX_LEN,
) -> List[str]:
    """Keep at most `max_chunks` Telegram messages; note if the rest was dropped."""
    if len(chunks) <= max_chunks:
        return chunks
    limited = list(chunks[:max_chunks])
    last = limited[-1]
    note = TELEGRAM_TRUNCATION_NOTE
    if len(last) + len(note) > max_len:
        last = last[: max(0, max_len - len(note))].rstrip()
    limited[-1] = last + note
    return limited


def build_approval_keyboard(action_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve (Admins Only)", callback_data=f"approve:{action_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{action_id}"),
            ]
        ]
    )


def build_session_footer_keyboard(session_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Start New Session", callback_data=f"new_session:{session_key}"),
            ]
        ]
    )


def build_session_list_keyboard(
    session_key: str,
    recent_sessions: List[Dict[str, Any]],
) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for item in recent_sessions[:5]:
        sid = item["session_id"]
        title_short = (item.get("title") or sid)[:24]
        rows.append(
            [
                InlineKeyboardButton(
                    f"🔁 Resume {sid} ({title_short})",
                    callback_data=f"resume_sess:{sid}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🔄 Start New Session", callback_data=f"new_session:{session_key}")])
    return InlineKeyboardMarkup(rows)


class TelegramOpsBot:
    """Telegram Bot wrapping `EngineRouter`, `OpsToolkit`, SQLite Session Manager, and Team RBAC."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.toolkit = OpsToolkit(config)
        self.router = EngineRouter(config, self.toolkit)
        self.audit = AuditLogger(
            file_path=config.engine.audit_log_file,
            db_path=config.engine.session.state_db_file,
        )

    def _session_key(self, update: Update) -> str:
        chat_id = update.effective_chat.id if update.effective_chat else 0
        thread_id = getattr(update.effective_message, "message_thread_id", None)
        return f"{chat_id}:{thread_id or 'main'}"

    def _check_access(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat:
            return False
        return self.config.is_authorized(user.id, chat.id)

    async def _deny_access(self, update: Update) -> None:
        user = update.effective_user
        chat = update.effective_chat
        self.audit.log(
            event_type="unauthorized_access_attempt",
            user_id=user.id if user else None,
            username=user.username or "" if user else "",
            chat_id=chat.id if chat else None,
        )
        if update.effective_message:
            await update.effective_message.reply_text(
                f"⛔ Access denied. Your Telegram User ID (`{user.id if user else 'unknown'}`) "
                f"or Chat ID (`{chat.id if chat else 'unknown'}`) is not in the authorized `config.yaml` allowlist."
            )

    # -------------------------------------------------------------------------
    # Command Handlers
    # -------------------------------------------------------------------------
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_access(update):
            await self._deny_access(update)
            return

        user = update.effective_user
        role = "Admin" if (user and self.config.is_admin(user.id)) else "Viewer"
        mode = self.router.current_mode
        sub_provider = self.config.engine.cli_subscription.provider
        sdk_cfg = self.config.engine.sdk_apikey
        sdk_provider = f"{sdk_cfg.harness} ({sdk_cfg.provider}/{sdk_cfg.model})"
        sess_cfg = self.config.engine.session

        msg = (
            f"🖥️ **{self.config.server_name} — Server Debug Agent**\n"
            f"• **Your Role:** `{role}` (ID: `{user.id if user else ''}`)\n"
            f"• **Active Engine Mode:** `{mode}`\n"
            f"• **CLI Subscription Provider:** `{sub_provider}`\n"
            f"• **SDK API Key Harness:** `{sdk_provider}`\n"
            f"• **Hard CLI Restrictions:** `{'enabled' if self.config.engine.cli_restrictions.enabled else 'disabled (prompt-only)'}`\n"
            f"• **Session Store:** SQLite (`{sess_cfg.state_db_file}`)\n"
            f"• **Session Auto-Rotation:** Idle > `{sess_cfg.idle_timeout_minutes}m` or > `{sess_cfg.max_turns_per_session}` turns\n"
            f"• **Registered Services:** `{', '.join(self.config.services.keys()) or 'None'}`\n\n"
            "**Commands:**\n"
            "• Ask any question in natural language to debug services, multi-file logs, source code, PM2, ngrok, n8n, or MySQL.\n"
            "• `/session` — View active & recent sessions (with `[🔁 Resume]` buttons)\n"
            "• `/session <session_id>` — Reuse any old session (resets turn count to `0` & idle timer to `0m`, and ports context across Cursor/OpenCode!)\n"
            "• `/new` (or `/clear`) — Start a brand-new debugging session\n"
            "• `/status` — Quick snapshot of system resources & all registered services\n"
            "• `/services` — List all configured services, action log categories, & code paths\n"
            "• `/mode` — View or switch engine (`cli_subscription` | `sdk_apikey`)\n"
            "• `/cli <question>` — Force a single turn through CLI Subscription mode\n"
            "• `/sdk <question>` — Force a single turn through SDK API Key mode"
        )
        if update.effective_message:
            await update.effective_message.reply_text(msg)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_access(update):
            await self._deny_access(update)
            return
        if not update.effective_message:
            return

        sys_res = await asyncio.to_thread(self.toolkit.get_system_resources)
        svc_res = await asyncio.to_thread(self.toolkit.list_registered_services_status)

        lines = [
            f"📊 **Server Status: {self.config.server_name}**",
            f"• **OS:** `{sys_res.get('os')}` (Uptime: `{sys_res.get('uptime_hours')}h`)",
            f"• **CPU:** `{sys_res.get('cpu_percent')}%` ({sys_res.get('cpu_count')} cores)",
            f"• **RAM:** `{sys_res['memory']['used_gb']} / {sys_res['memory']['total_gb']} GB ({sys_res['memory']['percent']}%)`",
        ]
        for d in sys_res.get("disks", []):
            lines.append(f"• **Disk `{d['mount']}`:** `{d['used_gb']}/{d['total_gb']} GB ({d['percent']}%)`")

        lines.append("\n**Registered Services:**")
        for s_name, s_info in svc_res.get("services", {}).items():
            pm2_state = (s_info.get("pm2") or {}).get("status")
            win_state = (s_info.get("windows_service") or {}).get("status")
            hc = s_info.get("health_check") or {}
            hc_str = f"HTTP {hc.get('status_code')}" if hc.get("reachable") else ("UNREACHABLE" if hc else "N/A")
            state_str = pm2_state or win_state or "configured"
            cats = ", ".join(s_info.get("log_categories") or []) or "default"
            lines.append(f"• `{s_name}` ({s_info.get('manager')}): state=`{state_str}`, health=`{hc_str}`, logs=`[{cats}]`")

        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_services(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_access(update):
            await self._deny_access(update)
            return
        if not update.effective_message:
            return
        svc_res = await asyncio.to_thread(self.toolkit.list_registered_services_status)
        text = "🔧 **Registered Services Detail:**\n" + json.dumps(svc_res, indent=2, ensure_ascii=False)
        for chunk in limit_telegram_chunks(split_telegram_message(text)):
            await update.effective_message.reply_text(chunk)

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._check_access(update):
            await self._deny_access(update)
            return
        if not update.effective_message or not update.effective_user:
            return

        args = context.args or []
        sdk_cfg = self.config.engine.sdk_apikey
        if not args:
            await update.effective_message.reply_text(
                f"⚙️ **Current Engine Mode:** `{self.router.current_mode}`\n"
                f"• CLI Subscription provider: `{self.config.engine.cli_subscription.provider}`\n"
                f"• SDK API Key harness: `{sdk_cfg.harness}` (`{sdk_cfg.provider}/{sdk_cfg.model}`)\n\n"
                "Admins can switch modes with:\n"
                "• `/mode cli_subscription` (or `/mode cli`)\n"
                "• `/mode sdk_apikey` (or `/mode sdk`)"
            )
            return

        if not self.config.engine.allow_telegram_mode_switch:
            await update.effective_message.reply_text("⚠️ Mode switching via Telegram is disabled in `config.yaml`.")
            return

        if not self.config.is_admin(update.effective_user.id):
            await update.effective_message.reply_text("⛔ Only Admins can switch the default engine mode.")
            return

        try:
            new_mode = self.router.switch_mode(args[0], persist=True)
            self.audit.log(
                event_type="engine_mode_switched",
                user_id=update.effective_user.id,
                username=update.effective_user.username or "",
                chat_id=update.effective_chat.id if update.effective_chat else None,
                engine_mode=new_mode,
            )
            await update.effective_message.reply_text(
                f"✅ Engine mode switched to `{new_mode}` and saved to `{self.config.config_path}`.\n"
                "*(Active sessions in SQLite will automatically port their context across tools!)*"
            )
        except ValueError as exc:
            await update.effective_message.reply_text(f"❌ {exc}")

    async def cmd_new_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start a brand-new debugging session (`/new` or `/clear`)."""
        if not self._check_access(update):
            await self._deny_access(update)
            return
        s_key = self._session_key(update)
        self.router.reset_session(s_key)
        if update.effective_message:
            await update.effective_message.reply_text(
                "🆕 **Started a fresh debugging session** for this chat/topic.\n"
                "Your next message will begin a clean session (`turn 1`), while previous sessions remain saved in SQLite (`/session` to view or reuse)."
            )

    async def cmd_session_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        `/session` -> Show active session + recent sessions in SQLite with `[🔁 Resume]` buttons.
        `/session <session_id>` -> Reuse/reactivate `<session_id>`, resetting turn_count=0 and idle_time=0m!
        """
        if not self._check_access(update):
            await self._deny_access(update)
            return
        if not update.effective_message or not update.effective_user:
            return

        s_key = self._session_key(update)
        sess_cfg = self.config.engine.session
        args = context.args or []

        # Case 1: `/session <session_id>` -> Reuse specified session & reset counters
        if args:
            target_id = args[0].strip()
            resumed = self.router.reuse_session(s_key, target_id)
            if not resumed:
                await update.effective_message.reply_text(
                    f"❌ Session `{target_id}` was not found in SQLite (`{sess_cfg.state_db_file}`).\n"
                    "Run `/session` without arguments to see all saved session IDs."
                )
                return

            self.audit.log(
                event_type="session_reused",
                user_id=update.effective_user.id,
                username=update.effective_user.username or "",
                chat_id=update.effective_chat.id if update.effective_chat else None,
                engine_mode=self.router.current_mode,
                details={"session_id": resumed.session_id, "target_input": target_id},
            )
            native_summary = (
                ", ".join(f"{k}=`{v}`" for k, v in resumed.all_native_ids.items()) or "will bind on next turn"
            )
            port_note = (
                "\n• **Cross-Tool Porting:** Prior transcript will automatically seed into the current engine on your next message!"
                if resumed.is_ported_across_tools
                else ""
            )
            await update.effective_message.reply_text(
                f"🔁 **Session `{resumed.session_id}` Reactivated!**\n"
                f"• **Topic:** {resumed.title or '(Untitled)'}\n"
                f"• **Turn Counter Reset:** `0 / {sess_cfg.max_turns_per_session}` (Total historical turns: `{resumed.total_turns_ever}`)\n"
                f"• **Idle Timer Reset:** `0.0m / {sess_cfg.idle_timeout_minutes}m`\n"
                f"• **Linked Native Tool IDs:** {native_summary}"
                f"{port_note}"
            )
            return

        # Case 2: `/session` (no args) -> Show active session + historical sessions from SQLite
        overview = self.router.get_session_status(s_key, limit=8)
        active = overview.get("active_session")
        recent = overview.get("recent_sessions") or []
        now = time.time()

        lines = [
            f"🧵 **SQLite Session Manager (`{s_key}`)**",
            f"• **Auto-Rotation Policy:** `{sess_cfg.idle_timeout_minutes}m` idle or `{sess_cfg.max_turns_per_session}` turns",
            "• **Reuse Any Session:** `/session <session_id>` (resets turn count & idle timer)",
        ]

        if active:
            idle_mins = round((now - float(active["last_active_at"])) / 60, 1)
            native_str = (
                ", ".join(f"`{k.split(':')[0]}={v}`" for k, v in (active.get("native_ids") or {}).items())
                or "in-memory"
            )
            lines.append(
                f"\n🟢 **Active Session: `{active['session_id']}`**\n"
                f"  • Title: _{active['title']}_\n"
                f"  • Window Turns: `{active['turn_count']} / {sess_cfg.max_turns_per_session}` "
                f"(Total turns: `{active['total_turns_ever']}`)\n"
                f"  • Idle Time: `{idle_mins}m / {sess_cfg.idle_timeout_minutes}m`\n"
                f"  • Native Tool IDs: {native_str}"
            )
        else:
            lines.append("\n⚪ **Active Session:** _None (next message starts a new session)_")

        if recent:
            lines.append("\n📚 **Recent Saved Sessions (tap button or run `/session <id>`):**")
            for r in recent[:6]:
                age_mins = int((now - float(r["last_active_at"])) // 60)
                age_str = f"{age_mins}m ago" if age_mins < 120 else f"{round(age_mins / 60, 1)}h ago"
                n_ids = ", ".join(f"`{v}`" for v in (r.get("native_ids") or {}).values()) or "no-cli-id"
                lines.append(
                    f"• `{r['session_id']}` (native: {n_ids}) — _{r['title']}_ "
                    f"[turns: `{r['total_turns_ever']}`, {age_str}]"
                )

        await update.effective_message.reply_text(
            "\n".join(lines),
            reply_markup=build_session_list_keyboard(s_key, recent),
        )

    async def cmd_force_cli(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        prompt = " ".join(context.args or []).strip()
        if not prompt and update.effective_message:
            await update.effective_message.reply_text("Usage: `/cli <your debugging question>`")
            return
        await self._process_question(update, prompt, override_mode="cli_subscription")

    async def cmd_force_sdk(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        prompt = " ".join(context.args or []).strip()
        if not prompt and update.effective_message:
            await update.effective_message.reply_text("Usage: `/sdk <your debugging question>`")
            return
        await self._process_question(update, prompt, override_mode="sdk_apikey")

    # -------------------------------------------------------------------------
    # Natural-Language Debugging Handler
    # -------------------------------------------------------------------------
    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_message.text:
            return
        if not self._check_access(update):
            await self._deny_access(update)
            return

        text = update.effective_message.text.strip()
        chat = update.effective_chat
        if chat and chat.id < 0 and self.config.telegram.require_mention_in_groups:
            bot_username = context.bot.username or ""
            is_reply_to_bot = (
                update.effective_message.reply_to_message
                and update.effective_message.reply_to_message.from_user
                and update.effective_message.reply_to_message.from_user.id == context.bot.id
            )
            if f"@{bot_username}" not in text and not is_reply_to_bot:
                return

        await self._process_question(update, text, override_mode=None)

    async def _send_html_or_plain(
        self,
        sender: Any,
        method: str,
        text: str,
        reply_markup: Any = None,
    ) -> None:
        """Send HTML, then retry as plain text if Telegram rejects oversized format entities."""
        send = getattr(sender, method)
        try:
            return await send(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception as exc:
            err = str(exc)
            retry_plain = any(
                needle in err
                for needle in (
                    "chunk is longer than limit",
                    "Can't parse entities",
                    "can't parse entities",
                    "Unsupported start tag",
                    "Unmatched",
                )
            )
            if not retry_plain:
                raise
            logger.warning("Telegram HTML parse failed (%s); retrying as plain text", err)
            return await send(html_to_plain(text), reply_markup=reply_markup)

    async def _deliver_chunks(
        self,
        status_msg: Any,
        fallback_msg: Any,
        chunks: List[str],
        last_markup: Any = None,
    ) -> None:
        """Always try to put the turn outcome in Telegram (edit Investigating, else new reply)."""
        if not chunks:
            chunks = ["(Empty response)"]
        first_markup = last_markup if len(chunks) == 1 else None
        posted = False
        if status_msg is not None:
            try:
                await self._send_html_or_plain(status_msg, "edit_text", chunks[0], first_markup)
                posted = True
            except Exception:
                logger.exception("Failed to edit Investigating message; sending a new reply")
        if not posted:
            if fallback_msg is None:
                raise RuntimeError("No Telegram message available to deliver the agent result.")
            await self._send_html_or_plain(fallback_msg, "reply_text", chunks[0], first_markup)
        for index, extra in enumerate(chunks[1:]):
            markup = last_markup if index == len(chunks) - 2 else None
            await self._send_html_or_plain(fallback_msg, "reply_text", extra, markup)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Catch-all: any unhandled handler exception still gets a Telegram reply."""
        err = redact_secrets(str(getattr(context, "error", None) or "Unknown error"))
        logger.exception("Unhandled Telegram handler error: %s", err)
        target = update.effective_message if isinstance(update, Update) else None
        if target is None:
            return
        try:
            await self._send_html_or_plain(
                target,
                "reply_text",
                telegram_html(f"❌ Bot Error:\n{err}")[:TELEGRAM_MAX_LEN],
            )
        except Exception:
            logger.exception("Failed to send Bot Error to Telegram")

    async def _process_question(
        self,
        update: Update,
        question: str,
        override_mode: Optional[str] = None,
    ) -> None:
        msg = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if not msg or not user or not chat:
            return

        s_key = self._session_key(update)
        active_mode = override_mode or self.router.current_mode
        self.audit.log(
            event_type="debug_question",
            user_id=user.id,
            username=user.username or "",
            chat_id=chat.id,
            engine_mode=active_mode,
            details={"question": redact_secrets(question), "session_key": s_key},
        )

        timeout_s = (
            self.config.engine.cli_subscription.timeout_seconds
            if active_mode == "cli_subscription"
            else self.config.engine.sdk_apikey.timeout_seconds
        )
        status_msg = None
        heartbeat_task = None
        stop_heartbeat = asyncio.Event()
        started = time.monotonic()

        async def _heartbeat() -> None:
            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=20)
                    return
                except asyncio.TimeoutError:
                    elapsed = int(time.monotonic() - started)
                    if status_msg is None:
                        continue
                    try:
                        await self._send_html_or_plain(
                            status_msg,
                            "edit_text",
                            telegram_html(
                                f"🔍 Investigating on `{self.config.server_name}` via `{active_mode}`...\n"
                                f"_Still running: {elapsed}s / {timeout_s}s._"
                            ),
                        )
                    except Exception:
                        pass
                    try:
                        await chat.send_action(ChatAction.TYPING)
                    except Exception:
                        pass

        try:
            try:
                status_msg = await self._send_html_or_plain(
                    msg,
                    "reply_text",
                    telegram_html(
                        f"🔍 Investigating on `{self.config.server_name}` via `{active_mode}`...\n"
                        f"_Timeout {timeout_s}s. This message is replaced with the answer or any error._"
                    ),
                )
            except Exception:
                logger.exception("Failed to post Investigating status")

            try:
                await chat.send_action(ChatAction.TYPING)
            except Exception:
                pass

            heartbeat_task = asyncio.create_task(_heartbeat())
            result = await self.router.handle_message(
                session_key=s_key,
                user_message=question,
                requester_user_id=user.id,
                requester_username=user.username or user.first_name or str(user.id),
                override_mode=override_mode,
            )

            max_turns = self.config.engine.session.max_turns_per_session
            logical_id = result.logical_session_id or "s_mem"
            native_short = (result.native_session_id or "mem")[:12]

            if result.is_new_session:
                reason_note = (
                    f" — auto-rotated: {result.session_rotation_reason}"
                    if result.session_rotation_reason not in ("", "first_turn")
                    else ""
                )
                session_badge = (
                    f"🆕 Session `{logical_id}` (native: `{native_short}` • Turn {result.session_turn}/{max_turns}{reason_note})"
                )
            elif result.is_ported_across_tools:
                session_badge = (
                    f"🔀 Ported Session `{logical_id}` ➔ `{native_short}` (Turn {result.session_turn}/{max_turns})"
                )
            else:
                session_badge = (
                    f"🔗 Resumed `{logical_id}` (native: `{native_short}` • Turn {result.session_turn}/{max_turns})"
                )

            header = f"🤖 [{result.engine_used} • {result.provider_used}]\n{session_badge}\n\n"
            answer = (result.answer or "").strip() or "Investigation completed with no output."
            chunks = [
                telegram_html(chunk)
                for chunk in limit_telegram_chunks(split_telegram_message(header + answer))
            ]
            footer_markup = (
                build_session_footer_keyboard(s_key)
                if self.config.engine.session.show_new_session_button
                else None
            )
            await self._deliver_chunks(status_msg, msg, chunks, last_markup=footer_markup)

            for action in result.pending_actions:
                card_text = (
                    "🛡️ **Gated Action Approval Required**\n"
                    f"• **Action ID:** `{action['action_id']}`\n"
                    f"• **Operation:** `{action['action_type']}`\n"
                    f"• **Target:** `{action['target']}`\n"
                    f"• **Reason:** {action['reason']}\n"
                    f"• **Requested by:** @{action.get('requested_by_username') or user.id}\n\n"
                    "_Only users listed in `telegram.admins` can approve this action._"
                )
                try:
                    await self._send_html_or_plain(
                        msg,
                        "reply_text",
                        telegram_html(card_text),
                        build_approval_keyboard(action["action_id"]),
                    )
                except Exception:
                    logger.exception("Failed to post approval card %s", action.get("action_id"))
                    await self._deliver_chunks(
                        None,
                        msg,
                        limit_telegram_chunks(split_telegram_message(card_text)),
                    )
        except Exception as exc:
            err_text = redact_secrets(str(exc)) or "Unknown error"
            logger.exception("Agent turn failed; delivering error to Telegram")
            error_chunks = [
                telegram_html(chunk)
                for chunk in limit_telegram_chunks(
                    split_telegram_message(f"❌ Agent Error (`{active_mode}`):\n{err_text}")
                )
            ]
            try:
                await self._deliver_chunks(status_msg, msg, error_chunks)
            except Exception:
                logger.exception("Failed to deliver agent error to Telegram")
        finally:
            stop_heartbeat.set()
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except (asyncio.CancelledError, Exception):
                    pass

    # -------------------------------------------------------------------------
    # Callback Query Handler (New Session + Resume Session + Admin Approvals)
    # -------------------------------------------------------------------------
    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data or not query.from_user:
            return

        user = query.from_user
        parts = query.data.split(":", 1)
        if len(parts) != 2:
            await query.answer("Invalid callback data.")
            return

        decision, payload = parts[0], parts[1]
        chat_id = update.effective_chat.id if update.effective_chat else 0

        # 1. Start a fresh session button (`new_session:<session_key>`)
        if decision == "new_session":
            if not self.config.is_authorized(user.id, chat_id):
                await query.answer("⛔ Unauthorized.", show_alert=True)
                return
            self.router.reset_session(payload)
            await query.answer("Started a fresh debugging session!")
            if query.message:
                await query.message.reply_text(
                    f"🆕 **New session started** by @{user.username or user.id}. "
                    "The next question will start with a clean context (`turn 1`)."
                )
            return

        # 2. Resume a saved session button (`resume_sess:<session_id>`)
        if decision == "resume_sess":
            if not self.config.is_authorized(user.id, chat_id):
                await query.answer("⛔ Unauthorized.", show_alert=True)
                return
            s_key = self._session_key(update)
            resumed = self.router.reuse_session(s_key, payload)
            if not resumed:
                await query.answer("Session not found.", show_alert=True)
                return
            sess_cfg = self.config.engine.session
            await query.answer(f"Resumed session {resumed.session_id}!")
            if query.message:
                await query.message.reply_text(
                    f"🔁 **Session `{resumed.session_id}` Reactivated** by @{user.username or user.id}!\n"
                    f"• **Topic:** {resumed.title or '(Untitled)'}\n"
                    f"• **Turn Counter Reset:** `0 / {sess_cfg.max_turns_per_session}`\n"
                    f"• **Idle Timer Reset:** `0.0m / {sess_cfg.idle_timeout_minutes}m`"
                )
            return

        # 3. Admin-only approval buttons (`approve:<action_id>` / `reject:<action_id>`)
        if not self.config.is_admin(user.id):
            await query.answer("⛔ Only Admins listed in config.yaml can approve or reject actions.", show_alert=True)
            self.audit.log(
                event_type="unauthorized_approval_click",
                user_id=user.id,
                username=user.username or "",
                details={"callback_data": query.data},
            )
            return

        action_id = payload
        if decision == "reject":
            removed = self.toolkit.action_store.pop_action(action_id)
            self.audit.log(
                event_type="action_rejected",
                user_id=user.id,
                username=user.username or "",
                details={"action_id": action_id, "action": removed},
            )
            await query.answer("Action rejected.")
            await query.edit_message_text(
                f"❌ **Action `{action_id}` Rejected** by Admin @{user.username or user.id}."
            )
            return

        if decision == "approve":
            await query.answer("Executing approved action...")
            exec_res = await asyncio.to_thread(self.toolkit.execute_approved_action, action_id)
            self.audit.log(
                event_type="action_approved_and_executed",
                user_id=user.id,
                username=user.username or "",
                details={"action_id": action_id, "result": exec_res},
            )
            action_info = exec_res.get("action") or {}
            status_icon = "✅" if exec_res.get("ok") else "⚠️"
            output_preview = (exec_res.get("output") or exec_res.get("error") or "Done")[:1000]
            await query.edit_message_text(
                f"{status_icon} **Action `{action_id}` Executed**\n"
                f"• **Operation:** `{action_info.get('action_type', 'unknown')}` on `{action_info.get('target', '')}`\n"
                f"• **Approved by:** @{user.username or user.id}\n"
                f"• **Output:**\n```\n{output_preview}\n```"
            )

    def build_application(self) -> Application:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token or token == "your_telegram_bot_token_here":
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in environment or `.env`.")

        app = ApplicationBuilder().token(token).build()
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_start))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("services", self.cmd_services))
        app.add_handler(CommandHandler("mode", self.cmd_mode))
        app.add_handler(CommandHandler("new", self.cmd_new_session))
        app.add_handler(CommandHandler("clear", self.cmd_new_session))
        app.add_handler(CommandHandler("session", self.cmd_session_info))
        app.add_handler(CommandHandler("cli", self.cmd_force_cli))
        app.add_handler(CommandHandler("sdk", self.cmd_force_sdk))
        app.add_handler(CallbackQueryHandler(self.handle_callback_query))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_message))
        app.add_error_handler(self.on_error)
        return app

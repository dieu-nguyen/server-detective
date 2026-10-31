"""
Telegram Gateway with Team Role-Based Access Control (RBAC), Mode Switching,
Native Session Lifecycle Controls (`/new`, `/session`, `[🔄 New Session]` button),
Audit Logging, and Interactive Admin Approval Buttons.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
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

TELEGRAM_MAX_LEN = 3900


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


class TelegramOpsBot:
    """Telegram Bot wrapping `EngineRouter`, `OpsToolkit`, Native Session Manager, and Team RBAC."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.toolkit = OpsToolkit(config)
        self.router = EngineRouter(config, self.toolkit)
        self.audit = AuditLogger(config.engine.audit_log_file)

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
            f"• **Session Auto-Rotation:** Idle > `{sess_cfg.idle_timeout_minutes}m` or > `{sess_cfg.max_turns_per_session}` turns\n"
            f"• **Registered Services:** `{', '.join(self.config.services.keys()) or 'None'}`\n\n"
            "**Commands:**\n"
            "• Ask any question in natural language to debug services, multi-file logs, source code, PM2, ngrok, n8n, or MySQL.\n"
            "• `/new` (or `/clear`) — Start a brand-new native debugging session\n"
            "• `/session` — Inspect active native session ID, turn count, and idle timer\n"
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
        for chunk in split_telegram_message(text):
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
                f"✅ Engine mode switched to `{new_mode}` and saved to `{self.config.config_path}`."
            )
        except ValueError as exc:
            await update.effective_message.reply_text(f"❌ {exc}")

    async def cmd_new_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start a brand-new native debugging session (`/new` or `/clear`)."""
        if not self._check_access(update):
            await self._deny_access(update)
            return
        s_key = self._session_key(update)
        self.router.reset_session(s_key)
        if update.effective_message:
            await update.effective_message.reply_text(
                "🆕 **Started a fresh debugging session** for this chat/topic.\n"
                "Your next message will begin a clean native session (`turn 1`)."
            )

    async def cmd_session_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Inspect active native CLI session IDs, turn counts, and idle timers for this chat."""
        if not self._check_access(update):
            await self._deny_access(update)
            return
        if not update.effective_message:
            return

        s_key = self._session_key(update)
        info = self.router.get_session_status(s_key)
        sess_cfg = self.config.engine.session
        if not info:
            await update.effective_message.reply_text(
                f"ℹ️ **No active native session** for `{s_key}`.\n"
                f"• Auto-expiry policy: `{sess_cfg.idle_timeout_minutes}m` idle or `{sess_cfg.max_turns_per_session}` max turns."
            )
            return

        now = time.time()
        lines = [
            f"🧵 **Active Native Sessions for `{s_key}`**",
            f"• Policy: Auto-rotates after `{sess_cfg.idle_timeout_minutes}m` idle or `{sess_cfg.max_turns_per_session}` turns.",
        ]
        for prov_key, rec in info.items():
            idle_mins = round((now - float(rec.get("last_active_at", now))) / 60, 1)
            lines.append(
                f"\n**Provider `{prov_key}`:**\n"
                f"  • Native Session ID: `{rec.get('native_session_id') or 'in-memory'}`\n"
                f"  • Turns used: `{rec.get('turn_count', 0)} / {sess_cfg.max_turns_per_session}`\n"
                f"  • Idle time: `{idle_mins}m` (expires at `{sess_cfg.idle_timeout_minutes}m`)"
            )
        await update.effective_message.reply_text(
            "\n".join(lines),
            reply_markup=build_session_footer_keyboard(s_key),
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

        status_msg = await msg.reply_text(
            f"🔍 Investigating on `{self.config.server_name}` via `{active_mode}`..."
        )
        try:
            await chat.send_action(ChatAction.TYPING)
        except Exception:
            pass

        try:
            result = await self.router.handle_message(
                session_key=s_key,
                user_message=question,
                requester_user_id=user.id,
                requester_username=user.username or user.first_name or str(user.id),
                override_mode=override_mode,
            )
        except Exception as exc:
            err_text = redact_secrets(str(exc))
            await status_msg.edit_text(f"❌ **Agent Error (`{active_mode}`):**\n{err_text}")
            return

        max_turns = self.config.engine.session.max_turns_per_session
        sid_short = (result.native_session_id or "mem")[:10]
        if result.is_new_session:
            reason_note = (
                f" — auto-rotated: {result.session_rotation_reason}"
                if result.session_rotation_reason not in ("", "first_turn")
                else ""
            )
            session_badge = f"🆕 Session `{sid_short}` (Turn {result.session_turn}/{max_turns}{reason_note})"
        else:
            session_badge = f"🔗 Resumed `{sid_short}` (Turn {result.session_turn}/{max_turns})"

        header = f"🤖 **[{result.engine_used} • {result.provider_used}]**\n{session_badge}\n\n"
        full_reply = header + result.answer
        chunks = split_telegram_message(full_reply)

        footer_markup = (
            build_session_footer_keyboard(s_key)
            if self.config.engine.session.show_new_session_button
            else None
        )

        if len(chunks) == 1:
            await status_msg.edit_text(chunks[0], reply_markup=footer_markup)
        else:
            await status_msg.edit_text(chunks[0])
            for extra_chunk in chunks[1:-1]:
                await msg.reply_text(extra_chunk)
            await msg.reply_text(chunks[-1], reply_markup=footer_markup)

        # Render interactive [Approve] / [Reject] buttons for any gated actions queued during the turn
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
            await msg.reply_text(
                card_text,
                reply_markup=build_approval_keyboard(action["action_id"]),
            )

    # -------------------------------------------------------------------------
    # Callback Query Handler (New Session Button + Admin Approval Buttons)
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

        # Any authorized viewer/admin can start a new session
        if decision == "new_session":
            chat_id = update.effective_chat.id if update.effective_chat else 0
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

        # Only Admins can approve or reject state-changing server actions
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
        return app

"""Inline-bot message protection for the primary Justice Faite group.

Uses Telegram Message.via_bot (not text matching) and stores owner settings in
SQLite. A matched message is stopped before normal handlers so it cannot enter
stats, archives, games, automod or other feature pipelines, even if Telegram
refuses deletion because permissions are missing.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

logger = logging.getLogger("jf_inline_guard")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


class InlineGuardState(StatesGroup):
    waiting_add = State()
    waiting_remove = State()


class InlineGuard:
    def __init__(self, *, bot, db_op: Callable, owner_id: int,
                 primary_chat_id: Callable[[], int], initialize: bool = True):
        self.bot = bot
        self.db_op = db_op
        self.owner_id = int(owner_id)
        self.primary_chat_id = primary_chat_id
        if initialize:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        def op(conn):
            conn.execute("""CREATE TABLE IF NOT EXISTS jf_inline_guard_settings (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
                mode TEXT NOT NULL DEFAULT 'selected' CHECK(mode IN ('selected','all')),
                updated_by INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS jf_inline_guard_bots (
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                added_by INTEGER NOT NULL DEFAULT 0,
                added_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(chat_id, username)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS jf_inline_guard_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                via_bot_id INTEGER NOT NULL DEFAULT 0,
                via_bot_username TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jf_inline_guard_events_time ON jf_inline_guard_events(created_at DESC)")
            conn.commit()
        self.db_op(op)

    def _chat_id(self) -> int:
        try:
            return int(self.primary_chat_id() or 0)
        except Exception:
            logger.exception("Could not resolve primary chat ID for inline guard")
            return 0

    def settings(self, chat_id: Optional[int] = None) -> dict:
        cid = int(self._chat_id() if chat_id is None else chat_id)
        row = self.db_op(lambda conn: conn.execute(
            "SELECT enabled,mode FROM jf_inline_guard_settings WHERE chat_id=?", (cid,)
        ).fetchone())
        if row is None:
            return {"chat_id": cid, "enabled": False, "mode": "selected"}
        return {"chat_id": cid, "enabled": bool(row[0]), "mode": str(row[1])}

    def set_settings(self, *, enabled: Optional[bool] = None,
                     mode: Optional[str] = None, actor_id: int = 0) -> dict:
        cid = self._chat_id()
        current = self.settings(cid)
        next_enabled = current["enabled"] if enabled is None else bool(enabled)
        next_mode = current["mode"] if mode is None else str(mode)
        if next_mode not in {"selected", "all"}:
            raise ValueError("Unknown inline guard mode")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        self.db_op(lambda conn: conn.execute(
            "INSERT INTO jf_inline_guard_settings(chat_id,enabled,mode,updated_by,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled,mode=excluded.mode,"
            "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (cid, 1 if next_enabled else 0, next_mode, int(actor_id or self.owner_id), stamp),
        ))
        return self.settings(cid)

    @staticmethod
    def normalize_username(value: str) -> Optional[str]:
        raw = str(value or "").strip()
        lowered = raw.casefold()
        if lowered.startswith(("https://t.me/", "http://t.me/")):
            raw = raw.split("/", 3)[-1]
            raw = raw.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0]
        raw = raw.lstrip("@").strip()
        if not USERNAME_RE.fullmatch(raw):
            return None
        return raw.casefold()

    def list_bots(self, chat_id: Optional[int] = None) -> list[str]:
        cid = int(self._chat_id() if chat_id is None else chat_id)
        rows = self.db_op(lambda conn: conn.execute(
            "SELECT username FROM jf_inline_guard_bots WHERE chat_id=? ORDER BY username COLLATE NOCASE",
            (cid,),
        ).fetchall())
        return [str(row[0]) for row in rows]

    def add_bot(self, value: str, *, actor_id: int = 0) -> tuple[bool, str]:
        username = self.normalize_username(value)
        if not username:
            return False, "Неверный формат. Отправь username бота, например @gif или @example_bot."
        cid = self._chat_id()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        cur = self.db_op(lambda conn: conn.execute(
            "INSERT OR IGNORE INTO jf_inline_guard_bots(chat_id,username,added_by,added_at) VALUES(?,?,?,?)",
            (cid, username, int(actor_id or self.owner_id), stamp),
        ))
        if cur.rowcount:
            return True, f"Добавлен @{username}."
        return False, f"@{username} уже есть в списке."

    def remove_bot(self, value: str) -> tuple[bool, str]:
        username = self.normalize_username(value)
        if not username:
            return False, "Неверный формат username. Пример: @gif."
        cur = self.db_op(lambda conn: conn.execute(
            "DELETE FROM jf_inline_guard_bots WHERE chat_id=? AND username=?",
            (self._chat_id(), username),
        ))
        if cur.rowcount:
            return True, f"Удалён @{username}."
        return False, f"@{username} не найден в списке."

    def matches(self, message: Message) -> bool:
        via_bot = getattr(message, "via_bot", None)
        if via_bot is None:
            return False
        chat = getattr(message, "chat", None)
        if not chat or getattr(chat, "type", None) not in ("group", "supergroup"):
            return False
        target_chat = self._chat_id()
        if target_chat >= 0 or int(getattr(chat, "id", 0) or 0) != target_chat:
            return False
        cfg = self.settings(target_chat)
        if not cfg["enabled"]:
            return False
        if cfg["mode"] == "all":
            return True
        username = self.normalize_username(getattr(via_bot, "username", "") or "")
        return bool(username and username in set(self.list_bots(target_chat)))

    def recent_events(self, limit: int = 10) -> list[tuple]:
        cid = self._chat_id()
        limit = max(1, min(int(limit), 30))
        rows = self.db_op(lambda conn: conn.execute(
            "SELECT message_id,via_bot_username,outcome,error,created_at "
            "FROM jf_inline_guard_events WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (cid, limit),
        ).fetchall())
        return [tuple(row) for row in rows]

    def log_view(self) -> tuple[str, InlineKeyboardMarkup]:
        from html import escape
        events = self.recent_events(15)
        lines = ["<b>🧾 История inline-защиты</b>", ""]
        if not events:
            lines.append("<i>Событий пока нет.</i>")
        for message_id, username, outcome, error, created_at in events:
            status = "✅ удалено" if outcome == "deleted" else "⚠️ ошибка удаления"
            name = "@" + escape(username) if username else "inline-бот без username"
            lines.append(f"• {escape(str(created_at))} · {name} · сообщение <code>{int(message_id)}</code> · {status}")
            if error:
                lines.append("  <i>" + escape(str(error)[:180]) + "</i>")
        lines.extend(["", "Хранится до 500 последних событий. Текст самих сообщений не записывается."])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить журнал", callback_data="ig:log")],
            [InlineKeyboardButton(text="‹ К защите inline-ботов", callback_data="ig:refresh")],
        ])

    def _record(self, message: Message, outcome: str, error: str = "") -> None:
        via_bot = getattr(message, "via_bot", None)
        try:
            self.db_op(lambda conn: (
                conn.execute(
                    "INSERT INTO jf_inline_guard_events(chat_id,message_id,via_bot_id,via_bot_username,outcome,error,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (int(message.chat.id), int(message.message_id), int(getattr(via_bot, "id", 0) or 0),
                     str(getattr(via_bot, "username", "") or "").casefold(), outcome,
                     str(error)[:500], time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())),
                ),
                conn.execute("DELETE FROM jf_inline_guard_events WHERE id NOT IN "
                             "(SELECT id FROM jf_inline_guard_events ORDER BY id DESC LIMIT 500)"),
            ))
        except Exception:
            logger.exception("Could not record inline guard event")

    async def middleware(self, handler, event, data):
        """Early message middleware. Stops matched messages before any feature handler."""
        if not self.matches(event):
            return await handler(event, data)
        try:
            await self.bot.delete_message(int(event.chat.id), int(event.message_id))
            self._record(event, "deleted")
            logger.info("Inline message deleted | chat=%s | message=%s | via_bot=@%s",
                        event.chat.id, event.message_id,
                        getattr(getattr(event, "via_bot", None), "username", "unknown"))
        except Exception as exc:
            self._record(event, "delete_failed", str(exc))
            logger.warning(
                "Inline message matched but Telegram deletion failed | chat=%s | message=%s | via_bot=@%s | error=%s",
                getattr(getattr(event, "chat", None), "id", 0),
                getattr(event, "message_id", 0),
                getattr(getattr(event, "via_bot", None), "username", "unknown"), exc,
            )
        # Never let a matched inline result reach stats, archive, automod or
        # command dispatch, even when Telegram rejected the delete request.
        return None

    async def check_delete_permission(self) -> tuple[bool, str]:
        """Check the bot's actual Telegram status and delete permission in the target group."""
        chat_id = self._chat_id()
        if chat_id >= 0:
            return False, "Не задан корректный PRIMARY_CHAT_ID основного супергруппового чата."
        try:
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(chat_id, int(me.id))
        except Exception as exc:
            logger.warning("Could not verify inline deletion permission: %s", exc)
            return False, "Не удалось проверить права через Telegram. Убедись, что бот добавлен в основной чат администратором."

        status = str(getattr(member, "status", "") or "")
        if status == "creator":
            return True, "У бота есть статус создателя чата."
        if status != "administrator":
            return False, "Бот не является администратором основного чата. Назначь его администратором с правом удаления сообщений."
        if not bool(getattr(member, "can_delete_messages", False)):
            return False, "Бот администратор, но право «Удаление сообщений» выключено. Включи его в правах администратора Telegram."
        return True, "Права подтверждены: бот может удалять сообщения в основном чате."

    def view(self) -> tuple[str, InlineKeyboardMarkup]:
        from html import escape
        cfg = self.settings()
        bots = self.list_bots()
        lines = [
            "<b>🛡 Защита от inline-ботов</b>",
            "",
            f"Состояние: {'🟢 включена' if cfg['enabled'] else '⚫ выключена'}",
            f"Режим: {'все inline-боты' if cfg['mode'] == 'all' else 'только выбранные'}",
            f"Список: {len(bots)} ботов",
            "",
        ]
        if bots:
            lines.extend("• <code>@" + escape(name) + "</code>" for name in bots[:30])
            if len(bots) > 30:
                lines.append(f"… и ещё {len(bots) - 30}")
        else:
            lines.append("<i>Список пока пуст.</i>")
        lines.extend([
            "",
            "Бот проверяет Telegram-поле via_bot, а не ищет @упоминания в тексте.",
            "Удаление работает только в основном флуде и требует право администратора на удаление сообщений.",
            "Перед включением бот проверяет это право через Telegram.",
        ])
        enabled_label = "⏸ Выключить" if cfg["enabled"] else "▶️ Включить"
        mode_label = ("Режим: выбранные → все" if cfg["mode"] == "selected"
                      else "Режим: все → выбранные")
        rows = [
            [InlineKeyboardButton(text=enabled_label, callback_data="ig:toggle")],
            [InlineKeyboardButton(text=mode_label, callback_data="ig:mode:toggle")],
            [InlineKeyboardButton(text="🔐 Проверить права удаления", callback_data="ig:check")],
            [InlineKeyboardButton(text="🧾 История действий", callback_data="ig:log")],
            [InlineKeyboardButton(text="➕ Добавить бота", callback_data="ig:add"),
             InlineKeyboardButton(text="➖ Убрать бота", callback_data="ig:remove")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="ig:refresh")],
        ]
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    async def open_panel(self, message: Message, state: FSMContext):
        if not message.from_user or int(message.from_user.id) != self.owner_id or message.chat.type != "private":
            return
        await state.clear()
        text, markup = self.view()
        await message.answer(text, reply_markup=markup, parse_mode="HTML")

    async def on_callback(self, callback: CallbackQuery, state: FSMContext):
        if not callback.from_user or int(callback.from_user.id) != self.owner_id:
            await callback.answer("Только для владельца.", show_alert=True)
            return
        action = (callback.data or "").split(":")
        if len(action) < 2:
            await callback.answer()
            return
        if action[1] == "toggle":
            cfg = self.settings()
            next_enabled = not cfg["enabled"]
            if next_enabled:
                allowed, details = await self.check_delete_permission()
                if not allowed:
                    await callback.answer(details, show_alert=True)
                    return
            self.set_settings(enabled=next_enabled, actor_id=callback.from_user.id)
            await callback.answer("Защита включена." if next_enabled else "Защита выключена.")
        elif action[1] == "check":
            allowed, details = await self.check_delete_permission()
            await callback.answer(("✅ " if allowed else "⚠️ ") + details, show_alert=True)
            return
        elif action[1] == "mode":
            cfg = self.settings()
            if cfg["mode"] == "selected":
                await callback.message.edit_text(
                    "<b>Переключить защиту на все inline-боты?</b>\n\n"
                    "В этом режиме будут удаляться сообщения от любого inline-бота в основном флуде, "
                    "а не только от usernames в списке.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="Да, все inline-боты", callback_data="ig:mode_apply:all"),
                        InlineKeyboardButton(text="Отмена", callback_data="ig:refresh"),
                    ]]),
                    parse_mode="HTML",
                )
                await callback.answer()
                return
            self.set_settings(mode="selected", actor_id=callback.from_user.id)
            await callback.answer("Режим: только выбранные боты.")
        elif action[1] == "mode_apply":
            target_mode = action[2] if len(action) > 2 else ""
            if target_mode != "all":
                await callback.answer("Неизвестный режим.", show_alert=True)
                return
            self.set_settings(mode="all", actor_id=callback.from_user.id)
            await callback.answer("Режим: все inline-боты.")
        elif action[1] == "log":
            text, markup = self.log_view()
            await self._show_callback(callback, (text, markup))
            await callback.answer()
            return
        elif action[1] == "add":
            await state.set_state(InlineGuardState.waiting_add)
            await callback.message.answer("Отправь username inline-бота, например @gif. Можно также отправить t.me-ссылку. Отмена: /cancel")
            await callback.answer()
            return
        elif action[1] == "remove":
            await state.set_state(InlineGuardState.waiting_remove)
            await callback.message.answer("Отправь username бота, которого нужно убрать из списка. Отмена: /cancel")
            await callback.answer()
            return
        else:
            await callback.answer()
        await self._show_callback(callback)

    async def _show_callback(self, callback: CallbackQuery, view=None):
        text, markup = view if view is not None else self.view()
        try:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception as exc:
            if "message is not modified" not in str(exc).casefold():
                logger.debug("Inline guard panel edit failed: %s", exc)

    async def on_text(self, message: Message, state: FSMContext, *, remove: bool = False):
        if not message.from_user or int(message.from_user.id) != self.owner_id:
            return
        raw = (message.text or "").strip()
        if raw.casefold() in {"/cancel", "-", "отмена"}:
            await state.clear()
            await message.answer("Действие отменено.")
            return
        ok, result = self.remove_bot(raw) if remove else self.add_bot(raw, actor_id=message.from_user.id)
        await state.clear()
        await message.answer(("✅ " if ok else "ℹ️ ") + result)
        text, markup = self.view()
        await message.answer(text, reply_markup=markup, parse_mode="HTML")


def register(dp, *, bot, db_op: Callable, owner_id: int,
             primary_chat_id: Callable[[], int]) -> InlineGuard:
    # Do not create SQLite files during module import: startup must inspect and,
    # if needed, restore the original database before any schema writes occur.
    guard = InlineGuard(bot=bot, db_op=db_op, owner_id=owner_id,
                        primary_chat_id=primary_chat_id, initialize=False)
    private_owner = (F.chat.type == "private") & (F.from_user.id == int(owner_id))

    async def add_text(message: Message, state: FSMContext):
        await guard.on_text(message, state, remove=False)

    async def remove_text(message: Message, state: FSMContext):
        await guard.on_text(message, state, remove=True)

    dp.message.outer_middleware(guard.middleware)
    dp.message.register(guard.open_panel, Command("inline_guard"), private_owner)
    dp.message.register(add_text, InlineGuardState.waiting_add, private_owner, F.text)
    dp.message.register(remove_text, InlineGuardState.waiting_remove, private_owner, F.text)
    dp.callback_query.register(guard.on_callback, F.data.startswith("ig:"))
    return guard

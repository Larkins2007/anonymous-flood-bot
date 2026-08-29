"""Justice Faite-specific features layered on top of the existing bot.

This module deliberately avoids cloning Iris commands. It owns community-only
features: admission workflow, persistent age-document review, rewards,
old/active checks, warning lifecycle, rest/birthday records, a dashboard,
and optional Iris hand-off for awards.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError


# Runtime-bound in register_justice_features().
_bot = None
_dp = None
_db = None
_db_op = None
_now = None
_role_for = None
_normalize_role = None
_group_member_is_active = None
_upsert_group_member = None
_get_member = None
_assign_role_db_atomic = None
_apply_member_tag = None
_finalize_role_assignment = None
_confirm_member = None
_lift_member_restriction = None
_send_or_edit_welcome = None
_display_username = None
_primary_chat_id = None
_is_primary_chat = None
_admin_id = None
_register_user = None

FEATURE_PREFIX = "jf:"
IRIS_USERNAME = os.getenv("IRIS_BOT_USERNAME", "").strip().lstrip("@")
IRIS_AUTO_AWARDS = os.getenv("IRIS_AUTO_AWARDS", "0") == "1"
IRIS_AWARD_LEVEL = max(1, min(8, int(os.getenv("IRIS_AWARD_LEVEL", "1"))))
IRIS_OLD_AWARD_TEXT = os.getenv("IRIS_OLD_AWARD_TEXT", "Старожил Justice Faite")
IRIS_ACTIVE_AWARD_TEXT = os.getenv("IRIS_ACTIVE_AWARD_TEXT", "Активный участник Justice Faite")
IRIS_COMMAND = os.getenv("IRIS_AWARD_COMMAND", "Наградить")
INVITE_TTL_HOURS = max(1, min(168, int(os.getenv("JF_INVITE_TTL_HOURS", "24"))))
ANON_COOLDOWN_SECONDS = max(0, int(os.getenv("JF_ANON_COOLDOWN_SECONDS", "300")))
WELCOME_RULES_URL = os.getenv("JF_RULES_URL", "").strip()

ROLE_SCAN_LIMIT = 180


class BirthdayState(StatesGroup):
    waiting_date = State()


def _q(fn, *args):
    if _db_op is not None:
        return _db_op(fn, *args)
    return fn(_db(), *args)


def _utc_iso() -> str:
    return _now() if _now else datetime.now(timezone.utc).isoformat()


def init_justice_features_db() -> None:
    def op(conn):
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jf_invites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invite_link TEXT NOT NULL UNIQUE,
                chat_id INTEGER NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                used_by INTEGER,
                used_at TEXT,
                revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jf_invites_status ON jf_invites(status);
            CREATE INDEX IF NOT EXISTS idx_jf_invites_user ON jf_invites(used_by);

            CREATE TABLE IF NOT EXISTS jf_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                invite_id INTEGER,
                invite_link TEXT,
                requested_role TEXT NOT NULL DEFAULT '',
                requested_role_key TEXT NOT NULL DEFAULT '',
                birth_date TEXT NOT NULL DEFAULT '',
                document_file_id TEXT NOT NULL DEFAULT '',
                document_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'awaiting_data',
                join_request_chat_id INTEGER,
                join_request_user_chat_id INTEGER,
                join_request_at TEXT,
                reviewed_by INTEGER,
                reviewed_at TEXT,
                review_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jf_applications_user_status ON jf_applications(user_id,status);
            CREATE INDEX IF NOT EXISTS idx_jf_applications_chat_status ON jf_applications(chat_id,status);

            CREATE TABLE IF NOT EXISTS jf_warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                level INTEGER NOT NULL,
                reason TEXT NOT NULL,
                issued_by INTEGER NOT NULL,
                issued_at TEXT NOT NULL,
                eligible_at TEXT,
                removed_at TEXT,
                removed_by INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_jf_warnings_user ON jf_warnings(chat_id,user_id,removed_at);

            CREATE TABLE IF NOT EXISTS jf_achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                issued_at TEXT NOT NULL,
                issued_by INTEGER,
                source TEXT NOT NULL DEFAULT 'manual',
                UNIQUE(chat_id,user_id,code)
            );

            CREATE TABLE IF NOT EXISTS jf_rest (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(chat_id,user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_jf_rest_end ON jf_rest(chat_id,end_at,active);

            CREATE TABLE IF NOT EXISTS jf_birthdays (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                month INTEGER NOT NULL,
                day INTEGER NOT NULL,
                year INTEGER,
                created_at TEXT NOT NULL,
                PRIMARY KEY(chat_id,user_id)
            );

            CREATE TABLE IF NOT EXISTS jf_anonymous_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                replied_at TEXT,
                last_user_sent_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jf_anon_status ON jf_anonymous_messages(status,created_at);

            CREATE TABLE IF NOT EXISTS jf_award_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                external_ref TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS jf_birthday_notices (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                year INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY(chat_id,user_id,year)
            );
        """
        )
        conn.commit()
    _q(op)


def _admin_only(message: Message) -> bool:
    return bool(message.from_user and int(message.from_user.id) == int(_admin_id))


def _mention_user(user_id: int, name: str, username: str | None) -> str:
    if username:
        return f"@{username}"
    safe = (name or "участник").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={int(user_id)}">{safe}</a>'


def _role_candidates_from_catalog() -> list[tuple[str, str, str]]:
    out = []
    try:
        from bot import ROLE_CATALOG
        for name, english, region in ROLE_CATALOG:
            out.append((name, english, region))
    except Exception:
        pass
    return out


def find_requested_role(text: str):
    raw = (text or "").strip()
    if not raw:
        return None
    # Prefer exact / longest role names; this allows natural sentences and any order.
    hay = re.sub(r"\s+", " ", raw.casefold()).strip()
    candidates = _role_candidates_from_catalog()
    candidates.sort(key=lambda x: max(len(x[0]), len(x[1])), reverse=True)
    for name, english, region in candidates[:ROLE_SCAN_LIMIT]:
        names = [name, english, english.replace(" ", "")]
        for candidate in names:
            c = re.sub(r"\s+", " ", candidate.casefold()).strip()
            if c and c in hay:
                role = _role_for(name)
                if role:
                    return role
    return None


def pending_application(user_id: int, chat_id: int | None = None):
    def op(conn):
        if chat_id is None:
            return conn.execute(
                "SELECT * FROM jf_applications WHERE user_id=? AND status IN ('awaiting_data','ready','pending_review') ORDER BY id DESC LIMIT 1",
                (int(user_id),),
            ).fetchone()
        return conn.execute(
            "SELECT * FROM jf_applications WHERE user_id=? AND chat_id=? AND status IN ('awaiting_data','ready','pending_review') ORDER BY id DESC LIMIT 1",
            (int(user_id), int(chat_id)),
        ).fetchone()
    return _q(op)


def _latest_requested_role_for_chat(chat_id: int):
    def op(conn):
        row = conn.execute(
            "SELECT requested_role,requested_role_key FROM jf_applications WHERE chat_id=? AND status IN ('awaiting_data','ready','pending_review') AND requested_role!='' ORDER BY id DESC LIMIT 1",
            (int(chat_id),),
        ).fetchone()
        if not row:
            return None
        return _role_for(row["requested_role"]) if row["requested_role"] else None
    return _q(op)


def latest_requested_role_for_chat(chat_id: int):
    return _latest_requested_role_for_chat(chat_id)


def _set_application_field(application_id: int, **fields):
    allowed = {
        "requested_role", "requested_role_key", "birth_date", "document_file_id",
        "document_message_id", "status", "reviewed_by", "reviewed_at",
        "review_reason", "join_request_at", "join_request_chat_id",
        "join_request_user_chat_id", "updated_at", "document_message_id"
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    fields["updated_at"] = _utc_iso()
    def op(conn):
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE jf_applications SET {sets} WHERE id=?", (*fields.values(), int(application_id)))
        conn.commit()
    _q(op)


def _application_ready(row) -> bool:
    return bool(row and str(row["requested_role"] or "").strip() and str(row["document_file_id"] or "").strip())


def _application_text(row, user=None) -> str:
    username = getattr(user, "username", None) if user is not None else None
    first_name = getattr(user, "first_name", "") if user is not None else ""
    last_name = getattr(user, "last_name", "") if user is not None else ""
    display = (f"@{username}" if username else (f"{first_name} {last_name}".strip() or "участник"))
    role = row["requested_role"] or "не указана"
    status_map = {
        "awaiting_data": "ожидание данных",
        "ready": "данные получены",
        "pending_review": "на проверке",
        "approved": "одобрена",
        "declined": "отклонена",
    }
    return (
        "༺ 𓆩 ✧ 𓆪 ༻\n\n"
        "🌸 Justice Faite · заявка\n\n"
        f"👤 {display}\n"
        f"🎭 Роль: {role}\n"
        f"🎂 Дата рождения: {row['birth_date'] or 'не указана'}\n"
        f"📄 Документ: {'получен' if row['document_file_id'] else 'ожидается'}\n"
        f"📌 Статус: {status_map.get(row['status'], row['status'])}\n\n"
        "༺ 𓆩 ✧ 𓆪 ༻"
    )


def _application_admin_kb(app_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить вступление", callback_data=f"{FEATURE_PREFIX}app_ok:{app_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"{FEATURE_PREFIX}app_no:{app_id}"),
        ],
        [InlineKeyboardButton(text="📄 Запросить документ ещё раз", callback_data=f"{FEATURE_PREFIX}app_doc:{app_id}")],
    ])


async def create_invite(message: Message):
    if not _admin_only(message):
        return
    try:
        expire_dt = datetime.now(timezone.utc) + timedelta(hours=INVITE_TTL_HOURS)
        link = await _bot.create_chat_invite_link(
            chat_id=int(_primary_chat_id),
            name=f"JF-{int(time.time())}",
            expire_date=int(expire_dt.timestamp()),
            creates_join_request=True,
        )
    except Exception as exc:
        await message.reply(
            "༺ 𓆩 ✧ 𓆪 ༻\n\n"
            "❌ Не удалось создать ссылку.\n\n"
            "Проверь, что бот является администратором флуда и имеет право приглашать пользователей."
        )
        return
    def op(conn):
        cur = conn.execute(
            "INSERT INTO jf_invites(invite_link,chat_id,created_by,created_at,expires_at,status) VALUES(?,?,?,?,?,'active')",
            (link.invite_link, int(_primary_chat_id), int(message.from_user.id), _utc_iso(), expire_dt.isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    invite_id = _q(op)
    await message.reply(
        "༺ 𓆩 ✧ 𓆪 ༻\n\n"
        "🌸 Новая ссылка Justice Faite\n\n"
        f"{link.invite_link}\n\n"
        f"⏳ Действует до: {expire_dt.astimezone().strftime('%d.%m.%Y %H:%M')}\n"
        "✦ Ссылка предназначена для одного нового кандидата и автоматически отзывается после первой заявки.\n\n"
        f"ID заявки-приглашения: {invite_id}"
    )


async def applications_list(message: Message):
    if not _admin_only(message):
        return
    rows = _q(lambda conn: conn.execute(
        "SELECT * FROM jf_applications WHERE chat_id=? AND status IN ('awaiting_data','ready','pending_review') ORDER BY id DESC LIMIT 25",
        (int(_primary_chat_id),),
    ).fetchall())
    if not rows:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n🌸 Сейчас нет активных заявок.")
        return
    lines = ["༺ 𓆩 ✧ 𓆪 ༻", "", "🌸 Заявки Justice Faite", ""]
    for row in rows:
        status = "✅" if row["status"] == "ready" else "🟡"
        lines.append(f"{status} #{row['id']} · {row['requested_role'] or 'роль не указана'} · {'документ' if row['document_file_id'] else 'без документа'}")
    await message.reply("\n".join(lines))



async def handle_join_request(event: ChatJoinRequest):
    if not _is_primary_chat(event.chat.id):
        return
    user = event.from_user
    if getattr(user, "is_bot", False):
        with suppress(Exception):
            await _bot.decline_chat_join_request(event.chat.id, user.id)
        return
    _register_user(user)
    invite = event.invite_link
    invite_text = getattr(invite, "invite_link", "") if invite else ""
    invite_row = None
    if invite_text:
        invite_row = _q(lambda conn: conn.execute(
            "SELECT * FROM jf_invites WHERE invite_link=? LIMIT 1", (invite_text,)
        ).fetchone())
    if invite_row:
        def op(conn):
            updated = conn.execute(
                "UPDATE jf_invites SET status='used',used_by=?,used_at=? WHERE id=? AND status='active'",
                (int(user.id), _utc_iso(), int(invite_row["id"])),
            ).rowcount
            conn.commit()
            return updated == 1
        used_now = _q(op)
        if used_now:
            with suppress(Exception):
                await _bot.revoke_chat_invite_link(event.chat.id, invite_text)
            # Rotate the admission link automatically: every accepted/used
            # personal link is replaced by a fresh one for the next candidate.
            try:
                expire_dt = datetime.now(timezone.utc) + timedelta(hours=INVITE_TTL_HOURS)
                replacement = await _bot.create_chat_invite_link(
                    chat_id=int(_primary_chat_id),
                    name=f"JF-{int(time.time())}",
                    expire_date=int(expire_dt.timestamp()),
                    creates_join_request=True,
                )
                _q(lambda conn: (conn.execute(
                    "INSERT OR IGNORE INTO jf_invites(invite_link,chat_id,created_by,created_at,expires_at,status) VALUES(?,?,?,?,?,'active')",
                    (replacement.invite_link, int(_primary_chat_id), int(_admin_id), _utc_iso(), expire_dt.isoformat()),
                ), conn.commit()))
                with suppress(Exception):
                    await _bot.send_message(
                        _admin_id,
                        title("Новая ссылка на вступление") + "\n\n"
                        + bullet(replacement.invite_link) + "\n"
                        + bullet(f"Действует до {expire_dt.astimezone().strftime('%d.%m.%Y %H:%M')}")
                    )
            except Exception:
                pass

    def create_app(conn):
        old = conn.execute(
            "SELECT * FROM jf_applications WHERE user_id=? AND chat_id=? AND status IN ('awaiting_data','ready','pending_review') ORDER BY id DESC LIMIT 1",
            (int(user.id), int(event.chat.id)),
        ).fetchone()
        if old:
            conn.execute(
                "UPDATE jf_applications SET join_request_chat_id=?,join_request_user_chat_id=?,join_request_at=?,invite_id=?,invite_link=?,updated_at=? WHERE id=?",
                (int(event.chat.id), int(event.user_chat_id), _utc_iso(), invite_row["id"] if invite_row else None, invite_text, _utc_iso(), int(old["id"])),
            )
            conn.commit()
            return old["id"]
        cur = conn.execute(
            "INSERT INTO jf_applications(user_id,chat_id,invite_id,invite_link,status,join_request_chat_id,join_request_user_chat_id,join_request_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (int(user.id), int(event.chat.id), invite_row["id"] if invite_row else None, invite_text, 'awaiting_data', int(event.chat.id), int(event.user_chat_id), _utc_iso(), _utc_iso(), _utc_iso()),
        )
        conn.commit()
        return cur.lastrowid
    app_id = _q(create_app)

    # Telegram explicitly allows a bot to use user_chat_id from a join request
    # for private messaging for up to 5 minutes; we use it immediately.
    with suppress(Exception):
        await _bot.send_message(
            event.user_chat_id,
            "༺ 𓆩 ✧ 𓆪 ༻\n\n"
            "🌸 Добро пожаловать в Justice Faite\n\n"
            "Чтобы администрация могла рассмотреть вступление, отправьте сюда в любом порядке:\n\n"
            "✦ желаемую роль;\n"
            "✦ фотографию документа, подтверждающего, что вам есть 16 лет.\n\n"
            "Можно скрыть все данные документа, кроме даты рождения.\n"
            "Фотография документа будет передана только администрации.\n\n"
            "Порядок не важен: сначала роль, потом документ — или наоборот."
        )

    row = pending_application(user.id, event.chat.id)
    # Send an admin application card immediately; document will be forwarded later.
    try:
        admin_msg = await _bot.send_message(_admin_id, _application_text(row, user), reply_markup=_application_admin_kb(app_id))
        _set_application_field(app_id, status=row["status"])
    except Exception:
        pass


async def _forward_document_to_admin(message: Message, app_row):
    user = message.from_user
    caption = (
        "༺ 𓆩 ✧ 𓆪 ༻\n\n"
        "📄 Документ кандидата Justice Faite\n\n"
        f"👤 {(('@' + user.username) if user.username else ((user.first_name or '') + ' ' + (user.last_name or '')).strip() or 'участник')}\n"
        f"🎭 Желаемая роль: {app_row['requested_role'] or 'не указана'}\n"
        f"📝 Заявка: #{app_row['id']}\n\n"
        "✦ Документ предназначен только для проверки возраста администрацией."
    )
    try:
        sent = await _bot.send_photo(_admin_id, message.photo[-1].file_id, caption=caption)
    except Exception:
        # As requested, the age proof MUST reach admin; if photo forwarding fails,
        # do not mark it as accepted.
        raise
    _set_application_field(app_row["id"], document_file_id=message.photo[-1].file_id, document_message_id=sent.message_id)
    return sent


async def application_private_photo(message: Message):
    app = pending_application(message.from_user.id)
    if not app or message.chat.type != "private":
        return False
    try:
        await _forward_document_to_admin(message, app)
    except Exception:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n❌ Не удалось передать документ администрации. Отправьте фото ещё раз.")
        return True
    status = "ready" if _application_ready(_q(lambda conn: conn.execute("SELECT * FROM jf_applications WHERE id=?", (int(app["id"]),)).fetchone())) else "awaiting_data"
    _set_application_field(app["id"], status=status)
    if status == "ready":
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n✅ Документ получен и передан администрации. Заявка готова к рассмотрению.")
    else:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n✅ Документ получен и передан администрации. Теперь укажите желаемую роль.")
    return True


async def application_private_text(message: Message):
    app = pending_application(message.from_user.id)
    if not app or message.chat.type != "private":
        return False
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return False
    role = find_requested_role(text)
    if role:
        # Never tell the applicant who owns a conflicting role.
        role_key = _normalize_role(role["name"])
        occupied = _q(lambda conn: conn.execute(
            "SELECT 1 FROM role_state WHERE chat_id=? AND role_key=? AND status='occupied' LIMIT 1",
            (int(app["chat_id"]), role_key),
        ).fetchone() is not None)
        if occupied:
            await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n⚠️ Эта роль сейчас недоступна. Укажите другую желаемую роль.")
            return True
        _set_application_field(app["id"], requested_role=role["name"], requested_role_key=role_key)
    fresh = _q(lambda conn: conn.execute("SELECT * FROM jf_applications WHERE id=?", (int(app["id"]),)).fetchone())
    if _application_ready(fresh):
        _set_application_field(app["id"], status="ready")
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n✅ Данные получены. Заявка передана администрации на рассмотрение.")
    elif role:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n🎭 Роль сохранена. Теперь отправьте фотографию документа, подтверждающего возраст 16+.")
    else:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\nНе удалось определить роль. Напишите название персонажа целиком.")
    return True


async def application_callback(callback: CallbackQuery):
    if callback.from_user.id != int(_admin_id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    data = callback.data or ""
    action, raw_id = data.split(":")[-2:]
    try:
        app_id = int(raw_id)
    except ValueError:
        await callback.answer("Некорректная заявка.", show_alert=True)
        return
    app = _q(lambda conn: conn.execute("SELECT * FROM jf_applications WHERE id=?", (app_id,)).fetchone())
    if not app:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if action == "app_doc":
        try:
            await _bot.send_message(app["join_request_user_chat_id"], "༺ 𓆩 ✧ 𓆪 ༻\n\n📄 Администрации нужен действительный документ с видимой датой рождения. Отправьте фото сюда ещё раз.")
        except Exception:
            pass
        await callback.answer("Запрос отправлен.")
        return
    if action == "app_no":
        try:
            await _bot.decline_chat_join_request(app["chat_id"], app["user_id"])
            status = "declined"
            await _bot.send_message(app["join_request_user_chat_id"], "༺ 𓆩 ✧ 𓆪 ༻\n\n❌ Заявка на вступление отклонена администрацией.")
        except Exception as exc:
            await callback.answer("Не удалось отклонить заявку.", show_alert=True)
            return
        _set_application_field(app_id, status=status, reviewed_by=int(_admin_id), reviewed_at=_utc_iso())
        with suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Заявка отклонена.")
        return
    if action == "app_ok":
        if not _application_ready(app):
            await callback.answer("Сначала нужны роль и документ.", show_alert=True)
            return
        try:
            await _bot.approve_chat_join_request(app["chat_id"], app["user_id"])
        except Exception:
            await callback.answer("Не удалось одобрить вступление. Возможно, заявка уже обработана.", show_alert=True)
            return
        _set_application_field(app_id, status="approved", reviewed_by=int(_admin_id), reviewed_at=_utc_iso())
        with suppress(Exception):
            await _bot.send_message(app["join_request_user_chat_id"], "༺ 𓆩 ✧ 𓆪 ༻\n\n✅ Заявка одобрена. Добро пожаловать в Justice Faite!\n\nПосле входа ваша выбранная роль будет обработана автоматически.")
        with suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Вступление одобрено.")


async def auto_bind_requested_role(chat_id: int, user):
    app = _q(lambda conn: conn.execute(
        "SELECT * FROM jf_applications WHERE user_id=? AND chat_id=? AND status='approved' AND requested_role!='' ORDER BY id DESC LIMIT 1",
        (int(user.id), int(chat_id)),
    ).fetchone())
    if not app:
        return False
    role = _role_for(app["requested_role"])
    if not role:
        return False
    try:
        tag, role_key = _assign_role_db_atomic(int(chat_id), user, role)
        ok, actual = await _apply_member_tag(int(chat_id), int(user.id), tag)
        if not ok:
            # assign_role_db_atomic may have reserved the role; release it atomically.
            try:
                from bot import release_role_assignment
                release_role_assignment(int(chat_id), int(user.id), role_key)
            except Exception:
                pass
            return False
        _finalize_role_assignment(int(chat_id), int(user.id), role_key, actual or tag)
        _confirm_member(int(chat_id), int(user.id))
        await _lift_member_restriction(int(chat_id), int(user.id))
        if _send_or_edit_welcome:
            await _send_or_edit_welcome(int(chat_id), int(user.id))
        await _bot.send_message(
            _admin_id,
            "༺ 𓆩 ✧ 𓆪 ༻\n\n"
            "✅ Роль привязана автоматически\n\n"
            f"Участник: {_display_username(user)}\n"
            f"Роль: {role['name']}\n"
            f"Тег: {actual or tag}"
        )
        _set_application_field(app["id"], status="approved")
        return True
    except ValueError as exc:
        if str(exc) == "ROLE_OCCUPIED":
            return False
    except Exception:
        return False
    return False


def warning_eligible_at(level: int, issued_at: str) -> Optional[str]:
    try:
        dt = datetime.fromisoformat(issued_at)
        days = 14 if level == 1 else 30 if level == 2 else None
        return (dt + timedelta(days=days)).isoformat() if days else None
    except Exception:
        return None


def active_warning_count(chat_id: int, user_id: int) -> int:
    row = _q(lambda conn: conn.execute(
        "SELECT COUNT(*) AS c FROM jf_warnings WHERE chat_id=? AND user_id=? AND removed_at IS NULL",
        (int(chat_id), int(user_id)),
    ).fetchone())
    return int(row["c"])


def issue_warning(chat_id: int, user_id: int, reason: str, issued_by: int):
    now = _utc_iso()
    def op(conn):
        level = int(conn.execute(
            "SELECT COALESCE(MAX(level),0)+1 AS n FROM jf_warnings WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        ).fetchone()["n"])
        eligible = warning_eligible_at(level, now)
        cur = conn.execute(
            "INSERT INTO jf_warnings(chat_id,user_id,level,reason,issued_by,issued_at,eligible_at) VALUES(?,?,?,?,?,?,?)",
            (int(chat_id), int(user_id), level, reason[:2000], int(issued_by), now, eligible),
        )
        # A new violation restarts the waiting period for all active warnings.
        for old in conn.execute("SELECT id,level FROM jf_warnings WHERE chat_id=? AND user_id=? AND removed_at IS NULL", (int(chat_id), int(user_id))).fetchall():
            conn.execute("UPDATE jf_warnings SET eligible_at=? WHERE id=?", (warning_eligible_at(int(old["level"]), now), int(old["id"])))
        conn.commit()
        return cur.lastrowid, level, eligible
    return _q(op)


def award_exists(chat_id: int, user_id: int, code: str) -> bool:
    return bool(_q(lambda conn: conn.execute(
        "SELECT 1 FROM jf_achievements WHERE chat_id=? AND user_id=? AND code=? LIMIT 1",
        (int(chat_id), int(user_id), code),
    ).fetchone()))


def issue_local_award(chat_id: int, user_id: int, code: str, title: str, description: str, issued_by: int | None, source: str):
    def op(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO jf_achievements(chat_id,user_id,code,title,description,issued_at,issued_by,source) VALUES(?,?,?,?,?,?,?,?)",
            (int(chat_id), int(user_id), code, title, description[:1000], _utc_iso(), issued_by, source),
        )
        conn.commit()
        return cur.rowcount > 0
    return bool(_q(op))




def iris_already_sent(chat_id: int, user_id: int, kind: str) -> bool:
    row = _q(lambda conn: conn.execute(
        "SELECT 1 FROM jf_award_audit WHERE chat_id=? AND user_id=? AND kind=? AND status IN ('sent','manual') LIMIT 1",
        (int(chat_id), int(user_id), kind),
    ).fetchone())
    return bool(row)

def record_iris_audit(chat_id: int, user_id: int, kind: str, status: str, reason: str, external_ref: str = "") -> None:
    _q(lambda conn: (conn.execute(
        "INSERT INTO jf_award_audit(chat_id,user_id,kind,status,reason,created_at,external_ref) VALUES(?,?,?,?,?,?,?)",
        (int(chat_id), int(user_id), kind, status, reason[:1000], _utc_iso(), external_ref[:200]),
    ), conn.commit()))

def compute_old_and_active_candidates(chat_id: int):
    rows = _q(lambda conn: conn.execute(
        "SELECT gm.user_id,gm.first_name,gm.last_name,gm.username,gm.joined_at,u.messages_count,u.last_seen FROM group_members gm LEFT JOIN users u ON u.user_id=gm.user_id WHERE gm.chat_id=? AND gm.active=1 AND gm.user_id!=? ORDER BY gm.joined_at ASC",
        (int(chat_id), int(_admin_id)),
    ).fetchall())
    now_dt = datetime.now(timezone.utc)
    old = []
    active = sorted(rows, key=lambda r: int(r["messages_count"] or 0), reverse=True)[:10]
    for row in rows:
        try:
            joined = datetime.fromisoformat(row["joined_at"])
            if joined.tzinfo is None:
                joined = joined.replace(tzinfo=timezone.utc)
            days = (now_dt - joined).days
        except Exception:
            days = 0
        if days >= 30:
            old.append(row)
    return old, active


def _pretty_dashboard(chat_id: int) -> str:
    rows = _q(lambda conn: conn.execute(
        "SELECT COUNT(*) AS c FROM group_members WHERE chat_id=? AND active=1", (int(chat_id),)
    ).fetchone())
    active = int(rows["c"])
    rest = _q(lambda conn: conn.execute(
        "SELECT COUNT(*) AS c FROM jf_rest WHERE chat_id=? AND active=1 AND end_at> ?", (int(chat_id), _utc_iso())
    ).fetchone())["c"]
    apps = _q(lambda conn: conn.execute(
        "SELECT COUNT(*) AS c FROM jf_applications WHERE chat_id=? AND status IN ('awaiting_data','ready','pending_review')", (int(chat_id),)
    ).fetchone())["c"]
    return (
        "༺ 𓆩 ✧ 𓆪 ༻\n\n"
        "🌸 LIFE OF JUSTICE FAITE\n\n"
        f"👥 Активных участников: {active}\n"
        f"🚪 Заявок: {apps}\n"
        f"🥹 В ресте: {rest}\n\n"
        "✦ Единый центр управления флудом"
    )


async def cmd_dashboard(message: Message):
    if not _admin_only(message) and message.chat.type not in {"group", "supergroup"}:
        return
    if message.chat.type in {"group", "supergroup"} and not _is_primary_chat(message.chat.id):
        return
    await message.reply(_pretty_dashboard(message.chat.id if message.chat.type in {"group", "supergroup"} else int(_primary_chat_id)))


async def cmd_awards_check(message: Message):
    if not _admin_only(message):
        return
    old, active = compute_old_and_active_candidates(int(_primary_chat_id))
    lines = ["༺ 𓆩 ✧ 𓆪 ༻", "", "🏆 Кандидаты на награды", ""]
    seen = set()
    for row in old:
        uid = int(row["user_id"])
        seen.add(uid)
        mark = "✅" if award_exists(_primary_chat_id, uid, "old_30d") else "🆕"
        name = _display_username(type("U", (), {"id": uid, "username": row["username"], "first_name": row["first_name"]})())
        lines.append(f"{mark} 🏵 {name} — старожил")
    for row in active[:5]:
        uid = int(row["user_id"])
        if uid in seen:
            continue
        mark = "✅" if award_exists(_primary_chat_id, uid, "active_top") else "🆕"
        name = _display_username(type("U", (), {"id": uid, "username": row["username"], "first_name": row["first_name"]})())
        lines.append(f"{mark} 🔥 {name} — топ активности ({int(row['messages_count'] or 0)} сообщений)")
    lines.extend(["", "✦ Для локальных достижений используйте /jf_award."])
    await message.reply("\n".join(lines))


async def cmd_award(message: Message):
    if not _admin_only(message):
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 4:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\nФормат:\n/jf_award @username code название | описание")
        return
    target_raw, code, rest = parts[1], parts[2], parts[3]
    if "|" in rest:
        award_title, desc = [x.strip() for x in rest.split("|", 1)]
    else:
        award_title, desc = rest.strip(), ""
    row = _q(lambda conn: conn.execute(
        "SELECT * FROM group_members WHERE chat_id=? AND username=? AND active=1 LIMIT 1",
        (int(_primary_chat_id), target_raw.lstrip("@").casefold()),
    ).fetchone())
    if not row:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\nУчастник не найден в базе флуда.")
        return
    created = issue_local_award(_primary_chat_id, row["user_id"], code, award_title, desc, _admin_id, "manual")
    await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n" + ("✅ Награда выдана в системе Justice Faite." if created else "ℹ️ Такая награда уже есть у участника."))


async def cmd_iris_sync(message: Message):
    if not _admin_only(message):
        return
    old, active = compute_old_and_active_candidates(int(_primary_chat_id))
    candidates = []
    for row in old:
        if not award_exists(_primary_chat_id, row["user_id"], "old_30d") and not iris_already_sent(_primary_chat_id, row["user_id"], "old_30d"):
            candidates.append((row, "old_30d", IRIS_OLD_AWARD_TEXT))
    for row in active[:5]:
        if not award_exists(_primary_chat_id, row["user_id"], "active_top") and not iris_already_sent(_primary_chat_id, row["user_id"], "active_top"):
            candidates.append((row, "active_top", IRIS_ACTIVE_AWARD_TEXT))
    if not candidates:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n✅ Новых кандидатов для синхронизации не найдено.")
        return
    commands = []
    for row, code, reason in candidates:
        username = (row["username"] or "").strip()
        if not username:
            continue
        commands.append(f"/{IRIS_COMMAND} {IRIS_AWARD_LEVEL} @{username}\n{reason}")
    if not commands:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n⚠️ У кандидатов нет username, поэтому безопасная автоматическая синхронизация невозможна.")
        return
    # We cannot read Iris' private database. Therefore the adapter is opt-in.
    # With bot-to-bot mode and auto enabled, commands are addressed directly to Iris.
    if IRIS_AUTO_AWARDS and IRIS_USERNAME:
        sent = 0
        for command in commands:
            try:
                command_to_iris = command.replace("/", f"/{IRIS_COMMAND}@{IRIS_USERNAME}", 1)
                await _bot.send_message(int(_primary_chat_id), command_to_iris)
                sent += 1
                # The external bot may still reject/ignore the command; keep a local audit so
                # we do not flood the chat with duplicate requests.
                for row2, code2, reason2 in candidates:
                    if (row2["username"] or "").strip() and f"@{(row2['username'] or '').strip()}" in command:
                        record_iris_audit(_primary_chat_id, row2["user_id"], code2, "sent", reason2, IRIS_USERNAME)
                        break
                await asyncio.sleep(0.7)
            except Exception:
                pass
        await message.reply(
            "༺ 𓆩 ✧ 𓆪 ༻\n\n"
            f"🟢 Передано Ирис: {sent}/{len(commands)}\n\n"
            "Если Ирис не принимает bot-to-bot команды, отключите автоматический режим и используйте сформированный пакет вручную."
        )
        return
    await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n🧾 Пакет команд для Ирис:\n\n" + "\n\n".join(commands))


async def cmd_warn(message: Message):
    if not _admin_only(message):
        return
    raw = (message.text or "").split(maxsplit=2)
    if len(raw) < 3:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\nФормат:\n/jf_warn @username причина")
        return
    username, reason = raw[1].lstrip("@"), raw[2].strip()
    row = _q(lambda conn: conn.execute(
        "SELECT * FROM group_members WHERE chat_id=? AND username=? AND active=1 LIMIT 1",
        (int(_primary_chat_id), username.casefold()),
    ).fetchone())
    if not row:
        await message.reply("Участник не найден.")
        return
    warn_id, level, eligible = issue_warning(_primary_chat_id, row["user_id"], reason, _admin_id)
    text = f"⚠️ Предупреждение #{level} выдано."
    if eligible:
        text += "\nСрок возможного снятия: " + eligible[:10] + "."
    await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n" + text)


async def cmd_warnings(message: Message):
    if not _admin_only(message):
        return
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2:
        await message.reply("Формат: /jf_warnings @username")
        return
    row = _q(lambda conn: conn.execute(
        "SELECT * FROM group_members WHERE chat_id=? AND username=? LIMIT 1",
        (int(_primary_chat_id), raw[1].lstrip("@").casefold()),
    ).fetchone())
    if not row:
        await message.reply("Участник не найден.")
        return
    warns = _q(lambda conn: conn.execute(
        "SELECT * FROM jf_warnings WHERE chat_id=? AND user_id=? ORDER BY id DESC",
        (int(_primary_chat_id), int(row["user_id"])),
    ).fetchall())
    if not warns:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n✅ Активных и снятых предупреждений нет.")
        return
    lines = ["༺ 𓆩 ✧ 𓆪 ༻", "", "⚠️ История предупреждений", ""]
    for w in warns[:20]:
        status = "снято" if w["removed_at"] else "активно"
        lines.append(f"#{w['id']} · {w['level']} уровень · {status}\n{w['reason']}\n{w['issued_at'][:10]}")
    await message.reply("\n\n".join(lines))


async def cmd_remove_warning(message: Message):
    if not _admin_only(message):
        return
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2 or not raw[1].isdigit():
        await message.reply("Формат: /jf_warn_remove ID")
        return
    wid = int(raw[1])
    def op(conn):
        row = conn.execute("SELECT * FROM jf_warnings WHERE id=?", (wid,)).fetchone()
        if not row:
            return None
        if row["removed_at"]:
            return False
        if row["eligible_at"]:
            try:
                eligible = datetime.fromisoformat(str(row["eligible_at"]))
                if eligible.tzinfo is None:
                    eligible = eligible.replace(tzinfo=timezone.utc)
                if eligible > datetime.now(timezone.utc):
                    return "early"
            except Exception:
                pass
        conn.execute("UPDATE jf_warnings SET removed_at=?,removed_by=? WHERE id=?", (_utc_iso(), int(_admin_id), wid))
        conn.commit()
        return True
    result = _q(op)
    if result is None:
        await message.reply("Предупреждение не найдено.")
    elif result is False:
        await message.reply("Оно уже снято.")
    elif result == "early":
        await message.reply("⏳ Срок снятия ещё не наступил по правилам флуда.")
    else:
        await message.reply("✅ Предупреждение снято.")


async def cmd_rest(message: Message):
    if not _admin_only(message):
        return
    raw = (message.text or "").split(maxsplit=3)
    if len(raw) < 3:
        await message.reply("Формат:\n/jf_rest @username 2026-09-15 заметка")
        return
    username, end_date = raw[1].lstrip("@"), raw[2]
    note = raw[3] if len(raw) > 3 else ""
    row = _q(lambda conn: conn.execute(
        "SELECT * FROM group_members WHERE chat_id=? AND username=? AND active=1 LIMIT 1", (int(_primary_chat_id), username.casefold())
    ).fetchone())
    if not row:
        await message.reply("Участник не найден.")
        return
    try:
        end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
    except Exception:
        await message.reply("Дата должна быть в формате YYYY-MM-DD.")
        return
    def op(conn):
        conn.execute(
            "INSERT INTO jf_rest(chat_id,user_id,start_at,end_at,note,created_by,created_at,active) VALUES(?,?,?,?,?,?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET end_at=excluded.end_at,note=excluded.note,created_by=excluded.created_by,created_at=excluded.created_at,active=1",
            (int(_primary_chat_id), int(row["user_id"]), _utc_iso(), end.isoformat(), note[:500], int(_admin_id), _utc_iso()),
        )
        conn.commit()
    _q(op)
    await message.reply("🥹 Рест обновлён.")


def _parse_birthday(value: str):
    raw = (value or "").strip()
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?", raw)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    year = int(m.group(3)) if m.group(3) else None
    try:
        # Use a leap year when validating a yearless birthday.
        datetime(year or 2024, month, day)
    except ValueError:
        return None
    return day, month, year


async def _save_birthday_for_user(user_id: int, value: str, state: FSMContext | None = None):
    parsed = _parse_birthday(value)
    if not parsed:
        return False
    day, month, year = parsed
    _q(lambda conn: (conn.execute(
        "INSERT INTO jf_birthdays(chat_id,user_id,month,day,year,created_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(chat_id,user_id) DO UPDATE SET month=excluded.month,day=excluded.day,year=excluded.year,created_at=excluded.created_at",
        (int(_primary_chat_id), int(user_id), month, day, year, _utc_iso()),
    ), conn.commit()))
    if state:
        await state.clear()
    return True


async def cmd_birthday(message: Message, state: FSMContext):
    if message.chat.type != "private" or not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=2)
    value = parts[1] if len(parts) == 2 else (parts[2] if len(parts) >= 3 and _admin_only(message) else "")
    target_id = message.from_user.id
    # Owner may optionally maintain another member's date: /jf_birthday @user DD.MM
    if len(parts) >= 3 and _admin_only(message):
        target = parts[1].lstrip("@")
        row = _q(lambda conn: conn.execute(
            "SELECT user_id FROM group_members WHERE chat_id=? AND username=? AND active=1 LIMIT 1",
            (int(_primary_chat_id), target.casefold()),
        ).fetchone())
        if not row:
            await message.reply(f"{title('День рождения')}\n\n{bullet('Участник не найден в активном составе флуда.')}")
            return
        target_id = int(row["user_id"])
        value = parts[2]

    if value:
        if await _save_birthday_for_user(target_id, value, state):
            await message.reply(f"{title('День рождения')}\n\n{bullet('Дата сохранена.')}\n{bullet(value.replace('-', '.').replace('/', '.'))}")
        else:
            await message.reply(f"{title('День рождения')}\n\n{bullet('Не удалось распознать дату. Используйте DD.MM.')}")
        return

    await state.set_state(BirthdayState.waiting_date)
    await message.reply(
        f"{title('День рождения')}\n\n"
        f"{bullet('Укажите дату в формате DD.MM.')}\n"
        f"{bullet('Год указывать не нужно.')}\n\n"
        f"{divider()}\n"
        "♡ Например: 24.03",
    )



async def _anonymous_send(message: Message):
    if message.chat.type != "private":
        return False
    now = datetime.now(timezone.utc)
    last = _q(lambda conn: conn.execute(
        "SELECT created_at FROM jf_anonymous_messages WHERE user_id=? ORDER BY id DESC LIMIT 1", (int(message.from_user.id),)
    ).fetchone())
    if last:
        try:
            elapsed = (now - datetime.fromisoformat(last["created_at"])).total_seconds()
            if elapsed < ANON_COOLDOWN_SECONDS:
                await message.reply(f"⏳ Повторное анонимное сообщение будет доступно через {int(ANON_COOLDOWN_SECONDS-elapsed)} сек.")
                return True
        except Exception:
            pass
    if len((message.text or "").strip()) > 3000:
        await message.reply("Слишком длинное сообщение. Максимум — 3000 символов.")
        return True
    text = (message.text or "").strip()
    if not text:
        return True
    def op(conn):
        cur = conn.execute(
            "INSERT INTO jf_anonymous_messages(user_id,chat_id,text,created_at,status) VALUES(?,?,?,?, 'new')",
            (int(message.from_user.id), int(_primary_chat_id), text, _utc_iso()),
        )
        conn.commit()
        return cur.lastrowid
    aid = _q(op)
    await _bot.send_message(_admin_id, "༺ 𓆩 ✧ 𓆪 ༻\n\n🤫 Анонимное обращение\n\n" + text)
    await message.reply("✅ Сообщение отправлено анонимно.")
    return True


async def cmd_anon(message: Message):
    if message.chat.type != "private":
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\nАнонимная связь запускается командой /anon")
        return
    # Rebuild a small Message-like path without leaking author in the visible content.
    class Proxy:
        pass
    proxy = Proxy()
    proxy.chat = message.chat
    proxy.from_user = message.from_user
    proxy.text = args[1]
    proxy.reply = message.reply
    await _anonymous_send(proxy)


async def join_help(message: Message):
    if message.chat.type != "private":
        return
    app = pending_application(message.from_user.id)
    if app:
        await message.reply(
            "༺ 𓆩 ✧ 𓆪 ༻\n\n"
            "🌸 Ваша заявка Justice Faite\n\n"
            f"🎭 Роль: {app['requested_role'] or 'ожидается'}\n"
            f"📄 Документ: {'получен' if app['document_file_id'] else 'ожидается'}\n\n"
            "✦ Можно прислать недостающую часть в любом порядке."
        )




async def birthday_state_text(message: Message, state: FSMContext):
    if message.chat.type != "private" or not message.from_user:
        return
    if await _save_birthday_for_user(message.from_user.id, message.text or "", state):
        parsed = _parse_birthday(message.text or "")
        await message.reply(f"{title('День рождения')}\n\n✅ Дата сохранена: {parsed[0]:02d}.{parsed[1]:02d}")
    else:
        await message.reply(f"{title('День рождения')}\n\n{bullet('Неверный формат. Укажите дату как DD.MM, например 24.03.')}")


async def cmd_birthdays(message: Message):
    if message.chat.type != "private" or not message.from_user:
        return
    rows = _q(lambda conn: conn.execute(
        "SELECT b.*,gm.username,gm.first_name,gm.last_name FROM jf_birthdays b LEFT JOIN group_members gm ON gm.chat_id=b.chat_id AND gm.user_id=b.user_id WHERE b.chat_id=? ORDER BY b.month,b.day",
        (int(_primary_chat_id),),
    ).fetchall())
    if not rows:
        await message.reply(f"{title('Дни рождения')}\n\n{bullet('Пока никто не добавил дату.')}")
        return
    today=(datetime.now(timezone.utc).month, datetime.now(timezone.utc).day)
    ordered=sorted(rows, key=lambda r: (((r["month"]-today[0])%12)*31 + (r["day"]-today[1])%31))
    lines=[title("Дни рождения"), ""]
    for r in ordered[:30]:
        name=_display_username(type("U",(),{"id":r["user_id"],"username":r["username"],"first_name":r["first_name"]})())
        lines.append(f"🎂 {r['day']:02d}.{r['month']:02d} — {name}")
    await message.reply("\n".join(lines))


async def cmd_restlist(message: Message):
    if not _admin_only(message) or message.chat.type != "private":
        return
    _q(lambda conn: (conn.execute("UPDATE jf_rest SET active=0 WHERE chat_id=? AND active=1 AND end_at<=?", (int(_primary_chat_id), _utc_iso())), conn.commit()))
    rows = _q(lambda conn: conn.execute(
        "SELECT r.*,gm.username,gm.first_name FROM jf_rest r LEFT JOIN group_members gm ON gm.chat_id=r.chat_id AND gm.user_id=r.user_id WHERE r.chat_id=? AND r.active=1 ORDER BY r.end_at",
        (int(_primary_chat_id),),
    ).fetchall())
    if not rows:
        await message.reply(f"{title('Рест')}\n\n{bullet('Сейчас никто не находится в ресте.')}")
        return
    lines=[title("Рест"), ""]
    for r in rows:
        name=f"@{r['username']}" if r["username"] else r["first_name"] or "участник"
        lines.append(f"🥹 {name} — до {str(r['end_at'])[:10]}")
    await message.reply("\n".join(lines))


async def feature_maintenance_worker():
    last_bday_date = None
    while True:
        try:
            now_dt=datetime.now(timezone.utc)
            _q(lambda conn: (conn.execute("UPDATE jf_rest SET active=0 WHERE active=1 AND end_at<=?", (now_dt.isoformat(),)), conn.commit()))
            day_key=now_dt.date().isoformat()
            if last_bday_date != day_key:
                rows=_q(lambda conn: conn.execute("SELECT * FROM jf_birthdays WHERE chat_id=? AND month=? AND day=?", (int(_primary_chat_id),now_dt.month,now_dt.day)).fetchall())
                for row in rows:
                    already=_q(lambda conn: conn.execute("SELECT 1 FROM jf_birthday_notices WHERE chat_id=? AND user_id=? AND year=?", (int(_primary_chat_id),int(row["user_id"]),now_dt.year)).fetchone())
                    if already:
                        continue
                    _q(lambda conn,r=row: (conn.execute("INSERT OR IGNORE INTO jf_birthday_notices(chat_id,user_id,year,sent_at) VALUES(?,?,?,?)", (int(_primary_chat_id),int(r["user_id"]),now_dt.year,_utc_iso())),conn.commit()))
                    with suppress(Exception):
                        await _bot.send_message(int(_primary_chat_id), f"🎂 Сегодня день рождения у <a href=\"tg://user?id={int(row['user_id'])}\">участника</a>!\n\n🌸 Поздравляем от Justice Faite.")
                last_bday_date=day_key
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(300)

def register_justice_features(ns: dict[str, Any]):
    global _bot, _dp, _db, _db_op, _now, _role_for, _normalize_role
    global _group_member_is_active, _upsert_group_member, _get_member, _assign_role_db_atomic
    global _apply_member_tag, _finalize_role_assignment, _confirm_member, _lift_member_restriction
    global _send_or_edit_welcome, _display_username, _primary_chat_id, _is_primary_chat, _admin_id, _register_user
    _bot = ns["bot"]
    _dp = ns["dp"]
    _db = ns["db"]
    _db_op = ns["group_db_op"]
    _now = ns["now"]
    _role_for = ns["role_for"]
    _normalize_role = ns["normalize_role"]
    _group_member_is_active = ns["_chat_member_is_active"]
    _upsert_group_member = ns["upsert_group_member"]
    _get_member = ns["get_member"]
    _assign_role_db_atomic = ns["assign_role_db_atomic"]
    _apply_member_tag = ns["apply_member_tag"]
    _finalize_role_assignment = ns["finalize_role_assignment"]
    _confirm_member = ns["confirm_member"]
    _lift_member_restriction = ns["lift_member_restriction"]
    _send_or_edit_welcome = ns["send_or_edit_welcome"]
    _display_username = ns["display_username_for_group"]
    _primary_chat_id = ns["PRIMARY_CHAT_ID"]
    _is_primary_chat = ns["is_primary_chat"]
    _admin_id = ns["ADMIN_ID"]
    _register_user = ns["register_user"]

    # Schema exists before handlers can execute, but call again is idempotent and
    # makes importing the module in simulations convenient.
    init_justice_features_db()

    private = F.chat.type == "private"
    private_admin = private
    _dp.message.register(create_invite, Command("jf_invite"), private_admin)
    _dp.message.register(applications_list, Command("jf_applications"), private_admin)
    _dp.message.register(cmd_dashboard, Command("jf_dashboard"), private_admin)
    _dp.message.register(cmd_awards_check, Command("jf_awards"), private_admin)
    _dp.message.register(cmd_award, Command("jf_award"), private_admin)
    _dp.message.register(cmd_iris_sync, Command("jf_iris_sync"), private_admin)
    _dp.message.register(cmd_warn, Command("jf_warn"), private_admin)
    _dp.message.register(cmd_warnings, Command("jf_warnings"), private_admin)
    _dp.message.register(cmd_remove_warning, Command("jf_warn_remove"), private_admin)
    _dp.message.register(cmd_rest, Command("jf_rest"), private_admin)
    _dp.message.register(cmd_restlist, Command("jf_restlist"), private_admin)
    _dp.message.register(cmd_birthday, Command("jf_birthday"), private)
    _dp.message.register(cmd_birthdays, Command("jf_birthdays"), private)
    _dp.message.register(join_help, Command("jf_join"), private)
    _dp.message.register(birthday_state_text, BirthdayState.waiting_date, private, F.text)
    _dp.chat_join_request.register(handle_join_request)
    _dp.callback_query.register(application_callback, F.data.startswith(FEATURE_PREFIX + "app_"))

    # These two handlers are intentionally last: they only catch private
    # application traffic that wasn't claimed by existing FSM handlers.
    _dp.message.register(application_private_photo, F.chat.type == "private", F.photo)
    _dp.message.register(application_private_text, F.chat.type == "private", F.text)

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
import difflib
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

# Unified Justice Faite visual style for all newly added messages.
def _fmt(text: str) -> str:
    return f"༺ 𓆩 ✧ 𓆪 ༻\n\n🌸 {text}"

def _back_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩ Назад", callback_data="u:home")]
    ])

IRIS_USERNAME = os.getenv("IRIS_BOT_USERNAME", "").strip().lstrip("@")
IRIS_AUTO_AWARDS = os.getenv("IRIS_AUTO_AWARDS", "1") == "1"
IRIS_COMMAND = os.getenv("IRIS_AWARD_COMMAND", "наградить")
IRIS_OLDS_COMMAND = os.getenv("IRIS_OLDS_COMMAND", "олды")
IRIS_AWARD_PREFIX = os.getenv("IRIS_AWARD_PREFIX", "!")
# Levels/titles are configurable because the exact Iris award scale is a community decision.
IRIS_30D_LEVEL = max(1, min(8, int(os.getenv("IRIS_30D_AWARD_LEVEL", "5"))))
IRIS_60D_LEVEL = max(1, min(8, int(os.getenv("IRIS_60D_AWARD_LEVEL", "6"))))
IRIS_120D_LEVEL = max(1, min(8, int(os.getenv("IRIS_120D_AWARD_LEVEL", "7"))))
IRIS_OLD_THRESHOLDS = (
    (30, IRIS_30D_LEVEL, "🌱 30 дней в Justice Faite"),
    (60, IRIS_60D_LEVEL, "🌿 60 дней в Justice Faite"),
    (120, IRIS_120D_LEVEL, "🌳 120 дней в Justice Faite"),
)
INVITE_TTL_HOURS = max(1, min(168, int(os.getenv("JF_INVITE_TTL_HOURS", "24"))))
ANON_COOLDOWN_SECONDS = max(0, int(os.getenv("JF_ANON_COOLDOWN_SECONDS", "300")))
WELCOME_RULES_URL = os.getenv("JF_RULES_URL", "").strip()

ROLE_SCAN_LIMIT = 180


class BirthdayState(StatesGroup):
    waiting_date = State()


class JoinApplicationState(StatesGroup):
    waiting_rules = State()
    waiting_role = State()
    waiting_document = State()


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
                approved_invite_link TEXT NOT NULL DEFAULT '',
                approved_invite_expires_at TEXT,
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

            CREATE TABLE IF NOT EXISTS jf_birthday_directory (
                chat_id INTEGER NOT NULL,
                person_label TEXT NOT NULL,
                person_key TEXT NOT NULL,
                month INTEGER NOT NULL,
                day INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'seed',
                PRIMARY KEY(chat_id,person_key)
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

            CREATE TABLE IF NOT EXISTS jf_iris_scan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                requested_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jf_iris_scan_status ON jf_iris_scan(chat_id,status);
            CREATE TABLE IF NOT EXISTS jf_birthday_notices (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                year INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY(chat_id,user_id,year)
            );
        """)
        # Forward-compatible migrations for an existing SQLite database.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jf_applications)").fetchall()}
        if "approved_invite_link" not in existing:
            conn.execute("ALTER TABLE jf_applications ADD COLUMN approved_invite_link TEXT NOT NULL DEFAULT ''")
        if "approved_invite_expires_at" not in existing:
            conn.execute("ALTER TABLE jf_applications ADD COLUMN approved_invite_expires_at TEXT")
        rest_existing = {row[1] for row in conn.execute("PRAGMA table_info(jf_rest)").fetchall()}
        if "ended_notified_at" not in rest_existing:
            conn.execute("ALTER TABLE jf_rest ADD COLUMN ended_notified_at TEXT")
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


def _role_match_key(value: str) -> str:
    """Normalize a role/name for tolerant Russian user input."""
    value = (value or "").casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", "", value)


def _role_word_stem(value: str) -> str:
    """Lightweight Russian case normalization (not a linguistic stemmer)."""
    value = _role_match_key(value)
    if len(value) < 5:
        return value
    for suffix in ("ами", "ями", "ого", "ему", "ому", "ыми", "ими", "ами", "ями", "ах", "ях", "ам", "ям", "ом", "ем", "ой", "ей", "ую", "ю", "ов", "ев", "ы", "и", "е", "о", "у", "а", "я"):
        if value.endswith(suffix) and len(value) - len(suffix) >= 4:
            return value[:-len(suffix)]
    return value


def _role_variants(name: str, english: str) -> set[str]:
    variants: set[str] = set()
    for value in (name, english):
        compact = _role_match_key(value)
        if compact:
            variants.add(compact)
        for word in re.split(r"\s+", value.casefold().replace("ё", "е")):
            stem = _role_word_stem(word)
            if stem:
                variants.add(stem)
    return variants


def find_requested_role(text: str):
    """Resolve a role from exact names, natural phrases and common typos.

    The user can type e.g. ``кадзуха``, ``казуха``, ``я хочу казуху``,
    ``чжун ли``, ``чжунли`` or ``син цю``.  Fuzzy matching is only accepted
    when the candidate is unambiguous, preventing random role assignment.
    """
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw:
        return None

    candidates = _role_candidates_from_catalog()
    if not candidates:
        return None

    # 1) Exact/substring matching is deterministic and preferred.
    hay = raw.casefold().replace("ё", "е")
    compact_hay = _role_match_key(raw)
    exact_hits = []
    for name, english, region in candidates:
        variants = _role_variants(name, english)
        normalized_name = re.sub(r"\s+", " ", name.casefold().replace("ё", "е")).strip()
        normalized_en = re.sub(r"\s+", " ", english.casefold()).strip()
        if normalized_name in hay or normalized_en in hay or _role_match_key(name) in compact_hay or _role_match_key(english) in compact_hay:
            exact_hits.append((name, english, region))
    if exact_hits:
        # Longest exact role wins; duplicate aliases of the same role collapse naturally.
        exact_hits.sort(key=lambda x: max(len(x[0]), len(x[1])), reverse=True)
        role = _role_for(exact_hits[0][0])
        if role:
            return role

    # 2) Compare individual words after removing simple grammatical endings.
    tokens = [_role_word_stem(t) for t in re.findall(r"[a-zа-яё0-9]+", raw.casefold())]
    tokens = [t for t in tokens if len(t) >= 4]
    if not tokens:
        return None

    scored = []
    for name, english, region in candidates:
        variants = _role_variants(name, english)
        best = 0.0
        for token in tokens:
            for variant in variants:
                if not variant:
                    continue
                ratio = difflib.SequenceMatcher(None, token, variant).ratio()
                if ratio > best:
                    best = ratio
        scored.append((best, name, english, region))

    scored.sort(reverse=True, key=lambda x: x[0])
    if not scored:
        return None
    best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    # 0.78 catches typos such as "казуха" while requiring a clear winner.
    if best[0] >= 0.78 and best[0] - second >= 0.045:
        return _role_for(best[1])
    return None


def pending_application(user_id: int, chat_id: int | None = None):
    def op(conn):
        if chat_id is None:
            return conn.execute(
                "SELECT * FROM jf_applications WHERE user_id=? AND status IN ('awaiting_data','awaiting_document','ready','pending_review') ORDER BY id DESC LIMIT 1",
                (int(user_id),),
            ).fetchone()
        return conn.execute(
            "SELECT * FROM jf_applications WHERE user_id=? AND chat_id=? AND status IN ('awaiting_data','awaiting_document','ready','pending_review') ORDER BY id DESC LIMIT 1",
            (int(user_id), int(chat_id)),
        ).fetchone()
    return _q(op)


def _latest_requested_role_for_chat(chat_id: int):
    def op(conn):
        row = conn.execute(
            "SELECT requested_role,requested_role_key FROM jf_applications WHERE chat_id=? AND status IN ('awaiting_data','awaiting_document','ready','pending_review') AND requested_role!='' ORDER BY id DESC LIMIT 1",
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
        "join_request_user_chat_id", "approved_invite_link",
        "approved_invite_expires_at", "updated_at"
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


async def _member_is_active_in_primary(user_id: int) -> bool:
    try:
        member = await _bot.get_chat_member(int(_primary_chat_id), int(user_id))
    except Exception:
        return False
    status = getattr(member, "status", None)
    return status in {"member", "administrator", "creator"} or (status == "restricted" and bool(getattr(member, "is_member", False)))


def _join_rules_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, ознакомлен", callback_data=FEATURE_PREFIX + "join_rules_yes")],
        [InlineKeyboardButton(text="❌ Нет", callback_data=FEATURE_PREFIX + "join_rules_no")],
        [InlineKeyboardButton(text="‹ Назад", callback_data=FEATURE_PREFIX + "join_back")],
    ])


def _join_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="‹ Назад", callback_data=FEATURE_PREFIX + "join_back")]])


def _application_ready(row) -> bool:
    return bool(row and str(row["requested_role"] or "").strip() and str(row["document_file_id"] or "").strip())


def _application_open_for_user(user_id: int):
    return _q(lambda conn: conn.execute(
        "SELECT * FROM jf_applications WHERE user_id=? AND status IN ('awaiting_rules','awaiting_data','awaiting_document','ready','pending_review','approved_waiting_join') ORDER BY id DESC LIMIT 1",
        (int(user_id),),
    ).fetchone())


def _create_application(user_id: int) -> int:
    def op(conn):
        cur = conn.execute(
            "INSERT INTO jf_applications(user_id,chat_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            (int(user_id), int(_primary_chat_id), "awaiting_rules", _utc_iso(), _utc_iso()),
        )
        conn.commit()
        return int(cur.lastrowid)
    return _q(op)


def _join_application_kb(app_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"{FEATURE_PREFIX}app_ok:{int(app_id)}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"{FEATURE_PREFIX}app_no:{int(app_id)}")],
    ])


def _application_summary(app) -> str:
    return (
        "༺ 𓆩 ✧ 𓆪 ༻\n\n"
        "🌸 Новая заявка Justice Faite\n\n"
        f"№ {int(app['id'])}\n"
        f"Роль: {app['requested_role'] or '—'}\n"
        f"Документ: {'получен' if app['document_file_id'] else 'ожидается'}\n\n"
        "✦ Документ предназначен только для проверки возраста."
    )


async def begin_join_application(callback: CallbackQuery, state: FSMContext):
    if callback.message.chat.type != "private":
        await callback.answer()
        return
    if await _member_is_active_in_primary(callback.from_user.id):
        await callback.answer("Вы уже состоите во флуде.", show_alert=True)
        return

    app = _application_open_for_user(callback.from_user.id)
    if not app:
        app_id = _create_application(callback.from_user.id)
        app = _q(lambda conn: conn.execute("SELECT * FROM jf_applications WHERE id=?", (app_id,)).fetchone())

    status = str(app["status"] or "")
    if status == "pending_review":
        await state.clear()
        await callback.message.edit_text(
            _fmt("Заявка уже отправлена") + "\n\n✦ Документ получен владельцем.\n✦ Дождитесь подтверждения.",
            reply_markup=_back_home_kb(),
        )
        await callback.answer()
        return

    if status == "approved_waiting_join":
        await state.clear()
        link = app["approved_invite_link"] or ""
        if link:
            await callback.message.edit_text(
                _fmt("Заявка одобрена") + f"\n\n✦ Ваша персональная ссылка:\n\n{link}\n\n✦ Ссылка предназначена только для вас.",
                reply_markup=_back_home_kb(),
            )
        else:
            await callback.message.edit_text(_fmt("Заявка одобрена") + "\n\n✦ Ожидайте персональную ссылку.", reply_markup=_back_home_kb())
        await callback.answer()
        return

    rules_text = (f"\n\n✦ Информация: {WELCOME_RULES_URL}" if WELCOME_RULES_URL else "")
    if status == "awaiting_data":
        await state.set_state(JoinApplicationState.waiting_role)
        await callback.message.edit_text(
            _fmt("Желаемая роль") + "\n\n"
            "✦ Напишите персонажа, которого хотите взять.\n"
            "✦ Можно обычной фразой — бот распознает роль.\n"
            "✦ Занятые роли не показываются.",
            reply_markup=_join_back_kb(),
        )
        await callback.answer()
        return

    if status == "awaiting_document" and app["requested_role"]:
        await state.set_state(JoinApplicationState.waiting_document)
        await callback.message.edit_text(
            _fmt("Подтверждение возраста") + "\n\n"
            "✦ Отправьте фотографию документа, подтверждающего возраст 16+.\n"
            "✦ Дата рождения должна быть видна.\n"
            "✦ Остальные данные можно скрыть.\n\n"
            "♡ Документ увидит только владелец.",
            reply_markup=_join_back_kb(),
        )
        await callback.answer()
        return

    await state.set_state(JoinApplicationState.waiting_rules)
    await callback.message.edit_text(
        _fmt("Вступление в Justice Faite") + "\n\n"
        "✦ Перед вступлением ознакомьтесь с информационным каналом и правилами флуда."
        + rules_text + "\n\n"
        "Ознакомились с правилами?",
        reply_markup=_join_rules_kb(),
    )
    _set_application_field(int(app["id"]), status="awaiting_rules")
    await callback.answer()


async def join_rules_callback(callback: CallbackQuery, state: FSMContext):
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer()
        return
    if callback.from_user.id is None:
        return
    data = callback.data or ""
    if data.endswith("join_rules_yes"):
        app = _application_open_for_user(callback.from_user.id)
        if not app:
            app_id = _create_application(callback.from_user.id)
            app = _q(lambda conn: conn.execute("SELECT * FROM jf_applications WHERE id=?", (app_id,)).fetchone())
        _set_application_field(int(app["id"]), status="awaiting_data")
        await state.set_state(JoinApplicationState.waiting_role)
        await callback.message.edit_text(
            "༺ 𓆩 ✧ 𓆪 ༻\n\n"
            "🎭 Желаемая роль\n\n"
            "✦ Напишите персонажа, которого хотите взять.\n"
            "✦ Можно написать обычной фразой — бот попробует распознать роль.\n"
            "✦ Занятые роли не показываются.",
            reply_markup=_join_back_kb(),
        )
    elif data.endswith("join_rules_no"):
        await state.clear()
        await callback.message.edit_text(
            "༺ 𓆩 ✧ 𓆪 ༻\n\n"
            "🌸 Тогда беги читать правила 😭\n\n"
            "✦ Без этого дальше не пускаем. Когда ознакомишься — возвращайся.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩ Назад", callback_data="u:home")]]),
        )
    elif data.endswith("join_back"):
        await state.clear()
        await callback.message.edit_text(
            "༺ 𓆩 ✧ 𓆪 ༻\n\n🌸 Justice Faite\n\n✦ Нажмите «Хочу вступить», чтобы подать заявку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚪 Хочу вступить", callback_data="u:join")],
                [InlineKeyboardButton(text="🤫 Анонимная связь", callback_data="u:send")],
                [InlineKeyboardButton(text="⚠ Жалоба", callback_data="u:report")],
                [InlineKeyboardButton(text="✦ Информация", callback_data="u:info")],
            ]),
        )
    await callback.answer()


async def join_role_text(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    app = _application_open_for_user(message.from_user.id)
    if not app or app["status"] != "awaiting_data":
        return
    role = find_requested_role(message.text or "")
    if not role:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n❌ Не удалось определить персонажа. Напишите название роли ещё раз.")
        return
    role_key = _normalize_role(role["name"])
    occupied = _q(lambda conn: conn.execute(
        "SELECT 1 FROM role_state WHERE chat_id=? AND role_key=? AND status='taken' LIMIT 1",
        (int(_primary_chat_id), role_key),
    ).fetchone())
    if occupied:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n❌ Эта роль сейчас недоступна. Выберите другую желаемую роль.")
        return
    _set_application_field(int(app["id"]), requested_role=role["name"], requested_role_key=role_key, status="awaiting_document")
    await state.set_state(JoinApplicationState.waiting_document)
    await message.reply(
        "༺ 𓆩 ✧ 𓆪 ༻\n\n"
        "📄 Подтверждение возраста\n\n"
        "✦ Отправьте фотографию документа, подтверждающего, что вам есть 16.\n"
        "✦ Дата рождения должна быть видна.\n"
        "✦ Остальные данные можно скрыть.\n\n"
        "Документ увидит только владелец Justice Faite."
    )


async def join_document_photo(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    app = _application_open_for_user(message.from_user.id)
    if not app or app["status"] not in {"awaiting_data", "awaiting_document"}:
        return
    if not app["requested_role"]:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\nСначала укажите желаемую роль.")
        return
    try:
        caption = (
            "༺ 𓆩 ✧ 𓆪 ༻\n\n📄 Проверка возраста — Justice Faite\n\n"
            f"Заявка №{int(app['id'])}\n"
            f"Пользователь: @{message.from_user.username}" if message.from_user.username else
            f"Заявка №{int(app['id'])}\nПользователь: {message.from_user.first_name or 'участник'}"
        )
        sent = await _bot.send_photo(_admin_id, message.photo[-1].file_id, caption=caption, reply_markup=_join_application_kb(int(app["id"])))
    except Exception:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n❌ Не удалось передать документ владельцу. Отправьте фотографию ещё раз.")
        return
    _set_application_field(int(app["id"]), document_file_id=message.photo[-1].file_id, document_message_id=sent.message_id, status="pending_review")
    await state.clear()
    await message.reply(
        "༺ 𓆩 ✧ 𓆪 ༻\n\n✅ Документ передан владельцу.\n\n"
        "✦ Дождитесь подтверждения. После одобрения бот пришлёт персональную ссылку на вступление во флуд."
    )


async def join_document_nonphoto(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    app = _application_open_for_user(message.from_user.id)
    if app and app["status"] in {"awaiting_data", "awaiting_document"} and app["requested_role"]:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n📄 Нужна именно фотография документа. Отправьте изображение с видимой датой рождения.")


async def join_role_fallback(message: Message, state: FSMContext):
    """Recover an interrupted application after a Render restart."""
    if message.chat.type != "private" or not message.from_user or not (message.text or "").strip():
        return
    app = _application_open_for_user(message.from_user.id)
    if not app or app["status"] not in {"awaiting_data", "awaiting_document"} or app["document_file_id"]:
        return
    # If a role was already saved, the only missing item is the document.
    if app["requested_role"]:
        return
    role = find_requested_role(message.text or "")
    if not role:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n❌ Не удалось определить персонажа. Попробуйте написать имя ещё раз.")
        return
    role_key = _normalize_role(role["name"])
    occupied = _q(lambda conn: conn.execute(
        "SELECT 1 FROM role_state WHERE chat_id=? AND role_key=? AND status='taken' LIMIT 1",
        (int(_primary_chat_id), role_key),
    ).fetchone())
    if occupied:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n❌ Эта роль сейчас недоступна. Укажите другую желаемую роль.")
        return
    _set_application_field(int(app["id"]), requested_role=role["name"], requested_role_key=role_key, status="awaiting_document")
    await state.set_state(JoinApplicationState.waiting_document)
    await message.reply(
        "༺ 𓆩 ✧ 𓆪 ༻\n\n🎭 Роль сохранена\n\n"
        f"✦ Желаемая роль: {role['name']}\n\n"
        "📄 Теперь отправьте фотографию документа, подтверждающего возраст 16+.\n"
        "✦ Дата рождения должна быть видна.\n"
        "✦ Остальные данные можно скрыть."
    )


async def join_document_photo_fallback(message: Message, state: FSMContext):
    """Recover the document step after an FSM restart."""
    if message.chat.type != "private" or not message.from_user or not message.photo:
        return
    app = _application_open_for_user(message.from_user.id)
    if not app or app["status"] not in {"awaiting_data", "awaiting_document"} or not app["requested_role"] or app["document_file_id"]:
        return
    await join_document_photo(message, state)


async def applications_list(message: Message):
    if not _admin_only(message) or message.chat.type != "private":
        return
    rows = _q(lambda conn: conn.execute("SELECT * FROM jf_applications WHERE status IN ('awaiting_data','ready','pending_review','approved_waiting_join','approved') ORDER BY id DESC LIMIT 25").fetchall())
    if not rows:
        await message.reply("༺ 𓆩 ✧ 𓆪 ༻\n\n📭 Активных заявок нет.")
        return
    lines=["༺ 𓆩 ✧ 𓆪 ༻","","📋 Заявки Justice Faite",""]
    for row in rows:
        lines.append(f"#{row['id']} · {row['status']} · {row['requested_role'] or '—'} · {'📄' if row['document_file_id'] else '—'}")
    await message.reply("\n".join(lines))


async def application_callback(callback: CallbackQuery):
    if callback.from_user.id != int(_admin_id):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    try:
        action, raw_id = (callback.data or "").split(":")[-2:]
        app_id = int(raw_id)
    except (ValueError, IndexError):
        await callback.answer("Некорректная заявка.", show_alert=True)
        return
    app = _q(lambda conn: conn.execute("SELECT * FROM jf_applications WHERE id=?", (app_id,)).fetchone())
    if not app:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if action == "app_no":
        if app["status"] in {"declined", "joined"}:
            await callback.answer("Заявка уже обработана.", show_alert=True)
            return
        _set_application_field(app_id, status="declined", reviewed_by=int(_admin_id), reviewed_at=_utc_iso(), review_reason="Отклонено администрацией")
        with suppress(Exception):
            await _bot.send_message(int(app["user_id"]), "༺ 𓆩 ✧ 𓆪 ༻\n\n❌ Заявка на вступление отклонена администрацией.")
        with suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Заявка отклонена.")
        return
    if action == "app_ok":
        if app["status"] == "approved_waiting_join":
            await callback.answer("Для этой заявки ссылка уже создана и ожидает входа.", show_alert=True)
            return
        if app["status"] == "approved" or app["status"] == "joined":
            await callback.answer("Заявка уже обработана.", show_alert=True)
            return
        if not _application_ready(app):
            await callback.answer("Нужны роль и документ.", show_alert=True)
            return
        try:
            expire = datetime.now(timezone.utc) + timedelta(hours=INVITE_TTL_HOURS)
            link = await _bot.create_chat_invite_link(
                int(_primary_chat_id),
                name=f"Justice Faite #{app_id}",
                creates_join_request=True,
                expire_date=expire,
            )
        except Exception:
            await callback.answer("Не удалось создать персональную ссылку. Проверьте права бота в чате.", show_alert=True)
            return
        _set_application_field(app_id, status="approved_waiting_join", reviewed_by=int(_admin_id), reviewed_at=_utc_iso(), approved_invite_link=link.invite_link, approved_invite_expires_at=expire.isoformat())
        _q(lambda conn: (conn.execute(
            "INSERT OR REPLACE INTO jf_invites(invite_link,chat_id,created_by,created_at,expires_at,status) VALUES(?,?,?,?,?,?)",
            (link.invite_link, int(_primary_chat_id), int(_admin_id), _utc_iso(), expire.isoformat(), "active"),
        ), conn.commit()))
        try:
            await _bot.send_message(
                int(app["user_id"]),
                "༺ 𓆩 ✧ 𓆪 ༻\n\n✅ Заявка одобрена\n\n"
                "✦ Ваша персональная ссылка на Justice Faite:\n\n"
                f"{link.invite_link}\n\n"
                "✦ Ссылка персональная: бот пропустит по ней только вас. Она ограничена по времени."
            )
        except Exception:
            # Keep the application approved; the admin can inspect the generated link in the application list.
            pass
        with suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Заявка одобрена, ссылка создана.")
        return


async def handle_join_request(event: ChatJoinRequest):
    if int(event.chat.id) != int(_primary_chat_id):
        return
    user = event.from_user
    invite_text = getattr(event.invite_link, "invite_link", "") if getattr(event, "invite_link", None) else ""
    app = _q(lambda conn: conn.execute(
        "SELECT * FROM jf_applications WHERE user_id=? AND chat_id=? AND status='approved_waiting_join' ORDER BY id DESC LIMIT 1",
        (int(user.id), int(event.chat.id)),
    ).fetchone())
    # Reject requests that do not belong to a currently approved application/link.
    if not app or not invite_text or invite_text != (app["approved_invite_link"] or ""):
        with suppress(Exception):
            await _bot.decline_chat_join_request(int(event.chat.id), int(user.id))
        return
    try:
        await _bot.approve_chat_join_request(int(event.chat.id), int(user.id))
        with suppress(Exception):
            await _bot.revoke_chat_invite_link(int(event.chat.id), invite_text)
        _q(lambda conn: (conn.execute("UPDATE jf_invites SET status='used',used_by=?,used_at=? WHERE invite_link=?", (int(user.id), _utc_iso(), invite_text)), conn.commit()))
        _set_application_field(int(app["id"]), status="approved", reviewed_by=int(_admin_id), reviewed_at=app["reviewed_at"] or _utc_iso())
    except Exception:
        # Keep the application waiting so a transient Telegram error can be retried.
        logger_msg = f"Join request approval failed | app={app['id']} user={user.id}"
        try:
            import logging as _logging
            _logging.getLogger(__name__).exception(logger_msg)
        except Exception:
            pass


async def auto_bind_requested_role(chat_id: int, user):
    app = _q(lambda conn: conn.execute(
        "SELECT * FROM jf_applications WHERE user_id=? AND chat_id=? AND status IN ('approved_waiting_join','approved') AND requested_role!='' ORDER BY id DESC LIMIT 1",
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
            with suppress(Exception):
                from bot import release_role_assignment
                release_role_assignment(int(chat_id), int(user.id), role_key)
            return False
        _finalize_role_assignment(int(chat_id), int(user.id), role_key, actual or tag)
        _confirm_member(int(chat_id), int(user.id))
        await _lift_member_restriction(int(chat_id), int(user.id))
        if _send_or_edit_welcome:
            await _send_or_edit_welcome(int(chat_id), int(user.id))
        _set_application_field(app["id"], status="joined")
        return True
    except ValueError as exc:
        return str(exc) != "ROLE_OCCUPIED" and False
    except Exception:
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

def _duration_to_days(value: str) -> int:
    """Parse Iris-style duration text into whole days.

    Examples: ``1 месяц 12 дн``, ``27 дней 8 ч``, ``12 ч``, ``37 мин``.
    Month is treated as 30 days for the thresholding purpose because the
    configured milestones are expressed as 30/60/120 days.
    """
    raw = (value or "").casefold().replace("ё", "е")
    total = 0.0
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:год|года|лет)", raw)
    if m:
        total += float(m.group(1).replace(',', '.')) * 365
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*месяц(?:а|ев)?", raw)
    if m:
        total += float(m.group(1).replace(',', '.')) * 30
    m = re.search(r"(\d+)\s*д(?:ень|ня|ней|н)?\b", raw)
    if m:
        total += int(m.group(1))
    m = re.search(r"(\d+)\s*ч(?:\.|ас(?:а|ов)?)?\b", raw)
    if m:
        total += int(m.group(1)) / 24
    m = re.search(r"(\d+)\s*мин(?:ут(?:а|ы|)?)?\b", raw)
    if m:
        total += int(m.group(1)) / 1440
    return int(total)


def _best_award_threshold(days: int):
    eligible = [x for x in IRIS_OLD_THRESHOLDS if days >= x[0]]
    return max(eligible, key=lambda x: x[0]) if eligible else None


def compute_old_and_active_candidates(chat_id: int):
    rows = _q(lambda conn: conn.execute(
        "SELECT gm.user_id,gm.first_name,gm.last_name,gm.username,gm.joined_at,u.messages_count,u.last_seen FROM group_members gm LEFT JOIN users u ON u.user_id=gm.user_id WHERE gm.chat_id=? AND gm.active=1 AND gm.user_id!=? ORDER BY gm.joined_at ASC",
        (int(chat_id), int(_admin_id)),
    ).fetchall())
    # Local fallback only; /jf_awards itself uses Iris' ``олды`` response as its
    # authoritative source when Iris integration is available.
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


def _display_key(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", (value or "").casefold().replace("ё", "е"))


def _utf16_prefix(text: str) -> list[int]:
    values = [0]
    total = 0
    for ch in text:
        total += len(ch.encode("utf-16-le")) // 2
        values.append(total)
    return values


def _utf16_slice(text: str, start: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[start * 2:(start + length) * 2].decode("utf-16-le", errors="ignore")


def _line_entity_identity(message: Message, line_start_py: int, line_end_py: int):
    """Return concrete Telegram user identities referenced by clickable entities in a line."""
    text = message.text or ""
    prefix = _utf16_prefix(text)
    start_u16 = prefix[line_start_py]
    end_u16 = prefix[line_end_py]
    identities: list[tuple[str, Any]] = []
    for entity in message.entities or []:
        ent_start = int(entity.offset)
        ent_end = ent_start + int(entity.length)
        if ent_start < start_u16 or ent_start >= end_u16:
            continue
        etype = str(getattr(entity, "type", ""))
        if etype == "text_mention" and getattr(entity, "user", None):
            identities.append(("id", entity.user))
        elif etype == "text_link":
            url = str(getattr(entity, "url", "") or "")
            match = re.search(r"tg://user\?id=(\d+)", url)
            if match:
                identities.append(("id", int(match.group(1))))
        elif etype == "mention":
            mention_text = _utf16_slice(text, ent_start, int(entity.length)).lstrip("@")
            identities.append(("username", mention_text.casefold()))
    return identities


def _match_member_by_identity(identity, active_rows: list[Any]):
    kind, value = identity
    if kind == "id":
        uid = int(value.id) if hasattr(value, "id") else int(value)
        for row in active_rows:
            if int(row["user_id"]) == uid:
                if hasattr(value, "username") and value.username and not row.get("username"):
                    row["username"] = value.username
                return row
        return None
    username = str(value).casefold().lstrip("@")
    hits = [r for r in active_rows if (r["username"] or "").casefold() == username]
    return hits[0] if len(hits) == 1 else None


async def _resolve_member_by_identity(identity, active_rows: list[Any]):
    hit = _match_member_by_identity(identity, active_rows)
    if hit:
        return hit
    kind, value = identity
    if kind != "id":
        return None
    uid = int(value.id) if hasattr(value, "id") else int(value)
    try:
        member = await _bot.get_chat_member(int(_primary_chat_id), uid)
    except Exception:
        return None
    status = getattr(member, "status", None)
    active = status in {"member", "administrator", "creator"} or (status == "restricted" and bool(getattr(member, "is_member", False)))
    if not active or not getattr(member, "user", None) or getattr(member.user, "is_bot", False):
        return None
    user = member.user
    with suppress(Exception):
        _upsert_group_member(int(_primary_chat_id), user, active=True)
    row = _get_member(int(_primary_chat_id), uid)
    if row:
        return dict(row)
    return {
        "user_id": uid,
        "first_name": getattr(user, "first_name", "") or "",
        "last_name": getattr(user, "last_name", "") or "",
        "username": getattr(user, "username", "") or "",
    }


async def _match_iris_old_line(line: str, active_rows: list[Any], message: Message | None = None, line_start_py: int = 0, line_end_py: int = 0):
    """Prefer Telegram clickable identity, then username/display-name fallback."""
    if message is not None:
        for identity in _line_entity_identity(message, line_start_py, line_end_py):
            hit = await _resolve_member_by_identity(identity, active_rows)
            if hit:
                return hit

    line_key = _display_key(line)
    username_hit = re.search(r"@([A-Za-z0-9_]{3,32})", line)
    if username_hit:
        uname = username_hit.group(1).casefold()
        hits = [r for r in active_rows if (r["username"] or "").casefold() == uname]
        if len(hits) == 1:
            return hits[0]
    exact = []
    for r in active_rows:
        display = " ".join(x for x in ((r["first_name"] or ""), (r["last_name"] or "")) if x).strip()
        username = r["username"] or ""
        candidates = {_display_key(display), _display_key(username)}
        if any(k and k in line_key for k in candidates):
            exact.append(r)
    if len(exact) == 1:
        return exact[0]
    best = []
    for r in active_rows:
        display = " ".join(x for x in ((r["first_name"] or ""), (r["last_name"] or "")) if x).strip()
        dk = _display_key(display)
        if not dk:
            continue
        ratio = difflib.SequenceMatcher(None, dk, line_key).ratio()
        best.append((ratio, r))
    best.sort(reverse=True, key=lambda x: x[0])
    if best and best[0][0] >= 0.78 and (len(best) == 1 or best[0][0] - best[1][0] >= 0.08):
        return best[0][1]
    return None


def _iter_text_lines_with_offsets(text: str):
    cursor = 0
    for raw in text.splitlines(True):
        line = raw.rstrip("\r\n")
        line_start = cursor
        line_end = cursor + len(line)
        yield line, line_start, line_end
        cursor += len(raw)


async def _extract_iris_old_candidates(message: Message, active_rows: list[Any]):
    """Parse an Iris ``олды`` response using clickable entities whenever available."""
    text = message.text or ""
    results = []
    for line, line_start, line_end in _iter_text_lines_with_offsets(text):
        stripped = line.strip()
        if not stripped:
            continue
        duration = re.search(r"—\s*(.+?)\s*$", stripped)
        if not duration:
            continue
        age_text = duration.group(1).strip()
        if not re.search(r"(?:дн|день|дня|дней|месяц|год|лет|ч\b|мин)", age_text.casefold()):
            continue
        days = _duration_to_days(age_text)
        if days < 30:
            continue
        member = await _match_iris_old_line(stripped, active_rows, message, line_start, line_end)
        if member:
            results.append((member, days, age_text))
    return results


async def _send_iris_command(command_text: str) -> bool:
    """Send a plain-text Iris command into the primary group.

    Bot-to-bot delivery is enabled in BotFather. We intentionally do not add
    ``@Iris`` to the command itself: the command syntax must stay exactly as
    Iris expects, e.g. ``!наградить 5 @username``.
    """
    if not _bot or not _primary_chat_id:
        return False
    try:
        await _bot.send_message(int(_primary_chat_id), command_text)
        return True
    except Exception:
        import logging
        logging.getLogger("justice_features").exception("Could not send Iris command")
        return False


async def _run_iris_old_awards() -> tuple[int, int, int]:
    """Ask Iris for ``олды``; awards are issued from the response handler."""
    if not IRIS_USERNAME:
        return 0, 0, 1
    _q(lambda conn: (
        conn.execute("UPDATE jf_iris_scan SET status='superseded',finished_at=? WHERE chat_id=? AND status='waiting'", (_utc_iso(), int(_primary_chat_id))),
        conn.execute("INSERT INTO jf_iris_scan(chat_id,requested_at,status) VALUES(?,?,?)", (int(_primary_chat_id), _utc_iso(), "waiting")),
        conn.commit(),
    ))
    sent = await _send_iris_command(IRIS_OLDS_COMMAND)
    if not sent:
        _q(lambda conn: (
            conn.execute("UPDATE jf_iris_scan SET status='failed',finished_at=? WHERE chat_id=? AND status='waiting'", (_utc_iso(), int(_primary_chat_id))),
            conn.commit(),
        ))
        return 1, 0, 1
    return 1, 0, 0


async def handle_iris_olds_response(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not _is_primary_chat(message.chat.id):
        return
    sender = message.from_user
    if not sender or not sender.is_bot:
        return
    if IRIS_USERNAME and (not sender.username or sender.username.casefold() != IRIS_USERNAME.casefold()):
        return
    if not IRIS_USERNAME:
        sender_name = " ".join(x for x in ((sender.first_name or ""), (sender.last_name or "")) if x).casefold()
        if "iris" not in sender_name:
            return

    waiting = _q(lambda conn: conn.execute(
        "SELECT * FROM jf_iris_scan WHERE chat_id=? AND status='waiting' ORDER BY id DESC LIMIT 1",
        (int(_primary_chat_id),),
    ).fetchone())
    if not waiting:
        return

    _q(lambda conn: (
        conn.execute("UPDATE jf_iris_scan SET status='processing' WHERE id=?", (int(waiting["id"]),)),
        conn.commit(),
    ))

    try:
        active_rows = _active_member_rows()
        parsed = await _extract_iris_old_candidates(message, active_rows)
        awarded = 0
        skipped = 0
        package: list[str] = []
        seen_pairs = set()
        for row, days, age_text in parsed:
            uid = int(row["user_id"])
            username = (row["username"] or "").strip()
            for threshold_days, level, title in IRIS_OLD_THRESHOLDS:
                if days < threshold_days:
                    continue
                pair = (uid, threshold_days)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                code = f"iris_old_{threshold_days}d"
                if iris_already_sent(_primary_chat_id, uid, code):
                    skipped += 1
                    continue
                if not username:
                    skipped += 1
                    record_iris_audit(_primary_chat_id, uid, code, "skipped", f"{title}. Нет username для команды Ирис.", "")
                    continue
                reason = f"{title}. Участник находится во флуде {age_text}."
                command = f"{IRIS_AWARD_PREFIX}{IRIS_COMMAND} {level} @{username}\n{title}"
                package.append(command)
                if not IRIS_AUTO_AWARDS:
                    continue
                if await _send_iris_command(command):
                    record_iris_audit(_primary_chat_id, uid, code, "sent", reason, IRIS_USERNAME)
                    awarded += 1
                else:
                    skipped += 1
                await asyncio.sleep(0.8)
        _q(lambda conn: (
            conn.execute("UPDATE jf_iris_scan SET status='done',finished_at=? WHERE id=?", (_utc_iso(), int(waiting["id"]))),
            conn.commit(),
        ))
        with suppress(Exception):
            summary = (
                _fmt("Награды Ирис") +
                f"\n\n✅ Проверка «олды» завершена.\n✦ Отправлено команд: {awarded}\n✦ Пропущено: {skipped}\n✦ Пороги: 30 / 60 / 120 дней."
            )
            if not IRIS_AUTO_AWARDS and package:
                summary += "\n\n🧾 Пакет команд без авто-выдачи:\n\n" + "\n\n".join(package)
            await _bot.send_message(int(_admin_id), summary)
    except Exception:
        _q(lambda conn: (
            conn.execute("UPDATE jf_iris_scan SET status='failed',finished_at=? WHERE id=?", (_utc_iso(), int(waiting["id"]))),
            conn.commit(),
        ))
        import logging
        logging.getLogger("justice_features").exception("Iris old-award processing failed")


def _pretty_dashboard(chat_id: int) -> str:
    rows = _q(lambda conn: conn.execute(
        "SELECT COUNT(*) AS c FROM group_members WHERE chat_id=? AND active=1", (int(chat_id),)
    ).fetchone())
    active = int(rows["c"])
    rest = _q(lambda conn: conn.execute(
        "SELECT COUNT(*) AS c FROM jf_rest WHERE chat_id=? AND active=1 AND end_at> ?", (int(chat_id), _utc_iso())
    ).fetchone())["c"]
    apps = _q(lambda conn: conn.execute(
        "SELECT COUNT(*) AS c FROM jf_applications WHERE chat_id=? AND status IN ('awaiting_data','awaiting_document','ready','pending_review')", (int(chat_id),)
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
    if not _admin_only(message) or message.chat.type != "private":
        return
    if not IRIS_USERNAME:
        await message.reply(_fmt("Награды Ирис") + "\n\n⚠️ Не задан IRIS_BOT_USERNAME в окружении.")
        return
    await message.reply(
        _fmt("Награды Ирис") +
        "\n\n✦ Проверяю участников через команду «олды» Ирис.\n"
        f"✦ Пороги: 30 / 60 / 120 дней.\n✦ Степени: {IRIS_30D_LEVEL} / {IRIS_60D_LEVEL} / {IRIS_120D_LEVEL}.\n"
        "✦ Подходящим участникам награда будет отправлена автоматически."
    )
    requested, awarded, skipped = await _run_iris_old_awards()
    if requested:
        await message.reply("✦ Запрос «олды» отправлен Ирис. Ожидаю список и автоматически обработаю найденных участников.")
    else:
        await message.reply("⚠️ Не удалось запустить проверку «олды».")


async def cmd_iris_sync(message: Message):
    # Backward-compatible alias: same secure owner-only behavior, but now it
    # triggers the real Iris-driven automatic milestone workflow.
    await cmd_awards_check(message)


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


MONTH_ALIASES = {
    "января": 1, "январь": 1, "янв": 1,
    "февраля": 2, "февраль": 2, "фев": 2,
    "марта": 3, "март": 3, "мар": 3,
    "апреля": 4, "апрель": 4, "апр": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июнь": 6, "июн": 6,
    "июля": 7, "июль": 7, "июл": 7,
    "августа": 8, "август": 8, "авг": 8,
    "сентября": 9, "сентябрь": 9, "сен": 9,
    "октября": 10, "октябрь": 10, "окт": 10,
    "ноября": 11, "ноябрь": 11, "ноя": 11,
    "декабря": 12, "декабрь": 12, "дек": 12,
}


def _parse_rest_day_month(value: str):
    parts = [p for p in re.split(r"[.\-/\s]+", (value or "").strip().casefold()) if p]
    if len(parts) != 2:
        return None
    try:
        day = int(parts[0])
    except ValueError:
        return None
    month = MONTH_ALIASES.get(parts[1])
    if month is None:
        try:
            month = int(parts[1])
        except ValueError:
            return None
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    try:
        datetime(2024, month, day)
    except ValueError:
        return None
    return day, month


def _next_rest_end(day: int, month: int) -> datetime:
    offset_hours = int(os.getenv("DEFAULT_TIMEZONE_OFFSET_HOURS", "3"))
    tz = timezone(timedelta(hours=offset_hours))
    now_local = datetime.now(tz)
    year = now_local.year
    candidate = datetime(year, month, day, 23, 59, 59, tzinfo=tz)
    if candidate <= now_local:
        candidate = datetime(year + 1, month, day, 23, 59, 59, tzinfo=tz)
    return candidate.astimezone(timezone.utc)


async def _resolve_member_target(raw: str):
    value = (raw or "").strip()
    if not value:
        return None
    username = value.lstrip("@").casefold()
    row = _q(lambda conn: conn.execute(
        "SELECT * FROM group_members WHERE chat_id=? AND lower(username)=? AND active=1 LIMIT 1",
        (int(_primary_chat_id), username),
    ).fetchone())
    if row:
        return row
    if username.isdigit():
        return _q(lambda conn: conn.execute(
            "SELECT * FROM group_members WHERE chat_id=? AND user_id=? AND active=1 LIMIT 1",
            (int(_primary_chat_id), int(username)),
        ).fetchone())
    return None


async def _set_rest_from_text(message: Message):
    raw = (message.text or "").strip()
    pattern = r"(?iu)^\s*(?:(?:выдать\s+)?рест|/(?:jf_)?rest)\s+(@[A-Za-z0-9_]{3,32}|\d+)\s+(.+?)\s*$"
    match = re.match(pattern, raw)
    if not match:
        await message.reply(_fmt("Рест") + "\n\n✦ Формат: `выдать рест @username 15 09`\n✦ Или: `/rest @username 15 09`")
        return
    target_raw, date_raw = match.group(1), match.group(2).strip()
    # The date can be "15 09", "15.09" or "15 сентября".
    if re.fullmatch(r"\d{1,2}\s+\d{1,2}", date_raw):
        a, b = date_raw.split()
        parsed = _parse_rest_day_month(f"{a}.{b}")
    else:
        parsed = _parse_rest_day_month(date_raw)
    if not parsed:
        await message.reply(_fmt("Рест") + "\n\n✦ Укажи дату как `15 09`, `15.09` или `15 сентября`.")
        return
    row = await _resolve_member_target(target_raw)
    if not row:
        await message.reply(_fmt("Рест") + "\n\n✦ Участник не найден во флуде.")
        return
    end_at = _next_rest_end(*parsed)
    now = _utc_iso()
    _q(lambda conn: (conn.execute(
        "INSERT INTO jf_rest(chat_id,user_id,start_at,end_at,note,created_by,created_at,active) VALUES(?,?,?,?,?,?,?,1) "
        "ON CONFLICT(chat_id,user_id) DO UPDATE SET end_at=excluded.end_at,note=excluded.note,created_by=excluded.created_by,created_at=excluded.created_at,active=1,ended_notified_at=NULL",
        (int(_primary_chat_id), int(row["user_id"]), now, end_at.isoformat(), "", int(_admin_id), now),
    ), conn.commit()))
    display = _mention_user(int(row["user_id"]), row["first_name"] or "участник", row["username"])
    local_tz = timezone(timedelta(hours=int(os.getenv("DEFAULT_TIMEZONE_OFFSET_HOURS", "3"))))
    await message.reply(_fmt("Рест оформлен") + f"\n\n🥹 {display}\n✦ До: {end_at.astimezone(local_tz).strftime('%d.%m.%Y')}")


async def natural_rest_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not _is_primary_chat(message.chat.id):
        return
    if not _admin_only(message):
        return
    await _set_rest_from_text(message)


async def cmd_rest(message: Message):
    if not _admin_only(message):
        return
    await _set_rest_from_text(message)


def _parse_birthday(value: str):
    raw = (value or "").strip()
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})", raw)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    try:
        datetime(2024, month, day)
    except ValueError:
        return None
    return day, month


# Community-provided birthday list. The labels are matched at runtime against
# the current participant's role/name/username, so people who leave the chat
# disappear automatically. The supplied 16 July entry is Митя.
BIRTHDAY_SEEDS = [
    (2, 7, "Кэйя"), (2, 7, "Лини"), (2, 12, "Хайтам"), (2, 27, "Ноэлль"), (2, 29, "Бай Чжу"),
    (3, 4, "Водяница"), (3, 18, "Кли"),
    (4, 3, "Ризли"), (4, 10, "Тиори"), (4, 22, "Альбедо"), (4, 30, "Ной"),
    (5, 13, "Странник"),
    (6, 12, "Иллуги"), (6, 24, "Тарталья"), (6, 30, "Дурин"),
    (7, 16, "Митя"),
    (8, 2, "Диона"), (8, 18, "Навия"),
    (9, 2, "Сяо"), (9, 26, "Рэйндоттир"),
    (10, 21, "Чжун Ли"), (10, 25, "Цици"),
    (11, 4, "Люмин"), (11, 12, "Аяка"), (11, 13, "Лаума"), (11, 15, "Венти"), (11, 28, "Капитано"),
    (12, 27, "Кавех"), (12, 30, "Ху Тао"),
]


def _person_key(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", (value or "").casefold().replace("ё", "е"))


def seed_birthday_directory() -> None:
    if not _primary_chat_id:
        return
    def op(conn):
        conn.executemany(
            "INSERT OR IGNORE INTO jf_birthday_directory(chat_id,person_label,person_key,month,day,source) VALUES(?,?,?,?,?,?)",
            [(int(_primary_chat_id), label, _person_key(label), month, day, "community_seed") for month, day, label in BIRTHDAY_SEEDS],
        )
        conn.commit()
    _q(op)


def _active_member_rows() -> list[dict[str, Any]]:
    rows = _q(lambda conn: conn.execute(
        "SELECT * FROM group_members WHERE chat_id=? AND active=1",
        (int(_primary_chat_id),),
    ).fetchall())
    return [dict(r) for r in rows]


def _resolve_seed_birthday(seed_row, members: list[dict[str, Any]]):
    label_key = _person_key(seed_row["person_label"])
    hits = []
    for m in members:
        candidates = {
            _person_key(m.get("first_name", "")),
            _person_key(m.get("last_name", "")),
            _person_key(m.get("username", "")),
            _person_key(m.get("role_name", "")),
            _person_key(m.get("role_key", "")),
        }
        full = _person_key((m.get("first_name", "") + " " + m.get("last_name", "")).strip())
        candidates.add(full)
        if label_key and label_key in candidates:
            hits.append(m)
    return hits[0] if len(hits) == 1 else None


async def _get_live_birthdays() -> list[dict[str, Any]]:
    seed_birthday_directory()
    members = _active_member_rows()
    # The cache can lag behind Telegram after a restart. Verify only seed matches
    # that are actually present in the chat before exposing or announcing them.
    seeds = _q(lambda conn: conn.execute(
        "SELECT * FROM jf_birthday_directory WHERE chat_id=? ORDER BY month,day",
        (int(_primary_chat_id),),
    ).fetchall())
    live = []
    seen_ids = set()
    for seed in seeds:
        member = _resolve_seed_birthday(seed, members)
        if not member:
            continue
        uid = int(member["user_id"])
        seen_ids.add(uid)
        live.append({
            "user_id": uid,
            "month": int(seed["month"]),
            "day": int(seed["day"]),
            "name": member.get("first_name") or seed["person_label"],
            "username": member.get("username") or "",
            "label": seed["person_label"],
            "source": "seed",
        })
    manual = _q(lambda conn: conn.execute(
        "SELECT b.*,gm.username,gm.first_name,gm.last_name,gm.active FROM jf_birthdays b LEFT JOIN group_members gm ON gm.chat_id=b.chat_id AND gm.user_id=b.user_id WHERE b.chat_id=?",
        (int(_primary_chat_id),),
    ).fetchall())
    for row in manual:
        if not row["active"]:
            continue
        uid = int(row["user_id"])
        if uid in seen_ids:
            continue
        try:
            member = await _bot.get_chat_member(int(_primary_chat_id), uid)
            if not (getattr(member, "status", None) in {"member","administrator","creator"} or (getattr(member, "status", None)=="restricted" and bool(getattr(member,"is_member",False)))):
                continue
        except Exception:
            continue
        live.append({
            "user_id": uid,
            "month": int(row["month"]),
            "day": int(row["day"]),
            "name": row["first_name"] or "участник",
            "username": row["username"] or "",
            "label": row["first_name"] or "участник",
            "source": "manual",
        })
    return live


async def _save_birthday_for_user(user_id: int, value: str, state: FSMContext | None = None):
    parsed = _parse_birthday(value)
    if not parsed:
        return False
    day, month = parsed
    _q(lambda conn: (conn.execute(
        "INSERT INTO jf_birthdays(chat_id,user_id,month,day,year,created_at) VALUES(?,?,?,?,NULL,?) "
        "ON CONFLICT(chat_id,user_id) DO UPDATE SET month=excluded.month,day=excluded.day,year=NULL,created_at=excluded.created_at",
        (int(_primary_chat_id), int(user_id), month, day, _utc_iso()),
    ), conn.commit()))
    if state:
        await state.clear()
    return True


async def cmd_birthday(message: Message, state: FSMContext):
    if message.chat.type != "private" or not message.from_user:
        return
    if not await _member_is_active_in_primary(message.from_user.id):
        await message.reply(f"{_fmt('Дни рождения')}\n\n✦ Эта функция доступна после вступления во флуд.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        if await _save_birthday_for_user(message.from_user.id, parts[1], state):
            parsed = _parse_birthday(parts[1])
            await message.reply(f"{_fmt('День рождения')}\n\n✅ Сохранено: {parsed[0]:02d}.{parsed[1]:02d}")
        else:
            await message.reply(f"{_fmt('День рождения')}\n\n✦ Формат: DD.MM, например 24.03")
        return
    await state.set_state(BirthdayState.waiting_date)
    await message.reply(f"{_fmt('День рождения')}\n\n✦ Укажите свою дату в формате DD.MM\n✦ Год указывать не нужно.")


async def birthday_state_text(message: Message, state: FSMContext):
    if message.chat.type != "private" or not message.from_user:
        return
    if not await _member_is_active_in_primary(message.from_user.id):
        await state.clear()
        return
    if await _save_birthday_for_user(message.from_user.id, message.text or "", state):
        parsed = _parse_birthday(message.text or "")
        await message.reply(f"{_fmt('День рождения')}\n\n✅ Сохранено: {parsed[0]:02d}.{parsed[1]:02d}")
    else:
        await message.reply(f"{_fmt('День рождения')}\n\n✦ Неверный формат. Укажите дату как DD.MM.")


async def cmd_birthdays(message: Message):
    if message.chat.type != "private" or not message.from_user:
        return
    if not await _member_is_active_in_primary(message.from_user.id):
        await message.reply(f"{_fmt('Дни рождения')}\n\n✦ Список доступен после вступления во флуд.")
        return
    live = await _get_live_birthdays()
    if not live:
        await message.reply(f"{_fmt('Дни рождения')}\n\n🎂 Пока нет сохранённых дней рождения участников, находящихся во флуде.")
        return
    now_dt = datetime.now(timezone.utc)
    def dist(r):
        current = r['month'] * 32 + r['day']
        today = now_dt.month * 32 + now_dt.day
        if current < today:
            current += 12 * 32
        return current - today
    ordered = sorted(live, key=dist)
    lines = [_fmt("Дни рождения"), ""]
    for r in ordered[:50]:
        name = f"@{r['username']}" if r['username'] else r['name']
        lines.append(f"✦ {r['day']:02d}.{r['month']:02d} — {name}")
    await message.reply("\n".join(lines))


async def cmd_restlist(message: Message):
    if message.chat.type not in {"private", "group", "supergroup"}:
        return
    if message.chat.type == "private" and not _admin_only(message):
        return
    if message.chat.type in {"group", "supergroup"} and not _is_primary_chat(message.chat.id):
        return
    rows = _q(lambda conn: conn.execute(
        "SELECT r.*,gm.username,gm.first_name FROM jf_rest r LEFT JOIN group_members gm ON gm.chat_id=r.chat_id AND gm.user_id=r.user_id WHERE r.chat_id=? AND r.active=1 AND r.end_at>? ORDER BY r.end_at",
        (int(_primary_chat_id), _utc_iso()),
    ).fetchall())
    if not rows:
        await message.reply(_fmt("Рестники") + "\n\n✦ Сейчас никто не находится на ресте.")
        return
    lines=[_fmt("Рестники"), ""]
    for r in rows:
        lines.append(f"🥹 {_mention_user(int(r['user_id']), r['first_name'] or 'участник', r['username'])} — до {str(r['end_at'])[:10]}")
    await message.reply("\n".join(lines))


async def natural_restlist_command(message: Message):
    if message.chat.type not in {"group", "supergroup"} or not _is_primary_chat(message.chat.id):
        return
    await cmd_restlist(message)


async def feature_maintenance_worker():
    last_bday_date = None
    while True:
        try:
            now_dt=datetime.now(timezone.utc)
            ended = _q(lambda conn: conn.execute(
                "SELECT r.*,gm.username,gm.first_name FROM jf_rest r LEFT JOIN group_members gm ON gm.chat_id=r.chat_id AND gm.user_id=r.user_id WHERE r.chat_id=? AND r.active=1 AND r.end_at<=?",
                (int(_primary_chat_id), now_dt.isoformat()),
            ).fetchall())
            if ended:
                _q(lambda conn: (conn.executemany(
                    "UPDATE jf_rest SET active=0,ended_notified_at=? WHERE chat_id=? AND user_id=? AND active=1",
                    [(_utc_iso(), int(r["chat_id"]), int(r["user_id"])) for r in ended],
                ), conn.commit()))
                for r in ended:
                    with suppress(Exception):
                        await _bot.send_message(int(_primary_chat_id), f"{_fmt('Рест окончен')}\n\n🥹 {_mention_user(int(r['user_id']), r['first_name'] or 'участник', r['username'])} снова свободен от реста.")
            day_key=now_dt.date().isoformat()
            if last_bday_date != day_key:
                live = await _get_live_birthdays()
                for row in live:
                    if int(row["month"]) != now_dt.month or int(row["day"]) != now_dt.day:
                        continue
                    already=_q(lambda conn: conn.execute("SELECT 1 FROM jf_birthday_notices WHERE chat_id=? AND user_id=? AND year=?", (int(_primary_chat_id),int(row["user_id"]),now_dt.year)).fetchone())
                    if already:
                        continue
                    _q(lambda conn,r=row: (conn.execute("INSERT OR IGNORE INTO jf_birthday_notices(chat_id,user_id,year,sent_at) VALUES(?,?,?,?)", (int(_primary_chat_id),int(r["user_id"]),now_dt.year,_utc_iso())),conn.commit()))
                    with suppress(Exception):
                        name = f"@{row['username']}" if row.get('username') else row.get('name') or 'участника'
                        await _bot.send_message(int(_primary_chat_id), f"༺ 𓆩 ✧ 𓆪 ༻\n\n🎂 Сегодня день рождения у {name}!\n\n✦ Поздравляем от Justice Faite ♡")
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
    seed_birthday_directory()

    private = F.chat.type == "private"
    private_admin = private & (F.from_user.id == _admin_id)

    _dp.message.register(cmd_birthday, Command("jf_birthday"), private)
    _dp.message.register(cmd_birthdays, Command("jf_birthdays"), private)
    _dp.message.register(birthday_state_text, BirthdayState.waiting_date, private, F.text)

    _dp.message.register(handle_iris_olds_response, lambda m: m.chat.type in {"group", "supergroup"} and _is_primary_chat(m.chat.id) and bool(m.from_user and m.from_user.is_bot and m.from_user.username and IRIS_USERNAME and m.from_user.username.casefold()==IRIS_USERNAME.casefold()))
    _dp.message.register(applications_list, Command("jf_applications"), private_admin)
    _dp.message.register(cmd_dashboard, Command("jf_dashboard"), private_admin)
    _dp.message.register(cmd_awards_check, Command("jf_awards"), private_admin)
    _dp.message.register(cmd_award, Command("jf_award"), private_admin)
    _dp.message.register(cmd_iris_sync, Command("jf_iris_sync"), private_admin)
    _dp.message.register(cmd_warn, Command("jf_warn"), private_admin)
    _dp.message.register(cmd_warnings, Command("jf_warnings"), private_admin)
    _dp.message.register(cmd_remove_warning, Command("jf_warn_remove"), private_admin)
    _dp.message.register(cmd_rest, Command("jf_rest"), private_admin)
    _dp.message.register(cmd_rest, Command("rest"), private_admin)
    _dp.message.register(cmd_restlist, Command("jf_restlist"), private_admin)

    _dp.message.register(natural_restlist_command, F.text.regexp(r"(?iu)^\s*рестники\s*$"))
    _dp.message.register(natural_rest_command, F.text.regexp(r"(?iu)^\s*выдать\s+рест\s+.+$"))

    _dp.callback_query.register(join_rules_callback, F.data.startswith(FEATURE_PREFIX + "join_"))
    _dp.message.register(join_role_text, JoinApplicationState.waiting_role, private, F.text)
    _dp.message.register(join_document_photo, JoinApplicationState.waiting_document, private, F.photo)
    _dp.message.register(join_document_nonphoto, JoinApplicationState.waiting_document, private)
    _dp.message.register(join_role_fallback, private, F.text)
    _dp.message.register(join_document_photo_fallback, private, F.photo)
    _dp.callback_query.register(application_callback, F.data.startswith(FEATURE_PREFIX + "app_"))
    _dp.chat_join_request.register(handle_join_request)


"""Justice Faite: функции сообщества поверх основного бота.

Здесь живут: предупреждения, рест, дни рождения, сводка и фоновый обработчик.
Награды вынесены в `awards.py`. Заявки и вступление через бота удалены.

Все команды проходят через `cmdcfg.gate`, поэтому у каждой есть собственная
карточка настроек (ЛС владельца → /settings).
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

import awards
import cmdcfg
import ui

logger = logging.getLogger("justice_features")

# Привязываются в register_justice_features().
_bot = None
_dp = None
_db_op = None
_now = None
_upsert_group_member = None
_get_member = None
_primary_chat_id = 0
_admin_id = 0
_is_primary_chat = None

GROUP_TYPES = {"group", "supergroup"}


class BirthdayState(StatesGroup):
    waiting_date = State()


# --------------------------------------------------------------------------
# База данных
# --------------------------------------------------------------------------

def _q(fn, *args):
    return _db_op(fn, *args)


def _effective_primary_chat_id() -> int:
    """Resolve a stale/missing configured ID from a sole persisted binding."""
    configured = int(_primary_chat_id or 0)
    if _db_op is None:
        return configured
    try:
        ids = _q(lambda conn: [int(row[0]) for row in conn.execute(
            "SELECT group_chat_id FROM managed_group ORDER BY group_chat_id"
        ).fetchall()])
    except Exception:
        return configured
    if configured in ids:
        return configured
    return ids[0] if len(ids) == 1 else configured


def _utc_iso() -> str:
    return _now() if _now else datetime.now(timezone.utc).isoformat()


def init_justice_features_db() -> None:
    def op(conn):
        conn.executescript(
            """
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

            -- Canonical warning ledger; jf_manager.Store adds its remaining schema too.
            CREATE TABLE IF NOT EXISTS jf_manager_warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                removed_at TEXT,
                removed_by INTEGER,
                removal_note TEXT NOT NULL DEFAULT '',
                level INTEGER NOT NULL DEFAULT 1,
                eligible_at TEXT,
                legacy_warning_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_jf_manager_warnings_active
                ON jf_manager_warnings(chat_id,user_id,removed_at,expires_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jf_manager_warnings_legacy
                ON jf_manager_warnings(chat_id,legacy_warning_id) WHERE legacy_warning_id IS NOT NULL;

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
        # Migrate away from the old, incorrect character-birthday inference.
        conn.execute("DELETE FROM jf_birthday_directory WHERE source='community_seed'")
        rest_cols = {row[1] for row in conn.execute("PRAGMA table_info(jf_rest)").fetchall()}
        if "ended_notified_at" not in rest_cols:
            conn.execute("ALTER TABLE jf_rest ADD COLUMN ended_notified_at TEXT")
        # Merge historical community warnings into the manager's canonical ledger.
        # The legacy table remains untouched as an audit source; legacy_warning_id
        # prevents duplicate imports if startup or migration runs more than once.
        manager_cols = {row[1] for row in conn.execute("PRAGMA table_info(jf_manager_warnings)").fetchall()}
        for column, definition in {
            "level": "INTEGER NOT NULL DEFAULT 1",
            "eligible_at": "TEXT",
            "legacy_warning_id": "INTEGER",
        }.items():
            if column not in manager_cols:
                conn.execute(f"ALTER TABLE jf_manager_warnings ADD COLUMN {column} {definition}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jf_manager_warnings_legacy "
            "ON jf_manager_warnings(chat_id,legacy_warning_id) WHERE legacy_warning_id IS NOT NULL"
        )
        conn.execute(
            """INSERT OR IGNORE INTO jf_manager_warnings
               (chat_id,user_id,moderator_id,reason,created_at,expires_at,removed_at,removed_by,level,eligible_at,legacy_warning_id)
               SELECT w.chat_id,w.user_id,w.issued_by,w.reason,w.issued_at,
                      '9999-12-31T23:59:59+00:00',w.removed_at,w.removed_by,w.level,w.eligible_at,w.id
               FROM jf_warnings w"""
        )
        conn.commit()

    _q(op)


# --------------------------------------------------------------------------
# Участники
# --------------------------------------------------------------------------

def _active_member_rows() -> list[dict[str, Any]]:
    rows = _q(lambda conn: conn.execute(
        "SELECT * FROM group_members WHERE chat_id=? AND active=1", (_effective_primary_chat_id(),)).fetchall())
    return [dict(r) for r in rows]


def member_by_id(user_id: int, *, active_only: bool = False) -> Optional[dict]:
    sql = "SELECT * FROM group_members WHERE chat_id=? AND user_id=?" + (" AND active=1" if active_only else "")
    row = _q(lambda conn: conn.execute(sql, (int(_primary_chat_id), int(user_id))).fetchone())
    return dict(row) if row else None


def member_by_username(username: str, *, active_only: bool = True) -> Optional[dict]:
    name = (username or "").strip().lstrip("@").lower()
    if not name:
        return None
    sql = ("SELECT * FROM group_members WHERE chat_id=? AND lower(username)=?"
           + (" AND active=1" if active_only else "") + " ORDER BY active DESC LIMIT 1")
    row = _q(lambda conn: conn.execute(sql, (int(_primary_chat_id), name)).fetchone())
    return dict(row) if row else None


async def lookup_member(user_id: int) -> Optional[dict]:
    """Проверяет участника через Telegram и добавляет в базу (для ссылок из списка Ирис)."""
    try:
        member = await _bot.get_chat_member(int(_primary_chat_id), int(user_id))
    except Exception:
        return None
    status = getattr(member, "status", None)
    active = status in {"member", "administrator", "creator"} or (
        status == "restricted" and bool(getattr(member, "is_member", False)))
    user = getattr(member, "user", None)
    if not active or user is None or getattr(user, "is_bot", False):
        return None
    with suppress(Exception):
        _upsert_group_member(int(_primary_chat_id), user, active=True)
    return member_by_id(int(user_id))


async def resolve_target(message: Message, args: str, *, active_only: bool = True) -> tuple[Optional[dict], str]:
    """Цель команды: ответ на сообщение → @username → числовой ID. Возвращает (участник, остаток текста)."""
    args = (args or "").strip()
    reply = getattr(message, "reply_to_message", None)
    if reply is not None and getattr(reply, "from_user", None) and not reply.from_user.is_bot:
        user = reply.from_user
        row = member_by_id(int(user.id))
        if row is None and message.chat.type in GROUP_TYPES:
            with suppress(Exception):
                _upsert_group_member(int(message.chat.id), user, active=True)
            row = member_by_id(int(user.id))
        return row, args
    if not args:
        return None, ""
    first, _, rest = args.partition(" ")
    if first.startswith("@") or ui.clean_username(first):
        row = member_by_username(first, active_only=active_only)
        if row:
            return row, rest.strip()
    if first.lstrip("-").isdigit():
        row = member_by_id(int(first), active_only=active_only)
        if row:
            return row, rest.strip()
    return None, args


def _command_args(message: Message) -> str:
    text = (message.text or "").strip()
    m = re.match(r"^/\w+(?:@\w+)?\s*(.*)$", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _mention_html(row: dict) -> str:
    username = (row.get("username") or "").strip()
    if username:
        return "@" + html.escape(username, quote=False)
    name = html.escape((row.get("first_name") or "участник").strip() or "участник", quote=False)
    return f'<a href="tg://user?id={int(row["user_id"])}">{name}</a>'


def _render_html(template: str, **named: str) -> str:
    """Экранирует шаблон владельца и подставляет уже безопасный HTML в {name}."""
    text = html.escape(template or "", quote=False)
    for key, value in named.items():
        text = text.replace("{" + key + "}", value)
    return text


async def _is_active_member(user_id: int) -> bool:
    return (await cmdcfg.level_of(int(user_id))) != "all"


def _is_owner(message: Message) -> bool:
    return bool(message.from_user and int(message.from_user.id) == int(_admin_id))


async def _reply(message: Message, key: str, text: str, **kwargs):
    return await cmdcfg.respond(message, key, text, **kwargs)


# --------------------------------------------------------------------------
# Награды (локальная запись; команды Ирис — в awards.py)
# --------------------------------------------------------------------------

def issue_local_award(chat_id: int, user_id: int, code: str, title: str, description: str,
                      issued_by: Optional[int], source: str) -> bool:
    def op(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO jf_achievements(chat_id,user_id,code,title,description,issued_at,issued_by,source) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (int(chat_id), int(user_id), code, title, (description or "")[:1000], _utc_iso(), issued_by, source))
        conn.commit()
        return cur.rowcount > 0
    return bool(_q(op))


# --------------------------------------------------------------------------
# Предупреждения
# --------------------------------------------------------------------------

def warning_eligible_at(level: int, issued_at: str) -> Optional[str]:
    days = {1: cmdcfg.get_int("warn", "l1_days"), 2: cmdcfg.get_int("warn", "l2_days")}.get(level)
    dt = ui.parse_iso(issued_at)
    if not dt or not days:
        return None
    return (dt + timedelta(days=days)).isoformat()


def active_warnings(chat_id: int, user_id: int) -> list:
    return _q(lambda conn: conn.execute(
        "SELECT * FROM jf_manager_warnings WHERE chat_id=? AND user_id=? AND removed_at IS NULL AND expires_at>? ORDER BY id",
        (int(chat_id), int(user_id), _utc_iso())).fetchall())


def issue_warning(chat_id: int, user_id: int, reason: str, issued_by: int):
    """Create a warning in the manager's canonical ledger, shared by all modules."""
    now = _utc_iso()
    far_future = "9999-12-31T23:59:59+00:00"

    def op(conn):
        active = conn.execute(
            "SELECT id,level FROM jf_manager_warnings WHERE chat_id=? AND user_id=? AND removed_at IS NULL AND expires_at>? ORDER BY id",
            (int(chat_id), int(user_id), now)).fetchall()
        level = len(active) + 1
        eligible = warning_eligible_at(level, now)
        cur = conn.execute(
            "INSERT INTO jf_manager_warnings(chat_id,user_id,moderator_id,reason,created_at,expires_at,level,eligible_at) VALUES(?,?,?,?,?,?,?,?)",
            (int(chat_id), int(user_id), int(issued_by), reason[:2000], now, far_future, level, eligible))
        for old in active:
            conn.execute("UPDATE jf_manager_warnings SET eligible_at=? WHERE id=?",
                         (warning_eligible_at(int(old["level"]), now), int(old["id"])))
        conn.commit()
        return cur.lastrowid, level, eligible, len(active) + 1
    return _q(op)


async def cmd_warn(message: Message):
    if not await cmdcfg.gate(message, "warn"):
        return
    target, reason = await resolve_target(message, _command_args(message))
    if not target or len(reason) < 3:
        await _reply(message, "warn", ui.card(
            "Предупреждение",
            ui.lines(ui.bullet("/jf_warn @username причина"),
                     ui.bullet("Или ответом на сообщение: /jf_warn причина"),
                     ui.bullet("Причина — минимум 3 символа."))))
        return
    warn_id, level, eligible, total = issue_warning(_primary_chat_id, target["user_id"], reason, message.from_user.id)
    who = ui.person(target)
    lines = [ui.kv("Участник", who), ui.kv("Уровень", level), ui.kv("Активных предупреждений", total), ui.kv("Причина", ui.short(reason, 200))]
    if eligible:
        lines.append(ui.kv("Снять можно после", ui.fmt_date(eligible)))
    else:
        lines.append(ui.kv("Снятие", "по решению владельца"))
    await _reply(message, "warn", ui.card(f"Предупреждение №{warn_id}", ui.lines(*lines)))

    if cmdcfg.get_bool("warn", "announce") and message.chat.type == "private":
        with suppress(Exception):
            await _bot.send_message(
                int(_primary_chat_id),
                f"⚠️ {_mention_html(target)}: предупреждение ({level}).\n{html.escape(reason, quote=False)}",
                parse_mode="HTML")
    if cmdcfg.get_bool("warn", "notify_user"):
        with suppress(Exception):
            await _bot.send_message(int(target["user_id"]), ui.card("Предупреждение", ui.kv("Причина", ui.short(reason, 300))))


async def cmd_warnings(message: Message):
    if not await cmdcfg.gate(message, "warnings"):
        return
    uid = int(message.from_user.id)
    level = await cmdcfg.level_of(uid)
    args = _command_args(message)
    target, _ = await resolve_target(message, args, active_only=False)
    if args or getattr(message, "reply_to_message", None):
        if cmdcfg.ACCESS_ORDER.get(level, 0) < cmdcfg.ACCESS_ORDER["admins"]:
            await _reply(message, "warnings", ui.fail("Чужие предупреждения может смотреть только администрация."))
            return
        if not target:
            await _reply(message, "warnings", ui.fail("Участник не найден."))
            return
    else:
        target = member_by_id(uid) or {"user_id": uid, "first_name": getattr(message.from_user, "first_name", ""),
                                       "username": getattr(message.from_user, "username", "")}
    limit = cmdcfg.get_int("warnings", "limit")
    rows = _q(lambda conn: conn.execute(
        "SELECT * FROM jf_manager_warnings WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
        (int(_primary_chat_id), int(target["user_id"]), limit)).fetchall())
    if not rows:
        await _reply(message, "warnings", ui.card("Предупреждения", ui.ok(f"У {ui.person(target)} нет предупреждений.")))
        return
    active = sum(1 for r in rows if not r["removed_at"])
    items = []
    for r in rows:
        status = "снято" if r["removed_at"] else "активно"
        extra = ""
        if not r["removed_at"] and r["eligible_at"]:
            extra = f" · снять можно с {ui.fmt_date(r['eligible_at'])}"
        items.append(f"#{r['id']} · уровень {r['level']} · {status}{extra}\n   {ui.short(r['reason'], 160)} ({ui.fmt_date(r['created_at'])})")
    await _reply(message, "warnings", ui.card(
        f"Предупреждения · {ui.person(target)}", ui.kv("Активных", active), "\n".join(items)))


async def cmd_remove_warning(message: Message):
    if not await cmdcfg.gate(message, "warn_remove"):
        return
    parts = _command_args(message).split()
    if not parts or not parts[0].lstrip("#").isdigit():
        await _reply(message, "warn_remove", ui.card("Снять предупреждение", ui.bullet("/jf_warn_remove ID"),
                                                    ui.bullet("ID виден в /jf_warnings. «force» в конце снимает досрочно.")))
        return
    wid = int(parts[0].lstrip("#"))
    force = len(parts) > 1 and parts[1].lower() in {"force", "досрочно", "!"}
    enforce = cmdcfg.get_bool("warn_remove", "enforce_wait") and not force

    def op(conn):
        row = conn.execute("SELECT * FROM jf_manager_warnings WHERE id=? AND chat_id=?", (wid, int(_primary_chat_id))).fetchone()
        if not row:
            return "missing", None
        if row["removed_at"]:
            return "removed", row
        eligible = ui.parse_iso(row["eligible_at"]) if row["eligible_at"] else None
        if enforce and eligible and eligible > datetime.now(timezone.utc):
            return "early", row
        conn.execute("UPDATE jf_manager_warnings SET removed_at=?,removed_by=? WHERE id=?", (_utc_iso(), int(message.from_user.id), wid))
        conn.commit()
        return "ok", row

    status, row = _q(op)
    text = {
        "missing": ui.fail("Предупреждение не найдено."),
        "removed": ui.note("Оно уже снято."),
        "early": ui.warning(f"Срок снятия ещё не наступил (с {ui.fmt_date(row['eligible_at']) if row else '—'}). Добавьте «force», чтобы снять досрочно.") if row else "",
        "ok": ui.ok(f"Предупреждение #{wid} снято."),
    }[status]
    await _reply(message, "warn_remove", text)


# --------------------------------------------------------------------------
# Рест
# --------------------------------------------------------------------------

MONTH_ALIASES = {
    "января": 1, "январь": 1, "янв": 1, "февраля": 2, "февраль": 2, "фев": 2,
    "марта": 3, "март": 3, "мар": 3, "апреля": 4, "апрель": 4, "апр": 4,
    "мая": 5, "май": 5, "июня": 6, "июнь": 6, "июн": 6, "июля": 7, "июль": 7, "июл": 7,
    "августа": 8, "август": 8, "авг": 8, "сентября": 9, "сентябрь": 9, "сен": 9,
    "октября": 10, "октябрь": 10, "окт": 10, "ноября": 11, "ноябрь": 11, "ноя": 11,
    "декабря": 12, "декабрь": 12, "дек": 12,
}
_MONTH_WORDS = "|".join(sorted(MONTH_ALIASES, key=len, reverse=True))
_DUR_RE = re.compile(r"(?iu)(?:\bна\s+)?(?<!\d)(\d{1,3})\s*(недел\w*|нед\b\.?|мес\w*|дней|дня|день|дн\b\.?|д\b\.?)")
_DATE_WORD_RE = re.compile(rf"(?iu)(?:\bдо\s+)?(?<!\d)(\d{{1,2}})\s+({_MONTH_WORDS})\b")
_DATE_NUM_RE = re.compile(r"(?iu)(?:\bдо\s+)?(?<!\d)(\d{1,2})\s*[.\-/\s]\s*(\d{1,2})(?!\d)")


def _valid_day_month(day: int, month: int) -> bool:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return False
    try:
        datetime(2024, month, day)
        return True
    except ValueError:
        return False


def _end_of_local_day(base: datetime) -> datetime:
    return base.replace(hour=23, minute=59, second=59, microsecond=0)


def parse_rest_period(text: str, *, now_local: Optional[datetime] = None) -> tuple[Optional[datetime], Optional[int], str]:
    """Срок реста из текста. Возвращает (конец в UTC | None, дней | None, остаток-заметка)."""
    now_local = now_local or ui.local_now()
    raw = (text or "").strip()
    m = _DUR_RE.search(raw)
    if m:
        number, unit = int(m.group(1)), m.group(2).casefold()
        days = number * (7 if unit.startswith("нед") else 30 if unit.startswith("мес") else 1)
        end_local = _end_of_local_day(now_local + timedelta(days=days))
        note = (raw[:m.start()] + " " + raw[m.end():]).strip()
        return end_local.astimezone(timezone.utc), days, " ".join(note.split())
    for rx in (_DATE_WORD_RE, _DATE_NUM_RE):
        m = rx.search(raw)
        if not m:
            continue
        day = int(m.group(1))
        month = MONTH_ALIASES.get(m.group(2).casefold()) if rx is _DATE_WORD_RE else int(m.group(2))
        if not month or not _valid_day_month(day, month):
            continue
        candidate = _end_of_local_day(datetime(now_local.year, month, day, tzinfo=now_local.tzinfo))
        if candidate <= now_local:
            candidate = _end_of_local_day(datetime(now_local.year + 1, month, day, tzinfo=now_local.tzinfo))
        days = max(1, (candidate.date() - now_local.date()).days)
        note = (raw[:m.start()] + " " + raw[m.end():]).strip()
        return candidate.astimezone(timezone.utc), days, " ".join(note.split())
    return None, None, raw


async def _rest_args(message: Message) -> str:
    text = (message.text or "").strip()
    m = re.match(r"(?iu)^(?:/\w+(?:@\w+)?|выдать\s+рест|снять\s+рест)\s*(.*)$", text, re.DOTALL)
    return m.group(1).strip() if m else ""


RESTFORMAT = ui.card(
    "Рест",
    ui.lines(
        ui.section("Форматы"),
        ui.bullet("выдать рест @user 15.09"),
        ui.bullet("выдать рест @user 15 сентября"),
        ui.bullet("выдать рест @user на 7 дней"),
        ui.bullet("ответом на сообщение: выдать рест на 2 недели"),
        ui.bullet("в конце можно добавить заметку"),
    ),
)


async def cmd_rest(message: Message):
    if not await cmdcfg.gate(message, "rest"):
        return
    target, rest_text = await resolve_target(message, await _rest_args(message))
    end_utc, days, note = parse_rest_period(rest_text)
    if not target and rest_text:
        await _reply(message, "rest", ui.fail("Участник не найден среди участников флуда."))
        return
    if not target or end_utc is None:
        await _reply(message, "rest", RESTFORMAT)
        return
    limit = cmdcfg.get_int("rest", "max_days")
    if days and days > limit:
        await _reply(message, "rest", ui.fail(f"Слишком долго: максимум {ui.days_word(limit)}."))
        return
    now = _utc_iso()
    note = note[:300]

    def op(conn):
        previous = conn.execute("SELECT end_at,active FROM jf_rest WHERE chat_id=? AND user_id=?",
                                (int(_primary_chat_id), int(target["user_id"]))).fetchone()
        conn.execute(
            "INSERT INTO jf_rest(chat_id,user_id,start_at,end_at,note,created_by,created_at,active,ended_notified_at) "
            "VALUES(?,?,?,?,?,?,?,1,NULL) ON CONFLICT(chat_id,user_id) DO UPDATE SET "
            "end_at=excluded.end_at,note=excluded.note,created_by=excluded.created_by,created_at=excluded.created_at,"
            "active=1,ended_notified_at=NULL",
            (int(_primary_chat_id), int(target["user_id"]), now, end_utc.isoformat(), note, int(message.from_user.id), now))
        conn.commit()
        return bool(previous and previous["active"])

    extended = _q(op)
    lines = [ui.kv("Участник", ui.person(target)), ui.kv("До", ui.fmt_date(end_utc)),
             ui.kv("Осталось", ui.left_text(end_utc))]
    if note:
        lines.append(ui.kv("Заметка", ui.short(note, 120)))
    head = "Рест продлён" if extended else "Рест оформлен"
    await _reply(message, "rest", ui.card(head, ui.lines(*lines)))
    if message.chat.type == "private" and cmdcfg.get_bool("rest", "announce"):
        with suppress(Exception):
            await _bot.send_message(
                int(_primary_chat_id),
                f"🥹 {_mention_html(target)} на ресте до {ui.fmt_date(end_utc)}.", parse_mode="HTML")


REST_SELF_FORMAT = ui.card(
    "Взять рест себе",
    ui.lines(
        ui.bullet("взять рест на 7 дней"),
        ui.bullet("взять рест на 2 недели"),
        ui.bullet("взять рест до 15.11"),
        ui.note("Самостоятельный рест ограничен 14 днями. Дату можно указать вместо срока."),
    ),
)


def _self_rest_args(message: Message) -> str:
    text = (message.text or "").strip()
    match = re.match(r"(?iu)^(?:взять\s+рест|/take_rest(?:@\w+)?)\s*(.*)$", text, re.DOTALL)
    return match.group(1).strip() if match else ""


async def cmd_take_rest_self(message: Message):
    """Участник оформляет короткий рест только для собственной учётной записи."""
    if not await cmdcfg.gate(message, "rest_self"):
        return
    if message.chat.type not in GROUP_TYPES or int(message.chat.id) != int(_primary_chat_id):
        return
    user = message.from_user
    if user is None or getattr(user, "is_bot", False):
        return
    args = _self_rest_args(message)
    if not args:
        await _reply(message, "rest_self", REST_SELF_FORMAT)
        return

    end_utc, days, note = parse_rest_period(args)
    if end_utc is None or days is None:
        await _reply(message, "rest_self", REST_SELF_FORMAT)
        return
    limit = cmdcfg.get_int("rest_self", "max_days")
    if days < 1 or days > limit:
        await _reply(message, "rest_self", ui.fail(f"Самостоятельный рест можно оформить максимум на {ui.days_word(limit)}."))
        return

    # A message in the configured group is proof of current membership; do not
    # require a second getChatMember round-trip just to save the self-request.
    with suppress(Exception):
        _upsert_group_member(int(_primary_chat_id), user, active=True)

    now = _utc_iso()
    note = note[:300]

    def op(conn):
        previous = conn.execute(
            "SELECT active FROM jf_rest WHERE chat_id=? AND user_id=?",
            (int(_primary_chat_id), int(user.id)),
        ).fetchone()
        conn.execute(
            "INSERT INTO jf_rest(chat_id,user_id,start_at,end_at,note,created_by,created_at,active,ended_notified_at) "
            "VALUES(?,?,?,?,?,?,?,1,NULL) ON CONFLICT(chat_id,user_id) DO UPDATE SET "
            "end_at=excluded.end_at,note=excluded.note,created_by=excluded.created_by,created_at=excluded.created_at,"
            "active=1,ended_notified_at=NULL",
            (int(_primary_chat_id), int(user.id), now, end_utc.isoformat(), note, int(user.id), now),
        )
        conn.commit()
        return bool(previous and previous["active"])

    extended = _q(op)
    current = member_by_id(int(user.id)) or {
        "user_id": int(user.id), "username": getattr(user, "username", ""),
        "first_name": getattr(user, "first_name", "участник"),
    }
    lines = [ui.kv("Участник", ui.person(current)), ui.kv("До", ui.fmt_date(end_utc)),
             ui.kv("Осталось", ui.left_text(end_utc))]
    if note:
        lines.append(ui.kv("Заметка", ui.short(note, 120)))
    await _reply(message, "rest_self", ui.card("Твой рест продлён" if extended else "Твой рест оформлен",
                                                ui.lines(*lines)))


async def cmd_unrest(message: Message):
    if not await cmdcfg.gate(message, "unrest"):
        return
    target, _ = await resolve_target(message, await _rest_args(message), active_only=False)
    if not target:
        await _reply(message, "unrest", ui.card("Снять рест", ui.bullet("снять рест @user"), ui.bullet("или ответом на сообщение")))
        return

    def op(conn):
        cur = conn.execute("UPDATE jf_rest SET active=0,ended_notified_at=? WHERE chat_id=? AND user_id=? AND active=1",
                           (_utc_iso(), int(_primary_chat_id), int(target["user_id"])))
        conn.commit()
        return cur.rowcount > 0

    done = _q(op)
    await _reply(message, "unrest", ui.ok(f"Рест снят: {ui.person(target)}.") if done else ui.note(f"У {ui.person(target)} нет активного реста."))


def active_rest_rows():
    return _q(lambda conn: conn.execute(
        "SELECT r.*,gm.username,gm.first_name FROM jf_rest r LEFT JOIN group_members gm "
        "ON gm.chat_id=r.chat_id AND gm.user_id=r.user_id WHERE r.chat_id=? AND r.active=1 AND r.end_at>? ORDER BY r.end_at",
        (int(_primary_chat_id), _utc_iso())).fetchall())


async def cmd_restlist(message: Message):
    if not await cmdcfg.gate(message, "restlist"):
        return
    rows = active_rest_rows()
    if not rows:
        await _reply(message, "restlist", ui.card("Рестники", ui.note("Сейчас никто не находится на ресте.")))
        return
    show_note = cmdcfg.get_bool("restlist", "show_reason")
    items = []
    for r in rows:
        line = f"🥹 {ui.person(dict(r))} — до {ui.fmt_date(r['end_at'])} ({ui.left_text(r['end_at'])})"
        if show_note and r["note"]:
            line += f"\n   ⌁ {ui.short(r['note'], 100)}"
        items.append(line)
    await _reply(message, "restlist", ui.card(f"Рестники · {len(rows)}", "\n".join(items)))


# --------------------------------------------------------------------------
# Дни рождения
# --------------------------------------------------------------------------

# Birthday dates are personal data: never infer them from Genshin roles,
# display names, tags, or a built-in character calendar. Only explicit entries
# in jf_birthdays are shown.


def parse_birthday(value: str) -> Optional[tuple[int, int]]:
    raw = (value or "").strip().casefold()
    m = re.fullmatch(r"(\d{1,2})\s*[.\-/\s]\s*(\d{1,2})(?:[.\-/]\d{2,4})?", raw)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
    else:
        m = re.fullmatch(rf"(\d{{1,2}})\s+({_MONTH_WORDS})", raw)
        if not m:
            return None
        day, month = int(m.group(1)), MONTH_ALIASES[m.group(2)]
    return (day, month) if _valid_day_month(day, month) else None


def seed_birthday_directory() -> None:
    """Legacy migration hook: remove the old character-to-member birthday seeds.

    The previous implementation matched a user's selected Genshin role to a
    character birthday and displayed it as the member's personal birthday.
    That inference was incorrect and is intentionally disabled.
    """
    if not _db_op:
        return
    try:
        _q(lambda conn: (conn.execute(
            "DELETE FROM jf_birthday_directory WHERE source='community_seed'"
        ), conn.commit()))
    except Exception:
        logger.exception("Could not remove legacy inferred birthday entries")


def live_birthdays() -> list[dict]:
    """Only dates explicitly saved by the participant or entered by the owner."""
    members = _active_member_rows()
    by_id = {int(m["user_id"]): m for m in members}
    manual = _q(lambda conn: conn.execute(
        "SELECT * FROM jf_birthdays WHERE chat_id=? ORDER BY month,day",
        (int(_effective_primary_chat_id()),),
    ).fetchall())
    result = []
    for row in manual:
        member = by_id.get(int(row["user_id"]))
        if not member:
            continue
        result.append({
            "user_id": int(row["user_id"]),
            "month": int(row["month"]),
            "day": int(row["day"]),
            "row": member,
        })
    return result


def days_until(month: int, day: int, today: Optional[datetime] = None) -> int:
    today = today or ui.local_now()
    for year in (today.year, today.year + 1, today.year + 2, today.year + 4):
        try:
            target = datetime(year, month, day, tzinfo=today.tzinfo)
        except ValueError:      # 29 февраля
            continue
        if target.date() >= today.date():
            return (target.date() - today.date()).days
    return 366


def _save_birthday(user_id: int, day: int, month: int) -> None:
    _q(lambda conn: (conn.execute(
        "INSERT INTO jf_birthdays(chat_id,user_id,month,day,year,created_at) VALUES(?,?,?,?,NULL,?) "
        "ON CONFLICT(chat_id,user_id) DO UPDATE SET month=excluded.month,day=excluded.day,year=NULL,created_at=excluded.created_at",
        (int(_effective_primary_chat_id()), int(user_id), month, day, _utc_iso())), conn.commit()))


def _delete_birthday(user_id: int) -> bool:
    def op(conn):
        cur = conn.execute("DELETE FROM jf_birthdays WHERE chat_id=? AND user_id=?", (int(_effective_primary_chat_id()), int(user_id)))
        conn.commit()
        return cur.rowcount > 0
    return bool(_q(op))


def _saved_birthday(user_id: int) -> Optional[tuple[int, int]]:
    row = _q(lambda conn: conn.execute("SELECT day,month FROM jf_birthdays WHERE chat_id=? AND user_id=?",
                                       (int(_effective_primary_chat_id()), int(user_id))).fetchone())
    return (int(row["day"]), int(row["month"])) if row else None


_BDAY_HELP = ui.card("День рождения", ui.lines(
    ui.bullet("Отправьте дату: 24.03 или «24 марта». Год не нужен."),
    ui.bullet("Во флуде: «др 24.03» — дата сохранится сразу в общей карте."),
    ui.bullet("«удалить» — убрать сохранённую дату.")))


def _birthday_args(message: Message) -> str:
    text = (message.text or "").strip()
    slash = _command_args(message)
    if text.startswith("/"):
        return slash
    match = re.match(r"(?iu)^(?:др|мой\s+др|день\s+рождения|мой\s+день\s+рождения)\s*(.*)$", text, re.DOTALL)
    return match.group(1).strip() if match else ""


async def cmd_birthday(message: Message, state: FSMContext):
    if not await cmdcfg.gate(message, "birthday"):
        return
    uid = int(message.from_user.id)
    args = _birthday_args(message)
    if _is_owner(message) and args:                    # владелец задаёт дату другому
        target, rest = await resolve_target(message, args)
        if target and rest:
            parsed = parse_birthday(rest)
            if not parsed:
                await message.answer(ui.fail("Дата: ДД.ММ, например 24.03."))
                return
            _save_birthday(int(target["user_id"]), *parsed)
            await message.answer(ui.ok(f"{ui.person(target)}: {parsed[0]:02d}.{parsed[1]:02d}"))
            return
    if message.chat.type in GROUP_TYPES:
        # A user who has just sent a message in the primary group is already
        # known to be present. Store that Telegram identity before saving the
        # date so the birthday immediately appears in the shared directory.
        if int(message.chat.id) != int(_primary_chat_id):
            return
        user = message.from_user
        if user is None or getattr(user, "is_bot", False):
            return
        with suppress(Exception):
            _upsert_group_member(int(_primary_chat_id), user, active=True)
        current = _saved_birthday(uid)
        if args:
            if args.strip().casefold() in {"удалить", "убрать", "delete", "-"}:
                await message.answer(ui.ok("Дата дня рождения удалена.") if _delete_birthday(uid)
                                     else ui.note("У тебя не было сохранённой даты."))
                return
            parsed = parse_birthday(args)
            if not parsed:
                await message.answer(ui.fail("Не понял дату. Напиши: др 24.03 или др 24 марта."))
                return
            _save_birthday(uid, *parsed)
            await message.answer(ui.ok(f"День рождения сохранён: {parsed[0]:02d}.{parsed[1]:02d}."))
            return
        head = ui.bullet(f"Сохранено сейчас: {current[0]:02d}.{current[1]:02d}") if current else None
        await message.answer(ui.card("Мой день рождения", head,
                                     ui.bullet("Напиши прямо в чате: др 24.03"),
                                     ui.bullet("Чтобы удалить дату: др удалить")))
        return
    if not await _is_active_member(uid) and not _is_owner(message):
        await message.answer(ui.card("Дни рождения", ui.note("Доступно участникам флуда.")))
        return
    if args:
        await _store_birthday_text(message, state, args)
        return
    current = _saved_birthday(uid)
    await state.set_state(BirthdayState.waiting_date)
    head = ui.bullet(f"Сохранено сейчас: {current[0]:02d}.{current[1]:02d}") if current else None
    await message.answer(ui.card("День рождения", head, _BDAY_HELP))


async def _store_birthday_text(message: Message, state: FSMContext, text: str):
    uid = int(message.from_user.id)
    if text.strip().casefold() in {"удалить", "убрать", "delete", "-"}:
        await state.clear()
        await message.answer(ui.ok("Дата удалена.") if _delete_birthday(uid) else ui.note("Сохранённой даты не было."))
        return
    parsed = parse_birthday(text)
    if not parsed:
        await message.answer(ui.fail("Не понял дату. Пример: 24.03 или 24 марта."))
        return
    _save_birthday(uid, *parsed)
    await state.clear()
    await message.answer(ui.ok(f"Сохранено: {parsed[0]:02d}.{parsed[1]:02d}"))


async def birthday_state_text(message: Message, state: FSMContext):
    if not message.from_user:
        return
    if not await _is_active_member(message.from_user.id) and not _is_owner(message):
        await state.clear()
        return
    await _store_birthday_text(message, state, message.text or "")


async def cmd_birthdays(message: Message):
    if not await cmdcfg.gate(message, "birthdays"):
        return
    uid = int(message.from_user.id)
    if not await _is_active_member(uid) and not _is_owner(message):
        await message.answer(ui.card("Дни рождения", ui.note("Список доступен участникам флуда.")))
        return
    live = live_birthdays()
    window = cmdcfg.get_int("birthdays", "window_days")
    today = ui.local_now()
    ordered = sorted(live, key=lambda r: days_until(r["month"], r["day"], today))
    if window:
        ordered = [r for r in ordered if days_until(r["month"], r["day"], today) <= window]
    ordered = ordered[:cmdcfg.get_int("birthdays", "limit")]
    if not ordered:
        await message.answer(ui.card("Дни рождения", ui.note("Пока нет сохранённых дат участников, находящихся во флуде.")))
        return
    items = []
    for r in ordered:
        left = days_until(r["month"], r["day"], today)
        tail = "сегодня 🎉" if left == 0 else "завтра" if left == 1 else f"через {ui.days_word(left)}"
        items.append(f"🎂 {r['day']:02d}.{r['month']:02d} — {ui.person(r['row'])} · {tail}")
    await message.answer(ui.card("Дни рождения", "\n".join(items)))


# --------------------------------------------------------------------------
# Сводка
# --------------------------------------------------------------------------

def dashboard_text() -> str:
    cid = _effective_primary_chat_id()
    now = _utc_iso()
    active = _q(lambda conn: conn.execute("SELECT COUNT(*) AS c FROM group_members WHERE chat_id=? AND active=1", (cid,)).fetchone())["c"]
    with_role = _q(lambda conn: conn.execute(
        "SELECT COUNT(*) AS c FROM group_members WHERE chat_id=? AND active=1 AND role_key IS NOT NULL", (cid,)).fetchone())["c"]
    rest = _q(lambda conn: conn.execute("SELECT COUNT(*) AS c FROM jf_rest WHERE chat_id=? AND active=1 AND end_at>?", (cid, now)).fetchone())["c"]
    blocks = [ui.lines(ui.section("Флуд"), ui.kv("Активных участников", active), ui.kv("С назначенной ролью", with_role),
                       ui.kv("Без роли", max(0, active - with_role)), ui.kv("На ресте", rest))]
    if cmdcfg.get_bool("dashboard", "show_warnings"):
        w = _q(lambda conn: conn.execute(
            "SELECT COUNT(*) AS c, COUNT(DISTINCT user_id) AS u "
            "FROM jf_manager_warnings WHERE chat_id=? AND removed_at IS NULL AND expires_at>?",
            (cid, now)).fetchone())
        blocks.append(ui.lines(ui.section("Предупреждения"), ui.kv("Активных", w["c"]), ui.kv("Участников с ними", w["u"])))
    if cmdcfg.get_bool("dashboard", "show_awards"):
        try:
            counts = awards.CTX.queue.counts(cid)
        except Exception:
            counts = {}
        blocks.append(ui.lines(ui.section("Награды"), ui.kv("Ждут выдачи", counts.get("ready", 0)), ui.kv("Выдано через очередь", counts.get("issued", 0))))
    if cmdcfg.get_bool("dashboard", "show_birthdays"):
        today = ui.local_now()
        soon = [r for r in live_birthdays() if days_until(r["month"], r["day"], today) <= 7]
        soon.sort(key=lambda r: days_until(r["month"], r["day"], today))
        lines = [ui.section("Дни рождения (7 дней)")]
        lines += [ui.bullet(f"{r['day']:02d}.{r['month']:02d} — {ui.person(r['row'])}") for r in soon[:6]] or [ui.bullet("никого")]
        blocks.append(ui.lines(*lines))
    return ui.card("Justice Faite · сводка", *blocks)


async def cmd_dashboard(message: Message):
    if not await cmdcfg.gate(message, "dashboard"):
        return
    await _reply(message, "dashboard", dashboard_text())


# --------------------------------------------------------------------------
# Естественные команды во флуде
# --------------------------------------------------------------------------

async def natural_rest_command(message: Message):
    await cmd_rest(message)


async def natural_unrest_command(message: Message):
    await cmd_unrest(message)


async def natural_restlist_command(message: Message):
    await cmd_restlist(message)


async def natural_take_rest_command(message: Message):
    await cmd_take_rest_self(message)


async def natural_birthday_command(message: Message, state: FSMContext):
    await cmd_birthday(message, state)


# --------------------------------------------------------------------------
# Фоновый обработчик: окончание реста, поздравления
# --------------------------------------------------------------------------

async def _announce_rest_end(row) -> None:
    if not cmdcfg.get_bool("rest", "notify_end"):
        return
    template = cmdcfg.get_str("rest", "end_text")
    member = dict(row)
    member.setdefault("user_id", row["user_id"])
    text = _render_html(template, name=_mention_html(member))
    await _bot.send_message(int(_primary_chat_id), text, parse_mode="HTML")


def _claim_birthday_notice(user_id: int, year: int) -> bool:
    """True, если поздравление за этот год ещё не отправлялось (и теперь помечено отправленным)."""
    def op(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO jf_birthday_notices(chat_id,user_id,year,sent_at) VALUES(?,?,?,?)",
            (int(_primary_chat_id), int(user_id), int(year), _utc_iso()))
        conn.commit()
        return cur.rowcount == 1
    return bool(_q(op))


async def maintenance_tick() -> None:
    now_dt = datetime.now(timezone.utc)
    ended = _q(lambda conn: conn.execute(
        "SELECT r.*,gm.username,gm.first_name FROM jf_rest r LEFT JOIN group_members gm "
        "ON gm.chat_id=r.chat_id AND gm.user_id=r.user_id WHERE r.chat_id=? AND r.active=1 AND r.end_at<=?",
        (int(_primary_chat_id), now_dt.isoformat())).fetchall())
    for row in ended:
        _q(lambda conn, r=row: (conn.execute(
            "UPDATE jf_rest SET active=0,ended_notified_at=? WHERE chat_id=? AND user_id=? AND active=1",
            (_utc_iso(), int(r["chat_id"]), int(r["user_id"]))), conn.commit()))
        with suppress(Exception):
            await _announce_rest_end(row)

    if not cmdcfg.is_enabled("birthday_greet"):
        return
    local = ui.local_now()
    if local.hour < cmdcfg.get_int("birthday_greet", "hour"):
        return
    template = cmdcfg.get_str("birthday_greet", "text")
    for item in live_birthdays():
        if int(item["month"]) != local.month or int(item["day"]) != local.day:
            continue
        claimed = _claim_birthday_notice(int(item["user_id"]), local.year)
        if not claimed:
            continue
        with suppress(Exception):
            await _bot.send_message(int(_primary_chat_id), _render_html(template, name=_mention_html(item["row"])), parse_mode="HTML")


async def feature_maintenance_worker():
    while True:
        try:
            await maintenance_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("maintenance tick failed")
        await asyncio.sleep(60)


# --------------------------------------------------------------------------
# Регистрация
# --------------------------------------------------------------------------

def set_primary_chat_id(chat_id: int) -> None:
    """Apply the database-resolved primary group after backup restoration.

    The module is imported before `main()` restores a missing SQLite file, so
    its first registration can see an empty/default environment value. The
    authoritative persisted group ID is applied again during startup.
    """
    global _primary_chat_id
    _primary_chat_id = int(chat_id)
    # The awards module captures its chat ID at registration time; keep its
    # queue and duplicate checks aligned with the resolved group as well.
    if awards.CTX.queue is not None:
        awards.CTX.chat_id = _primary_chat_id
    if _db_op is not None:
        seed_birthday_directory()

def register_justice_features(ns: dict[str, Any]):
    global _bot, _dp, _db_op, _now, _upsert_group_member, _get_member
    global _primary_chat_id, _admin_id, _is_primary_chat
    _bot = ns["bot"]
    _dp = ns["dp"]
    _db_op = ns["group_db_op"]
    _now = ns["now"]
    _upsert_group_member = ns["upsert_group_member"]
    _get_member = ns["get_member"]
    _primary_chat_id = int(ns["PRIMARY_CHAT_ID"])
    _admin_id = int(ns["ADMIN_ID"])
    _is_primary_chat = ns["is_primary_chat"]

    init_justice_features_db()
    seed_birthday_directory()

    awards.register(
        _dp, bot=_bot, owner_id=_admin_id, chat_id=_primary_chat_id, db_op=_db_op, now=_now,
        active_members=_active_member_rows, lookup_member=lookup_member, issue_local_award=issue_local_award,
    )

    private = F.chat.type == "private"
    group = F.chat.type.in_(GROUP_TYPES)

    _dp.message.register(cmd_birthday, Command("jf_birthday"))
    _dp.message.register(cmd_birthdays, Command("jf_birthdays"), private)
    _dp.message.register(birthday_state_text, BirthdayState.waiting_date, private, F.text, ~F.text.startswith("/"))
    _dp.message.register(natural_birthday_command,
                         F.chat.type.in_(GROUP_TYPES),
                         F.text.regexp(r"(?iu)^\s*(?:др|мой\s+др|день\s+рождения|мой\s+день\s+рождения)(?:\s+.*)?$"))

    _dp.message.register(cmd_dashboard, Command("jf_dashboard"))
    _dp.message.register(cmd_warn, Command("jf_warn"))
    _dp.message.register(cmd_warnings, Command("jf_warnings"))
    _dp.message.register(cmd_remove_warning, Command("jf_warn_remove"))
    _dp.message.register(cmd_rest, Command("rest", "jf_rest"))
    _dp.message.register(cmd_take_rest_self, Command("take_rest"))
    _dp.message.register(cmd_unrest, Command("unrest"))
    _dp.message.register(cmd_restlist, Command("jf_restlist", "restlist"))

    _dp.message.register(natural_restlist_command, F.text.regexp(r"(?iu)^\s*рестники\s*$"))
    _dp.message.register(natural_rest_command, F.text.regexp(r"(?iu)^\s*выдать\s+рест(?:\s+.+)?$"))
    _dp.message.register(natural_take_rest_command, F.text.regexp(r"(?iu)^\s*взять\s+рест(?:\s+.+)?$"))
    _dp.message.register(natural_unrest_command, F.text.regexp(r"(?iu)^\s*снять\s+рест(?:\s+.+)?$"))

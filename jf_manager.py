"""Justice Faite Manager — core moderation compatibility layer.

This is an original implementation inspired by common chat-manager workflows.
It does not connect to Iris or reuse Iris databases. All state is stored in the
bot's own SQLite database and scoped by Telegram chat ID.
"""
from __future__ import annotations

import json
import html
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Public command catalog: the same keys are used by command-access settings.
COMMANDS = {
    "mod_add": ("Назначение модератора", 3),
    "mod_remove": ("Снятие модератора", 3),
    "mod_list": ("Список администрации", 0),
    "warn": ("Выдача предупреждения", 2),
    "warn_view": ("Просмотр предупреждений", 1),
    "warn_remove": ("Снятие предупреждений", 2),
    "mute": ("Выдача мута", 2),
    "unmute": ("Снятие мута", 2),
    "mute_list": ("Список мутов", 1),
    "ban": ("Бан пользователя", 3),
    "unban": ("Разбан пользователя", 3),
    "kick": ("Исключение пользователя", 2),
    "ban_list": ("Список банов", 1),
    "reason": ("Причина наказания", 1),
    "mod_log": ("Журнал модерации", 2),
    "access_view": ("Просмотр доступа команд", 1),
    "access_edit": ("Настройка доступа команд", 4),
    "settings": ("Настройки наказаний", 4),
}

RANK_NAMES = {
    0: "Участник",
    1: "Младший модератор",
    2: "Старший модератор",
    3: "Администратор",
    4: "Старший администратор",
    5: "Создатель",
}

# Accepted in group messages. Keep this fairly strict: arbitrary chat prose is
# never passed to this module's dispatcher.
COMMAND_PATTERN = (
    r"(?iu)^\s*(?:/jf_(?:manager|mod|mods|mute|unmute|ban|unban|kick|access|modlog)"
    r"(?:@[A-Za-z0-9_]+)?(?:\s|$)|"
    r"(?:[!.\/]{0,4})(?:(?:ирис(?:ка)?|iris)\s+)?(?:\+модер|\+админ|повысить|понизить|снять модер|снять|разжаловать|"
    r"!!{0,3}модер|!!!!модер|модер лог|мой модер лог|твой модер лог|кто назначил|кто админ|"
    r"а судьи кто|кто здесь власть|варнлист|варны|мои варны|снять все варны|снять варны|-варн|"
    r"варн|предупреждение|пред|муты|проверить мут|-мут|размут|снять мут|мут период|мут|"
    r"модераторы названия|ранг название|сбросить названия рангов|"
    r"банлист|причина|разбан|вернуть|бан период|бан|кик|доступ команд|мой доступ команд|"
    r"дк|лог дк|модер лог|варны лимит|варны период|варны чс)\b.*$)"
)

_DURATION_RE = re.compile(
    r"^(?P<n>\d+)\s*(?P<u>с|сек(?:унда|унды)?|секунд|м|min|мин(?:ута|уты|ут)?|"
    r"ч|час(?:а|ов)?|д|дн(?:я|ей)?|сут(?:ки|ок)?|нед(?:еля|ели|ель)?|"
    r"мес(?:яц|яца|яцев)?|г(?:од|ода|лет)?)$",
    re.IGNORECASE,
)
_DURATION_UNITS = {
    "с": 1, "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1,
    "м": 60, "min": 60, "мин": 60, "минута": 60, "минуты": 60, "минут": 60,
    "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600,
    "д": 86400, "дн": 86400, "дня": 86400, "дней": 86400,
    "сутки": 86400, "суток": 86400,
    "неделя": 604800, "недели": 604800, "недель": 604800, "нед": 604800,
    "мес": 2592000, "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
    "г": 31536000, "год": 31536000, "года": 31536000, "лет": 31536000,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat()


def parse_duration_tokens(tokens: list[str]) -> tuple[Optional[int], list[str]]:
    """Parse a leading `number unit` pair or compact form; return seconds and remainder."""
    if not tokens:
        return None, tokens
    compact = re.match(r"^(\d+)(с|м|ч|д)$", tokens[0].lower())
    if compact:
        return int(compact.group(1)) * _DURATION_UNITS[compact.group(2)], tokens[1:]
    if len(tokens) < 2:
        return None, tokens
    raw = f"{tokens[0]} {tokens[1]}".strip().lower()
    match = _DURATION_RE.match(raw)
    if not match:
        # Compact forms: 10м, 2д, 1ч.
        compact = re.match(r"^(\d+)(с|м|ч|д)$", tokens[0].lower())
        if compact:
            return int(compact.group(1)) * _DURATION_UNITS[compact.group(2)], tokens[1:]
        return None, tokens
    unit = match.group("u").lower()
    return int(match.group("n")) * _DURATION_UNITS[unit], tokens[2:]


def human_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "навсегда"
    for unit, size, label in (("дн.", 86400, "дн."), ("ч.", 3600, "ч."), ("мин.", 60, "мин.")):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size} {label}"
    return f"{seconds} сек."


class Store:
    def __init__(self, db_path: str, lock: Optional[threading.RLock] = None):
        self.db_path = str(db_path)
        self.lock = lock or threading.RLock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def run(self, callback, *args):
        with self.lock:
            conn = self.connect()
            try:
                result = callback(conn, *args)
                if conn.in_transaction:
                    conn.commit()
                return result
            except Exception:
                with suppress(Exception):
                    conn.rollback()
                raise
            finally:
                conn.close()

    def init(self):
        def op(conn):
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jf_manager_moderators (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    rank INTEGER NOT NULL CHECK(rank BETWEEN 1 AND 4),
                    assigned_by INTEGER NOT NULL,
                    assigned_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_jf_manager_moderators_chat_rank
                    ON jf_manager_moderators(chat_id, rank DESC);

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
                    removal_note TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_jf_manager_warnings_active
                    ON jf_manager_warnings(chat_id, user_id, removed_at, expires_at);

                CREATE TABLE IF NOT EXISTS jf_manager_restrictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('mute','ban')),
                    moderator_id INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_jf_manager_restrictions_active
                    ON jf_manager_restrictions(chat_id, kind, active, expires_at);

                CREATE TABLE IF NOT EXISTS jf_manager_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    actor_id INTEGER NOT NULL,
                    target_id INTEGER,
                    action TEXT NOT NULL,
                    duration_seconds INTEGER,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_jf_manager_actions_chat
                    ON jf_manager_actions(chat_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_jf_manager_actions_target
                    ON jf_manager_actions(chat_id, target_id, id DESC);

                CREATE TABLE IF NOT EXISTS jf_manager_command_access (
                    chat_id INTEGER NOT NULL,
                    command_key TEXT NOT NULL,
                    min_rank INTEGER NOT NULL DEFAULT 0 CHECK(min_rank BETWEEN 0 AND 6),
                    updated_by INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, command_key)
                );
                CREATE TABLE IF NOT EXISTS jf_manager_chat_settings (
                    chat_id INTEGER NOT NULL,
                    setting_key TEXT NOT NULL,
                    setting_value TEXT NOT NULL,
                    updated_by INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, setting_key)
                );
                CREATE TABLE IF NOT EXISTS jf_manager_rank_labels (
                    chat_id INTEGER NOT NULL,
                    rank INTEGER NOT NULL CHECK(rank BETWEEN 0 AND 5),
                    label TEXT NOT NULL,
                    updated_by INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id,rank)
                );
                CREATE TABLE IF NOT EXISTS jf_manager_member_names (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    last_seen TEXT NOT NULL,
                    PRIMARY KEY(chat_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_jf_manager_member_names_username
                    ON jf_manager_member_names(chat_id, username);
            """)
            # Existing database is retained; settings are inserted only once.
            defaults = {
                "warn_limit": "3",
                "warn_period_seconds": str(30 * 86400),
                "warn_ban_seconds": str(7 * 86400),
                "ban_period_seconds": "0",
                "mute_period_seconds": str(7 * 86400),
            }
            for key, value in defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO jf_manager_chat_settings(chat_id,setting_key,setting_value,updated_by,updated_at) VALUES(0,?,?,0,?)",
                    (key, value, iso()),
                )
            conn.commit()
        self.run(op)

    def remember_user(self, chat_id: int, user: Any):
        if not user or not getattr(user, "id", None):
            return
        username = str(getattr(user, "username", "") or "").lstrip("@").casefold()
        display = " ".join(x for x in [getattr(user, "first_name", ""), getattr(user, "last_name", "")] if x).strip()
        self.run(lambda c: c.execute(
            """INSERT INTO jf_manager_member_names(chat_id,user_id,username,display_name,last_seen)
               VALUES(?,?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET
               username=excluded.username,display_name=excluded.display_name,last_seen=excluded.last_seen""",
            (int(chat_id), int(user.id), username, display, iso()),
        ))

    def member_id(self, chat_id: int, username: str) -> Optional[int]:
        key = username.strip().lstrip("@").casefold()
        def op(c):
            row = c.execute("SELECT user_id FROM jf_manager_member_names WHERE chat_id=? AND username=? ORDER BY last_seen DESC LIMIT 1", (chat_id, key)).fetchone()
            if row:
                return int(row["user_id"])
            if _table_exists(c, "group_members"):
                row = c.execute("SELECT user_id FROM group_members WHERE chat_id=? AND lower(username)=? AND active=1 ORDER BY joined_at DESC LIMIT 1", (chat_id, key)).fetchone()
                if row:
                    return int(row["user_id"])
            if _table_exists(c, "participant_roster"):
                row = c.execute("SELECT user_id FROM participant_roster WHERE chat_id=? AND lower(username)=? ORDER BY last_seen DESC LIMIT 1", (chat_id, key)).fetchone()
                if row and row["user_id"]:
                    return int(row["user_id"])
            return None
        return self.run(op)

    def setting(self, chat_id: int, key: str, default: Optional[str] = None) -> Optional[str]:
        def op(c):
            row = c.execute("SELECT setting_value FROM jf_manager_chat_settings WHERE chat_id IN (?,0) AND setting_key=? ORDER BY CASE WHEN chat_id=? THEN 0 ELSE 1 END LIMIT 1", (chat_id, key, chat_id)).fetchone()
            return str(row["setting_value"]) if row else default
        return self.run(op)

    def set_setting(self, chat_id: int, key: str, value: str, actor_id: int):
        self.run(lambda c: c.execute(
            """INSERT INTO jf_manager_chat_settings(chat_id,setting_key,setting_value,updated_by,updated_at)
               VALUES(?,?,?,?,?) ON CONFLICT(chat_id,setting_key) DO UPDATE SET
               setting_value=excluded.setting_value,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (chat_id, key, str(value), actor_id, iso()),
        ))

    def rank(self, chat_id: int, user_id: int) -> int:
        row = self.run(lambda c: c.execute("SELECT rank FROM jf_manager_moderators WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone())
        return int(row["rank"]) if row else 0

    def change_rank(self, chat_id: int, user_id: int, rank: int, actor_id: int):
        def op(c):
            if rank <= 0:
                c.execute("DELETE FROM jf_manager_moderators WHERE chat_id=? AND user_id=?", (chat_id, user_id))
            else:
                c.execute("""INSERT INTO jf_manager_moderators(chat_id,user_id,rank,assigned_by,assigned_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET
                    rank=excluded.rank,assigned_by=excluded.assigned_by,assigned_at=excluded.assigned_at""",
                    (chat_id, user_id, rank, actor_id, iso()))
            c.execute("INSERT INTO jf_manager_actions(chat_id,actor_id,target_id,action,created_at,details_json) VALUES(?,?,?,?,?,?)",
                      (chat_id, actor_id, user_id, "rank_set" if rank else "rank_remove", iso(), json.dumps({"rank": rank}, ensure_ascii=False)))
        self.run(op)

    def active_warnings(self, chat_id: int, user_id: int) -> list[sqlite3.Row]:
        return self.run(lambda c: c.execute(
            "SELECT * FROM jf_manager_warnings WHERE chat_id=? AND user_id=? AND removed_at IS NULL AND expires_at>? ORDER BY id DESC",
            (chat_id, user_id, iso()),
        ).fetchall())

    def add_warning(self, chat_id: int, user_id: int, actor_id: int, reason: str, period: int) -> tuple[int, int]:
        def op(c):
            created = iso()
            expires = iso(utcnow() + timedelta(seconds=period))
            cur = c.execute("INSERT INTO jf_manager_warnings(chat_id,user_id,moderator_id,reason,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                            (chat_id, user_id, actor_id, reason[:1000], created, expires))
            count = c.execute("SELECT COUNT(*) AS n FROM jf_manager_warnings WHERE chat_id=? AND user_id=? AND removed_at IS NULL AND expires_at> ?",
                              (chat_id, user_id, created)).fetchone()["n"]
            return int(cur.lastrowid), int(count)
        return self.run(op)

    def remove_warning(self, chat_id: int, warning_id: int, actor_id: int, user_id: Optional[int] = None) -> bool:
        def op(c):
            sql = "UPDATE jf_manager_warnings SET removed_at=?,removed_by=? WHERE chat_id=? AND id=? AND removed_at IS NULL"
            params = [iso(), actor_id, chat_id, warning_id]
            if user_id is not None:
                sql += " AND user_id=?"
                params.append(user_id)
            cur = c.execute(sql, params)
            return cur.rowcount > 0
        return self.run(op)

    def remove_warnings(self, chat_id: int, user_id: int, actor_id: int, limit: Optional[int] = None) -> int:
        def op(c):
            rows = c.execute("SELECT id FROM jf_manager_warnings WHERE chat_id=? AND user_id=? AND removed_at IS NULL AND expires_at>? ORDER BY id DESC" + (" LIMIT ?" if limit else ""),
                             (chat_id, user_id, iso(), limit) if limit else (chat_id, user_id, iso())).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                c.executemany("UPDATE jf_manager_warnings SET removed_at=?,removed_by=? WHERE id=?", [(iso(), actor_id, x) for x in ids])
            return len(ids)
        return self.run(op)

    def log_action(self, chat_id: int, actor_id: int, target_id: Optional[int], action: str,
                   duration: Optional[int], reason: str, details: Optional[dict] = None):
        self.run(lambda c: c.execute(
            "INSERT INTO jf_manager_actions(chat_id,actor_id,target_id,action,duration_seconds,reason,created_at,details_json) VALUES(?,?,?,?,?,?,?,?)",
            (chat_id, actor_id, target_id, action, duration, reason[:1000], iso(), json.dumps(details or {}, ensure_ascii=False)),
        ))

    def set_restriction(self, chat_id: int, user_id: int, kind: str, actor_id: int,
                        reason: str, duration: Optional[int], active: bool = True):
        def op(c):
            c.execute("UPDATE jf_manager_restrictions SET active=0 WHERE chat_id=? AND user_id=? AND kind=? AND active=1", (chat_id,user_id,kind))
            expires = iso(utcnow()+timedelta(seconds=duration)) if duration else None
            if active:
                c.execute("INSERT INTO jf_manager_restrictions(chat_id,user_id,kind,moderator_id,reason,created_at,expires_at,active) VALUES(?,?,?,?,?,?,?,1)",
                          (chat_id,user_id,kind,actor_id,reason[:1000],iso(),expires))
        self.run(op)

    def set_access(self, chat_id: int, key: str, rank: int, actor_id: int):
        self.run(lambda c: c.execute(
            """INSERT INTO jf_manager_command_access(chat_id,command_key,min_rank,updated_by,updated_at)
               VALUES(?,?,?,?,?) ON CONFLICT(chat_id,command_key) DO UPDATE SET
               min_rank=excluded.min_rank,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
            (chat_id,key,rank,actor_id,iso()),
        ))

    def access(self, chat_id: int, key: str) -> int:
        default = COMMANDS.get(key, ("", 1))[1]
        row = self.run(lambda c: c.execute("SELECT min_rank FROM jf_manager_command_access WHERE chat_id=? AND command_key=?", (chat_id,key)).fetchone())
        return int(row["min_rank"]) if row else int(default)

    def action_rows(self, chat_id: int, limit: int = 15, target_id: Optional[int] = None):
        def op(c):
            if target_id is None:
                return c.execute("SELECT * FROM jf_manager_actions WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id,limit)).fetchall()
            return c.execute("SELECT * FROM jf_manager_actions WHERE chat_id=? AND target_id=? ORDER BY id DESC LIMIT ?", (chat_id,target_id,limit)).fetchall()
        return self.run(op)

    def moderators(self, chat_id: int):
        return self.run(lambda c: c.execute("SELECT * FROM jf_manager_moderators WHERE chat_id=? ORDER BY rank DESC,assigned_at", (chat_id,)).fetchall())

    def rank_label(self, chat_id: int, rank: int) -> str:
        row = self.run(lambda c: c.execute("SELECT label FROM jf_manager_rank_labels WHERE chat_id=? AND rank=?", (chat_id, rank)).fetchone())
        return str(row[0]) if row else RANK_NAMES.get(int(rank), "Модератор")

    def rank_labels(self, chat_id: int):
        rows = self.run(lambda c: c.execute("SELECT rank,label FROM jf_manager_rank_labels WHERE chat_id=?", (chat_id,)).fetchall())
        custom = {int(r[0]): str(r[1]) for r in rows}
        return {i: custom.get(i, RANK_NAMES[i]) for i in range(6)}

    def set_rank_label(self, chat_id: int, rank: int, label: str, actor_id: int):
        self.run(lambda c: c.execute("INSERT INTO jf_manager_rank_labels(chat_id,rank,label,updated_by,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id,rank) DO UPDATE SET label=excluded.label,updated_by=excluded.updated_by,updated_at=excluded.updated_at", (chat_id, rank, label, actor_id, iso())))

    def reset_rank_labels(self, chat_id: int):
        self.run(lambda c: c.execute("DELETE FROM jf_manager_rank_labels WHERE chat_id=?", (chat_id,)))


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _format_user(user_id: int, username: str = "", display: str = "") -> str:
    if username:
        return f"@{username} · <code>{user_id}</code>"
    if display:
        return f"{display} · <code>{user_id}</code>"
    return f"<code>{user_id}</code>"


class Manager:
    def __init__(self, dp, bot, F, db_path: str, lock=None, permissions_cls=None, owner_id: int = 0):
        self.dp, self.bot, self.F = dp, bot, F
        self.store = Store(db_path, lock)
        self.permissions_cls = permissions_cls
        self.owner_id = int(owner_id or 0)
        self._registered = False
        self._ready = False

    def init(self):
        if not self._ready:
            self.store.init()
            self._ready = True

    def register(self):
        if self._registered:
            return
        self.dp.message.register(self.handle, self.F.chat.type.in_({"group", "supergroup"}), self.F.text.regexp(COMMAND_PATTERN))
        self._registered = True

    async def _rank(self, chat_id: int, user_id: int) -> int:
        if self.owner_id and user_id == self.owner_id:
            return 5
        rank = self.store.rank(chat_id, user_id)
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
            status = getattr(member, "status", "")
            if status == "creator":
                return 5
            if status == "administrator":
                if getattr(member, "can_promote_members", False):
                    return max(rank, 4)
                if any(getattr(member, p, False) for p in ("can_restrict_members", "can_delete_messages", "can_pin_messages", "can_manage_topics")):
                    return max(rank, 3)
                return max(rank, 2)
        except Exception:
            pass
        return rank

    async def _gate(self, message, key: str, *, silent: bool = True) -> bool:
        rank = await self._rank(int(message.chat.id), int(message.from_user.id))
        required = self.store.access(int(message.chat.id), key)
        allowed = rank >= 5 or (required < 6 and rank >= required)
        if not allowed and not silent:
            await message.reply(f"⛔ Для команды требуется ранг {required if required < 6 else '— (отключена)'}. Твой ранг: {rank}.")
        return allowed

    async def _target(self, message, args: list[str]) -> tuple[Optional[int], list[str], str]:
        """Resolve reply, text-mention entity, @username or numeric Telegram ID."""
        for entity in (getattr(message, "entities", None) or []):
            if getattr(entity, "type", None) == "text_mention" and getattr(entity, "user", None):
                user = entity.user
                self.store.remember_user(message.chat.id, user)
                return int(user.id), args, getattr(user, "username", "") or getattr(user, "first_name", "") or str(user.id)
        if message.reply_to_message and getattr(message.reply_to_message, "from_user", None):
            u = message.reply_to_message.from_user
            self.store.remember_user(message.chat.id, u)
            return int(u.id), args, getattr(u, "username", "") or getattr(u, "first_name", "") or str(u.id)
        # User mention first, then numeric ID.
        for i, token in enumerate(args):
            m = re.fullmatch(r"@([A-Za-z0-9_]{4,})", token)
            if m:
                uid = self.store.member_id(int(message.chat.id), m.group(1))
                if uid:
                    return uid, args[:i] + args[i+1:], m.group(1)
                return None, args, m.group(1)
            if token.isdigit() and len(token) >= 5:
                return int(token), args[:i] + args[i+1:], token
        return None, args, ""

    async def _target_guard(self, message, target_id: int, caller_rank: int) -> tuple[bool, str]:
        if target_id == int(message.from_user.id):
            return False, "Нельзя применить это действие к самому себе."
        target_rank = await self._rank(int(message.chat.id), target_id)
        if target_rank >= caller_rank:
            return False, "Нельзя менять права или наказывать участника с равным или более высоким рангом."
        try:
            member = await self.bot.get_chat_member(message.chat.id, target_id)
            if getattr(member, "status", "") in {"left", "kicked"}:
                return False, "Пользователь не состоит в чате."
        except Exception:
            # Some operations such as unban target users who are not currently in the chat.
            pass
        return True, ""

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    def _parse(self, raw: str) -> tuple[str, list[str], Optional[int]]:
        line = (raw or "").strip()
        forced_rank = None
        # Slash shortcuts.
        m = re.match(r"^/jf_(\w+)(?:@[A-Za-z0-9_]+)?\s*(.*)$", line, re.I | re.S)
        if m:
            shortcut = m.group(1).lower()
            keymap = {"manager":"help","mod":"mod_add","mods":"mod_list","mute":"mute","unmute":"unmute","ban":"ban","unban":"unban","kick":"kick","access":"access_view","modlog":"mod_log"}
            return keymap.get(shortcut,"help"), m.group(2).split(), None
        # !mod, !!mod ... !!!!mod: number of marks is the requested rank.
        m = re.match(r"^(!{1,4})\s*модер\s*(.*)$", line, re.I | re.S)
        if m:
            forced_rank = min(4, len(m.group(1)))
            return "mod_add", m.group(2).split(), forced_rank
        # Strip Iris-like command prefix, but preserve normal command arguments.
        line = re.sub(r"^[!.\/]+\s*", "", line)
        line = re.sub(r"^(?:ирис(?:ка)?|iris)\s+", "", line, flags=re.I)
        lower = line.casefold().strip()
        if lower == "модераторы названия":
            return "rank_labels", [], None
        if lower.startswith("ранг название "):
            return "rank_label_set", line[len("ранг название "):].split(maxsplit=1), None
        if lower == "сбросить названия рангов":
            return "rank_labels_reset", [], None
        # Longest phrase wins.
        aliases = [
            ("мой доступ команд", "access_view_self"), ("доступ команд", "access_view"),
            ("мои варны", "warnings_self"), ("модер лог от", "mod_log_by"),
            ("твой модер лог", "mod_log_target"), ("мой модер лог", "mod_log_self"),
            ("модер лог", "mod_log"), ("кто назначил", "who_assigned"),
            ("кто здесь власть", "mod_list"), ("а судьи кто", "mod_list"), ("кто админ", "mod_list"),
            ("снять все варны", "warn_clear"), ("снять варн номер", "warn_remove_number"),
            ("снять варны", "warn_clear_n"), ("кик тихо", "kick_quiet"),
            ("варны лимит", "setting_warn_limit"), ("варны период", "setting_warn_period"),
            ("варны чс", "setting_warn_ban"), ("мут период", "setting_mute_period"),
            ("бан период", "setting_ban_period"), ("мой доступ", "access_view_self"),
            ("проверить мут", "mute_check"), ("варнлист", "warn_list"), ("банлист", "ban_list"),
            ("разжаловать", "mod_remove"), ("снять модер", "mod_remove"),
            ("повысить", "mod_promote"), ("понизить", "mod_demote"),
            ("+модер", "mod_add"), ("+админ", "mod_add"),
            ("муты", "mute_list"), ("размут", "unmute"), ("снять мут", "unmute"), ("-мут", "unmute"),
            ("разбан", "unban"), ("вернуть", "unban"),
            ("-варн", "warn_remove"), ("варны", "warnings"),
            ("предупреждение", "warn"), ("варн", "warn"), ("пред", "warn"),
            ("причина", "reason"), ("кик", "kick"), ("бан", "ban"), ("мут", "mute"),
            ("дк", "access_set"), ("лог дк", "access_log"),
        ]
        for phrase, key in sorted(aliases, key=lambda x: len(x[0]), reverse=True):
            if lower == phrase or lower.startswith(phrase + " "):
                rest = line[len(phrase):].strip()
                args = rest.split()
                # "!модер 2 @user" and "+модер 2 @user".
                if key == "mod_add" and args and args[0].isdigit() and 1 <= int(args[0]) <= 4:
                    forced_rank = int(args[0]); args = args[1:]
                return key, args, forced_rank
        return "unknown", [], None

    async def _send_error(self, message, text: str):
        await message.reply("⚠️ " + text)

    async def _do_ban(self, message, target_id: int, caller_rank: int, args: list[str], *, action: str = "ban", quiet: bool = False):
        if not await self._gate(message, "ban" if action == "ban" else "kick"):
            return
        ok, why = await self._target_guard(message, target_id, caller_rank)
        if not ok:
            await self._send_error(message, why); return
        duration, rest = parse_duration_tokens(args)
        if duration is None and args and args[0].isdigit() and len(args) > 1 and args[1].startswith("@"):
            duration = int(args[0]) * 86400; rest = args[2:]
        elif duration is None and action == "ban":
            configured = int(self.store.setting(message.chat.id, "ban_period_seconds", "0") or 0)
            duration = configured or None
            rest = args
        elif duration is None and action == "kick":
            rest = args
        reason = " ".join(rest).strip()
        if action == "kick":
            try:
                await self.bot.ban_chat_member(message.chat.id, target_id)
            except Exception:
                await self._send_error(message, "Telegram не выполнил кик. Проверь, что бот — администратор с правом блокировать участников.")
                return
            try:
                await self.bot.unban_chat_member(message.chat.id, target_id, only_if_banned=True)
            except Exception:
                # Do not report a successful kick when the temporary ban could not
                # be lifted: the user may remain blocked until an explicit unban.
                self.store.set_restriction(message.chat.id, target_id, "ban", message.from_user.id,
                                           reason or "Кик не завершён: Telegram не подтвердил разбан", None)
                self.store.log_action(message.chat.id, message.from_user.id, target_id,
                                      "kick_partial_ban", None, reason)
                await self._send_error(message, "Пользователь заблокирован, но Telegram не подтвердил снятие блокировки. Проверь список банов и выполни «Разбан» вручную.")
                return
            self.store.log_action(message.chat.id, message.from_user.id, target_id, "kick_quiet" if quiet else "kick", None, reason)
            if quiet:
                with suppress(Exception):
                    await self.bot.delete_message(message.chat.id, message.message_id)
            else:
                await message.reply(f"👢 Пользователь <code>{target_id}</code> исключён.\nПричина: {reason or 'не указана'}")
            return
        if duration is not None and (duration < 30 or duration > 366 * 86400):
            await self._send_error(message, "Срок бана должен быть от 30 секунд до 366 дней."); return
        until = utcnow() + timedelta(seconds=duration) if duration else None
        try:
            await self.bot.ban_chat_member(message.chat.id, target_id, until_date=until)
        except Exception:
            await self._send_error(message, "Telegram не выполнил бан. Проверь, что бот — администратор с правом блокировать участников.")
            return
        self.store.set_restriction(message.chat.id, target_id, "ban", message.from_user.id, reason, duration)
        self.store.log_action(message.chat.id, message.from_user.id, target_id, "ban", duration, reason)
        await message.reply(f"🔨 Пользователь <code>{target_id}</code> заблокирован на {human_duration(duration)}.\nПричина: {reason or 'не указана'}")

    async def _do_mute(self, message, target_id: int, caller_rank: int, args: list[str]):
        if not await self._gate(message, "mute"):
            return
        ok, why = await self._target_guard(message, target_id, caller_rank)
        if not ok:
            await self._send_error(message, why); return
        duration, rest = parse_duration_tokens(args)
        if duration is None:
            duration = int(self.store.setting(message.chat.id, "mute_period_seconds", str(7*86400)) or 7*86400)
            rest = args
        if duration < 60 or duration > 366 * 86400:
            await self._send_error(message, "Срок мута должен быть от 1 минуты до 366 дней."); return
        reason = " ".join(rest).strip()
        if self.permissions_cls is None:
            await self._send_error(message, "Не удалось подготовить разрешения Telegram."); return
        permissions = self.permissions_cls(can_send_messages=False, can_send_audios=False, can_send_documents=False,
                                             can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
                                             can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
                                             can_add_web_page_previews=False)
        until = utcnow() + timedelta(seconds=duration)
        try:
            await self.bot.restrict_chat_member(message.chat.id, target_id, permissions=permissions,
                                                until_date=until, use_independent_chat_permissions=True)
        except Exception:
            await self._send_error(message, "Telegram не выполнил мут. Проверь, что бот — администратор с правом ограничивать участников.")
            return
        self.store.set_restriction(message.chat.id, target_id, "mute", message.from_user.id, reason, duration)
        self.store.log_action(message.chat.id, message.from_user.id, target_id, "mute", duration, reason)
        await message.reply(f"🔇 Пользователь <code>{target_id}</code> получил мут на {human_duration(duration)}.\nПричина: {reason or 'не указана'}")

    async def _unmute(self, message, target_id: int, caller_rank: int):
        if not await self._gate(message, "unmute"):
            return
        ok, why = await self._target_guard(message, target_id, caller_rank)
        if not ok:
            await self._send_error(message, why); return
        if self.permissions_cls is None:
            await self._send_error(message, "Не удалось подготовить разрешения Telegram."); return
        permissions = None
        try:
            chat = await self.bot.get_chat(message.chat.id)
            permissions = getattr(chat, "permissions", None)
        except Exception:
            permissions = None
        if permissions is None:
            # Fallback for groups without an explicit defaults object.
            permissions = self.permissions_cls(can_send_messages=True, can_send_audios=True, can_send_documents=True,
                                                can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                                                can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                                                can_add_web_page_previews=True)
        try:
            await self.bot.restrict_chat_member(message.chat.id, target_id, permissions=permissions,
                                                use_independent_chat_permissions=True)
        except Exception:
            await self._send_error(message, "Telegram не снял мут. Проверь права бота и статус участника.")
            return
        self.store.set_restriction(message.chat.id, target_id, "mute", message.from_user.id, "", None, active=False)
        self.store.log_action(message.chat.id, message.from_user.id, target_id, "unmute", None, "")
        await message.reply(f"🔊 Ограничение переписки снято с <code>{target_id}</code>.")

    async def handle(self, message):
        if not message.from_user or getattr(message.from_user, "is_bot", False):
            return
        chat_id, actor_id = int(message.chat.id), int(message.from_user.id)
        self.init()
        self.store.remember_user(chat_id, message.from_user)
        key, args, forced_rank = self._parse(message.text or "")
        caller_rank = await self._rank(chat_id, actor_id)

        if key == "unknown":
            return
        if key == "rank_labels":
            labels = self.store.rank_labels(chat_id)
            await message.reply("<b>Названия рангов</b>\n" + "\n".join(f"{rank} — {html.escape(label)}" for rank,label in labels.items()))
            return
        if key == "rank_label_set":
            if caller_rank < 5:
                await self._send_error(message, "Изменять названия рангов может только создатель чата."); return
            if len(args) < 2 or not args[0].isdigit() or not 0 <= int(args[0]) <= 5:
                await self._send_error(message, "Формат: Ранг название 1 Младший модератор (ранг от 0 до 5)."); return
            rank = int(args[0]); label = " ".join(args[1:]).strip()
            if not label or len(label) > 32:
                await self._send_error(message, "Название должно содержать от 1 до 32 символов."); return
            self.store.set_rank_label(chat_id, rank, label, actor_id)
            self.store.log_action(chat_id, actor_id, None, "rank_label_set", None, f"{rank}={label}")
            await message.reply(f"✅ Название ранга {rank} изменено на «{html.escape(label)}».")
            return
        if key == "rank_labels_reset":
            if caller_rank < 5:
                await self._send_error(message, "Сбрасывать названия может только создатель чата."); return
            self.store.reset_rank_labels(chat_id)
            await message.reply("Названия рангов возвращены к стандартным.")
            return
        if key == "help":
            await message.reply(
                "<b>Justice Faite Manager</b>\n\n"
                "<b>Модерация:</b>\n"
                "Бан [срок] @user причина · Разбан @user · Кик @user\n"
                "Мут [срок] @user причина · -Мут @user\n"
                "Варн [@user] причина · Варны @user · -Варн @user\n"
                "Варнлист · Банлист · Муты · Причина @user\n\n"
                "<b>Ранги:</b>\n"
                "+Модер [ранг] @user · Повысить @user · Понизить @user · Снять модер @user\n"
                "Кто админ · Модер лог · Модераторы названия · Ранг название 1 Текст\n\n"
                "<b>Настройки:</b> Доступ команд · ДК бан 3 · Варны лимит 3 · Варны период 30 дней\n"
                "Участника удобнее всего указывать ответом на его сообщение. Для @username бот должен знать его ID."
            )
            return

        # Command access menu.
        if key in {"access_view", "access_view_self"}:
            if not await self._gate(message, "access_view", silent=False): return
            rows = []
            for cmd, (label, default) in COMMANDS.items():
                required = self.store.access(chat_id, cmd)
                if key == "access_view_self":
                    mark = "✅" if required < 6 and caller_rank >= required else "❌"
                else:
                    mark = "❌" if required == 6 else "✅"
                status = "выключена" if required == 6 else f"ранг {required}"
                rows.append(f"{mark} {label}: {status} (по умолчанию {default})")
            heading = "Твой доступ команд" if key == "access_view_self" else "Доступ команд Justice Faite"
            await message.reply(f"<b>{heading}</b>\n" + "\n".join(rows))
            return
        if key == "access_set":
            if not await self._gate(message, "access_edit", silent=False): return
            if len(args) < 2:
                await message.reply("Формат: ДК бан 3 · ДК мут 2 · ДК бан 6 (отключить).\nДопустимые разделы: " + ", ".join(sorted(COMMANDS)))
                return
            command_name = "_".join(args[:-1]).casefold()
            aliases = {"модер":"mod_add", "повысить":"mod_add", "снять модер":"mod_remove", "варн":"warn", "пред":"warn", "предупреждения":"warn_view", "предупреждения пользователя":"warn_view", "выдача варнов":"warn", "снятие варнов":"warn_remove", "мут":"mute", "выдача мута":"mute", "список мутов":"mute_list", "размут":"unmute", "бан":"ban", "выдача бана":"ban", "разбан":"unban", "кик":"kick", "кто админ":"mod_list", "банлист":"ban_list", "варнлист":"warn_view", "причина":"reason", "причина бана":"reason", "модер лог":"mod_log", "вызов дк":"access_view", "мдк":"access_view_self", "доступ команд":"access_view"}
            command_key = aliases.get(" ".join(args[:-1]).casefold(), aliases.get(command_name, command_name))
            if command_key not in COMMANDS:
                await self._send_error(message, "Неизвестная команда. Используй название из списка /jf_manager."); return
            try: rank = int(args[-1])
            except ValueError:
                await self._send_error(message, "Ранг должен быть числом от 0 до 6."); return
            if not 0 <= rank <= 6:
                await self._send_error(message, "Ранг должен быть числом от 0 до 6."); return
            self.store.set_access(chat_id, command_key, rank, actor_id)
            self.store.log_action(chat_id, actor_id, None, "access_set", None, f"{command_key}={rank}", {"command": command_key, "rank": rank})
            await message.reply(f"⚙️ Доступ к «{COMMANDS[command_key][0]}»: {'отключён' if rank == 6 else f'с ранга {rank}'}.")
            return
        if key == "access_log":
            if not await self._gate(message, "access_edit", silent=False): return
            rows = self.store.action_rows(chat_id, 20)
            data = [r for r in rows if r["action"] == "access_set"]
            await message.reply("<b>Последние действия с доступом</b>\n" + ("\n".join(f"#{r['id']} · {r['details_json']} · {r['created_at'][:16]}" for r in data[:10]) or "Изменений нет."))
            return

        # Chat-specific configuration.
        if key.startswith("setting_"):
            if not await self._gate(message, "settings", silent=False): return
            raw = " ".join(args).strip()
            if key == "setting_warn_limit":
                try: value = int(raw)
                except ValueError: value = -1
                if not 1 <= value <= 20:
                    await self._send_error(message, "Лимит предупреждений: целое число от 1 до 20."); return
                setting_key, stored = "warn_limit", str(value)
            else:
                seconds, remainder = parse_duration_tokens(args)
                if seconds is None or remainder:
                    await self._send_error(message, "Укажи срок, например: Варны период 30 дней."); return
                if seconds < 60 or seconds > 366*86400:
                    await self._send_error(message, "Срок должен быть от 1 минуты до 366 дней."); return
                setting_key = {"setting_warn_period":"warn_period_seconds", "setting_warn_ban":"warn_ban_seconds", "setting_mute_period":"mute_period_seconds", "setting_ban_period":"ban_period_seconds"}[key]
                stored = str(seconds)
            self.store.set_setting(chat_id, setting_key, stored, actor_id)
            await message.reply(f"⚙️ Настройка сохранена: {setting_key} = {human_duration(int(stored)) if 'period' in setting_key or 'seconds' in setting_key else stored}.")
            return

        # Moderator rank management.
        if key in {"mod_add", "mod_promote", "mod_demote", "mod_remove", "mod_list", "who_assigned", "mod_log", "mod_log_self", "mod_log_target", "mod_log_by"}:
            if key == "mod_list":
                if not await self._gate(message, "mod_list", silent=False): return
                await self._show_moderators(message)
                return
            if key == "mod_log_self":
                target_id = actor_id
            else:
                target_id, args, label = await self._target(message, args)
                if target_id is None and key not in {"mod_log", "mod_log_by"}:
                    await self._send_error(message, "Ответь на сообщение пользователя или укажи @username, которого бот уже видел, либо Telegram ID."); return
            if key in {"mod_log", "mod_log_self", "mod_log_target", "mod_log_by"}:
                if not await self._gate(message, "mod_log", silent=False): return
                if key == "mod_log_by":
                    rows = self.store.run(lambda c: c.execute("SELECT * FROM jf_manager_actions WHERE chat_id=? AND actor_id=? ORDER BY id DESC LIMIT 20", (chat_id,target_id)).fetchall())
                else:
                    rows = self.store.action_rows(chat_id, 20, target_id if key != "mod_log" else None)
                if key == "mod_log" or key == "mod_log_by": rows = [r for r in rows if r["action"].startswith("rank_")]
                lines = [f"#{r['id']} · {r['action']} · участник <code>{r['target_id'] or '—'}</code> · {r['created_at'][:16]}" for r in rows[:15]]
                await message.reply("<b>Журнал модерации</b>\n" + ("\n".join(lines) or "Записей нет."))
                return
            if key == "who_assigned":
                if not await self._gate(message, "mod_list", silent=False): return
                rows = self.store.action_rows(chat_id, 50, target_id)
                item = next((r for r in rows if r["action"] == "rank_set"), None)
                await message.reply(f"Кто назначил: <code>{item['actor_id']}</code>" if item else "История назначения не найдена.")
                return
            if not await self._gate(message, "mod_add" if key != "mod_remove" else "mod_remove", silent=False): return
            allowed, why = await self._target_guard(message, target_id, caller_rank)
            if not allowed:
                await self._send_error(message, why); return
            current = await self._rank(chat_id, target_id)
            if key == "mod_remove": new_rank = 0
            elif key == "mod_promote": new_rank = current + 1
            elif key == "mod_demote": new_rank = current - 1
            else: new_rank = forced_rank or 1
            if new_rank < 0 or new_rank > 4:
                await self._send_error(message, "Ранг модератора может быть от 1 до 4. Ранг 5 принадлежит создателю Telegram-чата."); return
            if new_rank >= caller_rank:
                await self._send_error(message, "Нельзя назначить ранг, равный или выше твоего."); return
            if key == "mod_demote" and current == 0:
                await self._send_error(message, "У пользователя нет ранга модератора."); return
            self.store.change_rank(chat_id, target_id, new_rank, actor_id)
            await message.reply(f"{'✅ Назначен' if new_rank else '↩️ Снят'} ранг: <b>{RANK_NAMES.get(new_rank, 'Участник')}</b>\nУчастник: <code>{target_id}</code>")
            return

        # Target-dependent moderation.
        if key in {"warn", "warnings", "warnings_self", "warn_remove", "warn_remove_number", "warn_clear", "warn_clear_n", "mute", "unmute", "mute_check", "ban", "unban", "kick", "kick_quiet", "reason"}:
            if key == "warnings_self":
                target_id = actor_id
            else:
                target_id, args, target_label = await self._target(message, args)
                if target_id is None and key not in {"warn_list"}:
                    await self._send_error(message, "Ответь на сообщение пользователя или укажи @username, которого бот уже видел, либо Telegram ID."); return
            if key in {"warnings", "warnings_self"}:
                if target_id != actor_id and not await self._gate(message, "warn_view", silent=False): return
                rows = self.store.active_warnings(chat_id, target_id)
                lines = [f"#{r['id']} · {r['reason'] or 'без причины'} · до {r['expires_at'][:16]} UTC" for r in rows[:20]]
                await message.reply(f"<b>Предупреждения · {len(rows)}</b>\n" + ("\n".join(lines) or "Активных предупреждений нет."))
                return
            if key in {"warn_remove", "warn_remove_number", "warn_clear", "warn_clear_n"}:
                if not await self._gate(message, "warn_remove", silent=False): return
                allowed, why = await self._target_guard(message, target_id, caller_rank)
                if not allowed:
                    await self._send_error(message, why); return
                if key == "warn_remove":
                    rows = self.store.active_warnings(chat_id, target_id)
                    removed = self.store.remove_warning(chat_id, int(rows[0]["id"]), actor_id, target_id) if rows else False
                    count = int(removed)
                elif key == "warn_remove_number":
                    try: warning_id = int(args[0])
                    except Exception:
                        await self._send_error(message, "Формат: Снять варн номер 12 @user"); return
                    count = int(self.store.remove_warning(chat_id, warning_id, actor_id, target_id))
                else:
                    limit = None
                    if key == "warn_clear_n":
                        try: limit = max(1, min(100, int(args[0])))
                        except Exception:
                            await self._send_error(message, "Формат: Снять варны 2 @user"); return
                    count = self.store.remove_warnings(chat_id, target_id, actor_id, limit)
                self.store.log_action(chat_id, actor_id, target_id, "warn_remove", None, f"снято: {count}")
                await message.reply(f"↩️ Снято предупреждений: {count}.")
                return
            if key == "warn":
                if not await self._gate(message, "warn", silent=False): return
                allowed, why = await self._target_guard(message, target_id, caller_rank)
                if not allowed:
                    await self._send_error(message, why); return
                # A leading period overrides the default warning lifetime.
                period, rest = parse_duration_tokens(args)
                if period is None:
                    period = int(self.store.setting(chat_id, "warn_period_seconds", str(30*86400)) or 30*86400)
                    rest = args
                reason = " ".join(rest).strip()
                warn_id, count = self.store.add_warning(chat_id, target_id, actor_id, reason, period)
                self.store.log_action(chat_id, actor_id, target_id, "warn", period, reason, {"warning_id":warn_id,"count":count})
                limit = int(self.store.setting(chat_id, "warn_limit", "3") or 3)
                result = f"⚠️ Предупреждение №{warn_id} выдано участнику <code>{target_id}</code>.\nАктивных: {count}/{limit}.\nПричина: {reason or 'не указана'}"
                if count >= limit:
                    ban_seconds = int(self.store.setting(chat_id, "warn_ban_seconds", str(7*86400)) or 7*86400)
                    try:
                        await self.bot.ban_chat_member(chat_id, target_id, until_date=utcnow()+timedelta(seconds=ban_seconds))
                    except Exception:
                        result += "\n⚠️ Не удалось автоматически заблокировать: проверь право бота на блокировку участников."
                    else:
                        self.store.set_restriction(chat_id, target_id, "ban", actor_id, f"лимит предупреждений ({count}/{limit})", ban_seconds)
                        self.store.log_action(chat_id, actor_id, target_id, "auto_ban_warn_limit", ban_seconds, f"лимит {limit}")
                        result += f"\n⛔ Достигнут лимит. Автоматическая блокировка на {human_duration(ban_seconds)}."
                await message.reply(result)
                return
            if key == "mute":
                await self._do_mute(message, target_id, caller_rank, args); return
            if key == "unmute":
                await self._unmute(message, target_id, caller_rank); return
            if key == "mute_check":
                if not await self._gate(message, "mute_list", silent=False): return
                rows = self.store.run(lambda c: c.execute("SELECT * FROM jf_manager_restrictions WHERE chat_id=? AND user_id=? AND kind='mute' AND active=1 ORDER BY id DESC LIMIT 1", (chat_id,target_id)).fetchone())
                await message.reply("🔇 Мут активен." if rows and (not rows["expires_at"] or rows["expires_at"] > iso()) else "🔊 Активный мут не найден.")
                return
            if key in {"ban", "kick", "kick_quiet"}:
                await self._do_ban(message, target_id, caller_rank, args,
                                   action="ban" if key == "ban" else "kick", quiet=(key == "kick_quiet")); return
            if key == "unban":
                if not await self._gate(message, "unban", silent=False): return
                # Unban may be used for a previously banned user who is not in chat.
                target_rank = await self._rank(chat_id, target_id)
                if target_rank >= caller_rank:
                    await self._send_error(message, "Нельзя разблокировать пользователя с равным или более высоким рангом."); return
                try:
                    await self.bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
                except Exception:
                    await self._send_error(message, "Telegram не выполнил разбан. Проверь права бота на блокировку участников.")
                    return
                self.store.set_restriction(chat_id, target_id, "ban", actor_id, "", None, active=False)
                self.store.log_action(chat_id, actor_id, target_id, "unban", None, "")
                await message.reply(f"✅ Пользователь <code>{target_id}</code> разблокирован.")
                return
            if key == "reason":
                if not await self._gate(message, "reason", silent=False): return
                rows = self.store.action_rows(chat_id, 50, target_id)
                item = next((r for r in rows if r["action"] in {"ban","mute","warn","kick","kick_quiet","kick_partial_ban","auto_ban_warn_limit"}), None)
                await message.reply(f"Причина: {item['reason'] or 'не указана'}\nМодератор: <code>{item['actor_id']}</code>\nДата: {item['created_at'][:16]} UTC" if item else "История наказаний не найдена.")
                return

        # Lists and history.
        if key == "warn_list":
            if not await self._gate(message, "warn_view", silent=False): return
            rows = self.store.run(lambda c: c.execute("SELECT * FROM jf_manager_warnings WHERE chat_id=? ORDER BY id DESC LIMIT 20", (chat_id,)).fetchall())
            lines = [f"#{r['id']} · <code>{r['user_id']}</code> · {r['reason'] or 'без причины'} · {r['created_at'][:16]}" for r in rows]
            await message.reply("<b>Журнал предупреждений</b>\n" + ("\n".join(lines) or "Записей нет.")); return
        if key == "ban_list":
            if not await self._gate(message, "ban_list", silent=False): return
            rows = self.store.run(lambda c: c.execute("SELECT * FROM jf_manager_restrictions WHERE chat_id=? AND kind='ban' AND active=1 ORDER BY id DESC LIMIT 30", (chat_id,)).fetchall())
            active = [r for r in rows if not r["expires_at"] or r["expires_at"] > iso()]
            await message.reply("<b>Список блокировок</b>\n" + ("\n".join(f"<code>{r['user_id']}</code> · {r['reason'] or 'без причины'} · {human_duration(None if not r['expires_at'] else max(0,int((datetime.fromisoformat(r['expires_at'])-utcnow()).total_seconds())))}" for r in active) or "Активных записей нет.")); return
        if key == "mute_list":
            if not await self._gate(message, "mute_list", silent=False): return
            rows = self.store.run(lambda c: c.execute("SELECT * FROM jf_manager_restrictions WHERE chat_id=? AND kind='mute' AND active=1 ORDER BY id DESC LIMIT 30", (chat_id,)).fetchall())
            active = [r for r in rows if not r["expires_at"] or r["expires_at"] > iso()]
            await message.reply("<b>Список ограничений</b>\n" + ("\n".join(f"<code>{r['user_id']}</code> · {r['reason'] or 'без причины'} · до {r['expires_at'][:16] if r['expires_at'] else 'бессрочно'}" for r in active) or "Активных мутов нет.")); return

    async def _show_moderators(self, message):
        chat_id = int(message.chat.id)
        admins = []
        with suppress(Exception):
            members = await self.bot.get_chat_administrators(chat_id)
            admins.extend(members)
        seen = set()
        lines = []
        for member in admins:
            user = getattr(member, "user", None)
            if not user or getattr(user, "is_bot", False):
                continue
            uid = int(user.id); seen.add(uid)
            rank = await self._rank(chat_id, uid)
            label = getattr(user, "username", "") or getattr(user, "first_name", "") or str(uid)
            lines.append(f"{html.escape(self.store.rank_label(chat_id, rank))} · @{label} · <code>{uid}</code>")
        for row in self.store.moderators(chat_id):
            if int(row["user_id"]) in seen:
                continue
            lines.append(f"{html.escape(self.store.rank_label(chat_id, int(row['rank'])))} · <code>{row['user_id']}</code>")
        await message.reply("<b>Администрация чата</b>\n" + ("\n".join(lines) or "Модераторы ещё не назначены."))


def register(dp, bot, F, db_path: str, lock=None, permissions_cls=None, owner_id: int = 0) -> Manager:
    manager = Manager(dp, bot, F, db_path, lock=lock, permissions_cls=permissions_cls, owner_id=owner_id)
    manager.register()
    return manager

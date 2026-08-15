import asyncio
import logging
import os
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Intentionally kept in code, as requested.
ADMIN_ID = 1682289834

DB_PATH = os.getenv("DB_PATH", "users.db").strip() or "users.db"
PORT = int(os.getenv("PORT", "10000"))

REPORT_COOLDOWN_SECONDS = 600
MAX_MESSAGE_LENGTH = 4000
MAX_REPORT_REASON_LENGTH = 2000
TELEGRAM_TEXT_LIMIT = 4096

# Conservative pacing for broadcast.
BROADCAST_DELAY_SECONDS = 0.10
BROADCAST_MAX_RETRIES = 3

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("anonymous-feedback-bot")


# =========================================================
# BOT
# =========================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# SQLite connections are short-lived, but DB writes/PRAGMAs are serialized.
DB_LOCK = threading.RLock()


# =========================================================
# STATES
# =========================================================


class FeedbackState(StatesGroup):
    waiting = State()


class ReportTargetState(StatesGroup):
    waiting = State()


class ReportReasonState(StatesGroup):
    waiting = State()


class AdminReplyState(StatesGroup):
    waiting = State()


class AdminSearchState(StatesGroup):
    waiting = State()


class BroadcastState(StatesGroup):
    waiting = State()


# =========================================================
# DESIGN
# =========================================================


def title(text: str) -> str:
    return f"♡₊˚ {text} ˚₊♡"


def section(text: str) -> str:
    return f"୨୧ {text} ୨୧"


def bullet(text: str) -> str:
    return f"♡ {text}"


def note(text: str) -> str:
    return f"♡ {text}"


def divider() -> str:
    return "୨୧ ───────────── ୨୧"


def warning(text: str) -> str:
    return f"⚠ {text}"


# =========================================================
# TIME / NORMALIZATION
# =========================================================


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_target_text(value: str) -> str:
    return value.strip().lstrip("@").strip().lower()


def target_key(
    target_text: str,
    target_user_id: Optional[int],
) -> str:
    if target_user_id is not None:
        return f"user:{target_user_id}"
    return f"text:{normalize_target_text(target_text)}"


def parse_target(value: str):
    value = value.strip()

    if not value or len(value) > 100:
        return None, None

    if value.startswith("@"):
        username = value[1:].strip()

        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", username):
            return None, None

        return f"@{username}", None

    if value.isdigit():
        try:
            target_id = int(value)
        except ValueError:
            return None, None

        if target_id <= 0:
            return None, None

        return value, target_id

    return None, None


def format_utc(value: str) -> str:
    try:
        return value[:19].replace("T", " ") + " UTC"
    except Exception:
        return value


# =========================================================
# DATABASE
# =========================================================


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def db_sync(func, *args, **kwargs):
    with DB_LOCK:
        conn = db_connect()
        try:
            return func(conn, *args, **kwargs)
        finally:
            conn.close()


async def db_call(func, *args, **kwargs):
    return await asyncio.to_thread(db_sync, func, *args, **kwargs)


def _table_columns(conn: sqlite3.Connection, table: str):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _init_db(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            messages_count INTEGER NOT NULL DEFAULT 0,
            reports_count INTEGER NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0,
            telegram_blocked INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_id INTEGER NOT NULL,
            target_text TEXT NOT NULL,
            target_user_id INTEGER,
            target_key TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new'
        );

        CREATE TABLE IF NOT EXISTS received_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            received_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_users_last_seen
            ON users(last_seen);

        CREATE INDEX IF NOT EXISTS idx_users_blocked
            ON users(blocked);

        CREATE INDEX IF NOT EXISTS idx_users_telegram_blocked
            ON users(telegram_blocked);

        CREATE INDEX IF NOT EXISTS idx_reports_status
            ON reports(status);

        CREATE INDEX IF NOT EXISTS idx_reports_reporter
            ON reports(reporter_id);
        """
    )
    conn.commit()


def _migrate_db(conn: sqlite3.Connection):
    user_columns = _table_columns(conn, "users")
    report_columns = _table_columns(conn, "reports")

    if "reports_count" not in user_columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN reports_count INTEGER NOT NULL DEFAULT 0"
        )

    if "blocked" not in user_columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0"
        )

    if "telegram_blocked" not in user_columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN telegram_blocked INTEGER NOT NULL DEFAULT 0"
        )

    # Keep compatibility with the old database table name.
    # init_db() creates received_messages before migration, so copy legacy
    # rows whenever the old sent_messages table exists.
    if _table_exists(conn, "sent_messages"):
        sent_count = conn.execute(
            "SELECT COUNT(*) FROM sent_messages"
        ).fetchone()[0]
        received_count = conn.execute(
            "SELECT COUNT(*) FROM received_messages"
        ).fetchone()[0]
        if sent_count > received_count:
            conn.execute("DELETE FROM received_messages")
            conn.execute(
                """
                INSERT INTO received_messages (user_id, received_at)
                SELECT user_id, sent_at
                FROM sent_messages
                """
            )

    if "target_key" not in report_columns:
        conn.execute(
            "ALTER TABLE reports ADD COLUMN target_key TEXT NOT NULL DEFAULT ''"
        )

        rows = conn.execute(
            "SELECT id, target_text, target_user_id FROM reports"
        ).fetchall()

        for row in rows:
            key = target_key(row["target_text"], row["target_user_id"])
            conn.execute(
                "UPDATE reports SET target_key = ? WHERE id = ?",
                (key, row["id"]),
            )

    # Make the target key unique per reporter.
    # This also protects against race conditions for concurrent confirms.
    duplicates = conn.execute(
        """
        SELECT reporter_id, target_key, MIN(id) AS keep_id
        FROM reports
        WHERE target_key != ''
        GROUP BY reporter_id, target_key
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    for row in duplicates:
        conn.execute(
            """
            DELETE FROM reports
            WHERE reporter_id = ?
              AND target_key = ?
              AND id != ?
            """,
            (row["reporter_id"], row["target_key"], row["keep_id"]),
        )

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_reporter_target_key
        ON reports(reporter_id, target_key)
        """
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reports_target_user ON reports(target_user_id)"
    )

    conn.commit()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


async def init_db():
    await db_call(_init_db)
    await db_call(_migrate_db)
    logger.info("SQLite initialized: %s", DB_PATH)


def _register_user(
    conn: sqlite3.Connection,
    user_id: int,
    first_name: str,
    last_name: str,
    username: str,
):
    stamp = now()
    conn.execute(
        """
        INSERT INTO users (
            user_id, first_name, last_name, username,
            first_seen, last_seen, telegram_blocked
        )
        VALUES (?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            username = excluded.username,
            last_seen = excluded.last_seen,
            telegram_blocked = 0
        """,
        (user_id, first_name or "", last_name or "", username or "", stamp, stamp),
    )
    conn.commit()


async def register_user(user):
    await db_call(
        _register_user,
        user.id,
        user.first_name or "",
        user.last_name or "",
        user.username or "",
    )


def _get_user(conn: sqlite3.Connection, user_id: int):
    return conn.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()


async def get_user(user_id: int):
    return await db_call(_get_user, user_id)


def _get_user_by_username(conn: sqlite3.Connection, username: str):
    normalized = normalize_target_text(username)
    if not normalized:
        return None
    return conn.execute(
        """
        SELECT * FROM users
        WHERE lower(ltrim(username, '@')) = ?
        ORDER BY last_seen DESC
        LIMIT 1
        """,
        (normalized,),
    ).fetchone()


async def get_user_by_username(username: str):
    return await db_call(_get_user_by_username, username)


def _is_blocked(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute(
        "SELECT blocked FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return bool(row and row["blocked"])


async def is_blocked(user_id: int) -> bool:
    return await db_call(_is_blocked, user_id)


def _set_blocked(conn: sqlite3.Connection, user_id: int, value: bool) -> bool:
    cur = conn.execute(
        "UPDATE users SET blocked = ? WHERE user_id = ?",
        (1 if value else 0, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


async def set_blocked(user_id: int, value: bool) -> bool:
    return await db_call(_set_blocked, user_id, value)


def _set_telegram_blocked(
    conn: sqlite3.Connection,
    user_id: int,
    value: bool,
):
    conn.execute(
        "UPDATE users SET telegram_blocked = ? WHERE user_id = ?",
        (1 if value else 0, user_id),
    )
    conn.commit()


async def set_telegram_blocked(user_id: int, value: bool):
    await db_call(_set_telegram_blocked, user_id, value)


def _increment_messages(conn: sqlite3.Connection, user_id: int):
    conn.execute(
        "UPDATE users SET messages_count = messages_count + 1 WHERE user_id = ?",
        (user_id,),
    )
    conn.execute(
        "INSERT INTO received_messages (user_id, received_at) VALUES (?, ?)",
        (user_id, now()),
    )
    conn.commit()


async def increment_messages(user_id: int):
    await db_call(_increment_messages, user_id)


def _increment_reports(conn: sqlite3.Connection, user_id: int):
    conn.execute(
        "UPDATE users SET reports_count = reports_count + 1 WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()


async def increment_reports(user_id: int):
    await db_call(_increment_reports, user_id)


def _last_report_timestamp(conn: sqlite3.Connection, user_id: int):
    row = conn.execute(
        """
        SELECT created_at
        FROM reports
        WHERE reporter_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    return row["created_at"] if row else None


async def report_cooldown_left(user_id: int) -> int:
    stamp = await db_call(_last_report_timestamp, user_id)
    if not stamp:
        return 0

    try:
        created = datetime.fromisoformat(stamp)
        elapsed = (datetime.now(timezone.utc) - created).total_seconds()
    except (ValueError, TypeError):
        return 0

    return max(0, int(REPORT_COOLDOWN_SECONDS - elapsed))


def _create_report(
    conn: sqlite3.Connection,
    reporter_id: int,
    target_text: str,
    target_user_id: Optional[int],
    target_key_value: str,
    reason: str,
):
    try:
        cur = conn.execute(
            """
            INSERT INTO reports (
                reporter_id, target_text, target_user_id,
                target_key, reason, created_at, status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'new')
            """,
            (
                reporter_id,
                target_text,
                target_user_id,
                target_key_value,
                reason,
                now(),
            ),
        )
        report_id = cur.lastrowid
        conn.commit()
        return report_id
    except sqlite3.IntegrityError:
        conn.rollback()
        return None


async def create_report(
    reporter_id: int,
    target_text: str,
    target_user_id: Optional[int],
    target_key_value: str,
    reason: str,
):
    return await db_call(
        _create_report,
        reporter_id,
        target_text,
        target_user_id,
        target_key_value,
        reason,
    )


def _delete_report(conn: sqlite3.Connection, report_id: int):
    conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    conn.commit()


async def delete_report(report_id: int):
    await db_call(_delete_report, report_id)


def _get_report(conn: sqlite3.Connection, report_id: int):
    return conn.execute(
        "SELECT * FROM reports WHERE id = ?",
        (report_id,),
    ).fetchone()


async def get_report(report_id: int):
    return await db_call(_get_report, report_id)


def _close_report(conn: sqlite3.Connection, report_id: int) -> bool:
    cur = conn.execute(
        """
        UPDATE reports
        SET status = 'closed'
        WHERE id = ? AND status = 'new'
        """,
        (report_id,),
    )
    conn.commit()
    return cur.rowcount > 0


async def close_report(report_id: int) -> bool:
    return await db_call(_close_report, report_id)


def _count_users(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


async def user_count() -> int:
    return await db_call(_count_users)


def _count_blocked(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE blocked = 1"
    ).fetchone()[0]


async def blocked_count() -> int:
    return await db_call(_count_blocked)


def _count_messages(conn: sqlite3.Connection) -> int:
    if _table_exists(conn, "received_messages"):
        return conn.execute("SELECT COUNT(*) FROM received_messages").fetchone()[0]
    return 0


async def message_count() -> int:
    return await db_call(_count_messages)


def _count_new_reports(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM reports WHERE status = 'new'"
    ).fetchone()[0]


async def new_reports_count() -> int:
    return await db_call(_count_new_reports)


def _list_users(conn: sqlite3.Connection, limit: int, offset: int):
    return conn.execute(
        """
        SELECT * FROM users
        ORDER BY last_seen DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()


async def list_users(limit: int = 11, offset: int = 0):
    return await db_call(_list_users, limit, offset)


def _search_users(conn: sqlite3.Connection, query: str, limit: int):
    q = f"%{query.lower()}%"
    return conn.execute(
        """
        SELECT * FROM users
        WHERE CAST(user_id AS TEXT) LIKE ?
           OR lower(username) LIKE ?
           OR lower(first_name) LIKE ?
           OR lower(last_name) LIKE ?
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (q, q, q, q, limit),
    ).fetchall()


async def search_users(query: str, limit: int = 15):
    return await db_call(_search_users, query, limit)


def _recent_reports(conn: sqlite3.Connection, limit: int):
    return conn.execute(
        """
        SELECT * FROM reports
        WHERE status = 'new'
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


async def recent_reports(limit: int = 20):
    return await db_call(_recent_reports, limit)


def _blocked_users(conn: sqlite3.Connection, limit: int):
    return conn.execute(
        """
        SELECT * FROM users
        WHERE blocked = 1
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


async def blocked_users(limit: int = 50):
    return await db_call(_blocked_users, limit)


def display_name(row) -> str:
    if not row:
        return "Без имени"
    value = " ".join(
        part
        for part in (row["first_name"], row["last_name"])
        if part
    ).strip()
    return value or "Без имени"


def display_username(row) -> str:
    if not row or not row["username"]:
        return "нет username"
    username = row["username"]
    return username if username.startswith("@") else f"@{username}"


# =========================================================
# TEXT / SCREEN HELPERS
# =========================================================


def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT):
    if len(text) <= limit:
        return [text]

    parts = []
    remaining = text

    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut < max(1, limit // 2):
            cut = limit
        part = remaining[:cut].rstrip()
        if not part:
            cut = limit
            part = remaining[:cut]
        parts.append(part)
        remaining = remaining[cut:].lstrip("\n")

    if remaining:
        parts.append(remaining)

    return parts


async def delete_user_message(message: Message):
    with suppress(TelegramBadRequest, TelegramForbiddenError, TelegramNotFound):
        await message.delete()


async def save_screen_message(state: FSMContext, message: Message):
    await state.update_data(
        screen_message_id=message.message_id,
        screen_chat_id=message.chat.id,
    )


async def get_screen(state: FSMContext):
    data = await state.get_data()
    return data.get("screen_message_id"), data.get("screen_chat_id")


async def edit_message(
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup=None,
) -> bool:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
        return True
    except TelegramBadRequest as exc:
        message = str(exc).lower()
        if "message is not modified" in message:
            return True
        logger.warning(
            "Telegram rejected message edit | chat_id=%s | message_id=%s | %s",
            chat_id,
            message_id,
            exc,
        )
        return False
    except (TelegramForbiddenError, TelegramNotFound) as exc:
        logger.warning(
            "Message edit unavailable | chat_id=%s | message_id=%s | %s",
            chat_id,
            message_id,
            exc,
        )
        return False
    except Exception:
        logger.exception(
            "Unexpected screen edit error | chat_id=%s | message_id=%s",
            chat_id,
            message_id,
        )
        return False


async def edit_state_screen(
    state: FSMContext,
    chat_id: int,
    text: str,
    reply_markup=None,
) -> bool:
    message_id, saved_chat_id = await get_screen(state)

    if not message_id:
        return False

    if saved_chat_id and saved_chat_id != chat_id:
        return False

    return await edit_message(
        chat_id,
        message_id,
        text,
        reply_markup,
    )


async def edit_callback_screen(
    callback: CallbackQuery,
    state: FSMContext,
    text: str,
    reply_markup=None,
) -> bool:
    edited = await edit_message(
        callback.message.chat.id,
        callback.message.message_id,
        text,
        reply_markup,
    )

    if edited:
        await save_screen_message(state, callback.message)
        return True

    try:
        sent = await callback.message.answer(
            text,
            reply_markup=reply_markup,
        )
    except Exception:
        logger.exception(
            "Could not send fallback screen | user_id=%s",
            callback.from_user.id,
        )
        return False

    await save_screen_message(state, sent)
    return True


async def send_screen(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup=None,
):
    sent = await message.answer(text, reply_markup=reply_markup)
    await save_screen_message(state, sent)
    return sent


async def callback_answer(
    callback: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
):
    with suppress(Exception):
        await callback.answer(text, show_alert=show_alert)


# =========================================================
# TELEGRAM SEND HELPERS
# =========================================================

async def send_with_retry(
    chat_id: int,
    text: str,
    reply_markup=None,
    max_retries: int = 3,
):
    backoff = 1.0

    for attempt in range(max_retries + 1):
        try:
            return await bot.send_message(
                chat_id,
                text,
                reply_markup=reply_markup,
            )
        except TelegramRetryAfter as exc:
            if attempt >= max_retries:
                raise
            wait = max(1, int(exc.retry_after))
            logger.warning(
                "Telegram rate limit | chat_id=%s | retry_after=%s",
                chat_id,
                wait,
            )
            await asyncio.sleep(wait)
        except (TelegramNetworkError, TelegramServerError) as exc:
            if attempt >= max_retries:
                raise
            logger.warning(
                "Temporary Telegram error | chat_id=%s | attempt=%s/%s | %s",
                chat_id,
                attempt + 1,
                max_retries,
                exc,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 8.0)

    raise RuntimeError("Message send failed")


async def send_long_message(
    chat_id: int,
    text: str,
    reply_markup=None,
):
    sent = []
    parts = split_text(text)

    for index, part in enumerate(parts):
        markup = reply_markup if index == len(parts) - 1 else None
        sent.append(await send_with_retry(chat_id, part, markup))

    return sent


async def send_admin_feedback(
    user_id: int,
    sender_name: str,
    username: str,
    text: str,
):
    admin_text = (
        f"{title('Новое сообщение')}\n\n"
        f"{section('Отправитель')}\n"
        f"{bullet('Имя: ' + sender_name)}\n"
        f"{bullet('Username: ' + username)}\n"
        f"{bullet('ID: ' + str(user_id))}\n\n"
        f"{divider()}\n"
        f"{section('Текст')}\n"
        f"{text}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↳ Ответить",
                    callback_data=f"a:replyuser:{user_id}",
                ),
                InlineKeyboardButton(
                    text="⊘ Заблокировать",
                    callback_data=f"a:block:{user_id}",
                ),
            ]
        ]
    )

    return await send_long_message(
        ADMIN_ID,
        admin_text,
        keyboard,
    )


async def send_admin_report(
    report_id: int,
    target_text: str,
    target_user_id: Optional[int],
    reason: str,
    reporter_name: str,
    reporter_username: str,
    reporter_id: int,
):
    target_id_text = (
        str(target_user_id) if target_user_id is not None else "не указан"
    )

    admin_text = (
        f"{title(f'Жалоба #{report_id}')}\n\n"
        f"{section('Пользователь')}\n"
        f"{bullet('Указано: ' + target_text)}\n"
        f"{bullet('ID: ' + target_id_text)}\n\n"
        f"{section('Причина')}\n"
        f"{reason}\n\n"
        f"{divider()}\n"
        f"{section('Заявитель')}\n"
        f"{bullet('Имя: ' + reporter_name)}\n"
        f"{bullet('Username: ' + reporter_username)}\n"
        f"{bullet('ID: ' + str(reporter_id))}"
    )

    rows = []
    if target_user_id is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⊘ Заблокировать пользователя",
                    callback_data=f"a:block:{target_user_id}:r{report_id}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="✓ Закрыть жалобу",
                callback_data=f"a:close_report:{report_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="‹ К жалобам",
                callback_data="a:reports",
            )
        ]
    )

    return await send_long_message(
        ADMIN_ID,
        admin_text,
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def send_admin_reply_to_user(user_id: int, text: str):
    user_text = f"{title('Ответ администрации')}\n\n{text}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="♡ Ответить",
                    callback_data="u:send",
                )
            ]
        ]
    )
    return await send_long_message(user_id, user_text, keyboard)


async def send_broadcast_to_user(user_id: int, text: str):
    return await send_long_message(
        user_id,
        f"{title('Сообщение от администрации')}\n\n{text}",
    )


# =========================================================
# KEYBOARDS
# =========================================================


def main_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="♡₊˚ Оставить сообщение ˚₊♡",
                    callback_data="u:send",
                )
            ],
            [
                InlineKeyboardButton(
                    text="୨୧ Как это работает",
                    callback_data="u:info",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚠ Пожаловаться",
                    callback_data="u:report",
                )
            ],
        ]
    )


def user_cancel_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="‹₊˚ Отмена ˚₊",
                    callback_data="u:cancel",
                )
            ]
        ]
    )


def admin_cancel_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="‹ Отмена",
                    callback_data="a:cancel",
                )
            ]
        ]
    )


def after_send_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="♡ Ещё сообщение",
                    callback_data="u:send",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⌂ В меню",
                    callback_data="u:home",
                )
            ],
        ]
    )


def admin_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="♙ Пользователи",
                    callback_data="a:users:0",
                ),
                InlineKeyboardButton(
                    text="♡ Сообщения",
                    callback_data="a:messages",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚠ Жалобы",
                    callback_data="a:reports",
                ),
                InlineKeyboardButton(
                    text="୨୧ Статистика",
                    callback_data="a:stats",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⊘ Заблокированные",
                    callback_data="a:blocked",
                )
            ],
            [
                InlineKeyboardButton(
                    text="➤ Рассылка",
                    callback_data="a:broadcast",
                )
            ],
        ]
    )


def back_admin():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="‹ В админку",
                    callback_data="a:home",
                )
            ]
        ]
    )


def user_admin_kb(user_id: int, blocked: bool):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("♡ Разблокировать" if blocked else "⊘ Заблокировать"),
                    callback_data=f"a:toggle:{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↳ Ответить",
                    callback_data=f"a:replyuser:{user_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="‹ Назад",
                    callback_data="a:users:0",
                )
            ],
        ]
    )


def report_admin_kb(report_id: int, target_user_id: Optional[int]):
    rows = []
    if target_user_id is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⊘ Заблокировать пользователя",
                    callback_data=f"a:block:{target_user_id}:r{report_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="✓ Закрыть жалобу",
                callback_data=f"a:close_report:{report_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="‹ К жалобам",
                callback_data="a:reports",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =========================================================
# TEXT
# =========================================================


def home_text():
    return (
        f"{title('Анонимная обратная связь')}\n\n"
        "Здесь вы можете оставить сообщение администрации.\n\n"
        f"{bullet('Другие участники не видят ваш профиль.')}\n\n"
        f"{divider()}\n"
        "♡ Выберите действие"
    )


async def admin_home_text():
    users, messages, blocked, reports = await asyncio.gather(
        user_count(),
        message_count(),
        blocked_count(),
        new_reports_count(),
    )

    return (
        f"{title('Панель администратора')}\n\n"
        f"{section('Сводка')}\n"
        f"{bullet(f'Пользователей: {users}')}\n"
        f"{bullet(f'Сообщений: {messages}')}\n"
        f"{bullet(f'Заблокировано: {blocked}')}\n"
        f"{bullet(f'Новых жалоб: {reports}')}\n\n"
        f"{divider()}\n"
        "♡ Выберите раздел"
    )


# =========================================================
# USER START / HOME / INFO
# =========================================================


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await register_user(message.from_user)
    await delete_user_message(message)

    if await is_blocked(message.from_user.id):
        await message.answer(
            f"{title('Доступ ограничен')}\n\n"
            f"{bullet('Для вашего аккаунта отправка сообщений отключена.')}"
        )
        return

    await send_screen(message, state, home_text(), main_kb())


@dp.callback_query(F.data == "u:home")
async def user_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_callback_screen(callback, state, home_text(), main_kb())
    await callback_answer(callback)


@dp.callback_query(F.data == "u:info")
async def user_info(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_callback_screen(
        callback,
        state,
        (
            "♡₊˚ Как это работает ˚₊♡\n\n"
            "Вы можете оставить обратную связь о флуде, "
            "поделиться своим мнением, предложением, советом "
            "или своими предпочтениями.\n\n"
            f"{divider()}\n"
            "♡ Напишите сообщение через бота.\n"
            "♡ Выберите нужное действие в меню.\n\n"
            f"{divider()}\n"
            "♡ Спасибо за вашу обратную связь."
        ),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="♡ О сообщениях",
                        callback_data="u:send_info",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⚠ О жалобах",
                        callback_data="u:report_info",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="‹ Назад",
                        callback_data="u:home",
                    )
                ],
            ]
        ),
    )
    await callback_answer(callback)


@dp.callback_query(F.data == "u:send_info")
async def send_info(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_callback_screen(
        callback,
        state,
        (
            "♡₊˚ О сообщениях ˚₊♡\n\n"
            "Здесь можно высказать своё мнение о флуде, "
            "предложить идею, поделиться советом "
            "или рассказать о своих предпочтениях.\n\n"
            f"{divider()}\n"
            "♡ Напишите сообщение.\n"
            "♡ Максимальная длина — 4000 символов.\n\n"
            "₊˚♡ Делитесь мыслями свободно. ♡˚₊"
        ),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="♡ Оставить сообщение",
                        callback_data="u:send",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="‹ Назад",
                        callback_data="u:info",
                    )
                ],
            ]
        ),
    )
    await callback_answer(callback)


@dp.callback_query(F.data == "u:report_info")
async def report_info(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_callback_screen(
        callback,
        state,
        (
            f"{title('О жалобах')}\n\n"
            f"{bullet('Укажите пользователя, на которого хотите пожаловаться.')}\n"
            f"{bullet('После этого укажите причину обращения.')}\n"
            f"{bullet('Перед отправкой можно проверить введённые данные.')}\n"
            f"{bullet('На одного пользователя можно пожаловаться один раз.')}\n"
            f"{bullet('Между жалобами действует пауза 10 минут.')}\n\n"
            f"{divider()}\n"
            "♡ Пожалуйста, указывайте достоверную информацию."
        ),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚠ Подать жалобу",
                        callback_data="u:report",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="‹ Назад",
                        callback_data="u:info",
                    )
                ],
            ]
        ),
    )
    await callback_answer(callback)


# =========================================================
# USER FEEDBACK
# =========================================================


@dp.callback_query(F.data == "u:send")
async def user_send(callback: CallbackQuery, state: FSMContext):
    if await is_blocked(callback.from_user.id):
        await callback_answer(callback, "Доступ ограничен.", True)
        return

    await state.set_state(FeedbackState.waiting)
    await edit_callback_screen(
        callback,
        state,
        (
            f"{title('Новое сообщение')}\n\n"
            f"{bullet('Напишите сообщение следующим сообщением.')}\n"
            f"{bullet('Максимальная длина — 4000 символов.')}\n\n"
            f"{divider()}\n"
            "♡ Вы можете написать всё, что хотите сообщить."
        ),
        user_cancel_kb(),
    )
    await callback_answer(callback)


@dp.callback_query(F.data == "u:cancel")
async def user_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_callback_screen(callback, state, home_text(), main_kb())
    await callback_answer(callback)


@dp.message(Command("cancel"))
async def command_cancel(message: Message, state: FSMContext):
    await state.clear()
    await delete_user_message(message)

    if message.from_user.id == ADMIN_ID:
        await send_screen(message, state, await admin_home_text(), admin_kb())
    else:
        await send_screen(message, state, home_text(), main_kb())


@dp.message(FeedbackState.waiting, F.text)
async def feedback(message: Message, state: FSMContext):
    await register_user(message.from_user)

    if await is_blocked(message.from_user.id):
        await delete_user_message(message)
        await state.clear()
        await message.answer(
            f"{title('Доступ ограничен')}\n\n"
            f"{bullet('Для вашего аккаунта отправка сообщений отключена.')}"
        )
        return

    text = message.text.strip()

    if not text:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            message.chat.id,
            (
                f"{title('Новое сообщение')}\n\n"
                f"{bullet('Сообщение не может быть пустым.')}\n\n"
                "Попробуйте ещё раз."
            ),
            user_cancel_kb(),
        )
        return

    if len(text) > MAX_MESSAGE_LENGTH:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            message.chat.id,
            (
                f"{title('Новое сообщение')}\n\n"
                f"{bullet('Сообщение слишком длинное.')}\n"
                f"{bullet('Максимум — 4000 символов.')}"
            ),
            user_cancel_kb(),
        )
        return

    screen_message_id, _ = await get_screen(state)
    user_row = await get_user(message.from_user.id)

    sender_name = display_name(user_row)
    username = display_username(user_row)

    try:
        await send_admin_feedback(
            user_id=message.from_user.id,
            sender_name=sender_name,
            username=username,
            text=text,
        )
    except TelegramUnauthorizedError:
        logger.critical("BOT_TOKEN is invalid while sending feedback.")
        await delete_user_message(message)
        await state.clear()
        error_text = (
            f"{title('Ошибка')}\n\n"
            f"{bullet('Сервис временно недоступен.')}\n"
            f"{bullet('Попробуйте позже.')}"
        )
        if not screen_message_id or not await edit_message(
            message.chat.id, screen_message_id, error_text, main_kb()
        ):
            await message.answer(error_text, reply_markup=main_kb())
        return
    except Exception:
        logger.exception(
            "Failed to send feedback | user_id=%s",
            message.from_user.id,
        )
        await delete_user_message(message)
        await state.clear()
        error_text = (
            f"{title('Ошибка')}\n\n"
            f"{bullet('Не удалось передать сообщение.')}\n"
            f"{bullet('Попробуйте немного позже.')}"
        )
        if not screen_message_id or not await edit_message(
            message.chat.id, screen_message_id, error_text, main_kb()
        ):
            await message.answer(error_text, reply_markup=main_kb())
        return

    # Counters are updated only after successful delivery to admin.
    await increment_messages(message.from_user.id)
    await delete_user_message(message)

    success_text = (
        f"{title('Сообщение отправлено')}\n\n"
        f"{bullet('Ваше сообщение успешно передано.')}\n"
        f"{bullet('При необходимости вы получите ответ через бота.')}\n\n"
        f"{divider()}\n"
        "♡ Спасибо за обратную связь."
    )

    await state.clear()

    if not screen_message_id or not await edit_message(
        message.chat.id, screen_message_id, success_text, after_send_kb()
    ):
        await message.answer(success_text, reply_markup=after_send_kb())


@dp.message(FeedbackState.waiting)
async def feedback_non_text(message: Message, state: FSMContext):
    await delete_user_message(message)
    updated = await edit_state_screen(
        state,
        message.chat.id,
        (
            f"{title('Новое сообщение')}\n\n"
            f"{bullet('Пожалуйста, отправьте сообщение текстом.')}"
        ),
        user_cancel_kb(),
    )
    if not updated:
        await send_screen(
            message,
            state,
            (
                f"{title('Новое сообщение')}\n\n"
                f"{bullet('Пожалуйста, отправьте сообщение текстом.')}"
            ),
            user_cancel_kb(),
        )


# =========================================================
# USER REPORT
# =========================================================


@dp.callback_query(F.data == "u:report")
async def report_start(callback: CallbackQuery, state: FSMContext):
    if await is_blocked(callback.from_user.id):
        await callback_answer(callback, "Доступ ограничен.", True)
        return

    left = await report_cooldown_left(callback.from_user.id)
    if left:
        minutes = (left + 59) // 60
        await callback_answer(
            callback,
            f"Следующую жалобу можно отправить примерно через {minutes} мин.",
            True,
        )
        return

    await state.set_state(ReportTargetState.waiting)
    await edit_callback_screen(
        callback,
        state,
        (
            f"{title('Жалоба на пользователя')}\n\n"
            f"{bullet('Укажите username или Telegram ID пользователя.')}\n\n"
            f"{divider()}\n"
            f"{warning('Жалоба не является анонимной для администрации.')}"
        ),
        user_cancel_kb(),
    )
    await callback_answer(callback)


@dp.message(ReportTargetState.waiting, F.text)
async def report_target(message: Message, state: FSMContext):
    value = message.text.strip()
    target_text, target_user_id = parse_target(value)

    if not target_text:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            message.chat.id,
            (
                f"{title('Жалоба')}\n\n"
                f"{bullet('Укажите корректный username или Telegram ID.')}\n\n"
                f"{bullet('Username может содержать только буквы, цифры и _.')}"
            ),
            user_cancel_kb(),
        )
        return

    # For known users, a username is upgraded to the stable Telegram ID.
    if target_user_id is None:
        known = await get_user_by_username(target_text)
        if known:
            target_user_id = known["user_id"]

    if target_user_id == message.from_user.id:
        await delete_user_message(message)
        await state.clear()
        await message.answer(
            f"{title('Жалоба')}\n\n"
            f"{bullet('Нельзя пожаловаться на самого себя.')}",
            reply_markup=main_kb(),
        )
        return

    if target_user_id is None:
        own = await get_user(message.from_user.id)
        if own and own["username"]:
            if normalize_target_text(own["username"]) == normalize_target_text(target_text):
                await delete_user_message(message)
                await state.clear()
                await message.answer(
                    f"{title('Жалоба')}\n\n"
                    f"{bullet('Нельзя пожаловаться на самого себя.')}",
                    reply_markup=main_kb(),
                )
                return

    key = target_key(target_text, target_user_id)

    if await _has_reported_target_safe(message.from_user.id, key):
        await delete_user_message(message)
        await state.clear()
        await message.answer(
            f"{title('Жалоба уже отправлена')}\n\n"
            f"{bullet('Вы уже отправляли жалобу на этого пользователя.')}",
            reply_markup=main_kb(),
        )
        return

    await state.update_data(
        target_text=target_text,
        target_user_id=target_user_id,
        target_key=key,
    )
    await state.set_state(ReportReasonState.waiting)
    await delete_user_message(message)
    await edit_state_screen(
        state,
        message.chat.id,
        (
            f"{title('Причина жалобы')}\n\n"
            f"{bullet('Кратко опишите причину обращения.')}\n"
            f"{bullet('Максимум — 2000 символов.')}"
        ),
        user_cancel_kb(),
    )


async def _has_reported_target_safe(reporter_id: int, key: str) -> bool:
    def _check(conn: sqlite3.Connection):
        row = conn.execute(
            """
            SELECT 1 FROM reports
            WHERE reporter_id = ? AND target_key = ?
            LIMIT 1
            """,
            (reporter_id, key),
        ).fetchone()
        return row is not None

    return await db_call(_check)


@dp.message(ReportReasonState.waiting, F.text)
async def report_reason(message: Message, state: FSMContext):
    reason = message.text.strip()

    if len(reason) < 3:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            message.chat.id,
            (
                f"{title('Причина жалобы')}\n\n"
                f"{bullet('Опишите причину немного подробнее.')}"
            ),
            user_cancel_kb(),
        )
        return

    if len(reason) > MAX_REPORT_REASON_LENGTH:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            message.chat.id,
            (
                f"{title('Причина жалобы')}\n\n"
                f"{bullet('Причина слишком длинная.')}\n"
                f"{bullet('Максимум — 2000 символов.')}"
            ),
            user_cancel_kb(),
        )
        return

    data = await state.get_data()
    target = data.get("target_text")
    target_id = data.get("target_user_id")
    target_key_value = data.get("target_key")

    if not target or not target_key_value:
        await delete_user_message(message)
        await state.clear()
        await message.answer(
            f"{title('Ошибка')}\n\n"
            f"{bullet('Данные жалобы устарели.')}",
            reply_markup=main_kb(),
        )
        return

    await state.update_data(reason=reason)
    await delete_user_message(message)

    target_id_line = (
        f"{bullet('ID: ' + str(target_id))}\n" if target_id is not None else ""
    )

    await edit_state_screen(
        state,
        message.chat.id,
        (
            f"{title('Проверьте жалобу')}\n\n"
            f"{section('Пользователь')}\n"
            f"{bullet(target)}\n"
            f"{target_id_line}\n"
            f"{section('Причина')}\n"
            f"{reason}\n\n"
            f"{divider()}\n"
            f"{warning('Перед отправкой убедитесь, что всё указано верно.')}"
        ),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚠ Отправить жалобу",
                        callback_data="u:report_confirm",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="↻ Изменить причину",
                        callback_data="u:report_edit",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="‹ Отмена",
                        callback_data="u:cancel",
                    )
                ],
            ]
        ),
    )


@dp.callback_query(F.data == "u:report_edit")
async def report_edit(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("target_text"):
        await callback_answer(callback, "Данные жалобы устарели.", True)
        return

    await state.set_state(ReportReasonState.waiting)
    await edit_callback_screen(
        callback,
        state,
        f"{title('Причина жалобы')}\n\n{bullet('Напишите причину заново.')}",
        user_cancel_kb(),
    )
    await callback_answer(callback)


@dp.callback_query(F.data == "u:report_confirm")
async def report_confirm(callback: CallbackQuery, state: FSMContext):
    if await is_blocked(callback.from_user.id):
        await state.clear()
        await callback_answer(callback, "Доступ ограничен.", True)
        return

    data = await state.get_data()
    target = data.get("target_text")
    target_id = data.get("target_user_id")
    target_key_value = data.get("target_key")
    reason = data.get("reason")

    if not target or not target_key_value or not reason:
        await state.clear()
        await callback_answer(callback, "Данные жалобы устарели.", True)
        return

    left = await report_cooldown_left(callback.from_user.id)
    if left:
        await state.clear()
        minutes = (left + 59) // 60
        await edit_callback_screen(
            callback,
            state,
            (
                f"{title('Слишком часто')}\n\n"
                f"{bullet(f'Следующую жалобу можно отправить примерно через {minutes} мин.')}"
            ),
            main_kb(),
        )
        await callback_answer(callback)
        return

    if target_id == callback.from_user.id:
        await state.clear()
        await edit_callback_screen(
            callback,
            state,
            f"{title('Жалоба')}\n\n{bullet('Нельзя пожаловаться на самого себя.')}",
            main_kb(),
        )
        await callback_answer(callback)
        return

    # DB unique index is the final race-condition protection.
    report_id = await create_report(
        reporter_id=callback.from_user.id,
        target_text=target,
        target_user_id=target_id,
        target_key_value=target_key_value,
        reason=reason,
    )

    if report_id is None:
        await state.clear()
        await edit_callback_screen(
            callback,
            state,
            (
                f"{title('Жалоба уже отправлена')}\n\n"
                f"{bullet('Вы уже отправляли жалобу на этого пользователя.')}"
            ),
            main_kb(),
        )
        await callback_answer(callback)
        return

    reporter = await get_user(callback.from_user.id)
    reporter_name = display_name(reporter)
    reporter_username = display_username(reporter)

    try:
        await send_admin_report(
            report_id=report_id,
            target_text=target,
            target_user_id=target_id,
            reason=reason,
            reporter_name=reporter_name,
            reporter_username=reporter_username,
            reporter_id=callback.from_user.id,
        )
    except TelegramUnauthorizedError:
        await delete_report(report_id)
        logger.critical("BOT_TOKEN is invalid while sending report | report_id=%s", report_id)
        await state.clear()
        await edit_callback_screen(
            callback,
            state,
            f"{title('Ошибка')}\n\n{bullet('Сервис временно недоступен.')}\n{bullet('Попробуйте позже.')}",
            main_kb(),
        )
        await callback_answer(callback)
        return
    except Exception:
        # The report is removed so a failed delivery does not start a cooldown
        # and does not create a false 'already reported' state.
        await delete_report(report_id)
        logger.exception(
            "Failed to send report | report_id=%s | reporter_id=%s",
            report_id,
            callback.from_user.id,
        )
        await state.clear()
        await edit_callback_screen(
            callback,
            state,
            (
                f"{title('Ошибка')}\n\n"
                f"{bullet('Не удалось передать жалобу.')}\n"
                f"{bullet('Попробуйте немного позже.')}"
            ),
            main_kb(),
        )
        await callback_answer(callback)
        return

    await increment_reports(callback.from_user.id)
    await state.clear()

    await edit_callback_screen(
        callback,
        state,
        (
            f"{title('Жалоба отправлена')}\n\n"
            f"{bullet('Ваше обращение передано.')}\n"
            f"{bullet('Следующую жалобу можно отправить через 10 минут.')}\n\n"
            f"{divider()}\n"
            "♡ Спасибо за обращение."
        ),
        main_kb(),
    )
    await callback_answer(callback, "Жалоба отправлена")


@dp.message(ReportTargetState.waiting)
@dp.message(ReportReasonState.waiting)
async def report_non_text(message: Message, state: FSMContext):
    await delete_user_message(message)
    updated = await edit_state_screen(
        state,
        message.chat.id,
        f"{title('Жалоба')}\n\n{bullet('Пожалуйста, отправьте ответ текстом.')}",
        user_cancel_kb(),
    )
    if not updated:
        await send_screen(
            message,
            state,
            f"{title('Жалоба')}\n\n{bullet('Пожалуйста, отправьте ответ текстом.')}",
            user_cancel_kb(),
        )


# =========================================================
# ADMIN ACCESS / COMMAND
# =========================================================


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


@dp.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await register_user(message.from_user)
    await state.clear()
    await delete_user_message(message)
    await send_screen(message, state, await admin_home_text(), admin_kb())


@dp.callback_query(F.data == "a:home")
async def admin_home(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    await state.clear()
    await edit_callback_screen(callback, state, await admin_home_text(), admin_kb())
    await callback_answer(callback)


@dp.callback_query(F.data == "a:cancel")
async def admin_cancel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    await state.clear()
    await edit_callback_screen(callback, state, await admin_home_text(), admin_kb())
    await callback_answer(callback)


# =========================================================
# ADMIN STATS / MESSAGES
# =========================================================


@dp.callback_query(F.data == "a:stats")
async def admin_stats(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    await state.clear()
    users, messages, blocked, reports = await asyncio.gather(
        user_count(),
        message_count(),
        blocked_count(),
        new_reports_count(),
    )

    await edit_callback_screen(
        callback,
        state,
        (
            f"{title('Статистика')}\n\n"
            f"{bullet(f'Пользователей: {users}')}\n"
            f"{bullet(f'Получено сообщений: {messages}')}\n"
            f"{bullet(f'Заблокировано: {blocked}')}\n"
            f"{bullet(f'Новых жалоб: {reports}')}\n\n"
            f"{divider()}\n"
            "♡ Статистика обновляется автоматически."
        ),
        back_admin(),
    )
    await callback_answer(callback)


@dp.callback_query(F.data == "a:messages")
async def admin_messages(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    await state.clear()
    count = await message_count()
    await edit_callback_screen(
        callback,
        state,
        (
            f"{title('Сообщения')}\n\n"
            f"{bullet(f'Всего получено: {count}')}\n\n"
            f"{divider()}\n"
            "♡ Новые сообщения приходят прямо в этот чат."
        ),
        back_admin(),
    )
    await callback_answer(callback)


# =========================================================
# ADMIN USERS / SEARCH
# =========================================================


@dp.callback_query(F.data.startswith("a:users:"))
async def admin_users(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    await state.clear()

    try:
        page = max(0, int(callback.data.split(":")[2]))
    except (ValueError, IndexError):
        page = 0

    rows = await list_users(limit=11, offset=page * 10)
    has_next = len(rows) > 10
    rows = rows[:10]
    total = await user_count()

    buttons = []

    for row in rows:
        label = display_name(row)
        username = display_username(row)
        if row["blocked"]:
            label = f"⊘ {label}"
        if username != "нет username":
            label = f"{label} {username}"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"a:user:{row['user_id']}",
                )
            ]
        )

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="‹",
                callback_data=f"a:users:{page - 1}",
            )
        )
    if has_next:
        nav.append(
            InlineKeyboardButton(
                text="›",
                callback_data=f"a:users:{page + 1}",
            )
        )
    if nav:
        buttons.append(nav)

    buttons.append(
        [InlineKeyboardButton(text="⌕ Поиск", callback_data="a:search")]
    )
    buttons.append(
        [InlineKeyboardButton(text="‹ Назад", callback_data="a:home")]
    )

    await edit_callback_screen(
        callback,
        state,
        (
            f"{title('Пользователи')}\n\n"
            f"{bullet(f'Всего: {total}')}\n"
            f"{bullet(f'Страница: {page + 1}')}\n\n"
            f"{divider()}\n"
            "♡ Выберите пользователя."
        ),
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback_answer(callback)


@dp.callback_query(F.data == "a:search")
async def admin_search_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    await state.set_state(AdminSearchState.waiting)
    await edit_callback_screen(
        callback,
        state,
        f"{title('Поиск пользователя')}\n\n{bullet('Введите имя, username или Telegram ID.')}",
        admin_cancel_kb(),
    )
    await callback_answer(callback)


@dp.message(AdminSearchState.waiting, F.text)
async def admin_search(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    query = message.text.strip()
    screen_message_id, _ = await get_screen(state)

    if not query:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            message.chat.id,
            f"{title('Поиск пользователя')}\n\n{bullet('Введите запрос.')}",
            admin_cancel_kb(),
        )
        return

    rows = await search_users(query)
    await delete_user_message(message)
    await state.clear()

    if not rows:
        text = f"{title('Поиск')}\n\n{bullet('Пользователи не найдены.')}"
        if not screen_message_id or not await edit_message(
            message.chat.id,
            screen_message_id,
            text,
            admin_kb(),
        ):
            await message.answer(text, reply_markup=admin_kb())
        return

    buttons = []
    for row in rows:
        label = display_name(row)
        username = display_username(row)
        if row["blocked"]:
            label = f"⊘ {label}"
        if username != "нет username":
            label = f"{label} {username}"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"a:user:{row['user_id']}",
                )
            ]
        )

    buttons.append(
        [InlineKeyboardButton(text="‹ В админку", callback_data="a:home")]
    )

    text = f"{title('Результаты поиска')}\n\n{bullet(f'Найдено: {len(rows)}')}"

    if not screen_message_id or not await edit_message(
        message.chat.id,
        screen_message_id,
        text,
        InlineKeyboardMarkup(inline_keyboard=buttons),
    ):
        await message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@dp.message(AdminSearchState.waiting)
async def admin_search_non_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await delete_user_message(message)
    await edit_state_screen(
        state,
        message.chat.id,
        f"{title('Поиск пользователя')}\n\n{bullet('Введите имя, username или Telegram ID.')}",
        admin_cancel_kb(),
    )


@dp.callback_query(F.data.startswith("a:user:"))
async def admin_user(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    await state.clear()

    try:
        user_id = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        await callback_answer(callback, "Некорректный ID.", True)
        return

    row = await get_user(user_id)
    if not row:
        await callback_answer(callback, "Пользователь не найден.", True)
        return

    status = "заблокирован" if row["blocked"] else "активен"
    text = (
        f"{title('Пользователь')}\n\n"
        f"{section('Профиль')}\n"
        f"{bullet('Имя: ' + display_name(row))}\n"
        f"{bullet('Username: ' + display_username(row))}\n"
        f"{bullet('Telegram ID: ' + str(row['user_id']))}\n\n"
        f"{section('Статистика')}\n"
        f"{bullet('Сообщений: ' + str(row['messages_count']))}\n"
        f"{bullet('Жалоб отправлено: ' + str(row['reports_count']))}\n"
        f"{bullet('Статус: ' + status)}\n\n"
        f"{section('Активность')}\n"
        f"{bullet('Первый запуск: ' + format_utc(row['first_seen']))}\n"
        f"{bullet('Последняя активность: ' + format_utc(row['last_seen']))}"
    )

    await edit_callback_screen(
        callback,
        state,
        text,
        user_admin_kb(user_id, bool(row["blocked"])),
    )
    await callback_answer(callback)


# =========================================================
# ADMIN BLOCKING
# =========================================================


async def handle_admin_block(
    callback: CallbackQuery,
    state: FSMContext,
    user_id: int,
    report_id: Optional[int] = None,
):
    if user_id == ADMIN_ID:
        await callback_answer(callback, "Нельзя заблокировать администратора.", True)
        return

    row = await get_user(user_id)
    if not row:
        await callback_answer(callback, "Пользователь не найден.", True)
        return

    await set_blocked(user_id, True)

    await callback_answer(callback, "Пользователь заблокирован.", True)

    if report_id is not None:
        report = await get_report(report_id)
        if report:
            await state.clear()
            reporter = await get_user(report["reporter_id"])
            target_id = report["target_user_id"]
            status = "новая" if report["status"] == "new" else "закрыта"
            text = (
                f"{title(f'Жалоба #{report_id}')}\n\n"
                f"{section('Статус')}\n"
                f"{bullet(status)}\n\n"
                f"{section('Пользователь')}\n"
                f"{bullet(report['target_text'])}\n"
                f"{bullet('ID: ' + str(user_id))}\n"
                f"{bullet('Статус пользователя: заблокирован')}\n\n"
                f"{section('Причина')}\n"
                f"{report['reason']}\n\n"
                f"{divider()}\n"
                f"{section('Заявитель')}\n"
                f"{bullet('Имя: ' + display_name(reporter))}\n"
                f"{bullet('Username: ' + display_username(reporter))}\n"
                f"{bullet('ID: ' + str(report['reporter_id']))}"
            )
            await edit_callback_screen(
                callback,
                state,
                text,
                report_admin_kb(report_id, target_id),
            )


@dp.callback_query(F.data.startswith("a:toggle:"))
async def admin_toggle(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    try:
        user_id = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        await callback_answer(callback, "Некорректный ID.", True)
        return

    if user_id == ADMIN_ID:
        await callback_answer(callback, "Нельзя заблокировать администратора.", True)
        return

    row = await get_user(user_id)
    if not row:
        await callback_answer(callback, "Пользователь не найден.", True)
        return

    new_value = not bool(row["blocked"])
    if not await set_blocked(user_id, new_value):
        await callback_answer(callback, "Не удалось изменить статус.", True)
        return

    row = await get_user(user_id)
    status = "заблокирован" if row["blocked"] else "активен"

    await edit_callback_screen(
        callback,
        state,
        (
            f"{title('Пользователь')}\n\n"
            f"{section('Профиль')}\n"
            f"{bullet('Имя: ' + display_name(row))}\n"
            f"{bullet('Username: ' + display_username(row))}\n"
            f"{bullet('ID: ' + str(row['user_id']))}\n\n"
            f"{section('Статус')}\n"
            f"{bullet(status)}"
        ),
        user_admin_kb(user_id, bool(row["blocked"])),
    )

    await callback_answer(
        callback,
        "Пользователь заблокирован." if new_value else "Пользователь разблокирован.",
        True,
    )


@dp.callback_query(F.data.startswith("a:block:"))
async def admin_block(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    parts = callback.data.split(":")
    try:
        user_id = int(parts[2])
    except (ValueError, IndexError):
        await callback_answer(callback, "Некорректный ID.", True)
        return

    report_id = None
    if len(parts) >= 4 and parts[3].startswith("r"):
        with suppress(ValueError):
            report_id = int(parts[3][1:])

    await handle_admin_block(callback, state, user_id, report_id)


# =========================================================
# ADMIN BLOCKED USERS
# =========================================================


@dp.callback_query(F.data == "a:blocked")
async def admin_blocked(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    await state.clear()
    rows = await blocked_users(50)

    text = f"{title('Заблокированные')}\n\n"
    if not rows:
        text += note("Список пуст.")
    else:
        for row in rows:
            text += (
                f"⊘ {display_name(row)}\n"
                f"{bullet(display_username(row))}\n"
                f"{bullet(str(row['user_id']))}\n\n"
            )
        text += f"{divider()}\n{note('Показаны последние 50 заблокированных пользователей.')}"

    await edit_callback_screen(callback, state, text, back_admin())
    await callback_answer(callback)


# =========================================================
# ADMIN REPORTS
# =========================================================


@dp.callback_query(F.data == "a:reports")
async def admin_reports(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    await state.clear()
    rows = await recent_reports(20)

    if not rows:
        await edit_callback_screen(
            callback,
            state,
            f"{title('Жалобы')}\n\n{note('Новых жалоб нет.')}",
            back_admin(),
        )
        await callback_answer(callback)
        return

    text = f"{title('Новые жалобы')}\n\n"
    buttons = []

    for row in rows:
        text += (
            f"⚠ #{row['id']} {row['target_text']}\n"
            f"{bullet(row['reason'][:100])}\n\n"
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"Открыть жалобу #{row['id']}",
                    callback_data=f"a:report:{row['id']}",
                )
            ]
        )

    buttons.append(
        [InlineKeyboardButton(text="‹ Назад", callback_data="a:home")]
    )

    await edit_callback_screen(
        callback,
        state,
        text,
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback_answer(callback)


@dp.callback_query(F.data.startswith("a:report:"))
async def admin_report(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    await state.clear()

    try:
        report_id = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        await callback_answer(callback, "Некорректный номер жалобы.", True)
        return

    report = await get_report(report_id)
    if not report:
        await callback_answer(callback, "Жалоба не найдена.", True)
        return

    reporter = await get_user(report["reporter_id"])
    target_id = report["target_user_id"]
    status = "новая" if report["status"] == "new" else "закрыта"

    text = (
        f"{title(f'Жалоба #{report_id}')}\n\n"
        f"{section('Статус')}\n"
        f"{bullet(status)}\n\n"
        f"{section('Пользователь')}\n"
        f"{bullet(report['target_text'])}\n"
        f"{bullet('ID: ' + (str(target_id) if target_id is not None else 'не указан'))}\n\n"
        f"{section('Причина')}\n"
        f"{report['reason']}\n\n"
        f"{divider()}\n"
        f"{section('Заявитель')}\n"
        f"{bullet('Имя: ' + display_name(reporter))}\n"
        f"{bullet('Username: ' + display_username(reporter))}\n"
        f"{bullet('ID: ' + str(report['reporter_id']))}"
    )

    await edit_callback_screen(
        callback,
        state,
        text,
        report_admin_kb(report_id, target_id),
    )
    await callback_answer(callback)


@dp.callback_query(F.data.startswith("a:close_report:"))
async def admin_close_report(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    try:
        report_id = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        await callback_answer(callback, "Некорректный номер.", True)
        return

    report = await get_report(report_id)
    if not report:
        await callback_answer(callback, "Жалоба не найдена.", True)
        return

    if report["status"] == "closed":
        await callback_answer(callback, "Жалоба уже закрыта.", True)
        return

    changed = await close_report(report_id)
    if not changed:
        await callback_answer(callback, "Жалоба уже закрыта.", True)
        return

    await state.clear()
    await edit_callback_screen(
        callback,
        state,
        (
            f"{title('Жалоба закрыта')}\n\n"
            f"{bullet('Номер: #' + str(report_id))}\n\n"
            f"{divider()}\n"
            "♡ Обращение отмечено как рассмотренное."
        ),
        back_admin(),
    )
    await callback_answer(callback, "Жалоба закрыта.", True)


# =========================================================
# ADMIN REPLY
# =========================================================


async def start_admin_reply(callback: CallbackQuery, state: FSMContext, user_id: int):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    row = await get_user(user_id)
    if not row:
        await callback_answer(callback, "Пользователь не найден.", True)
        return

    await state.set_state(AdminReplyState.waiting)
    await state.update_data(reply_to=user_id)

    sent = await callback.message.answer(
        (
            f"{title('Ответ пользователю')}\n\n"
            f"{bullet('Напишите текст ответа.')}\n"
            f"{bullet('Максимум — 4000 символов.')}"
        ),
        reply_markup=admin_cancel_kb(),
    )
    await save_screen_message(state, sent)
    await callback_answer(callback)


@dp.callback_query(F.data.startswith("a:replyuser:"))
async def admin_reply_user_start(callback: CallbackQuery, state: FSMContext):
    try:
        user_id = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        await callback_answer(callback, "Некорректный пользователь.", True)
        return
    await start_admin_reply(callback, state, user_id)


# Compatibility with the old reply:<user_id> callback.
@dp.callback_query(F.data.startswith("reply:"))
async def admin_reply_legacy_start(callback: CallbackQuery, state: FSMContext):
    try:
        user_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback_answer(callback, "Некорректный пользователь.", True)
        return
    await start_admin_reply(callback, state, user_id)


@dp.message(AdminReplyState.waiting, F.text)
async def admin_reply(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    user_id = data.get("reply_to")
    screen_message_id, _ = await get_screen(state)

    text = message.text.strip()

    if not user_id:
        await delete_user_message(message)
        await state.clear()
        await message.answer(
            f"{title('Ошибка')}\n\n{bullet('Получатель не найден.')}",
            reply_markup=admin_kb(),
        )
        return

    if not text:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            message.chat.id,
            f"{title('Ответ пользователю')}\n\n{bullet('Ответ не может быть пустым.')}",
            admin_cancel_kb(),
        )
        return

    if len(text) > MAX_MESSAGE_LENGTH:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            message.chat.id,
            (
                f"{title('Ответ пользователю')}\n\n"
                f"{bullet('Ответ слишком длинный.')}\n"
                f"{bullet('Максимум — 4000 символов.')}"
            ),
            admin_cancel_kb(),
        )
        return

    await delete_user_message(message)

    try:
        await send_admin_reply_to_user(user_id, text)
        await set_telegram_blocked(user_id, False)
    except (TelegramForbiddenError, TelegramNotFound) as exc:
        await set_telegram_blocked(user_id, True)
        logger.info(
            "User unavailable for admin reply | user_id=%s | error=%s",
            user_id,
            type(exc).__name__,
        )
        await state.clear()
        result_text = (
            f"{title('Ошибка доставки')}\n\n"
            f"{bullet('Пользователь недоступен для сообщений.')}"
        )
        if not screen_message_id or not await edit_message(
            message.chat.id, screen_message_id, result_text, admin_kb()
        ):
            await message.answer(result_text, reply_markup=admin_kb())
        return
    except TelegramUnauthorizedError:
        logger.critical("BOT_TOKEN is invalid while sending admin reply | user_id=%s", user_id)
        await state.clear()
        result_text = f"{title('Ошибка')}\n\n{bullet('Сервис временно недоступен.')}"
        if not screen_message_id or not await edit_message(
            message.chat.id, screen_message_id, result_text, admin_kb()
        ):
            await message.answer(result_text, reply_markup=admin_kb())
        return
    except TelegramRetryAfter as exc:
        logger.warning(
            "Unexpected unhandled retry after in admin reply | user_id=%s | retry_after=%s",
            user_id,
            exc.retry_after,
        )
        await state.clear()
        result_text = (
            f"{title('Слишком часто')}\n\n"
            f"{bullet('Telegram временно ограничил отправку. Попробуйте чуть позже.')}"
        )
        if not screen_message_id or not await edit_message(
            message.chat.id, screen_message_id, result_text, admin_kb()
        ):
            await message.answer(result_text, reply_markup=admin_kb())
        return
    except Exception:
        logger.exception(
            "Failed to send admin reply | user_id=%s",
            user_id,
        )
        await state.clear()
        result_text = f"{title('Ошибка доставки')}\n\n{bullet('Не удалось доставить ответ.')}"
        if not screen_message_id or not await edit_message(
            message.chat.id, screen_message_id, result_text, admin_kb()
        ):
            await message.answer(result_text, reply_markup=admin_kb())
        return

    await state.clear()
    result_text = (
        f"{title('Ответ отправлен')}\n\n"
        f"{bullet('Сообщение доставлено пользователю.')}"
    )

    if not screen_message_id or not await edit_message(
        message.chat.id, screen_message_id, result_text, admin_kb()
    ):
        await message.answer(result_text, reply_markup=admin_kb())


@dp.message(AdminReplyState.waiting)
async def admin_reply_non_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await delete_user_message(message)
    await edit_state_screen(
        state,
        message.chat.id,
        f"{title('Ответ пользователю')}\n\n{bullet('Пожалуйста, отправьте ответ текстом.')}",
        admin_cancel_kb(),
    )


# =========================================================
# BROADCAST
# =========================================================


def _broadcast_users(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT user_id
        FROM users
        WHERE blocked = 0
          AND telegram_blocked = 0
        ORDER BY user_id
        """
    ).fetchall()


async def broadcast_users():
    return await db_call(_broadcast_users)


async def run_broadcast(text: str):
    rows = await broadcast_users()
    total = len(rows)
    sent_count = 0
    failed_count = 0
    unavailable_count = 0
    retry_count = 0

    for index, row in enumerate(rows, start=1):
        user_id = row["user_id"]
        delivered = False

        for attempt in range(BROADCAST_MAX_RETRIES):
            try:
                await send_broadcast_to_user(user_id, text)
                await set_telegram_blocked(user_id, False)
                sent_count += 1
                delivered = True
                break
            except TelegramRetryAfter as exc:
                retry_count += 1
                wait = max(1, int(exc.retry_after))
                logger.warning(
                    "Broadcast rate limit | user_id=%s | retry_after=%s",
                    user_id,
                    wait,
                )
                if attempt >= BROADCAST_MAX_RETRIES - 1:
                    break
                await asyncio.sleep(wait)
            except (TelegramForbiddenError, TelegramNotFound):
                unavailable_count += 1
                await set_telegram_blocked(user_id, True)
                logger.info(
                    "Broadcast user unavailable | user_id=%s",
                    user_id,
                )
                break
            except TelegramUnauthorizedError:
                logger.critical("BOT_TOKEN rejected during broadcast.")
                raise
            except (TelegramNetworkError, TelegramServerError) as exc:
                logger.warning(
                    "Temporary broadcast error | user_id=%s | attempt=%s/%s | %s",
                    user_id,
                    attempt + 1,
                    BROADCAST_MAX_RETRIES,
                    exc,
                )
                if attempt >= BROADCAST_MAX_RETRIES - 1:
                    break
                await asyncio.sleep(2**attempt)
            except Exception:
                logger.exception(
                    "Broadcast failed | user_id=%s | attempt=%s/%s",
                    user_id,
                    attempt + 1,
                    BROADCAST_MAX_RETRIES,
                )
                break

        if not delivered:
            failed_count += 1

        if BROADCAST_DELAY_SECONDS > 0:
            await asyncio.sleep(BROADCAST_DELAY_SECONDS)

        if index % 100 == 0:
            logger.info(
                "Broadcast progress | processed=%s/%s | sent=%s | failed=%s | unavailable=%s",
                index,
                total,
                sent_count,
                failed_count,
                unavailable_count,
            )

    return {
        "total": total,
        "sent": sent_count,
        "failed": failed_count,
        "unavailable": unavailable_count,
        "retries": retry_count,
    }


@dp.callback_query(F.data == "a:broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback_answer(callback, "Нет доступа.", True)
        return

    await state.set_state(BroadcastState.waiting)
    await edit_callback_screen(
        callback,
        state,
        (
            f"{title('Рассылка')}\n\n"
            f"{bullet('Напишите текст для зарегистрированных пользователей.')}\n"
            f"{bullet('Заблокированные пользователи рассылку не получают.')}\n"
            f"{bullet('Пользователи, заблокировавшие бота, также будут пропущены.')}\n\n"
            f"{divider()}\n"
            "♡ Используйте рассылку для важных объявлений."
        ),
        admin_cancel_kb(),
    )
    await callback_answer(callback)


@dp.message(BroadcastState.waiting, F.text)
async def admin_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    text = message.text.strip()
    screen_message_id, _ = await get_screen(state)

    if not text:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            message.chat.id,
            f"{title('Рассылка')}\n\n{bullet('Текст не может быть пустым.')}",
            admin_cancel_kb(),
        )
        return

    if len(text) > MAX_MESSAGE_LENGTH:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            message.chat.id,
            (
                f"{title('Рассылка')}\n\n"
                f"{bullet('Текст слишком длинный.')}\n"
                f"{bullet('Максимум — 4000 символов.')}"
            ),
            admin_cancel_kb(),
        )
        return

    await delete_user_message(message)
    await state.clear()

    running_text = (
        f"{title('Рассылка запущена')}\n\n"
        f"{bullet('Бот отправляет сообщения с учётом ограничений Telegram.')}\n\n"
        f"{divider()}\n"
        "♡ Результат будет показан после завершения."
    )

    if not screen_message_id or not await edit_message(
        message.chat.id,
        screen_message_id,
        running_text,
        admin_kb(),
    ):
        await message.answer(running_text, reply_markup=admin_kb())

    try:
        result = await run_broadcast(text)
    except TelegramUnauthorizedError:
        logger.critical("Broadcast stopped because BOT_TOKEN was rejected.")
        await message.answer(
            f"{title('Рассылка остановлена')}\n\n{bullet('Telegram отклонил токен бота.')}",
            reply_markup=admin_kb(),
        )
        return

    await message.answer(
        (
            f"{title('Рассылка завершена')}\n\n"
            f"{bullet('Всего получателей: ' + str(result['total']))}\n"
            f"{bullet('Отправлено: ' + str(result['sent']))}\n"
            f"{bullet('Не доставлено: ' + str(result['failed']))}\n"
            f"{bullet('Недоступно: ' + str(result['unavailable']))}\n"
            f"{bullet('Повторов из-за лимитов: ' + str(result['retries']))}\n\n"
            f"{divider()}\n"
            "♡ Рассылка завершена."
        ),
        reply_markup=admin_kb(),
    )


@dp.message(BroadcastState.waiting)
async def admin_broadcast_non_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await delete_user_message(message)
    await edit_state_screen(
        state,
        message.chat.id,
        f"{title('Рассылка')}\n\n{bullet('Пожалуйста, отправьте текст рассылки.')}",
        admin_cancel_kb(),
    )


# =========================================================
# COMMANDS
# =========================================================


async def setup_commands():
    user_commands = [
        BotCommand(command="start", description="Открыть меню"),
        BotCommand(command="cancel", description="Отменить действие"),
    ]

    admin_commands = [
        BotCommand(command="start", description="Открыть меню"),
        BotCommand(command="cancel", description="Отменить действие"),
        BotCommand(command="admin", description="Панель администратора"),
    ]

    await bot.set_my_commands(
        user_commands,
        scope=BotCommandScopeDefault(),
    )

    await bot.set_my_commands(
        admin_commands,
        scope=BotCommandScopeChat(chat_id=ADMIN_ID),
    )

    logger.info("Commands configured.")


# =========================================================
# HTTP / HEALTH
# =========================================================


async def health(request: web.Request):
    return web.Response(text="OK")


async def start_http_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info("HTTP server started on port %s", PORT)
    return runner


# =========================================================
# STARTUP / POLLING
# =========================================================


async def validate_bot():
    me = await bot.get_me()
    logger.info(
        "Telegram bot connected | id=%s | username=@%s",
        me.id,
        me.username or "unknown",
    )


async def remove_old_webhook():
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("Old Telegram webhook removed.")
    except TelegramUnauthorizedError:
        logger.critical(
            "BOT_TOKEN is invalid. Telegram rejected the bot token while removing webhook."
        )
        raise
    except Exception:
        logger.exception("Could not remove old webhook")
        raise


async def polling_loop():
    logger.info("Starting polling...")
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
        handle_signals=True,
    )


# =========================================================
# MAIN
# =========================================================


async def main():
    logger.info("Starting Anonymous Feedback Bot...")

    await init_db()
    await validate_bot()
    await remove_old_webhook()

    try:
        await setup_commands()
    except TelegramUnauthorizedError:
        logger.critical("BOT_TOKEN is invalid while configuring commands.")
        raise
    except Exception:
        logger.exception("Could not configure commands")

    http_runner = await start_http_server()

    try:
        await polling_loop()
    except TelegramUnauthorizedError:
        logger.critical("Polling stopped: BOT_TOKEN is invalid.")
        raise
    except TelegramConflictError:
        logger.critical(
            "Polling stopped: another instance is using the same BOT_TOKEN."
        )
        raise
    except asyncio.CancelledError:
        logger.info("Polling task cancelled.")
        raise
    finally:
        logger.info("Stopping bot...")

        with suppress(Exception):
            await dp.stop_polling()

        try:
            await http_runner.cleanup()
        except Exception:
            logger.exception("HTTP cleanup error")

        try:
            await bot.session.close()
        except Exception:
            logger.exception("Bot session cleanup error")


# =========================================================
# ENTRY POINT
# =========================================================


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")

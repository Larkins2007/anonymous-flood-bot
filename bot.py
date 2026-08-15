import asyncio
import logging
import os
import re
import sqlite3
import threading
import sys
import traceback
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional
from logging.handlers import RotatingFileHandler

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

# Conservative pacing for broadcast (raised to reduce rate-limit issues).
BROADCAST_DELAY_SECONDS = float(os.getenv("BROADCAST_DELAY_SECONDS", "0.3"))
BROADCAST_MAX_RETRIES = int(os.getenv("BROADCAST_MAX_RETRIES", "3"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")


# =========================================================
# LOGGING
# =========================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "bot.log")

# Configure root logger with both console and rotating file handlers.
_root_logger = logging.getLogger()
_root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
_formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(_formatter)
_root_logger.addHandler(_console)

try:
    _file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5)
    _file_handler.setFormatter(_formatter)
    _root_logger.addHandler(_file_handler)
except Exception:
    _root_logger.exception("Could not create rotating file handler; continuing without file logging")

logger = logging.getLogger("anonymous-feedback-bot")


# Global uncaught exception hook
def excepthook(exc_type, exc, exc_tb):
    try:
        logger.critical("Uncaught exception", exc_info=(exc_type, exc, exc_tb))
    except Exception:
        traceback.print_exception(exc_type, exc, exc_tb)

sys.excepthook = excepthook

# asyncio event loop exception handler
def aio_exc_handler(loop, context):
    try:
        logger.critical("Unhandled exception in event loop: %s", context)
        exc = context.get("exception")
        if exc:
            logger.critical("Event loop exception detail:", exc_info=exc)
    except Exception:
        traceback.print_exc()


# =========================================================
# BOT
# =========================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# SQLite connections are short-lived, but DB writes/PRAGMAs are serialized.
DB_LOCK = threading.RLock()


# Register a global aiogram errors handler if possible
try:
    @dp.errors.register()
    async def global_error_handler(update, exception):
        try:
            logger.exception("aiogram handler error: %s — update: %s", exception, update)
        except Exception:
            logger.exception("aiogram handler error (unable to log update)")
        return True
except Exception:
    logger.debug("Could not register dp.errors handler; aiogram version mismatch possible")


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

# (keyboard functions unchanged)


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

# ... rest of file unchanged


# Minimal additions for Variant B: ensure setup_commands and start_http_server exist
async def setup_commands():
    try:
        user_commands = [
            BotCommand(command="start", description="Открыть меню"),
            BotCommand(command="cancel", description="Отменить действие"),
        ]
        await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

        admin_commands = [
            BotCommand(command="start", description="Открыть меню"),
            BotCommand(command="admin", description="Панель администратора"),
        ]
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))

        logger.info("Commands configured.")
    except Exception:
        logger.exception("Could not configure commands")
        raise


async def start_http_server():
    app = web.Application()

    async def health(request):
        return web.Response(text="OK")

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

    # Reinstall asyncio exception handler on startup (ensures it's active on the running loop).
    try:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(aio_exc_handler)
    except Exception:
        logger.debug("Could not set asyncio exception handler in main")

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

    # Resilient polling loop with exponential backoff for transient errors.
    backoff = 1
    try:
        while True:
            try:
                await polling_loop()
                # start_polling returns when polling stops (clean shutdown). Exit loop.
                break
            except (TelegramUnauthorizedError, TelegramConflictError):
                logger.critical("Polling stopped due to unrecoverable Telegram error. Exiting.")
                raise
            except asyncio.CancelledError:
                logger.info("Polling cancelled.")
                raise
            except Exception:
                logger.exception("Polling crashed; will restart after %s seconds", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
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

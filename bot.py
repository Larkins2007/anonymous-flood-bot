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


async def set_telegr... (truncated)
import asyncio
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import threading
import traceback
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
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
    ChatMemberUpdated,
    ChatPermissions,
    ErrorEvent,
    InlineKeyboardButton,
    MessageEntity,
    InlineKeyboardMarkup,
    Message,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_ID = 1682289834

DB_PATH = os.getenv("DB_PATH", "users.db").strip() or "users.db"
PORT = int(os.getenv("PORT", "10000"))

REPORT_COOLDOWN_SECONDS = 600
MAX_MESSAGE_LENGTH = 4000
MAX_REPORT_REASON_LENGTH = 2000
TELEGRAM_TEXT_LIMIT = 3500
BROADCAST_DELAY_SECONDS = float(os.getenv("BROADCAST_DELAY_SECONDS", "0.20"))
BROADCAST_MAX_RETRIES = int(os.getenv("BROADCAST_MAX_RETRIES", "3"))


# =========================================================
# FLOOD / GROUP MODERATION CONFIG
# =========================================================

ADMIN_MENTION = os.getenv("ADMIN_MENTION", "@Belochki_Rulyat")
GROUP_WELCOME_TIMEOUT = int(os.getenv("GROUP_WELCOME_TIMEOUT", "0"))
DEFAULT_OCCUPIED_MARKER = os.getenv("DEFAULT_OCCUPIED_MARKER", "💛")
ROLE_ASSIGNMENT_WINDOW_SECONDS = int(os.getenv("ROLE_ASSIGNMENT_WINDOW_SECONDS", "600"))



if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")


# =========================================================
# LOGGING
# =========================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "bot.log")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger("anonymous-feedback-bot")

try:
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
        logger.addHandler(file_handler)
except Exception:
    logger.exception("Could not create file logger; continuing with console logging")


def _uncaught_exception(exc_type, exc, exc_tb):
    logger.critical(
        "Uncaught exception",
        exc_info=(exc_type, exc, exc_tb),
    )

sys.excepthook = _uncaught_exception

# =========================================================
# BOT
# =========================================================

bot = Bot(token=BOT_TOKEN)

dp = Dispatcher(
    storage=MemoryStorage()
)

DB_LOCK = threading.RLock()

DELIVERY_QUEUE = None
BROADCAST_QUEUE = None
DELIVERY_WORKERS = max(1, int(os.getenv("DELIVERY_WORKERS", "2")))
BROADCAST_WORKERS = max(1, int(os.getenv("BROADCAST_WORKERS", "1")))
DELIVERY_PART_DELAY = max(0.0, float(os.getenv("DELIVERY_PART_DELAY", "0.2")))

polling_running = False
last_polling_activity = None
fatal_error = None
FATAL_EVENT = None
NOTIFICATION_LOCKS = {}

@dp.errors()
async def global_error_handler(event: ErrorEvent):
    exc = event.exception
    logger.error(
        "Unhandled aiogram handler error | update_id=%s | exception=%s",
        event.update.update_id,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    if isinstance(exc, (TelegramUnauthorizedError, TelegramConflictError)):
        global fatal_error
        fatal_error = "unauthorized" if isinstance(exc, TelegramUnauthorizedError) else "conflict"
        if FATAL_EVENT is not None:
            FATAL_EVENT.set()
        return False
    return True


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
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def now():
    return datetime.now(timezone.utc).isoformat()


def db_transaction(callback, *args):
    with DB_LOCK:
        conn = db()
        try:
            return callback(conn, *args)
        finally:
            conn.close()


def _table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def normalize_target_text(value):
    return (value or "").strip().lstrip("@").strip().lower()


def target_key(target_text, target_user_id):
    if target_user_id is not None:
        return f"user:{target_user_id}"
    return f"text:{normalize_target_text(target_text)}"


def init_db():
    def create(conn):
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

            CREATE TABLE IF NOT EXISTS sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                ref_id INTEGER,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                admin_message_ids TEXT NOT NULL DEFAULT '[]',
                markup_type TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_admin_notifications_status
                ON admin_notifications(status);
            CREATE INDEX IF NOT EXISTS idx_admin_notifications_kind_ref
                ON admin_notifications(kind, ref_id);

            CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen);
            CREATE INDEX IF NOT EXISTS idx_users_blocked ON users(blocked);
            CREATE INDEX IF NOT EXISTS idx_users_telegram_blocked ON users(telegram_blocked);
            CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
            CREATE INDEX IF NOT EXISTS idx_reports_reporter ON reports(reporter_id);
            CREATE INDEX IF NOT EXISTS idx_reports_target_user ON reports(target_user_id);
            """
        )
        conn.commit()

    db_transaction(create)


def migrate_db():
    def migrate(conn):
        user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        report_cols = {row["name"] for row in conn.execute("PRAGMA table_info(reports)").fetchall()}

        if "reports_count" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN reports_count INTEGER NOT NULL DEFAULT 0")
        if "blocked" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        if "telegram_blocked" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN telegram_blocked INTEGER NOT NULL DEFAULT 0")

        if "target_key" not in report_cols:
            conn.execute("ALTER TABLE reports ADD COLUMN target_key TEXT NOT NULL DEFAULT ''")
            rows = conn.execute(
                "SELECT id, target_text, target_user_id FROM reports"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE reports SET target_key=? WHERE id=?",
                    (target_key(row["target_text"], row["target_user_id"]), row["id"]),
                )

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
                WHERE reporter_id=? AND target_key=? AND id!=?
                """ ,
                (row["reporter_id"], row["target_key"], row["keep_id"]),
            )

        if "delivery_status" not in report_cols:
            conn.execute(
                "ALTER TABLE reports ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'pending'"
            )
        if "admin_message_ids" not in report_cols:
            conn.execute(
                "ALTER TABLE reports ADD COLUMN admin_message_ids TEXT NOT NULL DEFAULT '[]'"
            )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                ref_id INTEGER,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                admin_message_ids TEXT NOT NULL DEFAULT '[]',
                markup_type TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_notifications_status ON admin_notifications(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_notifications_kind_ref ON admin_notifications(kind, ref_id)"
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_reporter_target_key
            ON reports(reporter_id, target_key)
            """
        )
        conn.commit()

    db_transaction(migrate)



def init_group_db():
    """Create/migrate the group-moderation tables without touching legacy data."""
    def op(conn):
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS managed_group (
                group_chat_id INTEGER PRIMARY KEY,
                info_channel_id INTEGER,
                bound_at TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS group_members (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                role_key TEXT,
                role_name TEXT,
                tag TEXT,
                confirmed INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                joined_at TEXT NOT NULL,
                confirmed_at TEXT,
                left_at TEXT,
                welcome_message_id INTEGER,
                tag_set_by_bot INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_group_members_active_role
                ON group_members(chat_id, active, role_key);
            CREATE INDEX IF NOT EXISTS idx_group_members_username
                ON group_members(chat_id, username);
            CREATE INDEX IF NOT EXISTS idx_group_members_joined
                ON group_members(chat_id, joined_at);

            CREATE TABLE IF NOT EXISTS role_state (
                chat_id INTEGER NOT NULL,
                role_key TEXT NOT NULL,
                role_name TEXT NOT NULL,
                user_id INTEGER,
                legacy_marker TEXT NOT NULL DEFAULT '',
                legacy_custom_emoji_id TEXT,
                bot_managed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, role_key)
            );

            CREATE INDEX IF NOT EXISTS idx_role_state_user
                ON role_state(chat_id, user_id);

            CREATE TABLE IF NOT EXISTS roster_sources (
                chat_id INTEGER NOT NULL,
                slot TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                source_text TEXT NOT NULL DEFAULT '',
                source_custom_emoji TEXT NOT NULL DEFAULT '{}',
                captured_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, slot)
            );
            """
        )
        conn.commit()
    db_transaction(op)


def register_user(user):
    def op(conn):
        stamp = now()
        conn.execute(
            """
            INSERT INTO users (
                user_id, first_name, last_name, username,
                first_seen, last_seen, telegram_blocked
            )
            VALUES (?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                username=excluded.username,
                last_seen=excluded.last_seen,
                telegram_blocked=0
            """,
            (
                user.id,
                user.first_name or "",
                user.last_name or "",
                user.username or "",
                stamp,
                stamp,
            ),
        )
        conn.commit()
    db_transaction(op)


def get_user(user_id):
    def op(conn):
        return conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return db_transaction(op)


def get_user_by_username(username):
    def op(conn):
        value = normalize_target_text(username)
        if not value:
            return None
        return conn.execute(
            """
            SELECT * FROM users
            WHERE lower(ltrim(username, '@'))=?
            ORDER BY last_seen DESC
            LIMIT 1
            """,
            (value,),
        ).fetchone()
    return db_transaction(op)


def is_blocked(user_id):
    row = get_user(user_id)
    return bool(row and row["blocked"])


def set_blocked(user_id, value):
    def op(conn):
        cur = conn.execute(
            "UPDATE users SET blocked=? WHERE user_id=?",
            (1 if value else 0, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    return db_transaction(op)


def set_telegram_blocked(user_id, value):
    def op(conn):
        conn.execute(
            "UPDATE users SET telegram_blocked=? WHERE user_id=?",
            (1 if value else 0, user_id),
        )
        conn.commit()
    db_transaction(op)


def increment_messages(user_id):
    def op(conn):
        conn.execute(
            "UPDATE users SET messages_count=messages_count+1 WHERE user_id=?",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO sent_messages(user_id, sent_at) VALUES (?, ?)"
            , (user_id, now()),
        )
        conn.commit()
    db_transaction(op)


def last_report_timestamp(user_id):
    def op(conn):
        row = conn.execute(
            """
            SELECT created_at FROM reports
            WHERE reporter_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return row["created_at"] if row else None
    return db_transaction(op)


def report_cooldown_left(user_id):
    stamp = last_report_timestamp(user_id)
    if not stamp:
        return 0
    try:
        elapsed = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(stamp)
        ).total_seconds()
    except (ValueError, TypeError):
        return 0
    return max(
        0,
        int(REPORT_COOLDOWN_SECONDS - elapsed),
    )


def increment_reports(user_id):
    def op(conn):
        conn.execute(
            "UPDATE users SET reports_count=reports_count+1 WHERE user_id=?",
            (user_id,),
        )
        conn.commit()
    db_transaction(op)


def has_reported_target(reporter_id, target_user_id=None, target_text=None):
    key = target_key(target_text or "", target_user_id)
    def op(conn):
        row = conn.execute(
            "SELECT 1 FROM reports WHERE reporter_id=? AND target_key=? LIMIT 1",
            (reporter_id, key),
        ).fetchone()
        return row is not None
    return db_transaction(op)


def create_report(reporter_id, target_text, target_user_id, reason):
    key = target_key(target_text, target_user_id)
    def op(conn):
        try:
            cur = conn.execute(
                """
                INSERT INTO reports(
                    reporter_id, target_text, target_user_id,
                    target_key, reason, created_at, status
                )
                VALUES (?, ?, ?, ?, ?, ?, 'new')
                """,
                (
                    reporter_id,
                    target_text,
                    target_user_id,
                    key,
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
    return db_transaction(op)


def delete_report(report_id):
    def op(conn):
        conn.execute("DELETE FROM reports WHERE id=?", (report_id,))
        conn.commit()
    db_transaction(op)


def _json_ids(value):
    try:
        data = json.loads(value or "[]")
        return [int(x) for x in data]
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def create_admin_notification(kind, ref_id, payload, markup_type=""):
    def op(conn):
        cur = conn.execute(
            """
            INSERT INTO admin_notifications(
                kind, ref_id, payload, created_at, status,
                admin_message_ids, markup_type
            )
            VALUES (?, ?, ?, ?, 'pending', '[]', ?)
            """,
            (kind, ref_id, payload, now(), markup_type),
        )
        conn.commit()
        return cur.lastrowid
    return db_transaction(op)


def create_report_and_notification(
    reporter_id,
    target_text,
    target_user_id,
    reason,
    admin_text_builder,
):
    key = target_key(target_text, target_user_id)

    def op(conn):
        try:
            cur = conn.execute(
                """
                INSERT INTO reports(
                    reporter_id, target_text, target_user_id,
                    target_key, reason, created_at, status,
                    delivery_status, admin_message_ids
                )
                VALUES (?, ?, ?, ?, ?, ?, 'new', 'pending', '[]')
                """,
                (reporter_id, target_text, target_user_id, key, reason, now()),
            )
            report_id = cur.lastrowid
            admin_text = admin_text_builder(report_id)
            ncur = conn.execute(
                """
                INSERT INTO admin_notifications(
                    kind, ref_id, payload, created_at, status,
                    admin_message_ids, markup_type
                )
                VALUES ('report', ?, ?, ?, 'pending', '[]', 'report')
                """,
                (report_id, admin_text, now()),
            )
            conn.execute(
                "UPDATE users SET reports_count=reports_count+1 WHERE user_id=?",
                (reporter_id,),
            )
            conn.commit()
            return report_id, ncur.lastrowid
        except Exception:
            conn.rollback()
            raise
    return db_transaction(op)


def create_feedback_notification(user_id, payload):
    def op(conn):
        try:
            conn.execute(
                "UPDATE users SET messages_count=messages_count+1 WHERE user_id=?",
                (user_id,),
            )
            conn.execute(
                "INSERT INTO sent_messages(user_id, sent_at) VALUES (?, ?)",
                (user_id, now()),
            )
            cur = conn.execute(
                """
                INSERT INTO admin_notifications(
                    kind, ref_id, payload, created_at, status,
                    admin_message_ids, markup_type
                )
                VALUES ('feedback', ?, ?, ?, 'pending', '[]', 'feedback')
                """,
                (user_id, payload, now()),
            )
            conn.commit()
            return cur.lastrowid
        except Exception:
            conn.rollback()
            raise
    return db_transaction(op)


def get_admin_notification(notification_id):
    return db_transaction(
        lambda conn: conn.execute(
            "SELECT * FROM admin_notifications WHERE id=?",
            (notification_id,),
        ).fetchone()
    )


def update_admin_notification(notification_id, status=None, admin_message_ids=None):
    def op(conn):
        sets, params = [], []
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if admin_message_ids is not None:
            sets.append("admin_message_ids=?")
            params.append(json.dumps(admin_message_ids, separators=(",", ":")))
        if not sets:
            return
        params.append(notification_id)
        conn.execute(
            "UPDATE admin_notifications SET " + ", ".join(sets) + " WHERE id=?",
            params,
        )
        conn.commit()
    db_transaction(op)


def set_report_delivery_status(report_id, status, admin_message_ids):
    def op(conn):
        conn.execute(
            """
            UPDATE reports
            SET delivery_status=?, admin_message_ids=?
            WHERE id=?
            """,
            (status, json.dumps(admin_message_ids, separators=(",", ":")), report_id),
        )
        conn.commit()
    db_transaction(op)


def get_pending_admin_notifications(limit=1000):
    return db_transaction(
        lambda conn: conn.execute(
            """
            SELECT * FROM admin_notifications
            WHERE status IN ('pending', 'partial')
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    )


def get_report(report_id):
    def op(conn):
        return conn.execute(
            "SELECT * FROM reports WHERE id=?",
            (report_id,),
        ).fetchone()
    return db_transaction(op)


def close_report(report_id):
    def op(conn):
        cur = conn.execute(
            """
            UPDATE reports SET status='closed'
            WHERE id=? AND status='new'
            """,
            (report_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    return db_transaction(op)


def user_count():
    def op(conn):
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return db_transaction(op)


def blocked_count():
    def op(conn):
        return conn.execute("SELECT COUNT(*) FROM users WHERE blocked=1").fetchone()[0]
    return db_transaction(op)


def message_count():
    def op(conn):
        return conn.execute("SELECT COUNT(*) FROM sent_messages").fetchone()[0]
    return db_transaction(op)


def new_reports_count():
    def op(conn):
        return conn.execute("SELECT COUNT(*) FROM reports WHERE status='new'").fetchone()[0]
    return db_transaction(op)


def list_users(limit=10, offset=0):
    def op(conn):
        return conn.execute(
            """
            SELECT * FROM users
            ORDER BY last_seen DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return db_transaction(op)


def search_users(query, limit=15):
    def op(conn):
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
    return db_transaction(op)


def recent_reports(limit=20):
    def op(conn):
        return conn.execute(
            """
            SELECT * FROM reports
            WHERE status='new'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return db_transaction(op)


def blocked_users(limit=50):
    def op(conn):
        return conn.execute(
            """
            SELECT * FROM users
            WHERE blocked=1
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return db_transaction(op)


def display_name(row):
    if not row:
        return "Без имени"
    value = " ".join(
        x for x in (row["first_name"], row["last_name"]) if x
    ).strip()
    return value or "Без имени"


def display_username(row):
    if not row or not row["username"]:
        return "нет username"
    username = row["username"]
    return username if username.startswith("@") else f"@{username}"

# =========================================================
# SCREEN HELPERS
# =========================================================

async def delete_user_message(message: Message):
    try:
        await message.delete()
    except (
        TelegramBadRequest,
        TelegramForbiddenError,
        TelegramNotFound,
    ):
        pass
    except Exception:
        logger.debug(
            "Could not delete user message | chat_id=%s | message_id=%s",
            message.chat.id,
            message.message_id,
            exc_info=True,
        )


async def edit_message_safe(
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup=None,
):
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
        return True
    except TelegramBadRequest as exc:
        lowered = str(exc).lower()
        if "message is not modified" in lowered:
            return True
        if (
            "message to edit not found" in lowered
            or "message can't be edited" in lowered
            or "message_id_invalid" in lowered
        ):
            return False
        logger.warning(
            "Could not edit screen | chat_id=%s | message_id=%s | %s",
            chat_id,
            message_id,
            exc,
        )
        return False
    except (
        TelegramForbiddenError,
        TelegramNotFound,
    ) as exc:
        logger.warning(
            "Could not edit screen | chat_id=%s | message_id=%s | %s",
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


async def edit_screen(
    callback: CallbackQuery,
    text: str,
    reply_markup=None,
):
    edited = await edit_message_safe(
        callback.message.chat.id,
        callback.message.message_id,
        text,
        reply_markup,
    )

    if edited:
        return callback.message

    try:
        sent = await callback.message.answer(
            text,
            reply_markup=reply_markup,
        )
        return sent
    except Exception:
        logger.exception(
            "Could not send fallback screen | user_id=%s",
            callback.from_user.id,
        )
        return None


async def edit_state_screen(
    state: FSMContext,
    bot_instance: Bot,
    chat_id: int,
    text: str,
    reply_markup=None,
):
    data = await state.get_data()
    screen_message_id = data.get("screen_message_id")
    screen_chat_id = data.get("screen_chat_id")

    if not screen_message_id or not screen_chat_id:
        return False

    try:
        await bot_instance.edit_message_text(
            chat_id=screen_chat_id,
            message_id=screen_message_id,
            text=text,
            reply_markup=reply_markup,
        )
        return True
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return True
        logger.warning(
            "Could not edit state screen | chat_id=%s | message_id=%s | %s",
            chat_id,
            screen_message_id,
            exc,
        )
        return False
    except Exception:
        logger.exception(
            "Could not edit state screen | chat_id=%s | message_id=%s",
            chat_id,
            screen_message_id,
        )
        return False


async def save_screen_message(
    state: FSMContext,
    message: Message,
):
    await state.update_data(
        screen_message_id=message.message_id,
        screen_chat_id=message.chat.id,
    )


async def send_screen(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup=None,
):
    sent = await message.answer(
        text,
        reply_markup=reply_markup,
    )
    await save_screen_message(state, sent)
    return sent


async def edit_callback_screen(
    callback: CallbackQuery,
    state: FSMContext,
    text: str,
    reply_markup=None,
):
    sent_or_edited = await edit_screen(
        callback,
        text,
        reply_markup,
    )

    if sent_or_edited is not None:
        await save_screen_message(
            state,
            sent_or_edited,
        )
        return True

    return False


async def safe_callback_answer(
    callback: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
):
    with suppress(Exception):
        await callback.answer(
            text,
            show_alert=show_alert,
        )


def touch_polling_activity():
    global last_polling_activity
    last_polling_activity = datetime.now(timezone.utc)

# =========================================================
# TELEGRAM MESSAGE HELPERS
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


async def send_with_retry(
    chat_id: int,
    text: str,
    reply_markup=None,
    max_retries: int = 3,
):
    delay = 1.0

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

        except (
            TelegramNetworkError,
            TelegramServerError,
        ) as exc:
            if attempt >= max_retries:
                raise

            logger.warning(
                "Temporary Telegram error | chat_id=%s | attempt=%s/%s | %s",
                chat_id,
                attempt + 1,
                max_retries,
                exc,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)

    raise RuntimeError("Telegram message send failed")


async def send_long_message(
    chat_id: int,
    text: str,
    reply_markup=None,
):
    messages = []
    parts = split_text(text)

    for index, part in enumerate(parts):
        markup = reply_markup if index == len(parts) - 1 else None
        messages.append(
            await send_with_retry(
                chat_id,
                part,
                markup,
            )
        )
        if index < len(parts) - 1 and DELIVERY_PART_DELAY:
            await asyncio.sleep(DELIVERY_PART_DELAY)

    return messages


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


def cancel_kb():
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


def user_admin_kb(
    user_id,
    blocked,
):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        "♡ Разблокировать"
                        if blocked
                        else "⊘ Заблокировать"
                    ),
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


def report_admin_kb(
    report_id,
    target_user_id=None,
):
    rows = []

    if target_user_id:

        rows.append(
            [
                InlineKeyboardButton(
                    text="⊘ Заблокировать пользователя",
                    callback_data=f"a:block:{target_user_id}",
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

    return InlineKeyboardMarkup(
        inline_keyboard=rows
    )


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


def admin_home_text():
    return (
        f"{title('Панель администратора')}\n\n"
        f"{section('Сводка')}\n"
        f"{bullet(f'Пользователей: {user_count()}')}\n"
        f"{bullet(f'Сообщений: {message_count()}')}\n"
        f"{bullet(f'Заблокировано: {blocked_count()}')}\n"
        f"{bullet(f'Новых жалоб: {new_reports_count()}')}\n\n"
        f"{divider()}\n"
        "♡ Выберите раздел"
    )


# =========================================================
# START
# =========================================================

@dp.message(CommandStart())
async def start(
    message: Message,
    state: FSMContext,
):
    touch_polling_activity()
    await state.clear()
    register_user(message.from_user)
    await delete_user_message(message)

    if is_blocked(message.from_user.id):
        await message.answer(
            f"{title('Доступ ограничен')}\n\n"
            f"{bullet('Для вашего аккаунта отправка сообщений отключена.')}"
        )
        return

    await send_screen(
        message,
        state,
        home_text(),
        main_kb(),
    )

# =========================================================
# HOME
# =========================================================

@dp.callback_query(F.data == "u:home")
async def user_home(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()
    await edit_callback_screen(
        callback,
        state,
        home_text(),
        main_kb(),
    )
    await safe_callback_answer(callback)


# =========================================================
# INFORMATION
# =========================================================

@dp.callback_query(F.data == "u:info")
async def user_info(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()
    await edit_callback_screen(
        callback,
        state,
        f"♡₊˚ Как это работает ˚₊♡\n\n"
        "Вы можете оставить обратную связь о флуде, "
        "поделиться своим мнением, предложением, советом "
        "или своими предпочтениями.\n\n"
        f"{divider()}\n"
        "♡ Напишите сообщение через бота.\n"
        "♡ Выберите нужное действие в меню.\n\n"
        f"{divider()}\n"
        "♡ Спасибо за вашу обратную связь.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="♡ О сообщениях", callback_data="u:send_info")],
                [InlineKeyboardButton(text="⚠ О жалобах", callback_data="u:report_info")],
                [InlineKeyboardButton(text="‹ Назад", callback_data="u:home")],
            ]
        ),
    )
    await safe_callback_answer(callback)


@dp.callback_query(F.data == "u:send_info")
async def send_info(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()
    await edit_callback_screen(
        callback,
        state,
        f"♡₊˚ О сообщениях ˚₊♡\n\n"
        "Здесь можно высказать своё мнение о флуде, "
        "предложить идею, поделиться советом "
        "или рассказать о своих предпочтениях.\n\n"
        f"{divider()}\n"
        "♡ Напишите сообщение.\n"
        "♡ Максимальная длина — 4000 символов.\n\n"
        "₊˚♡ Делитесь мыслями свободно. ♡˚₊",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="♡ Оставить сообщение", callback_data="u:send")],
                [InlineKeyboardButton(text="‹ Назад", callback_data="u:info")],
            ]
        ),
    )
    await safe_callback_answer(callback)


@dp.callback_query(F.data == "u:report_info")
async def report_info(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()
    await edit_callback_screen(
        callback,
        state,
        f"{title('О жалобах')}\n\n"
        f"{bullet('Укажите пользователя, на которого хотите пожаловаться.')}\n"
        f"{bullet('После этого укажите причину обращения.')}\n"
        f"{bullet('Перед отправкой можно проверить введённые данные.')}\n"
        f"{bullet('На одного пользователя можно пожаловаться один раз.')}\n"
        f"{bullet('Между жалобами действует пауза 10 минут.')}\n\n"
        f"{divider()}\n"
        "♡ Пожалуйста, указывайте достоверную информацию.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⚠ Подать жалобу", callback_data="u:report")],
                [InlineKeyboardButton(text="‹ Назад", callback_data="u:info")],
            ]
        ),
    )
    await safe_callback_answer(callback)

# =========================================================
# SEND
# =========================================================

@dp.callback_query(F.data == "u:send")
async def user_send(
    callback: CallbackQuery,
    state: FSMContext,
):
    if is_blocked(callback.from_user.id):
        await safe_callback_answer(callback, "Доступ ограничен.", True)
        return

    await state.set_state(FeedbackState.waiting)
    await edit_callback_screen(
        callback,
        state,
        f"{title('Новое сообщение')}\n\n"
        f"{bullet('Напишите сообщение следующим сообщением.')}\n"
        f"{bullet('Максимальная длина — 4000 символов.')}\n\n"
        f"{divider()}\n"
        "♡ Вы можете написать всё, что хотите сообщить.",
        cancel_kb(),
    )
    await safe_callback_answer(callback)


@dp.callback_query(F.data == "u:cancel")
async def user_cancel(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()
    await edit_callback_screen(
        callback,
        state,
        home_text(),
        main_kb(),
    )
    await safe_callback_answer(callback)


@dp.callback_query(F.data == "a:cancel")
async def admin_cancel(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await safe_callback_answer(callback, "Нет доступа.", True)
        return
    await state.clear()
    await edit_callback_screen(
        callback,
        state,
        admin_home_text(),
        admin_kb(),
    )
    await safe_callback_answer(callback)


@dp.message(Command("cancel"))
async def command_cancel(
    message: Message,
    state: FSMContext,
):
    await state.clear()
    await delete_user_message(message)

    if message.from_user.id == ADMIN_ID:
        await send_screen(
            message,
            state,
            admin_home_text(),
            admin_kb(),
        )
        return

    await send_screen(
        message,
        state,
        home_text(),
        main_kb(),
    )

# =========================================================
# FEEDBACK MESSAGE
# =========================================================

@dp.message(
    FeedbackState.waiting,
    F.text,
)
async def feedback(
    message: Message,
    state: FSMContext,
):
    touch_polling_activity()
    register_user(message.from_user)

    if is_blocked(message.from_user.id):
        await state.clear()
        await delete_user_message(message)
        await message.answer(
            f"{title('Доступ ограничен')}\n\n"
            f"{bullet('Для вашего аккаунта отправка сообщений отключена.')}",
            reply_markup=main_kb(),
        )
        return

    text = (message.text or "").strip()
    if not text:
        await delete_user_message(message)
        await edit_state_screen(
            state, bot, message.chat.id,
            f"{title('Новое сообщение')}\n\n"
            f"{bullet('Сообщение не может быть пустым.')}\n\n"
            "Попробуйте ещё раз.",
            cancel_kb(),
        )
        return

    if len(text) > MAX_MESSAGE_LENGTH:
        await delete_user_message(message)
        await edit_state_screen(
            state, bot, message.chat.id,
            f"{title('Новое сообщение')}\n\n"
            f"{bullet('Сообщение слишком длинное.')}\n"
            f"{bullet('Максимум — 4000 символов.')}",
            cancel_kb(),
        )
        return

    screen_message_id = (await state.get_data()).get("screen_message_id")
    sender_name = " ".join(
        part for part in (
            message.from_user.first_name,
            message.from_user.last_name,
        ) if part
    ).strip() or "Без имени"
    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "нет username"
    )

    admin_text = (
        f"{title('Новое сообщение')}\n\n"
        f"{section('Текст')}\n"
        f"{text}\n\n"
        f"{divider()}\n"
        f"{section('Отправитель')}\n"
        f"{bullet('Имя: ' + sender_name)}\n"
        f"{bullet('Username: ' + username)}\n"
        f"{bullet('ID: ' + str(message.from_user.id))}"
    )

    try:
        notification_id = create_feedback_notification(
            message.from_user.id,
            admin_text,
        )
        await DELIVERY_QUEUE.put({"notification_id": notification_id})
    except Exception:
        logger.exception(
            "Failed to persist feedback notification | user_id=%s",
            message.from_user.id,
        )
        await delete_user_message(message)
        await state.clear()
        await message.answer(
            f"{title('Ошибка')}\n\n"
            f"{bullet('Не удалось сохранить сообщение.')}\n"
            f"{bullet('Попробуйте немного позже.')}",
            reply_markup=main_kb(),
        )
        return

    await delete_user_message(message)
    await state.clear()

    success = (
        f"{title('Сообщение отправлено')}\n\n"
        f"{bullet('Ваше сообщение успешно передано.')}\n"
        f"{bullet('При необходимости вы получите ответ через бота.')}\n\n"
        f"{divider()}\n"
        "♡ Спасибо за обратную связь."
    )

    if screen_message_id:
        edited = await edit_message_safe(
            message.chat.id,
            screen_message_id,
            success,
            after_send_kb(),
        )
        if edited:
            return

    await message.answer(
        success,
        reply_markup=after_send_kb(),
    )

@dp.message(FeedbackState.waiting)
async def feedback_non_text(
    message: Message,
    state: FSMContext,
):
    await delete_user_message(message)
    updated = await edit_state_screen(
        state,
        bot,
        message.chat.id,
        f"{title('Новое сообщение')}\n\n"
        f"{bullet('Пожалуйста, отправьте сообщение текстом.')}",
        cancel_kb(),
    )
    if not updated:
        await send_screen(
            message,
            state,
            f"{title('Новое сообщение')}\n\n"
            f"{bullet('Пожалуйста, отправьте сообщение текстом.')}",
            cancel_kb(),
        )

# =========================================================
# REPORT START
# =========================================================

@dp.callback_query(F.data == "u:report")
async def report_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    if is_blocked(callback.from_user.id):
        await safe_callback_answer(callback, "Доступ ограничен.", True)
        return

    left = report_cooldown_left(callback.from_user.id)
    if left:
        minutes = (left + 59) // 60
        await safe_callback_answer(
            callback,
            f"Следующую жалобу можно отправить примерно через {minutes} мин.",
            True,
        )
        return

    await state.set_state(ReportTargetState.waiting)
    await edit_callback_screen(
        callback,
        state,
        f"{title('Жалоба на пользователя')}\n\n"
        f"{bullet('Укажите username или Telegram ID пользователя.')}\n\n"
        f"{divider()}\n"
        f"{warning('Жалоба не является анонимной для администрации.')}",
        cancel_kb(),
    )
    await safe_callback_answer(callback)


@dp.message(ReportTargetState.waiting, F.text)
async def report_target(
    message: Message,
    state: FSMContext,
):
    value = message.text.strip()
    if len(value) < 2 or len(value) > 100:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            bot,
            message.chat.id,
            f"{title('Жалоба')}\n\n"
            f"{bullet('Укажите корректный username или Telegram ID.')}",
            cancel_kb(),
        )
        return

    cleaned = value.lstrip("@")

    if cleaned.isdigit():
        try:
            target_user_id = int(cleaned)
        except ValueError:
            target_user_id = None
        target_text = value
    else:
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", cleaned):
            await delete_user_message(message)
            await edit_state_screen(
                state,
                bot,
                message.chat.id,
                f"{title('Жалоба')}\n\n"
                f"{bullet('Username может содержать только буквы, цифры и _.')}",
                cancel_kb(),
            )
            return
        target_user_id = None
        target_text = f"@{cleaned}"

        known_user = get_user_by_username(target_text)
        if known_user:
            target_user_id = known_user["user_id"]

    if target_user_id is not None and target_user_id == message.from_user.id:
        await state.clear()
        await delete_user_message(message)
        await message.answer(
            f"{title('Жалоба')}\n\n"
            f"{bullet('Нельзя пожаловаться на самого себя.')}",
            reply_markup=main_kb(),
        )
        return

    if target_user_id is None:
        me = get_user(message.from_user.id)
        own_username = normalize_target_text(me["username"]) if me else ""
        if own_username and own_username == normalize_target_text(target_text):
            await state.clear()
            await delete_user_message(message)
            await message.answer(
                f"{title('Жалоба')}\n\n"
                f"{bullet('Нельзя пожаловаться на самого себя.')}",
                reply_markup=main_kb(),
            )
            return

    if has_reported_target(
        message.from_user.id,
        target_user_id,
        target_text,
    ):
        await state.clear()
        await delete_user_message(message)
        await message.answer(
            f"{title('Жалоба уже отправлена')}\n\n"
            f"{bullet('Вы уже отправляли жалобу на этого пользователя.')}",
            reply_markup=main_kb(),
        )
        return

    await state.update_data(
        target_text=target_text,
        target_user_id=target_user_id,
        target_key=target_key(target_text, target_user_id),
    )
    await state.set_state(ReportReasonState.waiting)
    await delete_user_message(message)
    await edit_state_screen(
        state,
        bot,
        message.chat.id,
        f"{title('Причина жалобы')}\n\n"
        f"{bullet('Кратко опишите причину обращения.')}\n"
        f"{bullet('Максимум — 2000 символов.')}",
        cancel_kb(),
    )


@dp.message(ReportReasonState.waiting, F.text)
async def report_reason(
    message: Message,
    state: FSMContext,
):
    reason = message.text.strip()

    if len(reason) < 3:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            bot,
            message.chat.id,
            f"{title('Причина жалобы')}\n\n"
            f"{bullet('Опишите причину немного подробнее.')}",
            cancel_kb(),
        )
        return

    if len(reason) > MAX_REPORT_REASON_LENGTH:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            bot,
            message.chat.id,
            f"{title('Причина жалобы')}\n\n"
            f"{bullet('Причина слишком длинная.')}\n"
            f"{bullet('Максимум — 2000 символов.')}",
            cancel_kb(),
        )
        return

    data = await state.get_data()
    target = data.get("target_text")
    target_id = data.get("target_user_id")
    if not target:
        await state.clear()
        await delete_user_message(message)
        await message.answer(
            f"{title('Ошибка')}\n\n"
            f"{bullet('Данные жалобы устарели.')}",
            reply_markup=main_kb(),
        )
        return

    await state.update_data(reason=reason)
    await delete_user_message(message)
    await edit_state_screen(
        state,
        bot,
        message.chat.id,
        f"{title('Проверьте жалобу')}\n\n"
        f"{section('Пользователь')}\n"
        f"{bullet(target)}\n"
        + (f"{bullet('ID: ' + str(target_id))}\n" if target_id is not None else "")
        + "\n"
        f"{section('Причина')}\n"
        f"{reason}\n\n"
        f"{divider()}\n"
        f"{warning('Перед отправкой убедитесь, что всё указано верно.')}",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⚠ Отправить жалобу", callback_data="u:report_confirm")],
                [InlineKeyboardButton(text="↻ Изменить причину", callback_data="u:report_edit")],
                [InlineKeyboardButton(text="‹ Отмена", callback_data="u:cancel")],
            ]
        ),
    )


@dp.callback_query(F.data == "u:report_edit")
async def report_edit(
    callback: CallbackQuery,
    state: FSMContext,
):
    data = await state.get_data()
    if not data.get("target_text"):
        await safe_callback_answer(callback, "Данные жалобы устарели.", True)
        return
    await state.set_state(ReportReasonState.waiting)
    await edit_callback_screen(
        callback,
        state,
        f"{title('Причина жалобы')}\n\n"
        f"{bullet('Напишите причину заново.')}",
        cancel_kb(),
    )
    await safe_callback_answer(callback)


@dp.callback_query(F.data == "u:report_confirm")
async def report_confirm(
    callback: CallbackQuery,
    state: FSMContext,
):
    touch_polling_activity()

    if is_blocked(callback.from_user.id):
        await state.clear()
        await safe_callback_answer(callback, "Доступ ограничен.", True)
        return

    data = await state.get_data()
    target = data.get("target_text")
    reason = data.get("reason")
    target_id = data.get("target_user_id")

    if not target or not reason:
        await state.clear()
        await safe_callback_answer(callback, "Данные жалобы устарели.", True)
        return

    left = report_cooldown_left(callback.from_user.id)
    if left:
        await state.clear()
        minutes = (left + 59) // 60
        await edit_callback_screen(
            callback, state,
            f"{title('Слишком часто')}\n\n"
            f"{bullet(f'Следующую жалобу можно отправить примерно через {minutes} мин.')}",
            main_kb(),
        )
        await safe_callback_answer(callback)
        return

    if target_id is not None and target_id == callback.from_user.id:
        await state.clear()
        await edit_callback_screen(
            callback, state,
            f"{title('Жалоба')}\n\n"
            f"{bullet('Нельзя пожаловаться на самого себя.')}",
            main_kb(),
        )
        await safe_callback_answer(callback)
        return

    if has_reported_target(callback.from_user.id, target_id, target):
        await state.clear()
        await edit_callback_screen(
            callback, state,
            f"{title('Жалоба уже отправлена')}\n\n"
            f"{bullet('Вы уже отправляли жалобу на этого пользователя.')}",
            main_kb(),
        )
        await safe_callback_answer(callback)
        return

    reporter = get_user(callback.from_user.id)
    reporter_name = display_name(reporter)
    reporter_username = display_username(reporter)

    def build_admin_text(report_id):
        return (
            f"{title(f'Жалоба #{report_id}')}\n\n"
            f"{section('Пользователь')}\n"
            f"{bullet('Указано: ' + target)}\n"
            f"{bullet('ID: ' + (str(target_id) if target_id is not None else 'не указан'))}\n\n"
            f"{section('Причина')}\n"
            f"{reason}\n\n"
            f"{divider()}\n"
            f"{section('Заявитель')}\n"
            f"{bullet('Имя: ' + reporter_name)}\n"
            f"{bullet('Username: ' + reporter_username)}\n"
            f"{bullet('ID: ' + str(callback.from_user.id))}"
        )

    try:
        report_id, notification_id = create_report_and_notification(
            callback.from_user.id,
            target,
            target_id,
            reason,
            build_admin_text,
        )
        await DELIVERY_QUEUE.put({"notification_id": notification_id})
    except sqlite3.IntegrityError:
        await state.clear()
        await edit_callback_screen(
            callback, state,
            f"{title('Жалоба уже отправлена')}\n\n"
            f"{bullet('Вы уже отправляли жалобу на этого пользователя.')}",
            main_kb(),
        )
        await safe_callback_answer(callback)
        return
    except Exception:
        logger.exception(
            "Failed to persist report and notification | reporter_id=%s",
            callback.from_user.id,
        )
        await state.clear()
        await edit_callback_screen(
            callback, state,
            f"{title('Ошибка')}\n\n"
            f"{bullet('Не удалось сохранить жалобу.')}\n"
            f"{bullet('Попробуйте немного позже.')}",
            main_kb(),
        )
        await safe_callback_answer(callback)
        return

    await state.clear()
    await edit_callback_screen(
        callback, state,
        f"{title('Жалоба отправлена')}\n\n"
        f"{bullet('Ваше обращение передано.')}\n"
        f"{bullet('Следующую жалобу можно отправить через 10 минут.')}\n\n"
        f"{divider()}\n"
        "♡ Спасибо за обращение.",
        main_kb(),
    )
    await safe_callback_answer(callback, "Жалоба отправлена")

@dp.message(ReportTargetState.waiting)
@dp.message(ReportReasonState.waiting)
async def report_non_text(
    message: Message,
    state: FSMContext,
):
    await delete_user_message(message)
    updated = await edit_state_screen(
        state,
        bot,
        message.chat.id,
        f"{title('Жалоба')}\n\n"
        f"{bullet('Пожалуйста, отправьте ответ текстом.')}",
        cancel_kb(),
    )
    if not updated:
        await send_screen(
            message,
            state,
            f"{title('Жалоба')}\n\n"
            f"{bullet('Пожалуйста, отправьте ответ текстом.')}",
            cancel_kb(),
        )

# =========================================================
# ADMIN REPLY
# =========================================================

@dp.callback_query(F.data.startswith("reply:"))
async def admin_reply_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await safe_callback_answer(callback, "Нет доступа.", True)
        return

    try:
        user_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await safe_callback_answer(callback, "Некорректный пользователь.", True)
        return

    if not get_user(user_id):
        await safe_callback_answer(callback, "Пользователь не найден.", True)
        return

    await state.set_state(AdminReplyState.waiting)
    await state.update_data(reply_to=user_id)

    sent = await callback.message.answer(
        f"{title('Ответ пользователю')}\n\n"
        f"{bullet('Напишите текст ответа.')}\n"
        f"{bullet('Максимум — 4000 символов.')}",
        reply_markup=admin_cancel_kb(),
    )
    await save_screen_message(state, sent)
    await safe_callback_answer(callback)


@dp.callback_query(F.data.startswith("a:replyuser:"))
async def admin_reply_user_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await safe_callback_answer(callback, "Нет доступа.", True)
        return

    try:
        user_id = int(callback.data.split(":")[2])
    except (ValueError, IndexError):
        await safe_callback_answer(callback, "Некорректный пользователь.", True)
        return

    if not get_user(user_id):
        await safe_callback_answer(callback, "Пользователь не найден.", True)
        return

    await state.set_state(AdminReplyState.waiting)
    await state.update_data(reply_to=user_id)

    sent = await callback.message.answer(
        f"{title('Ответ пользователю')}\n\n"
        f"{bullet('Напишите текст ответа.')}\n"
        f"{bullet('Максимум — 4000 символов.')}",
        reply_markup=admin_cancel_kb(),
    )
    await save_screen_message(state, sent)
    await safe_callback_answer(callback)


@dp.message(AdminReplyState.waiting, F.text)
async def admin_reply(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    user_id = data.get("reply_to")
    screen_message_id = data.get("screen_message_id")
    text = message.text.strip()

    if not user_id:
        await delete_user_message(message)
        await state.clear()
        await message.answer(
            f"{title('Ошибка')}\n\n"
            f"{bullet('Получатель не найден.')}",
            reply_markup=admin_kb(),
        )
        return

    if not text:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            bot,
            message.chat.id,
            f"{title('Ответ пользователю')}\n\n"
            f"{bullet('Ответ не может быть пустым.')}",
            admin_cancel_kb(),
        )
        return

    if len(text) > MAX_MESSAGE_LENGTH:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            bot,
            message.chat.id,
            f"{title('Ответ пользователю')}\n\n"
            f"{bullet('Ответ слишком длинный.')}\n"
            f"{bullet('Максимум — 4000 символов.')}",
            admin_cancel_kb(),
        )
        return

    try:
        await send_long_message(
            user_id,
            f"{title('Ответ администрации')}\n\n"
            f"{text}\n\n"
            f"{divider()}\n"
            "♡ Вы можете ответить через бота.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="♡ Ответить", callback_data="u:send")]
                ]
            ),
        )
        set_telegram_blocked(user_id, False)
    except (TelegramForbiddenError, TelegramNotFound) as exc:
        set_telegram_blocked(user_id, True)
        logger.info(
            "User unavailable for admin reply | user_id=%s | %s",
            user_id,
            type(exc).__name__,
        )
        await delete_user_message(message)
        await state.clear()
        text_out = (
            f"{title('Ошибка доставки')}\n\n"
            f"{bullet('Пользователь недоступен для сообщений.')}"
        )
        if screen_message_id and await edit_message_safe(
            message.chat.id,
            screen_message_id,
            text_out,
            admin_kb(),
        ):
            return
        await message.answer(text_out, reply_markup=admin_kb())
        return
    except TelegramUnauthorizedError:
        logger.critical(
            "BOT_TOKEN rejected while sending admin reply | user_id=%s",
            user_id,
        )
        await delete_user_message(message)
        await state.clear()
        await message.answer(
            f"{title('Ошибка')}\n\n"
            f"{bullet('Сервис временно недоступен.')}",
            reply_markup=admin_kb(),
        )
        return
    except Exception:
        logger.exception(
            "Failed to send admin reply | user_id=%s",
            user_id,
        )
        await delete_user_message(message)
        await state.clear()
        text_out = (
            f"{title('Ошибка доставки')}\n\n"
            f"{bullet('Не удалось доставить ответ.')}"
        )
        if screen_message_id and await edit_message_safe(
            message.chat.id,
            screen_message_id,
            text_out,
            admin_kb(),
        ):
            return
        await message.answer(text_out, reply_markup=admin_kb())
        return

    await delete_user_message(message)
    await state.clear()
    success = (
        f"{title('Ответ отправлен')}\n\n"
        f"{bullet('Сообщение доставлено пользователю.')}"
    )
    if screen_message_id and await edit_message_safe(
        message.chat.id,
        screen_message_id,
        success,
        admin_kb(),
    ):
        return
    await message.answer(success, reply_markup=admin_kb())


@dp.message(AdminReplyState.waiting)
async def admin_reply_non_text(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return
    await delete_user_message(message)
    updated = await edit_state_screen(
        state,
        bot,
        message.chat.id,
        f"{title('Ответ пользователю')}\n\n"
        f"{bullet('Пожалуйста, отправьте ответ текстом.')}",
        admin_cancel_kb(),
    )
    if not updated:
        await send_screen(
            message,
            state,
            f"{title('Ответ пользователю')}\n\n"
            f"{bullet('Пожалуйста, отправьте ответ текстом.')}",
            admin_cancel_kb(),
        )

# =========================================================
# ADMIN HOME
# =========================================================

@dp.message(Command("admin"))
async def admin_command(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return

    register_user(message.from_user)
    await state.clear()
    await delete_user_message(message)
    await send_screen(
        message,
        state,
        admin_home_text(),
        admin_kb(),
    )


@dp.callback_query(F.data == "a:home")
async def admin_home(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await safe_callback_answer(callback, "Нет доступа.", True)
        return

    await state.clear()
    await edit_callback_screen(
        callback,
        state,
        admin_home_text(),
        admin_kb(),
    )
    await safe_callback_answer(callback)

# =========================================================
# ADMIN STATS
# =========================================================

@dp.callback_query(F.data == "a:stats")
async def admin_stats(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    await state.clear()

    await edit_screen(
        callback,

        f"{title('Статистика')}\n\n"
        f"{bullet(f'Пользователей: {user_count()}')}\n"
        f"{bullet(f'Получено сообщений: {message_count()}')}\n"
        f"{bullet(f'Заблокировано: {blocked_count()}')}\n"
        f"{bullet(f'Новых жалоб: {new_reports_count()}')}\n\n"
        f"{divider()}\n"
        "♡ Статистика обновляется автоматически.",

        back_admin(),
    )

    await callback.answer()


# =========================================================
# ADMIN MESSAGES
# =========================================================

@dp.callback_query(F.data == "a:messages")
async def admin_messages(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    await state.clear()

    await edit_screen(
        callback,

        f"{title('Сообщения')}\n\n"
        f"{bullet(f'Всего получено: {message_count()}')}\n\n"
        f"{divider()}\n"
        "♡ Новые сообщения приходят прямо в этот чат.",

        back_admin(),
    )

    await callback.answer()


# =========================================================
# ADMIN USERS
# =========================================================

@dp.callback_query(
    F.data.startswith("a:users:")
)
async def admin_users(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    await state.clear()

    try:
        page = int(
            callback.data.split(":")[2]
        )

    except (
        ValueError,
        IndexError,
    ):
        page = 0

    page = max(
        0,
        page,
    )

    rows = list_users(
        10,
        page * 10,
    )

    buttons = []

    for row in rows:

        label = display_name(row)

        if row["blocked"]:
            label = f"⊘ {label}"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=label[:40],
                    callback_data=(
                        f"a:user:{row['user_id']}"
                    ),
                )
            ]
        )

    nav = []

    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="‹",
                callback_data=(
                    f"a:users:{page - 1}"
                ),
            )
        )

    if len(rows) == 10:
        nav.append(
            InlineKeyboardButton(
                text="›",
                callback_data=(
                    f"a:users:{page + 1}"
                ),
            )
        )

    if nav:
        buttons.append(nav)

    buttons.append(
        [
            InlineKeyboardButton(
                text="⌕ Поиск",
                callback_data="a:search",
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                text="‹ Назад",
                callback_data="a:home",
            )
        ]
    )

    await edit_screen(
        callback,

        f"{title('Пользователи')}\n\n"
        f"{bullet(f'Всего: {user_count()}')}\n"
        f"{bullet(f'Страница: {page + 1}')}\n\n"
        f"{divider()}\n"
        "♡ Выберите пользователя.",

        InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )

    await callback.answer()


# =========================================================
# ADMIN SEARCH
# =========================================================

@dp.callback_query(F.data == "a:search")
async def admin_search_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await safe_callback_answer(callback, "Нет доступа.", True)
        return

    await state.set_state(AdminSearchState.waiting)
    sent = await callback.message.answer(
        f"{title('Поиск пользователя')}\n\n"
        f"{bullet('Введите имя, username или Telegram ID.')}",
        reply_markup=admin_cancel_kb(),
    )
    await save_screen_message(state, sent)
    await safe_callback_answer(callback)


@dp.message(AdminSearchState.waiting, F.text)
async def admin_search(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return

    query = message.text.strip()
    if not query:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            bot,
            message.chat.id,
            f"{title('Поиск пользователя')}\n\n"
            f"{bullet('Введите запрос.')}",
            admin_cancel_kb(),
        )
        return

    rows = list(search_users(query))
    data = await state.get_data()
    screen_message_id = data.get("screen_message_id")

    await delete_user_message(message)
    await state.clear()

    buttons = []
    for row in rows:
        label = display_name(row)
        username = display_username(row)
        if row["blocked"]:
            label = f"⊘ {label}"
        if username != "нет username":
            label = f"{label} {username}"
        buttons.append([
            InlineKeyboardButton(
                text=label[:64],
                callback_data=f"a:user:{row['user_id']}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="‹ В админку",
            callback_data="a:home",
        )
    ])

    if rows:
        text = (
            f"{title('Результаты поиска')}\n\n"
            f"{bullet(f'Найдено: {len(rows)}')}"
        )
    else:
        text = (
            f"{title('Поиск')}\n\n"
            f"{bullet('Пользователи не найдены.')}"
        )

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    if screen_message_id:
        if await edit_message_safe(
            message.chat.id,
            screen_message_id,
            text,
            markup,
        ):
            return

    await message.answer(
        text,
        reply_markup=markup,
    )


@dp.message(AdminSearchState.waiting)
async def admin_search_non_text(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return
    await delete_user_message(message)
    await edit_state_screen(
        state,
        bot,
        message.chat.id,
        f"{title('Поиск пользователя')}\n\n"
        f"{bullet('Введите имя, username или Telegram ID.')}",
        admin_cancel_kb(),
    )

# =========================================================
# ADMIN USER CARD
# =========================================================

@dp.callback_query(
    F.data.startswith("a:user:")
)
async def admin_user(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    try:

        user_id = int(
            callback.data.split(":")[2]
        )

    except (
        ValueError,
        IndexError,
    ):

        await callback.answer(
            "Некорректный ID.",
            show_alert=True,
        )

        return

    await state.clear()

    row = get_user(
        user_id
    )

    if not row:

        await callback.answer(
            "Пользователь не найден.",
            show_alert=True,
        )

        return

    username = (
        f"@{row['username']}"
        if row["username"]
        else "нет username"
    )

    status = (
        "заблокирован"
        if row["blocked"]
        else "активен"
    )

    text = (
        f"{title('Пользователь')}\n\n"
        f"{section('Профиль')}\n"
        f"{bullet('Имя: ' + display_name(row))}\n"
        f"{bullet('Username: ' + username)}\n"
        f"{bullet('Telegram ID: ' + str(row['user_id']))}\n\n"
        f"{section('Статистика')}\n"
        f"{bullet('Сообщений: ' + str(row['messages_count']))}\n"
        f"{bullet('Жалоб отправлено: ' + str(row['reports_count']))}\n"
        f"{bullet('Статус: ' + status)}\n\n"
        f"{section('Активность')}\n"
        f"{bullet('Первый запуск: ' + row['first_seen'][:19].replace('T', ' ') + ' UTC')}\n"
        f"{bullet('Последняя активность: ' + row['last_seen'][:19].replace('T', ' ') + ' UTC')}"
    )

    await edit_callback_screen(
        callback,
        state,
        text,
        user_admin_kb(
            user_id,
            bool(row["blocked"]),
        ),
    )

    await safe_callback_answer(callback)


# =========================================================
# TOGGLE BLOCK
# =========================================================

@dp.callback_query(
    F.data.startswith("a:toggle:")
)
async def admin_toggle(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    await state.clear()

    try:

        user_id = int(
            callback.data.split(":")[2]
        )

    except (
        ValueError,
        IndexError,
    ):

        await callback.answer(
            "Некорректный ID.",
            show_alert=True,
        )

        return

    if user_id == ADMIN_ID:
        await callback.answer(
            "Нельзя заблокировать администратора.",
            show_alert=True,
        )
        return

    row = get_user(
        user_id
    )

    if not row:

        await callback.answer(
            "Пользователь не найден.",
            show_alert=True,
        )

        return

    new_value = not bool(
        row["blocked"]
    )

    set_blocked(
        user_id,
        new_value,
    )

    await callback.answer(
        (
            "Пользователь заблокирован."
            if new_value
            else "Пользователь разблокирован."
        ),
        show_alert=True,
    )

    row = get_user(
        user_id
    )

    username = (
        f"@{row['username']}"
        if row["username"]
        else "нет username"
    )

    status = (
        "заблокирован"
        if row["blocked"]
        else "активен"
    )

    await callback.message.edit_text(
        f"{title('Пользователь')}\n\n"
        f"{section('Профиль')}\n"
        f"{bullet('Имя: ' + display_name(row))}\n"
        f"{bullet('Username: ' + username)}\n"
        f"{bullet('ID: ' + str(row['user_id']))}\n\n"
        f"{section('Статус')}\n"
        f"{bullet(status)}",

        reply_markup=user_admin_kb(
            user_id,
            bool(row["blocked"]),
        ),
    )


# =========================================================
# BLOCK
# =========================================================

@dp.callback_query(
    F.data.startswith("a:block:")
)
async def admin_block(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    await state.clear()

    try:

        user_id = int(
            callback.data.split(":")[2]
        )

    except (
        ValueError,
        IndexError,
    ):

        await callback.answer(
            "Некорректный ID.",
            show_alert=True,
        )

        return

    if user_id == ADMIN_ID:
        await callback.answer(
            "Нельзя заблокировать администратора.",
            show_alert=True,
        )
        return

    if not get_user(
        user_id
    ):

        await callback.answer(
            "Пользователь не найден.",
            show_alert=True,
        )

        return

    set_blocked(
        user_id,
        True,
    )

    await callback.answer(
        "Пользователь заблокирован.",
        show_alert=True,
    )


# =========================================================
# BLOCKED
# =========================================================

@dp.callback_query(
    F.data == "a:blocked"
)
async def admin_blocked(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    await state.clear()

    rows = blocked_users(50)

    text = (
        f"{title('Заблокированные')}\n\n"
    )

    if not rows:

        text += note(
            "Список пуст."
        )

    else:

        for row in rows:

            username = (
                f"@{row['username']}"
                if row["username"]
                else "нет username"
            )

            text += (
                f"⊘ {display_name(row)}\n"
                f"{bullet(username)}\n"
                f"{bullet(str(row['user_id']))}\n\n"
            )

    await edit_screen(
        callback,
        text,
        back_admin(),
    )

    await callback.answer()


# =========================================================
# ADMIN REPORTS
# =========================================================

@dp.callback_query(
    F.data == "a:reports"
)
async def admin_reports(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    await state.clear()

    rows = recent_reports()

    if not rows:

        await edit_screen(
            callback,

            f"{title('Жалобы')}\n\n"
            f"{note('Новых жалоб нет.')}",

            back_admin(),
        )

        await callback.answer()

        return

    buttons = []

    text = (
        f"{title('Новые жалобы')}\n\n"
    )

    for row in rows:

        text += (
            f"⚠ #{row['id']} "
            f"{row['target_text']}\n"
            f"{bullet(row['reason'][:100])}\n\n"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"Открыть жалобу #{row['id']}",
                    callback_data=(
                        f"a:report:{row['id']}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                text="‹ Назад",
                callback_data="a:home",
            )
        ]
    )

    await edit_screen(
        callback,

        text,

        InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )

    await callback.answer()


# =========================================================
# ADMIN REPORT
# =========================================================

@dp.callback_query(
    F.data.startswith("a:report:")
)
async def admin_report(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    await state.clear()

    try:

        report_id = int(
            callback.data.split(":")[2]
        )

    except (
        ValueError,
        IndexError,
    ):

        await callback.answer(
            "Некорректный номер жалобы.",
            show_alert=True,
        )

        return

    report = get_report(
        report_id
    )

    if not report:

        await callback.answer(
            "Жалоба не найдена.",
            show_alert=True,
        )

        return

    reporter = get_user(
        report["reporter_id"]
    )

    reporter_username = (
        f"@{reporter['username']}"
        if reporter
        and reporter["username"]
        else "нет username"
    )

    target_id = (
        str(report["target_user_id"])
        if report["target_user_id"]
        else "не указан"
    )

    status = (
        "новая"
        if report["status"]
        == "new"
        else "закрыта"
    )

    text = (
        f"{title(f'Жалоба #{report_id}')}\n\n"
        f"{section('Статус')}\n"
        f"{bullet(status)}\n\n"
        f"{section('Пользователь')}\n"
        f"{bullet(report['target_text'])}\n"
        f"{bullet('ID: ' + target_id)}\n\n"
        f"{section('Причина')}\n"
        f"{report['reason']}\n\n"
        f"{divider()}\n"
        f"{section('Заявитель')}\n"
        f"{bullet('Имя: ' + display_name(reporter))}\n"
        f"{bullet('Username: ' + reporter_username)}\n"
        f"{bullet('ID: ' + str(report['reporter_id']))}"
    )

    await edit_screen(
        callback,
        text,
        report_admin_kb(
            report_id,
            report["target_user_id"],
        ),
    )

    await callback.answer()


# =========================================================
# CLOSE REPORT
# =========================================================

@dp.callback_query(
    F.data.startswith("a:close_report:")
)
async def admin_close_report(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    await state.clear()

    try:

        report_id = int(
            callback.data.split(":")[2]
        )

    except (
        ValueError,
        IndexError,
    ):

        await callback.answer(
            "Некорректный номер.",
            show_alert=True,
        )

        return

    report = get_report(report_id)

    if not report:
        await callback.answer(
            "Жалоба не найдена.",
            show_alert=True,
        )
        return

    if report["status"] == "closed":
        await callback.answer(
            "Жалоба уже закрыта.",
            show_alert=True,
        )
        return

    if not close_report(report_id):
        await callback.answer(
            "Не удалось закрыть жалобу.",
            show_alert=True,
        )
        return

    await callback.answer(
        "Жалоба закрыта.",
        show_alert=True,
    )

    await edit_screen(
        callback,

        f"{title('Жалоба закрыта')}\n\n"
        f"{bullet('Номер: #' + str(report_id))}\n\n"
        f"{divider()}\n"
        "♡ Обращение отмечено как рассмотренное.",

        back_admin(),
    )


# =========================================================
# BROADCAST
# =========================================================

def _broadcast_user_ids():
    with DB_LOCK:
        conn = db()
        try:
            return [
                row["user_id"]
                for row in conn.execute(
                    """
                    SELECT user_id
                    FROM users
                    WHERE blocked=0
                      AND telegram_blocked=0
                    ORDER BY user_id
                    """
                ).fetchall()
            ]
        finally:
            conn.close()


async def broadcast_send_one(user_id: int, text: str):
    for attempt in range(BROADCAST_MAX_RETRIES):
        try:
            await send_with_retry(
                user_id,
                f"{title('Сообщение от администрации')}\n\n{text}",
            )
            set_telegram_blocked(user_id, False)
            return "sent"
        except (TelegramForbiddenError, TelegramNotFound):
            set_telegram_blocked(user_id, True)
            return "unavailable"
        except TelegramUnauthorizedError:
            raise
        except TelegramRetryAfter as exc:
            if attempt >= BROADCAST_MAX_RETRIES - 1:
                return "failed"
            await asyncio.sleep(max(1, int(exc.retry_after)))
        except (TelegramNetworkError, TelegramServerError):
            if attempt >= BROADCAST_MAX_RETRIES - 1:
                return "failed"
            await asyncio.sleep(2 ** attempt)
        except Exception:
            logger.exception(
                "Broadcast failed | user_id=%s | attempt=%s/%s",
                user_id,
                attempt + 1,
                BROADCAST_MAX_RETRIES,
            )
            return "failed"
    return "failed"


@dp.callback_query(F.data == "a:broadcast")
async def admin_broadcast_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    touch_polling_activity()
    if callback.from_user.id != ADMIN_ID:
        await safe_callback_answer(callback, "Нет доступа.", True)
        return

    await state.set_state(BroadcastState.waiting)
    sent = await callback.message.answer(
        f"{title('Рассылка')}\n\n"
        f"{bullet('Напишите текст для зарегистрированных пользователей.')}\n"
        f"{bullet('Заблокированные пользователи рассылку не получают.')}\n\n"
        f"{divider()}\n"
        "♡ Используйте рассылку для важных объявлений.",
        reply_markup=admin_cancel_kb(),
    )
    await save_screen_message(state, sent)
    await safe_callback_answer(callback)


@dp.message(BroadcastState.waiting, F.text)
async def admin_broadcast(
    message: Message,
    state: FSMContext,
):
    touch_polling_activity()
    if message.from_user.id != ADMIN_ID:
        return

    text = message.text.strip()
    if not text:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            bot,
            message.chat.id,
            f"{title('Рассылка')}\n\n"
            f"{bullet('Текст не может быть пустым.')}",
            admin_cancel_kb(),
        )
        return

    if len(text) > MAX_MESSAGE_LENGTH:
        await delete_user_message(message)
        await edit_state_screen(
            state,
            bot,
            message.chat.id,
            f"{title('Рассылка')}\n\n"
            f"{bullet('Текст слишком длинный.')}\n"
            f"{bullet('Максимум — 4000 символов.')}",
            admin_cancel_kb(),
        )
        return

    data = await state.get_data()
    screen_message_id = data.get("screen_message_id")

    await delete_user_message(message)
    await state.clear()

    started_text = (
        f"{title('Рассылка запущена')}\n\n"
        f"{bullet('Подготавливаю список получателей...')}"
    )

    if screen_message_id:
        edited = await edit_message_safe(
            message.chat.id,
            screen_message_id,
            started_text,
            admin_kb(),
        )
        if not edited:
            await message.answer(
                started_text,
                reply_markup=admin_kb(),
            )
    else:
        await message.answer(
            started_text,
            reply_markup=admin_kb(),
        )

    try:
        notification_id = create_admin_notification(
            "broadcast",
            None,
            text,
            "",
        )
        await BROADCAST_QUEUE.put({
            "notification_id": notification_id,
            "text": text,
        })
    except Exception:
        logger.exception("Failed to persist broadcast job")
        await message.answer(
            f"{title('Ошибка')}\n\n"
            f"{bullet('Не удалось поставить рассылку в очередь.')}",
            reply_markup=admin_kb(),
        )
        return

    await message.answer(
        f"{title('Рассылка запущена')}\n\n"
        f"{bullet('Задача поставлена в очередь.')}",
        reply_markup=admin_kb(),
    )

@dp.message(BroadcastState.waiting)
async def admin_broadcast_non_text(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return
    await delete_user_message(message)
    await edit_state_screen(
        state,
        bot,
        message.chat.id,
        f"{title('Рассылка')}\n\n"
        f"{bullet('Пожалуйста, отправьте текст рассылки.')}",
        admin_cancel_kb(),
    )

# =========================================================
# PERSISTENT ADMIN DELIVERY WORKERS
# =========================================================

def notification_markup(kind, ref_id):
    if kind == "feedback" and ref_id is not None:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="↳ Ответить",
                        callback_data=f"reply:{ref_id}",
                    ),
                    InlineKeyboardButton(
                        text="⊘ Заблокировать",
                        callback_data=f"a:block:{ref_id}",
                    ),
                ]
            ]
        )

    if kind == "report" and ref_id is not None:
        report = get_report(ref_id)
        if not report:
            return None
        rows = []
        target_id = report["target_user_id"]
        if target_id is not None:
            rows.append([
                InlineKeyboardButton(
                    text="⊘ Заблокировать пользователя",
                    callback_data=f"a:block:{target_id}:r{ref_id}",
                )
            ])
        rows.append([
            InlineKeyboardButton(
                text="✓ Закрыть жалобу",
                callback_data=f"a:close_report:{ref_id}",
            )
        ])
        rows.append([
            InlineKeyboardButton(
                text="‹ К жалобам",
                callback_data="a:reports",
            )
        ])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return None


async def _deliver_notification(notification_id):
    global fatal_error
    lock = NOTIFICATION_LOCKS.setdefault(notification_id, asyncio.Lock())
    async with lock:
        notif = get_admin_notification(notification_id)
        if not notif or notif["status"] == "delivered":
            return

        kind = notif["kind"]
        ref_id = notif["ref_id"]
        payload = notif["payload"]
        sent_ids = _json_ids(notif["admin_message_ids"])
        parts = split_text(payload)  # exactly once
        sent_ids = sent_ids[:len(parts)]
        markup = notification_markup(kind, ref_id)

        try:
            for index in range(len(sent_ids), len(parts)):
                last = index == len(parts) - 1
                msg = await send_with_retry(
                    ADMIN_ID,
                    parts[index],
                    markup if last else None,
                )
                sent_ids.append(msg.message_id)

                status = "delivered" if last else "partial"
                update_admin_notification(
                    notification_id,
                    status=status,
                    admin_message_ids=sent_ids,
                )
                if kind == "report" and ref_id is not None:
                    set_report_delivery_status(ref_id, status, sent_ids)

                if not last and DELIVERY_PART_DELAY:
                    await asyncio.sleep(DELIVERY_PART_DELAY)

            update_admin_notification(
                notification_id,
                status="delivered",
                admin_message_ids=sent_ids,
            )
            if kind == "report" and ref_id is not None:
                set_report_delivery_status(ref_id, "delivered", sent_ids)

        except TelegramUnauthorizedError:
            fatal_error = "unauthorized"
            logger.critical(
                "BOT_TOKEN unauthorized during admin notification %s",
                notification_id,
            )
            if FATAL_EVENT is not None:
                FATAL_EVENT.set()
            raise
        except TelegramConflictError:
            fatal_error = "conflict"
            logger.critical(
                "Telegram conflict during admin notification %s",
                notification_id,
            )
            if FATAL_EVENT is not None:
                FATAL_EVENT.set()
            raise
        except (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest) as exc:
            status = "partial" if sent_ids else "failed"
            logger.error(
                "Permanent admin delivery failure | id=%s | error=%s",
                notification_id,
                exc,
            )
            update_admin_notification(
                notification_id,
                status=status,
                admin_message_ids=sent_ids,
            )
            if kind == "report" and ref_id is not None:
                set_report_delivery_status(ref_id, status, sent_ids)
        except Exception:
            status = "partial" if sent_ids else "failed"
            logger.exception(
                "Admin delivery failure | id=%s | kind=%s",
                notification_id,
                kind,
            )
            update_admin_notification(
                notification_id,
                status=status,
                admin_message_ids=sent_ids,
            )
            if kind == "report" and ref_id is not None:
                set_report_delivery_status(ref_id, status, sent_ids)


async def delivery_worker(worker_index):
    logger.info("Delivery worker %s started", worker_index)
    while True:
        item = await DELIVERY_QUEUE.get()
        try:
            notification_id = item.get("notification_id")
            if notification_id is not None:
                await _deliver_notification(notification_id)
        except asyncio.CancelledError:
            raise
        except (TelegramUnauthorizedError, TelegramConflictError):
            return
        except Exception:
            logger.exception("Delivery worker %s failed | item=%r", worker_index, item)
        finally:
            DELIVERY_QUEUE.task_done()


async def broadcast_worker(worker_index):
    logger.info("Broadcast worker %s started", worker_index)
    while True:
        job = await BROADCAST_QUEUE.get()
        notification_id = job.get("notification_id")
        try:
            notif = get_admin_notification(notification_id) if notification_id else None
            if not notif or notif["status"] == "delivered":
                continue

            text = notif["payload"]
            user_ids = await asyncio.to_thread(_broadcast_user_ids)
            sent_count = unavailable_count = failed_count = 0

            for index, user_id in enumerate(user_ids, start=1):
                try:
                    result = await broadcast_send_one(user_id, text)
                except TelegramUnauthorizedError:
                    global fatal_error
                    fatal_error = "unauthorized"
                    logger.critical("BOT_TOKEN unauthorized during broadcast")
                    if FATAL_EVENT is not None:
                        FATAL_EVENT.set()
                    return

                if result == "sent":
                    sent_count += 1
                elif result == "unavailable":
                    unavailable_count += 1
                else:
                    failed_count += 1

                if index < len(user_ids) and BROADCAST_DELAY_SECONDS:
                    await asyncio.sleep(BROADCAST_DELAY_SECONDS)

            update_admin_notification(
                notification_id,
                status="delivered",
                admin_message_ids=[],
            )
            logger.info(
                "Broadcast finished | notification=%s | total=%s | sent=%s | unavailable=%s | failed=%s",
                notification_id, len(user_ids), sent_count, unavailable_count, failed_count,
            )

            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"{title('Рассылка завершена')}\n\n"
                    f"{bullet('Всего получателей: ' + str(len(user_ids)))}\n"
                    f"{bullet('Отправлено: ' + str(sent_count))}\n"
                    f"{bullet('Недоступно: ' + str(unavailable_count))}\n"
                    f"{bullet('Ошибок: ' + str(failed_count))}",
                    reply_markup=admin_kb(),
                )
            except TelegramUnauthorizedError:
                fatal_error = "unauthorized"
                if FATAL_EVENT is not None:
                    FATAL_EVENT.set()
                return
            except Exception:
                logger.exception("Could not send broadcast completion notice")

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Broadcast worker %s failed | notification=%s",
                worker_index, notification_id,
            )
            if notification_id:
                update_admin_notification(notification_id, status="failed")
        finally:
            BROADCAST_QUEUE.task_done()


async def enqueue_pending_notifications():
    for row in get_pending_admin_notifications():
        if row["kind"] == "broadcast":
            await BROADCAST_QUEUE.put({
                "notification_id": row["id"],
                "text": row["payload"],
            })
        else:
            await DELIVERY_QUEUE.put({"notification_id": row["id"]})



# =========================================================
# GROUP / FLOOD MODERATION
# =========================================================

from group_logic import ROLE_BY_KEY, ROLE_CATALOG, make_tag, normalize_role, parse_kall, role_for, utf16_slice

JOIN_ACTIVE_STATUSES = {"member", "restricted"}
LEAVE_STATUSES = {"left", "kicked"}


def group_db_op(callback, *args):
    return db_transaction(callback, *args)


def bind_group(chat, title_value=""):
    def op(conn):
        conn.execute(
            """
            INSERT INTO managed_group(group_chat_id, info_channel_id, bound_at, title)
            VALUES (?, COALESCE((SELECT info_channel_id FROM managed_group WHERE group_chat_id=?), NULL), ?, ?)
            ON CONFLICT(group_chat_id) DO UPDATE SET
                title=excluded.title
            """,
            (chat.id, chat.id, now(), title_value or ""),
        )
        conn.commit()
    group_db_op(op)


def bind_info_channel_db(chat_id):
    def op(conn):
        row = conn.execute("SELECT group_chat_id FROM managed_group ORDER BY bound_at DESC LIMIT 1").fetchone()
        if row:
            group_id = row["group_chat_id"]
            conn.execute("UPDATE managed_group SET info_channel_id=? WHERE group_chat_id=?", (chat_id, group_id))
            if group_id != chat_id:
                # Migrate any legacy role_state accidentally captured before the group was linked.
                conn.execute(
                    """
                    INSERT OR IGNORE INTO role_state(chat_id,role_key,role_name,user_id,legacy_marker,legacy_custom_emoji_id,bot_managed)
                    SELECT ?,role_key,role_name,user_id,legacy_marker,legacy_custom_emoji_id,bot_managed
                    FROM role_state WHERE chat_id=?
                    """,
                    (group_id, chat_id),
                )
        else:
            # Temporary link; the first /bind_group will update its info channel later.
            conn.execute(
                "INSERT OR IGNORE INTO managed_group(group_chat_id, info_channel_id, bound_at, title) VALUES (?, ?, ?, ?)",
                (0, chat_id, now(), ""),
            )
        conn.commit()
        return row["group_chat_id"] if row else None
    return group_db_op(op)


def get_managed_group():
    def op(conn):
        row = conn.execute("SELECT * FROM managed_group WHERE group_chat_id != 0 ORDER BY bound_at DESC LIMIT 1").fetchone()
        return row
    return group_db_op(op)


def get_managed_info_for_group(group_chat_id):
    return group_db_op(lambda conn: conn.execute("SELECT * FROM managed_group WHERE group_chat_id=?", (group_chat_id,)).fetchone())


def upsert_group_member(chat_id, user, *, active=True, role_key=None, role_name=None, tag=None, confirmed=False, welcome_message_id=None, tag_set_by_bot=False):
    def op(conn):
        existing = conn.execute("SELECT * FROM group_members WHERE chat_id=? AND user_id=?", (chat_id, user.id)).fetchone()
        conn.execute(
            """
            INSERT INTO group_members(
                chat_id,user_id,first_name,last_name,username,role_key,role_name,tag,
                confirmed,active,joined_at,confirmed_at,left_at,welcome_message_id,tag_set_by_bot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                username=excluded.username,
                role_key=COALESCE(excluded.role_key, group_members.role_key),
                role_name=COALESCE(excluded.role_name, group_members.role_name),
                tag=COALESCE(excluded.tag, group_members.tag),
                active=excluded.active,
                confirmed=excluded.confirmed,
                left_at=NULL,
                welcome_message_id=COALESCE(excluded.welcome_message_id, group_members.welcome_message_id),
                tag_set_by_bot=excluded.tag_set_by_bot
            """,
            (
                chat_id, user.id, user.first_name or "", user.last_name or "", user.username or "",
                role_key, role_name, tag, 1 if confirmed else 0, 1 if active else 0,
                now(), welcome_message_id, 1 if tag_set_by_bot else 0,
            ),
        )
        conn.commit()
    group_db_op(op)


def mark_member_left(chat_id, user_id):
    def op(conn):
        row = conn.execute("SELECT role_key, role_name, tag FROM group_members WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        conn.execute(
            "UPDATE group_members SET active=0, left_at=?, confirmed=0 WHERE chat_id=? AND user_id=?",
            (now(),chat_id,user_id),
        )
        if row and row["role_key"]:
            conn.execute(
                "UPDATE role_state SET user_id=NULL, bot_managed=0 WHERE chat_id=? AND role_key=? AND user_id=?",
                (chat_id,row["role_key"],user_id),
            )
        conn.commit()
        return dict(row) if row else None
    return group_db_op(op)


def confirm_member(chat_id, user_id):
    def op(conn):
        conn.execute(
            "UPDATE group_members SET confirmed=1, confirmed_at=? WHERE chat_id=? AND user_id=?",
            (now(),chat_id,user_id),
        )
        conn.commit()
    group_db_op(op)


def get_member(chat_id, user_id):
    return group_db_op(lambda conn: conn.execute("SELECT * FROM group_members WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone())


def find_group_member(chat_id, username):
    key = normalize_target_text(username)
    return group_db_op(lambda conn: conn.execute(
        "SELECT * FROM group_members WHERE chat_id=? AND lower(username)=? AND active=1 LIMIT 1", (chat_id,key)
    ).fetchone())


def latest_pending_member(chat_id):
    def op(conn):
        rows = conn.execute(
            """
            SELECT * FROM group_members
            WHERE chat_id=? AND active=1 AND confirmed=0
              AND joined_at >= ?
            ORDER BY joined_at DESC LIMIT 2
            """,
            (chat_id, (datetime.now(timezone.utc) - timedelta(seconds=ROLE_ASSIGNMENT_WINDOW_SECONDS)).isoformat()),
        ).fetchall()
        return rows[0] if len(rows) == 1 else None
    return group_db_op(op)


def role_is_occupied(chat_id, role_key, *, exclude_user_id=None):
    def op(conn):
        row = conn.execute(
            "SELECT * FROM role_state WHERE chat_id=? AND role_key=?", (chat_id,role_key)
        ).fetchone()
        if row and row["user_id"] is not None and row["user_id"] != exclude_user_id:
            return True
        if row and row["legacy_marker"] and not row["bot_managed"] and row["user_id"] is None:
            return True
        return False
    return group_db_op(op)


def assign_role_db(chat_id, user, role):
    role_key = normalize_role(role["name"])
    tag = make_tag(role["english"])
    def op(conn):
        state = conn.execute("SELECT * FROM role_state WHERE chat_id=? AND role_key=?", (chat_id,role_key)).fetchone()
        if state and state["user_id"] not in (None, user.id):
            raise ValueError("ROLE_OCCUPIED")
        old = conn.execute("SELECT role_key FROM group_members WHERE chat_id=? AND user_id=? AND active=1", (chat_id,user.id)).fetchone()
        if old and old["role_key"] and old["role_key"] != role_key:
            conn.execute("UPDATE role_state SET user_id=NULL, bot_managed=0 WHERE chat_id=? AND role_key=? AND user_id=?", (chat_id,old["role_key"],user.id))
        conn.execute(
            """
            INSERT INTO role_state(chat_id,role_key,role_name,user_id,legacy_marker,legacy_custom_emoji_id,bot_managed)
            VALUES(?,?,?,?,?, ?,1)
            ON CONFLICT(chat_id,role_key) DO UPDATE SET user_id=excluded.user_id, role_name=excluded.role_name, bot_managed=1
            """,
            (chat_id, role_key, role["name"], user.id, state["legacy_marker"] if state else "", state["legacy_custom_emoji_id"] if state else None),
        )
        conn.execute(
            """
            INSERT INTO group_members(chat_id,user_id,first_name,last_name,username,role_key,role_name,tag,confirmed,active,joined_at,tag_set_by_bot)
            VALUES(?,?,?,?,?,?,?,?,1,1,?,1)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET
                first_name=excluded.first_name,last_name=excluded.last_name,username=excluded.username,
                role_key=excluded.role_key,role_name=excluded.role_name,tag=excluded.tag,active=1,tag_set_by_bot=1
            """,
            (chat_id,user.id,user.first_name or "",user.last_name or "",user.username or "",role_key,role["name"],tag,now()),
        )
        conn.commit()
        return tag, role_key
    return group_db_op(op)



def split_role_line(line):
    """Return (role, marker) for roster lines like 'Диона -💛' or 'Сяо - 💛'."""
    raw = line or ""
    for ru, en, region in ROLE_CATALOG:
        pattern = rf"^\s*{re.escape(ru)}\s*-\s*(.*)$"
        m = re.match(pattern, raw, flags=re.IGNORECASE)
        if m:
            return role_for(ru), m.group(1).strip()
    return None, None

def store_roster_source(chat_id, slot, message):
    custom = []
    source_text = message.text or ""
    line_starts = [0]
    acc = 0
    for line in source_text.splitlines(True):
        acc += len(line.encode("utf-16-le")) // 2
        line_starts.append(acc)
    for ent in message.entities or []:
        if getattr(ent, "type", None) == "custom_emoji" and getattr(ent, "custom_emoji_id", None):
            char = utf16_slice(source_text, ent.offset, ent.length)
            line_index = 0
            for idx in range(len(line_starts) - 1):
                if line_starts[idx] <= ent.offset < line_starts[idx + 1]:
                    line_index = idx
                    break
            local_offset = ent.offset - line_starts[line_index]
            custom.append({
                "line": line_index,
                "offset": local_offset,
                "char": char,
                "custom_emoji_id": ent.custom_emoji_id,
                "length": ent.length,
            })
    def op(conn):
        conn.execute(
            """
            INSERT INTO roster_sources(chat_id,slot,message_id,source_text,source_custom_emoji,captured_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(chat_id,slot) DO UPDATE SET
                message_id=excluded.message_id,source_text=excluded.source_text,
                source_custom_emoji=excluded.source_custom_emoji,captured_at=excluded.captured_at
            """,
            (chat_id,slot,message.message_id,message.text or "",json.dumps(custom,ensure_ascii=False),now()),
        )
        linked = conn.execute(
            "SELECT group_chat_id FROM managed_group WHERE info_channel_id=? ORDER BY bound_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        state_chat_id = linked["group_chat_id"] if linked and linked["group_chat_id"] else chat_id
        # Initialize role_state legacy markers for roles not yet managed by bot.
        lines = (message.text or "").splitlines()
        custom_values = list(custom)
        for line in lines:
            role, marker = split_role_line(line)
            if not role:
                continue
            existing = conn.execute("SELECT * FROM role_state WHERE chat_id=? AND role_key=?", (state_chat_id,normalize_role(role["name"]))).fetchone()
            if not existing:
                emoji_id = None
                for item in custom_values:
                    if item.get("char") == marker:
                        emoji_id = item.get("custom_emoji_id")
                        break
                conn.execute(
                    "INSERT INTO role_state(chat_id,role_key,role_name,user_id,legacy_marker,legacy_custom_emoji_id,bot_managed) VALUES(?,?,?,?,?,?,0)",
                    (state_chat_id,normalize_role(role["name"]),role["name"],None,marker,emoji_id),
                )
        conn.commit()
    group_db_op(op)


def roster_rows_for(chat_id):
    return group_db_op(lambda conn: conn.execute("SELECT * FROM role_state WHERE chat_id=?", (chat_id,)).fetchall())


def roster_sources(chat_id):
    return group_db_op(lambda conn: conn.execute("SELECT * FROM roster_sources WHERE chat_id=? ORDER BY slot", (chat_id,)).fetchall())


def _custom_emoji_entity(offset_utf16, char, custom_emoji_id):
    try:
        length = len(char.encode("utf-16-le")) // 2
    except Exception:
        length = len(char)
    return MessageEntity(
        type="custom_emoji",
        offset=offset_utf16,
        length=length,
        custom_emoji_id=custom_emoji_id,
    )


def render_roster_with_entities(text, role_states, source_custom_emoji=None):
    """Render the roster while preserving/reapplying captured Telegram custom-emoji IDs."""
    state_by_key = {row["role_key"]: row for row in role_states}
    source_entities = source_custom_emoji or []
    lines = []
    entities = []
    line_infos = []
    default_custom = None
    for row in role_states:
        if row["legacy_marker"] == DEFAULT_OCCUPIED_MARKER and row["legacy_custom_emoji_id"]:
            default_custom = row["legacy_custom_emoji_id"]
            break
    if default_custom is None:
        for item in source_entities:
            if item.get("char") == DEFAULT_OCCUPIED_MARKER and item.get("custom_emoji_id"):
                default_custom = item["custom_emoji_id"]
                break

    for idx, line in enumerate((text or "").splitlines()):
        role = None
        marker = ""
        custom_marker_id = None
        left = None
        role, original_marker = split_role_line(line)
        if role:
            left = role["name"]
        if role:
            st = state_by_key.get(normalize_role(role["name"]))
            if st:
                if st["user_id"] is not None:
                    marker = st["legacy_marker"] or DEFAULT_OCCUPIED_MARKER
                    custom_marker_id = st["legacy_custom_emoji_id"] or default_custom
                elif st["legacy_marker"] and not st["bot_managed"]:
                    marker = st["legacy_marker"]
                    custom_marker_id = st["legacy_custom_emoji_id"]
            rendered_line = f"{left} - {marker}".rstrip()
        else:
            rendered_line = line
        line_infos.append((idx, line, rendered_line, role, marker, custom_marker_id))
        lines.append(rendered_line)

    # Calculate UTF-16 start offsets for rendered lines.
    rendered_starts = []
    acc = 0
    for line in lines:
        rendered_starts.append(acc)
        acc += len(line.encode("utf-16-le")) // 2 + 1

    # Reapply every captured custom emoji. Marker entities are moved to the new marker position.
    for item in source_entities:
        try:
            idx = int(item.get("line", 0))
            if idx >= len(line_infos):
                continue
            _old_idx, old_line, new_line, role, marker, custom_marker_id = line_infos[idx]
            entity_id = item.get("custom_emoji_id")
            char = item.get("char", "")
            if role and marker and custom_marker_id and char.strip() in {"💛", "🧡", "💚"}:
                pos = new_line.rfind(marker)
                if pos >= 0:
                    offset = rendered_starts[idx] + len(new_line[:pos].encode("utf-16-le")) // 2
                    entities.append(_custom_emoji_entity(offset, marker, custom_marker_id))
                    continue
            local = int(item.get("offset", 0))
            new_len = int(item.get("length", 1))
            # Keep non-marker custom emoji at the same line-local position when possible.
            if local + new_len <= len(old_line.encode("utf-16-le")) // 2:
                # Convert UTF-16 local offset to Python string position approximately by decoding prefix bytes.
                prefix_bytes = old_line.encode("utf-16-le")[:local * 2]
                prefix = prefix_bytes.decode("utf-16-le", errors="ignore")
                candidate = rendered_starts[idx] + len(prefix.encode("utf-16-le")) // 2
                visible = utf16_slice(new_line, len(prefix.encode("utf-16-le")) // 2, new_len)
                if visible:
                    entities.append(_custom_emoji_entity(candidate, visible, entity_id))
        except Exception:
            logger.exception("Failed to rebuild custom emoji entity")

    # Deduplicate exact entities.
    unique = []
    seen = set()
    for ent in entities:
        key = (ent.offset, ent.length, ent.custom_emoji_id)
        if key not in seen:
            seen.add(key)
            unique.append(ent)
    return "\n".join(lines), unique


async def update_group_roster(chat_id):
    cfg = get_managed_info_for_group(chat_id)
    if not cfg or not cfg["info_channel_id"]:
        return
    sources = roster_sources(cfg["info_channel_id"])
    states = roster_rows_for(chat_id)
    for src in sources:
        rendered, entities = render_roster_with_entities(src["source_text"], states, json.loads(src["source_custom_emoji"] or "[]"))
        try:
            await bot.edit_message_text(
                chat_id=cfg["info_channel_id"],
                message_id=src["message_id"],
                text=rendered,
                entities=entities or None,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning(
                    "Roster update failed | channel=%s | message=%s | %s",
                    cfg["info_channel_id"],
                    src["message_id"],
                    exc,
                )
        except Exception:
            logger.exception("Unexpected roster update error")


def find_target_from_kall(message):
    argument = parse_kall(message.text or "")
    if not argument:
        return None, None
    reply = message.reply_to_message
    if reply and reply.from_user:
        role = role_for(argument)
        return reply.from_user, role
    parts = argument.split()
    # @username role...
    if parts and parts[0].startswith("@"):
        user = get_user_by_username(parts[0])
        role = role_for(" ".join(parts[1:]))
        if user and role:
            class U:
                pass
            u = U(); u.id = user["user_id"]; u.first_name = user["first_name"]; u.last_name = user["last_name"]; u.username = user["username"]
            return u, role
    # role @username
    if parts and parts[-1].startswith("@"):
        user = get_user_by_username(parts[-1])
        role = role_for(" ".join(parts[:-1]))
        if user and role:
            class U:
                pass
            u = U(); u.id = user["user_id"]; u.first_name = user["first_name"]; u.last_name = user["last_name"]; u.username = user["username"]
            return u, role
    role = role_for(argument)
    if role:
        pending = latest_pending_member(message.chat.id)
        if pending:
            class U:
                pass
            u = U(); u.id = pending["user_id"]; u.first_name = pending["first_name"]; u.last_name = pending["last_name"]; u.username = pending["username"]
            return u, role
    return None, None


def set_group_member_tag_db(chat_id, user_id, actual_tag):
    def op(conn):
        conn.execute("UPDATE group_members SET tag=? WHERE chat_id=? AND user_id=?", (actual_tag,chat_id,user_id))
        conn.commit()
    group_db_op(op)


async def apply_member_tag(chat_id, user_id, desired_tag):
    try:
        ok = await bot.set_chat_member_tag(chat_id, user_id, tag=desired_tag)
        return bool(ok), desired_tag
    except TelegramBadRequest as exc:
        logger.warning("Could not set requested tag '%s' for %s: %s; trying safe fallback", desired_tag, user_id, exc)
        safe = desired_tag.replace("❦", "")[:16]
        if safe != desired_tag:
            try:
                ok = await bot.set_chat_member_tag(chat_id, user_id, tag=safe)
                return bool(ok), safe
            except Exception:
                logger.exception("Safe fallback tag also failed | chat=%s | user=%s", chat_id, user_id)
        return False, None


async def send_or_edit_welcome(chat_id, user_id):
    row = get_member(chat_id, user_id)
    if not row:
        return
    display = (row["username"] and "@" + row["username"]) or row["first_name"] or "участник"
    if row["role_name"] and row["tag"]:
        role_text = f"{row['tag']}\n{row['role_name']} ({role_for(row['role_name'])['english'] if role_for(row['role_name']) else ''})"
        button_text = "✅ Подтвердить"
    else:
        role_text = "⏳ Роль ещё не назначена администрацией.\n\nПосле назначения роли бот автоматически обновит это сообщение и установит Telegram-тег."
        button_text = "⏳ Ожидается роль"
    text = (
        f"{title('Добро пожаловать')}\n\n"
        f"Привет, {display}! 🤍\n\n"
        f"Входя в чат, вы обязуетесь соблюдать правила сообщества, уважать других участников и не нарушать комфорт общения.\n\n"
        f"{section('Ваша роль')}\n{role_text}\n\n"
        f"{divider()}\n"
        f"Нажмите кнопку подтверждения после ознакомления с правилами."
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button_text, callback_data=f"fm:confirm:{chat_id}:{user_id}")]])
    if row["welcome_message_id"]:
        try:
            await bot.edit_message_text(chat_id=chat_id,message_id=row["welcome_message_id"],text=text,reply_markup=markup)
            return
        except Exception:
            pass
    sent = await bot.send_message(chat_id, text, reply_markup=markup)
    def op(conn):
        conn.execute("UPDATE group_members SET welcome_message_id=? WHERE chat_id=? AND user_id=?", (sent.message_id,chat_id,user_id)); conn.commit()
    group_db_op(op)


@dp.chat_member()
async def group_member_update(event: ChatMemberUpdated):
    global last_polling_activity
    last_polling_activity = datetime.now(timezone.utc)
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    user = event.new_chat_member.user
    chat_id = event.chat.id

    if old_status in LEAVE_STATUSES and new_status in JOIN_ACTIVE_STATUSES:
        register_user(user)
        bind_group(event.chat, event.chat.title or "")
        existing = get_member(chat_id, user.id)
        # Reactivate member row; previous role is restored only if it is free.
        prior_role = existing["role_key"] if existing and existing["role_key"] else None
        prior_role_meta = role_for(prior_role) if prior_role else None
        upsert_group_member(chat_id, user, active=True, role_key=None, role_name=None, tag=None, confirmed=False, welcome_message_id=None, tag_set_by_bot=False)
        restored = False
        if prior_role_meta and not role_is_occupied(chat_id, prior_role_meta["name"]):
            try:
                tag = make_tag(prior_role_meta["english"])
                ok, actual = await apply_member_tag(chat_id,user.id,tag)
                if ok:
                    assign_role_db(chat_id,user,prior_role_meta)
                    set_group_member_tag_db(chat_id, user.id, actual)
                    restored = True
            except Exception:
                logger.exception("Could not restore previous role | chat=%s | user=%s", chat_id,user.id)
        # Restrict until confirmation. The role assignment command can happen immediately afterwards.
        try:
            await bot.restrict_chat_member(
                chat_id, user.id,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_audios=False,
                    can_send_documents=False,
                    can_send_photos=False,
                    can_send_videos=False,
                    can_send_video_notes=False,
                    can_send_voice_notes=False,
                    can_send_polls=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False,
                    can_change_info=False,
                    can_invite_users=False,
                    can_pin_messages=False,
                    can_manage_topics=False,
                ),
                use_independent_chat_permissions=True,
            )
        except Exception:
            logger.exception("Could not restrict new member | chat=%s | user=%s", chat_id,user.id)
        await send_or_edit_welcome(chat_id,user.id)
        if restored:
            await update_group_roster(chat_id)
        return

    if old_status not in LEAVE_STATUSES and new_status in LEAVE_STATUSES:
        old = mark_member_left(chat_id,user.id)
        role_text = (old or {}).get("tag") or (old or {}).get("role_name") or "роль не назначена"
        username = f"@{user.username}" if user.username else user.first_name or str(user.id)
        try:
            await bot.send_message(ADMIN_ID, f"{ADMIN_MENTION}\n\n🚪 Участник вышел из чата\n\n{username}\nID: {user.id}\nРоль: {role_text}")
        except Exception:
            logger.exception("Could not notify admin about member leaving | user=%s",user.id)
        await update_group_roster(chat_id)


@dp.callback_query(F.data.startswith("fm:confirm:"))
async def group_confirm(callback: CallbackQuery):
    try:
        _, _, chat_raw, user_raw = (callback.data or "").split(":", 3)
        chat_id = int(chat_raw); target_user_id = int(user_raw)
    except Exception:
        await callback.answer("Некорректное подтверждение.", show_alert=True); return
    if callback.from_user.id != target_user_id:
        await callback.answer("Эта кнопка предназначена для другого участника.", show_alert=True); return
    row = get_member(chat_id,target_user_id)
    if not row or not row["active"]:
        await callback.answer("Вы уже не являетесь участником этого чата.", show_alert=True); return
    if not row["role_key"]:
        await callback.answer("Сначала дождитесь назначения роли администратором.", show_alert=True); return
    try:
        await bot.restrict_chat_member(
            chat_id,target_user_id,
            permissions=ChatPermissions(
                can_send_messages=True,can_send_audios=True,can_send_documents=True,can_send_photos=True,
                can_send_videos=True,can_send_video_notes=True,can_send_voice_notes=True,can_send_polls=True,
                can_send_other_messages=True,can_add_web_page_previews=True,can_change_info=False,
                can_invite_users=True,can_pin_messages=False,can_manage_topics=False,
            ),
            use_independent_chat_permissions=True,
        )
        confirm_member(chat_id,target_user_id)
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Подтверждено", callback_data=f"fm:confirmed:{chat_id}:{target_user_id}")]])
        await callback.message.edit_reply_markup(reply_markup=markup)
        await callback.answer("Правила подтверждены.")
    except Exception:
        logger.exception("Could not confirm group member | chat=%s | user=%s",chat_id,target_user_id)
        await callback.answer("Не удалось завершить подтверждение. Попробуйте ещё раз.", show_alert=True)


@dp.message(F.text.regexp(r"^\s*калл\b"))
async def group_role_command(message: Message):
    if message.chat.type not in {"group","supergroup"}:
        return
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    user, role = find_target_from_kall(message)
    if not role or not user:
        await message.reply("Не нашёл пользователя или роль.\n\nПример: калл Чжун Ли\nИли ответьте на сообщение участника: калл Чжун Ли\nИли: калл @username Чжун Ли")
        return
    try:
        if role_is_occupied(message.chat.id, normalize_role(role["name"]), exclude_user_id=user.id):
            await message.reply(f"Роль «{role['name']}» уже занята.")
            return
        tag = make_tag(role["english"])
        ok, actual = await apply_member_tag(message.chat.id,user.id,tag)
        if not ok:
            await message.reply(f"Не удалось установить тег для {display_username_for_group(user)}. Проверьте право Manage Tags.")
            return
        assign_role_db(message.chat.id,user,role)
        set_group_member_tag_db(message.chat.id, user.id, actual)
        await send_or_edit_welcome(message.chat.id,user.id)
        await update_group_roster(message.chat.id)
        await message.reply(f"✅ Назначено\n{display_username_for_group(user)}\nРоль: {role['name']}\nТег: {actual}")
    except ValueError as exc:
        if str(exc) == "ROLE_OCCUPIED":
            await message.reply(f"Роль «{role['name']}» уже занята.")
        else:
            raise
    except Exception:
        logger.exception("Group role assignment failed")
        await message.reply("Не удалось назначить роль. Подробность записана в лог.")


def display_username_for_group(user):
    return f"@{user.username}" if getattr(user,"username",None) else getattr(user,"first_name",str(user.id))


@dp.message(Command("bind_group"))
async def bind_group_cmd(message: Message):
    if message.chat.type not in {"group","supergroup"} or not message.from_user or message.from_user.id != ADMIN_ID:
        return
    bind_group(message.chat, message.chat.title or "")
    await message.reply(f"✅ Группа привязана.\nChat ID: {message.chat.id}")


@dp.message(Command("bind_info"))
async def bind_info_from_group(message: Message):
    if message.chat.type not in {"group","supergroup"} or not message.from_user or message.from_user.id != ADMIN_ID:
        return
    await message.reply("Для инфоканала используйте /bind_info прямо в самом канале.")


@dp.channel_post(Command("bind_info"))
async def bind_info_channel(message: Message):
    # For channel posts, verify that the configured owner is an administrator of this channel.
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
        if not any(a.user.id == ADMIN_ID for a in admins):
            return
    except Exception:
        logger.exception("Could not verify info-channel admin")
        return
    group = bind_info_channel_db(message.chat.id)
    await message.answer("✅ Инфоканал привязан." if hasattr(message,"answer") else None)


@dp.message(Command("id"))
async def ids_group(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    reply_id = message.reply_to_message.message_id if message.reply_to_message else "—"
    await message.reply(f"🆔 Chat ID: {message.chat.id}\nMessage ID (если ответ): {reply_id}\nВаш ID: {message.from_user.id}")


async def capture_list_from_message(message: Message, slot: str, source: Message):
    if source.chat.type != "channel":
        await message.reply("Нужно отвечать командой на сообщение именно в инфоканале.")
        return
    store_roster_source(source.chat.id, slot, source)
    await message.reply(f"✅ Список {slot} сохранён.\nMessage ID: {source.message_id}\nCustom emoji сущностей: {len([e for e in (source.entities or []) if getattr(e,'type',None)=='custom_emoji'])}")


@dp.channel_post(Command("capture_list"))
async def capture_list_channel(message: Message):
    try:
        admins = await bot.get_chat_administrators(message.chat.id)
        if not any(a.user.id == ADMIN_ID for a in admins):
            return
    except Exception:
        logger.exception("Could not verify channel admin for capture_list")
        return
    if not message.reply_to_message:
        await message.answer("Ответьте /capture_list 40 или /capture_list 41 на нужное сообщение списка.")
        return
    parts = (message.text or "").split()
    slot = parts[1] if len(parts) > 1 else "40"
    await capture_list_from_message(message,slot,message.reply_to_message)


@dp.message(Command("capture_list"))
async def capture_list_group(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    if not message.reply_to_message:
        await message.reply("Ответьте этой командой на сообщение, которое нужно сохранить.")
        return
    if message.reply_to_message.chat.type != "channel":
        await message.reply("Сообщение-источник должно находиться в инфоканале.")
        return
    await capture_list_from_message(message,(message.text or "/capture_list 40").split()[1],message.reply_to_message)


@dp.message(Command("sync_list"))
async def sync_list_cmd(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    group = get_managed_group()
    if not group:
        await message.reply("Сначала привяжите тестовую группу через /bind_group.")
        return
    await update_group_roster(group["group_chat_id"])
    await message.reply("✅ Список синхронизирован.")


@dp.message(Command("roles"))
async def role_list_cmd(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    await message.reply(f"Зарегистрировано ролей: {len(ROLE_CATALOG)}\nПример: калл Чжун Ли\nТег: {make_tag('Zhongli')}")


@dp.message(Command("pending"))
async def pending_cmd(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID or message.chat.type not in {"group","supergroup"}:
        return
    rows = group_db_op(lambda conn: conn.execute("SELECT * FROM group_members WHERE chat_id=? AND active=1 AND confirmed=0 ORDER BY joined_at DESC LIMIT 20",(message.chat.id,)).fetchall())
    if not rows:
        await message.reply("Ожидающих подтверждения нет.")
        return
    lines = []
    for row in rows:
        who = "@" + row["username"] if row["username"] else row["first_name"] or str(row["user_id"])
        lines.append(f"• {who} | {row['user_id']} | {row['role_name'] or 'роль не назначена'}")
    await message.reply("\n".join(lines))


@dp.message(Command("release"))
async def release_role_cmd(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID or message.chat.type not in {"group","supergroup"}:
        return
    argument = (message.text or "").split(maxsplit=1)
    if len(argument) < 2:
        await message.reply("Пример: /release Навия")
        return
    role = role_for(argument[1])
    if not role:
        await message.reply("Такой роли нет в каталоге.")
        return
    key = normalize_role(role["name"])
    def op(conn):
        conn.execute("UPDATE role_state SET user_id=NULL, bot_managed=0, legacy_marker='' WHERE chat_id=? AND role_key=?", (message.chat.id,key))
        conn.execute("UPDATE group_members SET role_key=NULL, role_name=NULL, tag=NULL, tag_set_by_bot=0 WHERE chat_id=? AND role_key=? AND active=1", (message.chat.id,key))
        conn.commit()
    group_db_op(op)
    await update_group_roster(message.chat.id)
    await message.reply(f"✅ Роль «{role['name']}» освобождена.")



@dp.message(Command("member"))
async def member_info_cmd(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID or message.chat.type not in {"group","supergroup"}:
        return
    target = message.reply_to_message.from_user if message.reply_to_message and message.reply_to_message.from_user else None
    if not target:
        parts = (message.text or "").split()
        if len(parts) == 2 and parts[1].startswith("@"):
            row = find_group_member(message.chat.id, parts[1])
        else:
            row = None
    else:
        row = get_member(message.chat.id, target.id)
    if not row:
        await message.reply("Участник не найден в журнале.")
        return
    await message.reply(
        f"👤 {('@'+row['username']) if row['username'] else row['first_name']}\n"
        f"ID: {row['user_id']}\n"
        f"Роль: {row['role_name'] or '—'}\n"
        f"Тег: {row['tag'] or '—'}\n"
        f"Подтверждение: {'да' if row['confirmed'] else 'нет'}\n"
        f"Активен: {'да' if row['active'] else 'нет'}"
    )


# =========================================================
# COMMANDS
# =========================================================

async def setup_commands():

    user_commands = [
        BotCommand(
            command="start",
            description="Открыть меню",
        ),
        BotCommand(
            command="cancel",
            description="Отменить действие",
        ),
    ]

    admin_commands = [
        BotCommand(
            command="start",
            description="Открыть меню",
        ),
        BotCommand(
            command="cancel",
            description="Отменить действие",
        ),
        BotCommand(
            command="admin",
            description="Панель администратора",
        ),
        BotCommand(command="id", description="Показать ID чата/сообщения"),
        BotCommand(command="roles", description="Список ролей"),
    ]

    await bot.set_my_commands(
        user_commands,
        scope=BotCommandScopeDefault(),
    )

    await bot.set_my_commands(
        admin_commands,
        scope=BotCommandScopeChat(
            chat_id=ADMIN_ID,
        ),
    )

    logger.info(
        "Commands configured."
    )


# =========================================================
# RENDER HTTP
# =========================================================

async def health(
    request: web.Request,
):
    try:
        db_ok = db_transaction(
            lambda conn: conn.execute("SELECT 1").fetchone() is not None
        )
    except Exception:
        db_ok = False

    return web.json_response(
        {
            "polling_running": bool(polling_running),
            "last_polling_activity": (
                last_polling_activity.isoformat()
                if last_polling_activity
                else None
            ),
            "delivery_queue_size": (
                DELIVERY_QUEUE.qsize() if DELIVERY_QUEUE is not None else 0
            ),
            "broadcast_queue_size": (
                BROADCAST_QUEUE.qsize() if BROADCAST_QUEUE is not None else 0
            ),
            "db_ok": bool(db_ok),
            "fatal_error": fatal_error,
        }
    )


async def start_http_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health,
    )

    app.router.add_get(
        "/health",
        health,
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    logger.info(
        "HTTP server started on port %s",
        port,
    )

    return runner


# =========================================================
# POLLING
# =========================================================

async def polling_loop():
    global polling_running, last_polling_activity, fatal_error

    logger.info("Starting polling...")
    polling_running = True
    last_polling_activity = datetime.now(timezone.utc)
    backoff = 1

    try:
        while not fatal_error:
            try:
                await dp.start_polling(
                    bot,
                    allowed_updates=dp.resolve_used_update_types(),
                    handle_signals=True,
                    close_bot_session=False,
                )
                if fatal_error:
                    break
                logger.warning(
                    "Polling stopped normally; restarting in %ss",
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except TelegramUnauthorizedError:
                fatal_error = "unauthorized"
                if FATAL_EVENT is not None:
                    FATAL_EVENT.set()
                raise
            except TelegramConflictError:
                fatal_error = "conflict"
                if FATAL_EVENT is not None:
                    FATAL_EVENT.set()
                raise
            except TelegramNetworkError:
                logger.exception(
                    "Telegram network error in polling; restarting in %ss",
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception:
                logger.exception(
                    "Polling crashed; restarting in %ss",
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
    finally:
        polling_running = False


# =========================================================
# MAIN
# =========================================================

async def main():
    global DELIVERY_QUEUE, BROADCAST_QUEUE, FATAL_EVENT

    logger.info("Starting Anonymous Feedback Bot...")

    init_db()
    migrate_db()
    init_group_db()

    DELIVERY_QUEUE = asyncio.Queue()
    BROADCAST_QUEUE = asyncio.Queue()
    FATAL_EVENT = asyncio.Event()

    logger.info("SQLite initialized: %s", DB_PATH)

    try:
        me = await bot.get_me()
        logger.info(
            "Telegram bot connected | id=%s | username=@%s",
            me.id,
            me.username or "unknown",
        )
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("Old Telegram webhook removed.")
    except TelegramUnauthorizedError:
        logger.critical("BOT_TOKEN is invalid or rejected by Telegram")
        raise
    except Exception:
        logger.exception("Could not remove old Telegram webhook")
        raise

    try:
        await setup_commands()
    except TelegramUnauthorizedError:
        logger.critical("BOT_TOKEN is invalid while configuring commands")
        raise
    except Exception:
        logger.exception("Could not configure commands; continuing")

    http_runner = await start_http_server()

    delivery_tasks = [
        asyncio.create_task(delivery_worker(i + 1), name=f"delivery-{i + 1}")
        for i in range(DELIVERY_WORKERS)
    ]
    broadcast_tasks = [
        asyncio.create_task(broadcast_worker(i + 1), name=f"broadcast-{i + 1}")
        for i in range(BROADCAST_WORKERS)
    ]

    await enqueue_pending_notifications()

    polling_task = asyncio.create_task(polling_loop(), name="telegram-polling")
    fatal_task = asyncio.create_task(FATAL_EVENT.wait(), name="fatal-error-watcher")

    try:
        done, _ = await asyncio.wait(
            {polling_task, fatal_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if fatal_task in done and fatal_error:
            logger.critical("Fatal bot error: %s", fatal_error)
            polling_task.cancel()
            with suppress(asyncio.CancelledError):
                await polling_task
            raise RuntimeError(f"Fatal Telegram error: {fatal_error}")

        if polling_task in done:
            exc = polling_task.exception()
            if exc:
                raise exc
    finally:
        for task in delivery_tasks + broadcast_tasks:
            task.cancel()
        for task in delivery_tasks + broadcast_tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task

        if not polling_task.done():
            polling_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await polling_task

        if not fatal_task.done():
            fatal_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await fatal_task

        logger.info("Stopping bot...")
        with suppress(Exception):
            await dp.stop_polling()
        with suppress(Exception):
            await http_runner.cleanup()
        with suppress(Exception):
            await bot.session.close()


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")


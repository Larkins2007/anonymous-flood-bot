import asyncio
import json
import html
import logging
import os
import re
import signal
import sqlite3
import sys
import threading
import traceback
import unicodedata
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
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllChatAdministrators,
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
PRIMARY_CHAT_ID = int(os.getenv("PRIMARY_CHAT_ID", "-1004313546398") or "-1004313546398")

DB_PATH = os.getenv("DB_PATH", "users.db").strip() or "users.db"
PORT = int(os.getenv("PORT", "10000"))

REPORT_COOLDOWN_SECONDS = 600
MAX_MESSAGE_LENGTH = 4000
MAX_REPORT_REASON_LENGTH = 2000
TELEGRAM_TEXT_LIMIT = 3500
BROADCAST_DELAY_SECONDS = float(os.getenv("BROADCAST_DELAY_SECONDS", "0.20"))
BROADCAST_MAX_RETRIES = int(os.getenv("BROADCAST_MAX_RETRIES", "3"))
GAME_POLL_MAX_MINUTES = int(os.getenv("GAME_POLL_MAX_MINUTES", "10080"))
GAME_POLL_OPTIONS_PER_ROW = 2
SCHEDULE_ANCHOR_DATE = os.getenv("SCHEDULE_ANCHOR_DATE", "2026-08-18")
SCHEDULE_CHECK_SECONDS = int(os.getenv("SCHEDULE_CHECK_SECONDS", "30"))
DEFAULT_TIMEZONE_OFFSET_HOURS = int(os.getenv("DEFAULT_TIMEZONE_OFFSET_HOURS", "3"))
BOT_USERNAME = os.getenv("BOT_USERNAME", "justice_faite_bot").strip().lstrip("@")


# =========================================================
# FLOOD / GROUP MODERATION CONFIG
# =========================================================

ADMIN_MENTION = os.getenv("ADMIN_MENTION", "@Belochki_Rulyat")
GROUP_WELCOME_TIMEOUT = int(os.getenv("GROUP_WELCOME_TIMEOUT", "0"))
DEFAULT_OCCUPIED_MARKER = os.getenv("DEFAULT_OCCUPIED_MARKER", "💛")
ROLE_ASSIGNMENT_WINDOW_SECONDS = int(os.getenv("ROLE_ASSIGNMENT_WINDOW_SECONDS", "600"))

NEW_MEMBER_RESTRICTION = ChatPermissions(can_send_messages=False)
MEMBER_ACTIVE_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)



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

DEBUG_GROUP_UPDATES = os.getenv("DEBUG_GROUP_UPDATES", "0") == "1"

def has_bot_command_entity(message: Message) -> bool:
    return any(getattr(e, "type", None) == "bot_command" for e in (getattr(message, "entities", None) or []))

def extract_command_from_message(message: Message) -> Optional[str]:
    text = getattr(message, "text", None) or ""
    for e in (getattr(message, "entities", None) or []):
        if getattr(e, "type", None) == "bot_command":
            try:
                return text[e.offset:e.offset + e.length]
            except Exception:
                return None
    return None

DELIVERY_PART_DELAY = max(0.0, float(os.getenv("DELIVERY_PART_DELAY", "0.2")))

polling_running = False
last_polling_activity = None
fatal_error = None
FATAL_EVENT = None
NOTIFICATION_LOCKS = {}
MEMBER_TAG_SYNC_CACHE = {}
MEMBER_TAG_SYNC_TTL_SECONDS = int(os.getenv("MEMBER_TAG_SYNC_TTL_SECONDS", "45"))

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


class RoleAssignSearchState(StatesGroup):
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

            CREATE TABLE IF NOT EXISTS participant_roster (
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                user_id INTEGER,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'seed',
                PRIMARY KEY(chat_id, username)
            );

            CREATE INDEX IF NOT EXISTS idx_participant_roster_user
                ON participant_roster(chat_id, user_id);

            CREATE TABLE IF NOT EXISTS role_state (
                chat_id INTEGER NOT NULL,
                role_key TEXT NOT NULL,
                role_name TEXT NOT NULL,
                user_id INTEGER,
                status TEXT NOT NULL DEFAULT 'free',
                legacy_marker TEXT NOT NULL DEFAULT '',
                legacy_custom_emoji_id TEXT,
                bot_managed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, role_key)
            );

            CREATE INDEX IF NOT EXISTS idx_role_state_user
                ON role_state(chat_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_role_state_status
                ON role_state(chat_id, status);

            CREATE TABLE IF NOT EXISTS mafia_bans (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                banned_by INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS mafia_games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'WAITING',
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_mafia_open_game
                ON mafia_games(chat_id)
                WHERE status = 'WAITING';

            CREATE TABLE IF NOT EXISTS mafia_players (
                game_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                first_name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                role TEXT,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (game_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS custom_commands (
                command TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT '',
                response TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT 'all',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                created_by INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_catalog (
                role_key TEXT PRIMARY KEY,
                role_name TEXT NOT NULL,
                english_name TEXT NOT NULL,
                region TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_key TEXT NOT NULL,
                role_name TEXT NOT NULL,
                tag TEXT NOT NULL DEFAULT '',
                event TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_role_history_chat_user
                ON role_history(chat_id, user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_role_history_chat_role
                ON role_history(chat_id, role_key, created_at);

            CREATE TABLE IF NOT EXISTS role_snapshot_state (
                chat_id INTEGER PRIMARY KEY,
                owner_message_id INTEGER,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS game_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                launch_text TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                created_by INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS game_polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'OPEN', winner_game_id INTEGER,
                options_json TEXT NOT NULL DEFAULT '', tie_round INTEGER NOT NULL DEFAULT 0,
                parent_poll_id INTEGER
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_open_game_poll_chat ON game_polls(chat_id) WHERE status='OPEN';
            CREATE TABLE IF NOT EXISTS game_poll_votes (
                poll_id INTEGER NOT NULL, user_id INTEGER NOT NULL, game_id INTEGER NOT NULL, voted_at TEXT NOT NULL,
                PRIMARY KEY(poll_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS weekly_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, weekday INTEGER NOT NULL,
                time_hm TEXT NOT NULL, game_name TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                UNIQUE(chat_id,weekday,time_hm)
            );
            CREATE TABLE IF NOT EXISTS weekly_schedule_messages (
                chat_id INTEGER PRIMARY KEY, message_id INTEGER, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS game_schedule_overrides (
                chat_id INTEGER NOT NULL, run_date TEXT NOT NULL, time_hm TEXT NOT NULL,
                game_name TEXT NOT NULL, source_poll_id INTEGER NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, run_date, time_hm)
            );
            CREATE TABLE IF NOT EXISTS schedule_runs (
                chat_id INTEGER NOT NULL, run_date TEXT NOT NULL, time_hm TEXT NOT NULL,
                game_name TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, run_date, time_hm)
            );
            CREATE TABLE IF NOT EXISTS spy_games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL, created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL, started_at TEXT, expires_at TEXT,
                status TEXT NOT NULL DEFAULT 'WAITING',
                lobby_message_id INTEGER,
                location TEXT NOT NULL DEFAULT '',
                spy_user_id INTEGER,
                current_asker_id INTEGER, current_target_id INTEGER,
                phase TEXT NOT NULL DEFAULT 'LOBBY',
                accusation_suspect_id INTEGER,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_spy_games_chat_status ON spy_games(chat_id,status);
            CREATE TABLE IF NOT EXISTS spy_players (
                game_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '', display_name TEXT NOT NULL DEFAULT '',
                joined_at TEXT NOT NULL, role_name TEXT NOT NULL DEFAULT '',
                is_spy INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, dm_ready INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(game_id,user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_spy_players_game ON spy_players(game_id);
            CREATE TABLE IF NOT EXISTS spy_votes (
                game_id INTEGER NOT NULL, voter_id INTEGER NOT NULL,
                suspect_id INTEGER NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(game_id,voter_id)
            );
            CREATE TABLE IF NOT EXISTS schedule_cycle (
                chat_id INTEGER NOT NULL, slot_index INTEGER NOT NULL, time_hm TEXT NOT NULL,
                game_name TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(chat_id, slot_index)
            );
            """
        )
        conn.commit()
    db_transaction(op)



INITIAL_PARTICIPANT_USERNAMES = ['TymKait', 'Snickers_oblachko_uwu', 'Istilllove_you', 'Nowstaru0', 'RisenDemon0928', 'hernaave', 'gogokitty', 'Dain_your_sleif', 'Chuuya_Nakahar', 'ekaterinze01', 'whd_hno', 'wiaxic', 'ahbty_75', 'Mommy_mua', 'Ggggyro', 'icenwi1l', 'mew_mewmew_mew', 'stheyess', 'munsin', 'Gingr_S', 'lapushok_0', 'Catic238', 'Shinshila_906', 'Aleksa_Dragon', 'Graciial', 'sleppem', 'Huipletikk', 'youpieyupie88', 'sihikamori', 'giwigiwigi', 'zachem_te', 'The_goldmoor_vale', 'pu_pu_puuu', 'BbnotS_3', 'kitm1z', 'dilucoox', 'cherry_cat_meow', 'Satanfathers', 'LingWenkinney', 're1nnieл']

def seed_participant_roster():
    now_ts = now()
    def op(conn):
        for username in INITIAL_PARTICIPANT_USERNAMES:
            if not username:
                continue
            conn.execute(
                """INSERT INTO participant_roster(chat_id,username,user_id,first_seen,last_seen,source)
                   VALUES(?,?,NULL,?,?,?)
                   ON CONFLICT(chat_id,username) DO NOTHING""",
                (PRIMARY_CHAT_ID, username, now_ts, now_ts, "manual_seed")
            )
        conn.commit()
    group_db_op(op)

def note_roster_user(chat_id, user):
    username = (getattr(user, "username", None) or "").strip().lstrip("@").casefold()
    if not username:
        return
    now_ts = now()
    def op(conn):
        conn.execute(
            """INSERT INTO participant_roster(chat_id,username,user_id,first_seen,last_seen,source)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(chat_id,username) DO UPDATE SET
                   user_id=excluded.user_id, last_seen=excluded.last_seen
            """,
            (chat_id, username, int(user.id), now_ts, now_ts, "observed")
        )
        conn.commit()
    group_db_op(op)

def role_snapshot_rows(chat_id):
    return group_db_op(lambda conn: conn.execute(
        """SELECT r.username, r.user_id, COALESCE(m.role_name,'') AS role_name,
                  COALESCE(m.tag,'') AS tag, COALESCE(m.active,0) AS active
           FROM participant_roster r
           LEFT JOIN group_members m ON m.chat_id=r.chat_id AND m.user_id=r.user_id
           WHERE r.chat_id=?
           ORDER BY r.username COLLATE NOCASE""",
        (chat_id,)
    ).fetchall())

async def update_role_snapshot(chat_id: int):
    rows=role_snapshot_rows(chat_id)
    visible=[]
    for row in rows:
        uid=row["user_id"]
        if not uid: continue
        uname=f"@{row['username']}" if row["username"] else str(uid)
        role_name=row["role_name"] or "роль не назначена"
        tag=row["tag"] or ""
        if uname.lower()=="@justice_faite_bot": continue
        visible.append(f"Роль участника — {role_name} — {uname}" + (f" — {tag}" if tag else ""))
    lines=["𝗥𝗢𝗟𝗘 𝗦𝗡𝗔𝗣𝗦𝗛𝗢𝗧","",f"Основной чат: {chat_id}",f"Участников в базе: {len(visible)}",""]+ (visible or ["Пока нет сохранённых участников."])
    text="\n".join(lines)[:3900]
    state=group_db_op(lambda conn: conn.execute("SELECT owner_message_id FROM role_snapshot_state WHERE chat_id=?",(chat_id,)).fetchone())
    mid=state["owner_message_id"] if state else None
    if mid:
        try:
            await bot.edit_message_text(chat_id=ADMIN_ID,message_id=mid,text=text)
            return mid
        except Exception:
            pass
    sent=await bot.send_message(ADMIN_ID,text)
    group_db_op(lambda conn:(conn.execute("INSERT INTO role_snapshot_state(chat_id,owner_message_id,updated_at) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET owner_message_id=excluded.owner_message_id,updated_at=excluded.updated_at",(chat_id,sent.message_id,now())),conn.commit()))
    return sent.message_id


def seed_role_catalog():
    def op(conn):
        if len(ROLE_CATALOG) != 148:
            raise RuntimeError(f"ROLE_CATALOG must contain 148 roles, got {len(ROLE_CATALOG)}")
        keys, english = set(), set()
        for name, en, region in ROLE_CATALOG:
            key = normalize_role(name)
            ek = en.casefold()
            if key in keys or ek in english:
                raise RuntimeError(f"Duplicate role in catalog: {name} / {en}")
            keys.add(key); english.add(ek)
            conn.execute(
                """INSERT INTO role_catalog(role_key,role_name,english_name,region,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(role_key) DO UPDATE SET
                       role_name=excluded.role_name,
                       english_name=excluded.english_name,
                       region=excluded.region,
                       updated_at=excluded.updated_at""",
                (key, name, en, region, now()),
            )
        conn.commit()
    group_db_op(op)


def migrate_group_state():
    def op(conn):
        mafia_cols = {row["name"] for row in conn.execute("PRAGMA table_info(mafia_games)").fetchall()}
        if "lobby_message_id" not in mafia_cols:
            conn.execute("ALTER TABLE mafia_games ADD COLUMN lobby_message_id INTEGER")
        poll_cols = {row["name"] for row in conn.execute("PRAGMA table_info(game_polls)").fetchall()}
        if "options_json" not in poll_cols:
            conn.execute("ALTER TABLE game_polls ADD COLUMN options_json TEXT NOT NULL DEFAULT ''")
        if "tie_round" not in poll_cols:
            conn.execute("ALTER TABLE game_polls ADD COLUMN tie_round INTEGER NOT NULL DEFAULT 0")
        if "parent_poll_id" not in poll_cols:
            conn.execute("ALTER TABLE game_polls ADD COLUMN parent_poll_id INTEGER")

        cols = {row["name"] for row in conn.execute("PRAGMA table_info(role_state)").fetchall()}
        if "status" not in cols:
            conn.execute("ALTER TABLE role_state ADD COLUMN status TEXT NOT NULL DEFAULT 'free'")
        rows = conn.execute("SELECT chat_id, role_key, role_name, user_id, status, legacy_marker, bot_managed FROM role_state").fetchall()
        for row in rows:
            status = row["status"] or STATUS_FREE
            if row["user_id"] is not None:
                status = STATUS_TAKEN
            elif row["legacy_marker"] in ("💛", "🧡", "💚"):
                status = {"💛": STATUS_TAKEN, "🧡": STATUS_WANTED, "💚": STATUS_RESERVED}[row["legacy_marker"]]
            elif status not in {STATUS_FREE, STATUS_TAKEN, STATUS_WANTED, STATUS_RESERVED}:
                status = STATUS_FREE
            conn.execute("UPDATE role_state SET status=? WHERE chat_id=? AND role_key=?", (status, row["chat_id"], row["role_key"]))

        # No external roster is imported. Roles are created on demand when assigned or observed in Telegram.
        conn.commit()
    group_db_op(op)


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
    # Spy deep-link bootstrap. This is intentionally checked before the
    # normal anonymous-feedback /start flow so opening the bot from a game
    # lobby can register the player without disturbing the existing menu.
    text = message.text or ""
    if message.chat.type == "private":
        m = re.match(r"^/start\s+spy_(\d+)$", text, flags=re.IGNORECASE)
        if m:
            await spy_handle_deep_link(message, int(m.group(1)))
            return
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


# Internal cancellation helper; not exposed as a standalone command.
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

async def _legacy_admin_command(
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

# Embedded role catalog and role logic.  The previous version depended on a
# separate group_logic.py which was not present in the uploaded project.
# Keeping it here makes this file self-contained.

STATUS_TAKEN = "taken"
STATUS_WANTED = "wanted"
STATUS_RESERVED = "reserved"
STATUS_FREE = "free"
STATUS_MARKER = {
    STATUS_TAKEN: "💛",
    STATUS_WANTED: "🧡",
    STATUS_RESERVED: "💚",
}
CUSTOM_EMOJI_IDS = {
    STATUS_TAKEN: "5366258639294181517",
    STATUS_WANTED: "5368545640659826312",
    STATUS_RESERVED: "5386465730277421816",
}

# Exact roster supplied by the user.  148 entries, grouped exactly as the
# two test roster messages are structured.
_ROLE_GROUPS = [
    ("✦ 🤍🤍𝑴𝒐𝒏𝒅𝒔𝒕𝒂𝒅𝒕🤍🤍✦", [
        ("Альбедо", "Albedo"), ("Барбара", "Barbara"), ("Беннет", "Bennett"),
        ("Варка", "Varka"), ("Венти", "Venti"), ("Далия", "Dahlia"),
        ("Джинн", "Jean"), ("Дилюк", "Diluc"), ("Диона", "Diona"),
        ("Дурин", "Durin"), ("Кли", "Klee"), ("Кэйя", "Kaeya"),
        ("Лиза", "Lisa"), ("Лоэн", "Lohen"), ("Мика", "Mika"),
        ("Мона", "Mona"), ("Ноэлль", "Noelle"), ("Прюн", "Prune"),
        ("Рейзор", "Razor"), ("Розария", "Rosaria"), ("Сахароза", "Sucrose"),
        ("Фишль", "Fischl"), ("Эмбер", "Amber"), ("Эола", "Eula"),
    ]),
    ("✦ 🤍🤍𝑳𝒊 𝒀𝒖𝒆🤍🤍✦", [
        ("Бай Чжу", "Baizhu"), ("Бэй Доу", "Beidou"), ("Гань Юй", "Ganyu"),
        ("Е Лань", "Yelan"), ("Камин", "Gaming"), ("Кэ Цин", "Keqing"),
        ("Лань Янь", "Lan Yan"), ("Нин Гуан", "Ningguang"), ("Син Цю", "Xingqiu"),
        ("Синь Янь", "Xinyan"), ("Сян Лин", "Xiangling"), ("Сянь Юнь", "Xianyun"),
        ("Сяо", "Xiao"), ("Ху Тао", "Hu Tao"), ("Цзы Бай", "Zibai"),
        ("Ци Ци", "Qiqi"), ("Чжун Ли", "Zhongli"), ("Чунь Юнь", "Chongyun"),
        ("Шэнь Хэ", "Shenhe"), ("Юнь Цзинь", "Yunjin"), ("Янь Фэй", "Yanfei"),
        ("Яо Яо", "Yaoyao"),
    ]),
    ("✦ 🤍🤍𝑰𝒏𝒂𝒛𝒖𝒎𝒂🤍🤍✦", [
        ("Аратаки Итто", "Itto"), ("Аяка", "Ayaka"), ("Аято", "Ayato"),
        ("Горо", "Gorou"), ("Ёимия", "Yoimiya"), ("Кадзуха", "Kazuha"),
        ("Кирара", "Kirara"), ("Кокоми", "Kokomi"), ("Мидзуки", "Mizuki"),
        ("Райдэн Эи", "Raiden Ei"), ("Сара", "Sara"), ("Саю", "Sayu"),
        ("Синобу", "Shinobu"), ("Тома", "Thoma"), ("Хэйдзо", "Heizou"),
        ("Яэ Мико", "Yae Miko"),
    ]),
    ("✦ 🤍🤍𝑺𝒖𝒎𝒆𝒓𝒖🤍🤍✦", [
        ("Аль-Хайтам", "Alhaitham"), ("Дори", "Dori"), ("Дэхья", "Dehya"),
        ("Кавех", "Kaveh"), ("Кандакия", "Candace"), ("Коллеи", "Collei"),
        ("Лайла", "Layla"), ("Нахида", "Nahida"), ("Нилу", "Nilou"),
        ("Сайно", "Cyno"), ("Сетос", "Sethos"), ("Странник", "Wanderer"),
        ("Тигнари", "Tighnari"), ("Фарузан", "Faruzan"),
    ]),
    ("✦ 🤍🤍𝑭𝒐𝒏𝒕𝒂𝒊𝒏𝒆🤍🤍✦", [
        ("Клоринда", "Clorinde"), ("Лини", "Lyney"), ("Линетт", "Lynette"),
        ("Навия", "Navia"), ("Нёвиллет", "Neuvillette"), ("Ризли", "Wriothesley"),
        ("Сиджвин", "Sigewinne"), ("Тиори", "Chiori"), ("Фремине", "Freminet"),
        ("Фурина", "Furina"), ("Шарлотта", "Charlotte"), ("Эмилия", "Emilie"),
        ("Эскофье", "Escoffier"),
    ]),
    ("✦ 🤍🤍𝑵𝒂𝒕𝒍𝒂𝒏🤍🤍✦", [
        ("Вареса", "Varesa"), ("Иансан", "Iansan"), ("Ифа", "Ifa"),
        ("Качина", "Kachina"), ("Кинич", "Kinich"), ("Мавуика", "Mavuika"),
        ("Муалани", "Mualani"), ("Оророн", "Ororon"), ("Ситлали", "Citlali"),
        ("Часка", "Chasca"), ("Шилонен", "Xilonen"),
    ]),
    ("✦ 🤍🤍𝑵𝒐𝒅-𝑲𝒓𝒂𝒊🤍🤍✦", [
        ("Айно", "Aino"), ("Иллуги", "Illuga"), ("Инеффа", "Ineffa"),
        ("Коломбина", "Columbina"), ("Лаума", "Lauma"), ("Линнея", "Linnea"),
        ("Нефер", "Nefer"), ("Флинс", "Flins"), ("Ягода", "Jahoda"),
    ]),
    ("✦ 🤍🤍𝑺𝒏𝒆𝒛𝒉𝒏𝒂𝒚𝒂🤍🤍✦", [
        ("Алёша", "Alyosha"), ("Арлекино", "Arlecchino"), ("Валера", "Valera"),
        ("Весна", "Vesna"), ("Водяница", "Vodyanitsa"), ("Даника", "Danika"),
        ("Дотторе", "Dottore"), ("Капитано", "Capitano"), ("Митя", "Mitya"),
        ("Ной", "Noy"), ("Одетта", "Odette"), ("Панталоне", "Pantalone"),
        ("Пьеро", "Pierro"), ("Пульчинелла", "Pulcinella"), ("Сандроне", "Sandrone"),
        ("Синьора", "Signora"), ("Тарталья", "Tartaglia"), ("Царица", "Tsaritsa"),
    ]),
    ("✦ 🤍🤍𝑲𝒉𝒂𝒆𝒏𝒓𝒊’𝒂𝒉🤍🤍✦", [
        ("Ведрфельнир", "Vedrfolnir"), ("Дайнслейф", "Dainsleif"), ("Рери", "Rerir"),
        ("Сурталоги", "Surtalogi"), ("Толиндис", "Tollindis"), ("Хальфдан", "Halfdan"),
        ("Хрофтатюр", "Hroptatyr"),
    ]),
    ("✦ 🤍🤍𝑺𝒉𝒂𝒃𝒂𝒔𝒉🤍🤍✦", [
        ("Алиса", "Alice"), ("Андерсдоттер", "Andersdotter"), ("Барбелот", "Barbeloth"),
        ("Николь Рейн", "Nicole Reeyn"), ("Октавия", "Octavia"), ("Рэйндоттир", "Rhinedottir"),
    ]),
    ("✦ 🤍🤍𝑺𝒉𝒂𝒅𝒐𝒘𝒔🤍🤍✦", [
        ("Астарот", "Astaroth"), ("Асмодей", "Asmoday"), ("Набериус", "Naberius"),
        ("Ронова", "Ronova"),
    ]),
    ("✦ 🤍🤍𝑨𝒏𝒐𝒕𝒉𝒆𝒓🤍🤍✦", [
        ("Итэр", "Aether"), ("Люмин", "Lumine"), ("Паймон", "Paimon"), ("Скирк", "Skirk"),
    ]),
]

ROLE_CATALOG = [(name, english, region) for region, entries in _ROLE_GROUPS for name, english in entries]
ROLE_BY_KEY = {}
for _name, _english, _region in ROLE_CATALOG:
    ROLE_BY_KEY[" ".join(_name.casefold().replace("ё", "е").split())] = {
        "name": _name,
        "english": _english,
        "region": _region,
    }

def normalize_role(value):
    return " ".join((value or "").casefold().replace("ё", "е").split())


def _compact_role_key(value):
    return re.sub(r"[^a-zа-я0-9]+", "", (value or "").casefold().replace("ё", "е"))


def role_for(value):
    key = normalize_role(value)
    exact = ROLE_BY_KEY.get(key)
    if exact:
        return exact
    compact = _compact_role_key(value)
    if not compact:
        return None
    matches=[]
    for name, english, region in ROLE_CATALOG:
        if _compact_role_key(name)==compact or _compact_role_key(english)==compact:
            matches.append({"name": name, "english": english, "region": region})
    return matches[0] if len(matches)==1 else None


def _stylize_latin(text):
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D468 + (ord(ch) - ord("A"))))
        elif "a" <= ch <= "z":
            # Mathematical Bold Italic lowercase; this matches the original tag style.
            out.append(chr(0x1D482 + (ord(ch) - ord("a"))))
        else:
            out.append(ch)
    return "".join(out)

def make_tag(english):
    value = f"❦{_stylize_latin(english.replace(' ', ''))}❦"
    # Telegram member tags are max 16 characters and don't allow emoji.
    if len(value) <= 16:
        return value
    compact = english.replace(" ", "")
    value = f"❦{compact}❦"
    if len(value) <= 16:
        return value
    return compact[:16]


JOIN_ACTIVE_STATUSES = {"member", "restricted"}
LEAVE_STATUSES = {"left", "kicked"}


def group_db_op(callback, *args):
    return db_transaction(callback, *args)


def upsert_group_member(chat_id, user, *, active=True, role_key=None, role_name=None, tag=None, confirmed=False, welcome_message_id=None, tag_set_by_bot=None):
    def op(conn):
        existing = conn.execute("SELECT * FROM group_members WHERE chat_id=? AND user_id=?", (chat_id, user.id)).fetchone()
        managed = (1 if tag_set_by_bot else 0) if tag_set_by_bot is not None else int(existing["tag_set_by_bot"] or 0) if existing else 0
        conn.execute(
            """
            INSERT INTO group_members(chat_id,user_id,first_name,last_name,username,role_key,role_name,tag,confirmed,active,joined_at,confirmed_at,left_at,welcome_message_id,tag_set_by_bot)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET
                first_name=excluded.first_name,last_name=excluded.last_name,username=excluded.username,
                role_key=COALESCE(excluded.role_key, group_members.role_key),
                role_name=COALESCE(excluded.role_name, group_members.role_name),
                tag=COALESCE(excluded.tag, group_members.tag),
                active=excluded.active,confirmed=excluded.confirmed,left_at=NULL,
                welcome_message_id=COALESCE(excluded.welcome_message_id, group_members.welcome_message_id),
                tag_set_by_bot=excluded.tag_set_by_bot
            """,
            (chat_id,user.id,user.first_name or "",user.last_name or "",user.username or "",role_key,role_name,tag,1 if confirmed else 0,1 if active else 0,now(),welcome_message_id,managed),
        )
        conn.commit()
    group_db_op(op)

def record_role_history(chat_id, user_id, role_key, role_name, tag, event):
    if not role_key or not role_name:
        return
    def op(conn):
        conn.execute(
            "INSERT INTO role_history(chat_id,user_id,role_key,role_name,tag,event,created_at) VALUES(?,?,?,?,?,?,?)",
            (chat_id, user_id, normalize_role(role_key), role_name, tag or "", event, now()),
        )
        conn.commit()
    group_db_op(op)


def get_role_history(chat_id, user_id, limit=20):
    return group_db_op(lambda conn: conn.execute(
        "SELECT * FROM role_history WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, user_id, int(limit)),
    ).fetchall())


def mark_member_left(chat_id, user_id):
    def op(conn):
        row = conn.execute("SELECT role_key, role_name, tag FROM group_members WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        stamp = now()
        conn.execute(
            "UPDATE group_members SET active=0, left_at=?, confirmed=0 WHERE chat_id=? AND user_id=?",
            (stamp,chat_id,user_id),
        )
        if row and row["role_key"]:
            conn.execute(
                "INSERT INTO role_history(chat_id,user_id,role_key,role_name,tag,event,created_at) VALUES(?,?,?,?,?,?,?)",
                (chat_id,user_id,row["role_key"],row["role_name"] or row["role_key"],row["tag"] or "","left",stamp),
            )
            conn.execute(
                "UPDATE role_state SET user_id=NULL,status='free',bot_managed=0,legacy_marker='' WHERE chat_id=? AND role_key=? AND user_id=?",
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
    """Return the newest active member with no assigned role."""
    return group_db_op(lambda conn: conn.execute(
        """
        SELECT * FROM group_members
        WHERE chat_id=? AND active=1 AND role_key IS NULL
          AND joined_at >= ?
        ORDER BY joined_at DESC, user_id DESC
        LIMIT 1
        """,
        (chat_id, (datetime.now(timezone.utc) - timedelta(seconds=ROLE_ASSIGNMENT_WINDOW_SECONDS)).isoformat()),
    ).fetchone())


def role_is_occupied(chat_id, role_key, *, exclude_user_id=None):
    key = normalize_role(role_key)
    row = group_db_op(lambda conn: conn.execute(
        "SELECT * FROM role_state WHERE chat_id=? AND role_key=?", (chat_id, key)
    ).fetchone())
    if not row:
        return False
    if row["user_id"] is not None and row["user_id"] != exclude_user_id:
        return True
    return (row["status"] == STATUS_TAKEN and row["user_id"] is None)


def assign_role_db_atomic(chat_id, user, role):
    """Reserve a role atomically. Telegram tag application happens after reservation.
    A conditional UPDATE under BEGIN IMMEDIATE prevents two admins from taking the same role.
    """
    role_key = normalize_role(role["name"])
    tag = make_tag(role["english"])
    def op(conn):
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute(
            "SELECT * FROM role_state WHERE chat_id=? AND role_key=?",
            (chat_id, role_key),
        ).fetchone()
        if state and state["user_id"] not in (None, user.id):
            conn.rollback()
            raise ValueError("ROLE_OCCUPIED")
        old = conn.execute(
            "SELECT role_key FROM group_members WHERE chat_id=? AND user_id=? AND active=1",
            (chat_id, user.id),
        ).fetchone()
        if old and old["role_key"] and old["role_key"] != role_key:
            conn.execute(
                "UPDATE role_state SET user_id=NULL,status='free',bot_managed=0,legacy_marker='' "
                "WHERE chat_id=? AND role_key=? AND user_id=?",
                (chat_id, old["role_key"], user.id),
            )
        if state:
            cur = conn.execute(
                "UPDATE role_state SET user_id=?,status='taken',role_name=?,bot_managed=1,legacy_marker=?,legacy_custom_emoji_id=? "
                "WHERE chat_id=? AND role_key=? AND (user_id IS NULL OR user_id=?)",
                (user.id, role["name"], STATUS_MARKER[STATUS_TAKEN], CUSTOM_EMOJI_IDS[STATUS_TAKEN], chat_id, role_key, user.id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO role_state(chat_id,role_key,role_name,user_id,status,legacy_marker,legacy_custom_emoji_id,bot_managed) "
                "VALUES(?,?,?,?,?,?,?,1)",
                (chat_id, role_key, role["name"], user.id, STATUS_TAKEN, STATUS_MARKER[STATUS_TAKEN], CUSTOM_EMOJI_IDS[STATUS_TAKEN]),
            )
        if cur.rowcount != 1:
            conn.rollback()
            raise ValueError("ROLE_OCCUPIED")
        conn.execute(
            """INSERT INTO group_members(chat_id,user_id,first_name,last_name,username,role_key,role_name,tag,confirmed,active,joined_at,tag_set_by_bot)
            VALUES(?,?,?,?,?,?,?,?,1,1,?,0)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET first_name=excluded.first_name,last_name=excluded.last_name,
            username=excluded.username,role_key=excluded.role_key,role_name=excluded.role_name,tag=excluded.tag,active=1,tag_set_by_bot=0""",
            (chat_id,user.id,user.first_name or "",user.last_name or "",user.username or "",role_key,role["name"],tag,now()),
        )
        conn.commit()
        return tag, role_key
    return group_db_op(op)


def release_role_assignment(chat_id, user_id, role_key):
    def op(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE role_state SET user_id=NULL,status='free',bot_managed=0,legacy_marker='' WHERE chat_id=? AND role_key=? AND user_id=?", (chat_id,role_key,user_id))
        conn.execute("UPDATE group_members SET role_key=NULL,role_name=NULL,tag=NULL,tag_set_by_bot=0 WHERE chat_id=? AND user_id=?", (chat_id,user_id))
        conn.commit()
    group_db_op(op)


def finalize_role_assignment(chat_id, user_id, role_key, actual_tag):
    def op(conn):
        row = conn.execute(
            "SELECT role_name FROM group_members WHERE chat_id=? AND user_id=? AND role_key=?",
            (chat_id,user_id,role_key),
        ).fetchone()
        conn.execute("UPDATE group_members SET tag=?,tag_set_by_bot=1 WHERE chat_id=? AND user_id=? AND role_key=?", (actual_tag,chat_id,user_id,role_key))
        if row:
            conn.execute(
                "INSERT INTO role_history(chat_id,user_id,role_key,role_name,tag,event,created_at) VALUES(?,?,?,?,?,?,?)",
                (chat_id,user_id,role_key,row["role_name"] or role_key,actual_tag or "","assigned",now()),
            )
        conn.commit()
    group_db_op(op)


async def lift_member_restriction(chat_id, user_id):
    try:
        await bot.restrict_chat_member(
            chat_id,
            user_id,
            permissions=MEMBER_ACTIVE_PERMISSIONS,
            use_independent_chat_permissions=True,
        )
        return True
    except Exception:
        logger.exception("Could not lift member restriction | chat=%s user=%s", chat_id, user_id)
        return False


# Backward-compatible name used by existing code.
def assign_role_db(chat_id, user, role):
    return assign_role_db_atomic(chat_id, user, role)


def _user_object_from_row(row):
    class U:
        pass
    u = U()
    u.id = row["user_id"] if isinstance(row, sqlite3.Row) else row.id
    u.first_name = row["first_name"] if isinstance(row, sqlite3.Row) else getattr(row, "first_name", "")
    u.last_name = row["last_name"] if isinstance(row, sqlite3.Row) else getattr(row, "last_name", "")
    u.username = row["username"] if isinstance(row, sqlite3.Row) else getattr(row, "username", "")
    return u


def set_group_member_tag_db(chat_id, user_id, actual_tag):
    def op(conn):
        conn.execute("UPDATE group_members SET tag=? WHERE chat_id=? AND user_id=?", (actual_tag,chat_id,user_id))
        conn.commit()
    group_db_op(op)


async def apply_member_tag(chat_id, user_id, desired_tag):
    if not isinstance(desired_tag, str):
        return False, None
    if len(desired_tag) > 16:
        logger.error("Refusing tag longer than Telegram's 16-character limit: %r", desired_tag)
        return False, None
    try:
        ok = await bot.set_chat_member_tag(chat_id, user_id, tag=desired_tag)
        return bool(ok), desired_tag
    except TelegramBadRequest as exc:
        logger.warning("Could not set tag %r | chat=%s user=%s | %s", desired_tag, chat_id, user_id, exc)
        return False, None


async def sync_member_tag(chat_id, user_id, *, user=None, refresh_roster=False, expected_role=None, force=False):
    cache_key=(chat_id,user_id); loop=asyncio.get_running_loop(); now_mono=loop.time()
    if not force and cache_key in MEMBER_TAG_SYNC_CACHE and now_mono-MEMBER_TAG_SYNC_CACHE[cache_key] < MEMBER_TAG_SYNC_TTL_SECONDS:
        current=get_member(chat_id,user_id)
        return role_for(current["role_name"]) if current and current["role_name"] else role_for_tag(current["tag"]) if current and current["tag"] else None
    try:
        member=await bot.get_chat_member(chat_id,user_id); MEMBER_TAG_SYNC_CACHE[cache_key]=now_mono
    except (TelegramForbiddenError,TelegramNotFound,TelegramBadRequest,TelegramNetworkError,TelegramServerError) as exc:
        logger.warning("Could not read member state | chat=%s user=%s | %s",chat_id,user_id,exc); return None
    target=user or getattr(member,"user",None)
    if not target or getattr(target,"is_bot",False): return None
    active=_chat_member_is_active(member); status=getattr(member,"status","")
    actual=(getattr(member,"tag",None) or getattr(member,"custom_title",None) or "").strip()
    previous=get_member(chat_id,target.id)
    prev_role=role_for(previous["role_name"]) if previous and previous["role_name"] else None
    upsert_group_member(chat_id,target,active=active,tag=actual,confirmed=bool(previous["confirmed"]) if previous else False,tag_set_by_bot=None)
    note_roster_user(chat_id,target)
    if not active: return None
    role=role_for_tag(actual)
    if actual and not role: return prev_role
    def op(conn):
        if role:
            key=normalize_role(role["name"]); holder=conn.execute("SELECT user_id FROM role_state WHERE chat_id=? AND role_key=?",(chat_id,key)).fetchone()
            if holder and holder["user_id"] not in (None,target.id): return False
            conn.execute("""INSERT INTO role_state(chat_id,role_key,role_name,user_id,status,legacy_marker,legacy_custom_emoji_id,bot_managed) VALUES(?,?,?,?,?,?,?,1) ON CONFLICT(chat_id,role_key) DO UPDATE SET user_id=excluded.user_id,role_name=excluded.role_name,status='taken',bot_managed=1""",(chat_id,key,role["name"],target.id,STATUS_TAKEN,STATUS_MARKER[STATUS_TAKEN],CUSTOM_EMOJI_IDS[STATUS_TAKEN]))
            conn.execute("UPDATE group_members SET role_key=?,role_name=?,tag=?,active=1,tag_set_by_bot=1 WHERE chat_id=? AND user_id=?",(key,role["name"],actual,chat_id,target.id))
        elif not actual:
            conn.execute("UPDATE role_state SET user_id=NULL,status='free',bot_managed=0,legacy_marker='' WHERE chat_id=? AND user_id=?",(chat_id,target.id))
            conn.execute("UPDATE group_members SET role_key=NULL,role_name=NULL,tag='',active=1 WHERE chat_id=? AND user_id=?",(chat_id,target.id))
        conn.commit(); return True
    result=group_db_op(op)
    if result and role and (not previous or previous["role_name"]!=role["name"] or previous["tag"]!=actual):
        record_role_history(chat_id,target.id,normalize_role(role["name"]),role["name"],actual,"synced")
    if refresh_roster: await update_group_roster(chat_id)
    return role or prev_role


async def adopt_external_roles(chat_id: int, user_ids: set[int]):
    adopted=[]; skipped_bot=skipped_no_tag=skipped_unknown=skipped_admin=conflicts=errors=0; checked=0
    for user_id in sorted(user_ids):
        try:
            member=await bot.get_chat_member(chat_id,user_id)
            target=getattr(member,"user",None)
            if not target or getattr(target,"is_bot",False): skipped_bot+=1; continue
            if not _chat_member_is_active(member): continue
            checked+=1
            status=getattr(member,"status","")
            tag=(getattr(member,"tag",None) or getattr(member,"custom_title",None) or "").strip()
            role=role_for_tag(tag)
            if not tag: skipped_no_tag+=1; continue
            if not role: skipped_unknown+=1; continue
            if status in {"administrator","creator"}: skipped_admin+=1
            current=get_member(chat_id,user_id)
            if current and int(current["tag_set_by_bot"] or 0): continue
            result=await sync_member_tag(chat_id,user_id,user=target,force=True)
            if result: adopted.append((role["name"],display_username_for_group(target)))
        except (TelegramForbiddenError,TelegramNotFound,TelegramBadRequest,TelegramNetworkError,TelegramServerError): errors+=1
        except Exception:
            errors+=1; logger.exception("Role adoption failed | chat=%s user=%s",chat_id,user_id)
    return {"checked":checked,"adopted":adopted,"skipped_bot":skipped_bot,"skipped_no_tag":skipped_no_tag,"skipped_unknown":skipped_unknown,"skipped_admin":skipped_admin,"conflicts":conflicts,"errors":errors}



def _normalize_role_tag_value(value):
    value=unicodedata.normalize("NFKC",value or "")
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+","",value).casefold().replace("ё","е")

def _role_tag_candidates(tag):
    raw=str(tag or "").strip()
    if not raw: return []
    pieces=re.findall(r"❦([^❦]{1,64})❦",raw)
    candidates=list(pieces)
    candidates.append(raw)
    norm=_normalize_role_tag_value(raw)
    prefixes=("gladmin","soowner","coowner","owner","admin","chat","mont")
    for p in prefixes:
        if norm.startswith(p) and len(norm)>len(p): candidates.append(norm[len(p):])
    out=[]; seen=set()
    for item in candidates:
        key=_normalize_role_tag_value(item)
        if key and key not in seen:
            seen.add(key); out.append(key)
    return out

def role_for_tag(tag):
    for candidate in _role_tag_candidates(tag):
        for name,english,_region in ROLE_CATALOG:
            keys={_normalize_role_tag_value(make_tag(english)),_normalize_role_tag_value(english),_normalize_role_tag_value(english.replace(" ","")),_normalize_role_tag_value(name)}
            if candidate in keys:
                return role_for(name)
    return None


async def send_or_edit_welcome(chat_id, user_id):
    row = get_member(chat_id, user_id)
    if not row or not row["active"]:
        return

    display = (row["username"] and "@" + row["username"]) or row["first_name"] or "участник"
    confirmed = bool(row["confirmed"])

    text = (
        "𝗪𝗘𝗟𝗖𝗢𝗠𝗘\n\n"
        f"Привет, {display}! 🤍\n\n"
        "Рады видеть тебя здесь. Перед началом общения, пожалуйста, ознакомься с правилами флуда.\n\n"
        "𝗥𝗨𝗟𝗘𝗦\n"
        "Нажимая кнопку ниже, ты подтверждаешь, что ознакомлен(а) с правилами флуда "
        "и самостоятельно несёшь ответственность за свои действия и сообщения в чате."
    )

    button_text = "✓ Правила подтверждены" if confirmed else "✓ Я ознакомлен(а) с правилами"
    callback_data = f"fm:confirmed:{chat_id}:{user_id}" if confirmed else f"fm:confirm:{chat_id}:{user_id}"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=button_text, callback_data=callback_data)],
        [InlineKeyboardButton(text="Назначить роль", callback_data=f"fm:role:{chat_id}:{user_id}")]
    ])

    if row["welcome_message_id"]:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=row["welcome_message_id"],
                text=text,
                reply_markup=markup,
            )
            return
        except Exception:
            pass

    try:
        sent = await bot.send_message(chat_id, text, reply_markup=markup)
        group_db_op(lambda conn: (
            conn.execute(
                "UPDATE group_members SET welcome_message_id=? WHERE chat_id=? AND user_id=?",
                (sent.message_id, chat_id, user_id),
            ),
            conn.commit(),
        ))
    except Exception:
        logger.exception("Could not send welcome | chat=%s user=%s", chat_id, user_id)

def _chat_member_is_active(member) -> bool:
    status = getattr(member, "status", None)
    if status in {"member", "administrator", "creator"}:
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


@dp.chat_member()
async def group_member_update(event: ChatMemberUpdated):
    if not is_primary_chat(event.chat.id):
        return
    global last_polling_activity
    last_polling_activity = datetime.now(timezone.utc)
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    old_active = _chat_member_is_active(event.old_chat_member)
    new_active = _chat_member_is_active(event.new_chat_member)
    user = event.new_chat_member.user
    chat_id = event.chat.id
    if getattr(user, "is_bot", False):
        return

    # New member / return to chat. A Telegram 'restricted' member is active
    # only when is_member=True. This prevents our own restrict_chat_member call
    # from being misread as a fresh join and sending the welcome twice.
    if new_active and not old_active:
        register_user(user)
        existing = get_member(chat_id, user.id)
        was_active = bool(existing and existing["active"])
        note_roster_user(chat_id, user)
        upsert_group_member(
            chat_id, user, active=True,
            role_key=existing["role_key"] if was_active else None,
            role_name=existing["role_name"] if was_active else None,
            tag=existing["tag"] if was_active else None,
            confirmed=False,
            welcome_message_id=None,
            tag_set_by_bot=False,
        )
        await sync_member_tag(chat_id, user.id, user=user, refresh_roster=False, force=True)
        refreshed = get_member(chat_id, user.id)
        if not (refreshed and refreshed["role_key"]):
            try:
                await bot.restrict_chat_member(
                    chat_id,
                    user.id,
                    permissions=NEW_MEMBER_RESTRICTION,
                    use_independent_chat_permissions=True,
                )
            except Exception:
                logger.exception("Could not restrict new member | chat=%s user=%s", chat_id, user.id)
        await send_or_edit_welcome(chat_id, user.id)
        return

    # A member became a full/known active member without being a fresh join.
    if new_active:
        await sync_member_tag(chat_id, user.id, user=user, refresh_roster=False, force=True)
        return

    # Left/kicked, including restricted users with is_member=False.
    if not new_active and old_active:
        previous = get_member(chat_id, user.id)
        welcome_message_id = previous["welcome_message_id"] if previous else None
        old = mark_member_left(chat_id, user.id)
        role_text = (old or {}).get("role_name") or (old or {}).get("tag") or "роль не назначена"
        old_tag = (old or {}).get("tag") or ""
        username = f"@{user.username}" if user.username else user.first_name or str(user.id)

        # Уход никогда не должен вызывать welcome. Удаляем старое welcome-сообщение.
        if welcome_message_id:
            with suppress(Exception):
                await bot.delete_message(chat_id, welcome_message_id)

        with suppress(Exception):
            await bot.send_message(
                ADMIN_ID,
                "𝗠𝗘𝗠𝗕𝗘𝗥 𝗟𝗘𝗙𝗧\n\n"
                f"Участник: {username}\n"
                f"ID: {user.id}\n"
                f"Последняя роль: {role_text}"
                + (f"\nTelegram-тег: {old_tag}" if old_tag and old_tag != role_text else ""),
            )
        return


@dp.callback_query(F.data.startswith("fm:confirm:"))
async def group_confirm(callback: CallbackQuery):
    try:
        _, _, chat_raw, user_raw = (callback.data or "").split(":", 3)
        chat_id = int(chat_raw)
        target_user_id = int(user_raw)
    except Exception:
        await callback.answer("Некорректное подтверждение.", show_alert=True)
        return

    if callback.from_user.id != target_user_id:
        await callback.answer("Эта кнопка предназначена для другого участника.", show_alert=True)
        return

    row = get_member(chat_id, target_user_id)
    if not row or not row["active"]:
        await callback.answer("Вы уже не являетесь участником этого чата.", show_alert=True)
        return

    confirm_member(chat_id, target_user_id)

    role_name = row["role_name"] or "роль не назначена"
    tag = row["tag"] or "тег не назначен"
    username = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.first_name or str(target_user_id)

    with suppress(Exception):
        await bot.send_message(
            ADMIN_ID,
            "𝗥𝗨𝗟𝗘𝗦 𝗖𝗢𝗡𝗙𝗜𝗥𝗠𝗘𝗗\n\n"
            f"Участник: {username}\n"
            f"ID: {target_user_id}\n"
            "Подтвердил(а) ознакомление с правилами флуда.\n"
            f"Роль: {role_name}\n"
            f"Тег: {tag}",
        )

    with suppress(Exception):
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="✓ Правила подтверждены",
                        callback_data=f"fm:confirmed:{chat_id}:{target_user_id}",
                    )
                ]]
            )
        )

    await callback.answer("Правила подтверждены. Роль участника назначается отдельно.")


@dp.message(F.chat.type.in_({"group", "supergroup"}), F.text.regexp(r"(?is)^\s*(?!/)(?!калл(?:\s|$)).+"))
async def group_passive_member_sync(message: Message):
    if not is_primary_chat(message.chat.id):
        return
    if DEBUG_GROUP_UPDATES:
        logger.info("DEBUG_GROUP_UPDATE chat=%s type=%s msg=%s from=%s cmd=%s entities=%r text=%r", message.chat.id, message.chat.type, message.message_id, getattr(message.from_user, "id", None), extract_command_from_message(message), [(getattr(e,"type",None),getattr(e,"offset",None),getattr(e,"length",None)) for e in (message.entities or [])], (message.text or "")[:200])
    if has_bot_command_entity(message):
        return
    if not message.from_user or message.from_user.is_bot:
        return
    try:
        group_db_op(lambda conn: (
            conn.execute(
                "INSERT OR IGNORE INTO managed_group(group_chat_id, bound_at, title) VALUES(?,?,?)",
                (message.chat.id, now(), getattr(message.chat, "title", "") or ""),
            ),
            conn.commit(),
        ))
        ensure_default_schedule(message.chat.id)
        register_user(message.from_user)
        note_roster_user(message.chat.id, message.from_user)
        existing = get_member(message.chat.id, message.from_user.id)
        if not existing or (existing and existing["active"]):
            await sync_member_tag(message.chat.id, message.from_user.id, user=message.from_user, refresh_roster=False)
    except Exception:
        logger.debug("Passive member sync failed | chat=%s user=%s", message.chat.id, message.from_user.id, exc_info=True)


@dp.message(F.chat.type.in_({"group", "supergroup"}), F.text.regexp(r"(?iu)^\s*калл\s+.+$"))
async def call_assign_role(message: Message):
    """The ONLY role/tag assignment trigger: `калл <роль>`.
    It targets the single newest pending member, sets the Telegram tag, marks
    the rules as acknowledged and lifts the new-member restriction.
    """
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return

    role_text = re.sub(r"^\s*калл\s+", "", message.text or "", flags=re.IGNORECASE).strip()
    role = role_for(role_text)
    if not role:
        return

    pending = latest_pending_member(message.chat.id)
    if not pending:
        await message.reply("𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗠𝗘𝗡𝗧\n\nНе найден новый участник без назначенной роли.")
        return

    try:
        target_member = await bot.get_chat_member(message.chat.id, pending["user_id"])
        if not _chat_member_is_active(target_member):
            await message.reply("𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗠𝗘𝗡𝗧\n\nУчастник уже не находится в чате.")
            return
        target = target_member.user

        current = get_member(message.chat.id, target.id)
        if current and current["role_key"] == normalize_role(role["name"]):
            return

        tag, role_key = assign_role_db_atomic(message.chat.id, target, role)
        ok, actual = await apply_member_tag(message.chat.id, target.id, tag)
        if not ok:
            release_role_assignment(message.chat.id, target.id, role_key)
            await message.reply("𝗧𝗘𝗟𝗘𝗚𝗥𝗔𝗠 𝗧𝗔𝗚\n\nНе удалось установить Telegram-тег. Роль освобождена.")
            return

        finalize_role_assignment(message.chat.id, target.id, role_key, actual or tag)
        confirm_member(message.chat.id, target.id)
        restriction_lifted = await lift_member_restriction(message.chat.id, target.id)
        await send_or_edit_welcome(message.chat.id, target.id)

        username = display_username_for_group(target)
        with suppress(Exception):
            await bot.send_message(
                ADMIN_ID,
                "𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗘𝗗\n\n"
                f"Участник: {username}\n"
                f"Роль: {role['name']}\n"
                f"Тег: {actual or tag}\n"
                f"Ограничение снято: {'да' if restriction_lifted else 'нет'}"
            )

        await message.reply(
            "✅ Назначено\n"
            f"{username}\n"
            f"Роль: {role['name']}\n"
            f"Тег: {actual or tag}"
        )
    except ValueError as exc:
        if str(exc) == "ROLE_OCCUPIED":
            await message.reply("𝗥𝗢𝗟𝗘 𝗢𝗖𝗖𝗨𝗣𝗜𝗘𝗗\n\nЭта роль уже занята.")
        else:
            logger.exception("Kall role assignment failed")
    except Exception:
        logger.exception("Kall role assignment failed")
        await message.reply("𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗠𝗘𝗡𝗧\n\nНе удалось назначить роль.")


async def resolve_bind_target(chat_id, target_text):
    value = (target_text or "").strip()
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        user_id = int(value)
        try:
            cm = await bot.get_chat_member(chat_id, user_id)
            return cm.user
        except Exception:
            row = get_member(chat_id, user_id)
            return _user_object_from_row(row) if row else None
    username = value.lstrip("@").casefold()
    row = find_group_member(chat_id, username)
    if row:
        return _user_object_from_row(row)
    return None


async def bind_role_from_private_admin(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=3)
    if len(parts) < 4:
        await message.reply(
            "𝗕𝗜𝗡𝗗 𝗥𝗢𝗟𝗘\n\n"
            "Формат:\n"
            "/bindrole CHAT_ID USER_ID РОЛЬ\n\n"
            "Пример:\n"
            "/bindrole -1001234567890 123456789 Кокоми\n\n"
            "Можно указать @username вместо USER_ID, если участник уже сохранён ботом."
        )
        return

    chat_raw, target_raw, role_text = parts[1], parts[2], parts[3].strip()
    try:
        chat_obj = await bot.get_chat(chat_raw)
        chat_id = int(chat_obj.id)
    except Exception:
        await message.reply("𝗕𝗜𝗡𝗗 𝗥𝗢𝗟𝗘\n\nНе удалось найти этот чат. Укажи корректный CHAT_ID.")
        return

    if not is_primary_chat(chat_id):
        await message.reply("𝗕𝗜𝗡𝗗 𝗥𝗢𝗟𝗘\n\nЭтот чат не является основным чатом бота.")
        return
    role = role_for(role_text)
    if not role:
        await message.reply(f"𝗕𝗜𝗡𝗗 𝗥𝗢𝗟𝗘\n\nРоль «{role_text}» не найдена в каталоге 148 ролей.")
        return

    target = await resolve_bind_target(chat_id, target_raw)
    if not target:
        await message.reply(
            "𝗕𝗜𝗡𝗗 𝗥𝗢𝗟𝗘\n\n"
            "Участник не найден. Он должен находиться в чате или уже быть сохранён в базе.\n"
            "Используй USER_ID или @username."
        )
        return

    try:
        cm = await bot.get_chat_member(chat_id, target.id)
        if not _chat_member_is_active(cm):
            await message.reply("𝗕𝗜𝗡𝗗 𝗥𝗢𝗟𝗘\n\nЭтот пользователь сейчас не является участником чата.")
            return
        target = cm.user
    except Exception:
        pass

    current = get_member(chat_id, target.id)
    try:
        tag, role_key = assign_role_db_atomic(chat_id, target, role)
        ok, actual = await apply_member_tag(chat_id, target.id, tag)
        if not ok:
            release_role_assignment(chat_id, target.id, role_key)
            await message.reply("𝗕𝗜𝗡𝗗 𝗥𝗢𝗟𝗘\n\nTelegram не разрешил установить тег. Изменение откатано.")
            return
        finalize_role_assignment(chat_id, target.id, role_key, actual or tag)
        await lift_member_restriction(chat_id, target.id)
        await send_or_edit_welcome(chat_id, target.id)
        await message.reply(
            "𝗕𝗜𝗡𝗗 𝗥𝗢𝗟𝗘\n\n"
            f"Участник: {display_username_for_group(target)}\n"
            f"ID: {target.id}\n"
            f"Роль: {role['name']}\n"
            f"Тег: {actual or tag}\n\n"
            "Роль сохранена в базе."
        )
    except ValueError as exc:
        if str(exc) == "ROLE_OCCUPIED":
            await message.reply("𝗕𝗜𝗡𝗗 𝗥𝗢𝗟𝗘\n\nЭта роль уже занята в указанном чате.")
        else:
            logger.exception("Private bind role failed")
            await message.reply("𝗕𝗜𝗡𝗗 𝗥𝗢𝗟𝗘\n\nНе удалось привязать роль.")
    except Exception:
        logger.exception("Private bind role failed")
        await message.reply("𝗕𝗜𝗡𝗗 𝗥𝗢𝗟𝗘\n\nНе удалось привязать роль. Подробность записана в лог.")


@dp.message(Command("bindrole"), F.chat.type == "private")
async def bindrole_private_command(message: Message):
    await bind_role_from_private_admin(message)


def display_username_for_group(user):
    return f"@{user.username}" if getattr(user,"username",None) else getattr(user,"first_name",str(user.id))


def is_group_admin_user(message):
    return bool(
        message.from_user
        and message.from_user.id == ADMIN_ID
        and message.chat.type in {"group", "supergroup"}
    )


def _is_group_message(message):
    return message.chat.type in {"group", "supergroup"}


async def _active_member_from_username(chat_id: int, username: str):
    """Resolve @username only if that user is actually active in this group."""
    row = find_group_member(chat_id, username)
    if row:
        try:
            cm = await bot.get_chat_member(chat_id, row["user_id"])
            if _chat_member_is_active(cm):
                return _user_object_from_row(row)
        except Exception:
            pass
    row = get_user_by_username(username)
    if not row:
        return None
    try:
        cm = await bot.get_chat_member(chat_id, row["user_id"])
        if not _chat_member_is_active(cm):
            return None
        return _user_object_from_row(row)
    except Exception:
        return None


@dp.message(Command("setrole"))
async def setrole_cmd(message: Message):
    if not require_primary_group(message) or not is_group_admin_user(message):
        return
    text=(message.text or "").strip()
    m=re.match(r"^\s*/setrole(?:@[A-Za-z0-9_]+)?(?:\s+|$)", text, re.IGNORECASE)
    remainder=text[m.end():].strip() if m else ""
    target=None; role_text=""
    if message.reply_to_message and message.reply_to_message.from_user:
        target=message.reply_to_message.from_user
        role_text=remainder
    else:
        parts=remainder.split(maxsplit=1)
        if len(parts)==2:
            target_text,role_text=parts
            if target_text.startswith("@"):
                row=find_group_member(message.chat.id,target_text)
                target=_user_object_from_row(row) if row else None
                if not target:
                    target=await resolve_target_user(message.chat.id,target_text)
            elif target_text.lstrip("-").isdigit():
                with suppress(Exception): target=(await bot.get_chat_member(message.chat.id,int(target_text))).user
    if not target or not role_text:
        await message.reply("𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗠𝗘𝗡𝗧\n\nОтветьте на сообщение участника: /setrole Роль\nИли: /setrole @username Роль")
        return
    role=role_for(role_text)
    if not role:
        await message.reply(f"𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗠𝗘𝗡𝗧\n\nРоль «{role_text}» не найдена в каталоге 148 ролей.")
        return
    try:
        cm=await bot.get_chat_member(message.chat.id,target.id)
        if not _chat_member_is_active(cm): raise RuntimeError("NOT_MEMBER")
        target=cm.user
        tag,key=assign_role_db_atomic(message.chat.id,target,role)
        ok,actual=await apply_member_tag(message.chat.id,target.id,tag)
        if not ok:
            release_role_assignment(message.chat.id,target.id,key)
            raise RuntimeError("TAG_FAIL")
        finalize_role_assignment(message.chat.id,target.id,key,actual or tag)
        confirm_member(message.chat.id,target.id)
        lifted=await lift_member_restriction(message.chat.id,target.id)
        await message.reply(f"✅ Назначено\n{display_username_for_group(target)}\nРоль: {role['name']}\nТег: {actual or tag}\nОграничение снято: {'да' if lifted else 'нет'}")
        with suppress(Exception): await bot.send_message(ADMIN_ID,f"𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗘𝗗\n\nУчастник: {display_username_for_group(target)}\nРоль участника: {role['name']}\nТег: {actual or tag}")
    except ValueError as exc:
        await message.reply("𝗥𝗢𝗟𝗘 𝗢𝗖𝗖𝗨𝗣𝗜𝗘𝗗\n\nЭта роль уже занята." if str(exc)=="ROLE_OCCUPIED" else "𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗠𝗘𝗡𝗧\n\nНе удалось назначить роль.")
    except RuntimeError as exc:
        msg="Участник сейчас не в чате." if str(exc)=="NOT_MEMBER" else "Telegram не разрешил установить тег. Назначение отменено."
        await message.reply("𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗠𝗘𝗡𝗧\n\n"+msg)
    except Exception:
        logger.exception("setrole failed")
        await message.reply("𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗠𝗘𝗡𝗧\n\nНе удалось назначить роль.")


def _normalize_custom_command_name(value: str) -> str:
    value = (value or "").strip().lower()
    value = value.removeprefix("/")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value):
        raise ValueError("INVALID_COMMAND")
    return value


BUILTIN_COMMANDS = {
    "help", "mafia", "mafia_leave",
    "syncroles", "game_poll",
    "manage_commands", "addcommand", "delcommand", "commands",
    "bindrole", "setrole", "spy",
}


def get_custom_commands(*, enabled_only=True):
    where = " WHERE enabled=1" if enabled_only else ""
    return group_db_op(lambda conn: conn.execute(
        f"SELECT * FROM custom_commands{where} ORDER BY command"
    ).fetchall())


def get_custom_command(command: str):
    name = _normalize_custom_command_name(command)
    return group_db_op(lambda conn: conn.execute(
        "SELECT * FROM custom_commands WHERE command=? AND enabled=1", (name,)
    ).fetchone())


def save_custom_command(command: str, description: str, response: str, scope: str, created_by: int):
    name = _normalize_custom_command_name(command)
    if name in BUILTIN_COMMANDS:
        raise ValueError("BUILTIN_COMMAND")
    scope = scope.strip().lower()
    if scope not in {"all", "group", "admin", "private"}:
        raise ValueError("INVALID_SCOPE")
    description = (description or "").strip()[:256]
    response = (response or "").strip()
    if not description or not response:
        raise ValueError("EMPTY_VALUE")
    if len(response) > TELEGRAM_TEXT_LIMIT:
        raise ValueError("RESPONSE_TOO_LONG")
    def op(conn):
        conn.execute(
            """INSERT INTO custom_commands(command,description,response,scope,enabled,created_at,created_by)
               VALUES(?,?,?,?,1,?,?)
               ON CONFLICT(command) DO UPDATE SET
                   description=excluded.description, response=excluded.response,
                   scope=excluded.scope, enabled=1, created_at=excluded.created_at, created_by=excluded.created_by""",
            (name, description, response, scope, now(), created_by),
        )
        conn.commit()
    group_db_op(op)
    return name


def delete_custom_command(command: str) -> bool:
    name = _normalize_custom_command_name(command)
    return group_db_op(lambda conn: (
        conn.execute("DELETE FROM custom_commands WHERE command=?", (name,)).rowcount,
        conn.commit(),
    )[0] > 0)


def _custom_command_allowed(row, message: Message) -> bool:
    scope = row["scope"]
    if scope == "all":
        return True
    if scope == "private":
        return message.chat.type == "private" and bool(message.from_user and message.from_user.id == ADMIN_ID)
    if scope == "group":
        return message.chat.type in {"group", "supergroup"}
    if scope == "admin":
        if message.chat.type == "private":
            return bool(message.from_user and message.from_user.id == ADMIN_ID)
        return is_group_admin_user(message)
    return False


def render_custom_response(response: str, message: Message) -> str:
    user = message.from_user
    username = f"@{user.username}" if user and user.username else (user.first_name if user else "участник")
    replacements = {
        "{user}": user.first_name if user else "участник",
        "{username}": username,
        "{user_id}": str(user.id) if user else "",
        "{chat_id}": str(message.chat.id),
        "{chat}": message.chat.title or "чат" if message.chat.type != "private" else "личные сообщения",
    }
    text = response
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


@dp.message(Command("manage_commands"))
async def manage_commands_cmd(message: Message):
    if message.chat.type != "private" or not message.from_user or message.from_user.id != ADMIN_ID:
        return
    rows = get_custom_commands()
    if not rows:
        text = "𝗖𝗢𝗠𝗠𝗔𝗡𝗗 𝗠𝗔𝗡𝗔𝗚𝗘𝗥\n\nПока нет пользовательских команд.\n\nИспользуйте /addcommand для добавления."
    else:
        lines = ["𝗖𝗢𝗠𝗠𝗔𝗡𝗗 𝗠𝗔𝗡𝗔𝗚𝗘𝗥", "", "Ваши пользовательские команды:", ""]
        for row in rows:
            lines.append(f"/{row['command']} — {row['description']} [{row['scope']}]")
        lines += ["", "/addcommand — добавить или изменить", "/delcommand <команда> — удалить", "/commands — показать полный список"]
        text = "\n".join(lines)
    await message.reply(text)


@dp.message(Command("addcommand"))
async def addcommand_cmd(message: Message):
    if message.chat.type != "private" or not message.from_user or message.from_user.id != ADMIN_ID:
        return
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2 or "|" not in raw[1]:
        await message.reply(
            "𝗔𝗗𝗗 𝗖𝗢𝗠𝗠𝗔𝗡𝗗\n\n"
            "Формат:\n"
            "/addcommand имя | описание | ответ | область\n\n"
            "Область: all / group / admin / private\n\n"
            "Пример:\n"
            "/addcommand rules | Правила чата | Пожалуйста, соблюдайте правила. | group"
        )
        return
    parts = [x.strip() for x in raw[1].split("|", 3)]
    if len(parts) != 4:
        await message.reply("Нужно ровно 4 поля, разделённых символом |.")
        return
    try:
        name = save_custom_command(parts[0], parts[1], parts[2], parts[3], message.from_user.id)
        await setup_commands()
        await message.reply(f"𝗖𝗢𝗠𝗠𝗔𝗡𝗗 𝗦𝗔𝗩𝗘𝗗\n\n/{name} сохранена и добавлена в меню.")
    except ValueError as exc:
        messages = {
            "INVALID_COMMAND": "Название команды должно быть на английском: a-z, 0-9, _. Максимум 32 символа.",
            "BUILTIN_COMMAND": "Это уже встроенная команда бота.",
            "INVALID_SCOPE": "Область должна быть: all, group, admin или private.",
            "EMPTY_VALUE": "Описание и ответ не могут быть пустыми.",
            "RESPONSE_TOO_LONG": "Ответ слишком длинный.",
        }
        await message.reply(f"𝗘𝗥𝗥𝗢𝗥\n\n{messages.get(str(exc), 'Не удалось сохранить команду.')}")
    except Exception:
        logger.exception("Custom command save failed")
        await message.reply("𝗘𝗥𝗥𝗢𝗥\n\nНе удалось сохранить команду.")


@dp.message(Command("delcommand"))
async def delcommand_cmd(message: Message):
    if message.chat.type != "private" or not message.from_user or message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("Формат: /delcommand <команда>")
        return
    try:
        deleted = delete_custom_command(parts[1])
    except ValueError:
        await message.reply("Некорректное имя команды.")
        return
    if not deleted:
        await message.reply("Такой пользовательской команды нет.")
        return
    await setup_commands()
    await message.reply("𝗖𝗢𝗠𝗠𝗔𝗡𝗗 𝗥𝗘𝗠𝗢𝗩𝗘𝗗\n\nКоманда удалена.")


@dp.message(Command("commands"))
async def commands_cmd(message: Message):
    if message.chat.type != "private" or not message.from_user or message.from_user.id != ADMIN_ID:
        return
    rows = get_custom_commands()
    lines = ["𝗖𝗢𝗠𝗠𝗔𝗡𝗗𝗦", "", "Встроенные:", "/help", "/mafia", "/mafia_leave", "/game_poll", "/spy", "/setrole", "/syncroles", ]
    lines += ["", "Пользовательские:"]
    if rows:
        for row in rows:
            lines.append(f"/{row['command']} — {row['description']} [{row['scope']}]")
    else:
        lines.append("нет")
    await message.reply("\n".join(lines))


@dp.message(Command("help"))
async def help_cmd(message: Message):
    if message.chat.type in {"group", "supergroup"} and not is_primary_chat(message.chat.id):
        return
    text=(
        "𝗛𝗘𝗟𝗣\n\n"
        "Основные команды\n"
        "/mafia — открыть лобби MafiaAzBot\n"
        "/mafia_leave — выйти из лобби\n"
    )
    if is_group_admin_user(message):
        text += (
            "\n𝗔𝗗𝗠𝗜𝗡\n"
            "/setrole — назначить роль участнику\n"
            "/syncroles — сверить и сохранить реальные Telegram-теги участников\n"
            "/game_poll — запустить опрос на игру\n"
                    )
    if message.chat.type=="private" and message.from_user and message.from_user.id==ADMIN_ID:
        text += (
            "\n𝗟𝗦 𝗔𝗗𝗠𝗜𝗡\n"
            "/start — открыть анонимную обратную связь\n"
            "/manage_commands\n/addcommand\n/delcommand\n/commands\n/bindrole\n/roles_audit\n"
        )
    custom=get_custom_commands()
    visible=[r for r in custom if _custom_command_allowed(r,message)]
    if visible:
        text += "\n𝗖𝗨𝗦𝗧𝗢𝗠\n" + "\n".join(f"/{r['command']} — {r['description']}" for r in visible)
    await message.reply(text)


@dp.message(Command("roles_audit"), F.chat.type == "private")
async def roles_audit_cmd(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    chat_id=PRIMARY_CHAT_ID
    active=group_db_op(lambda conn: conn.execute("SELECT COUNT(*) AS n FROM group_members WHERE chat_id=? AND active=1",(chat_id,)).fetchone())["n"]
    assigned=group_db_op(lambda conn: conn.execute("SELECT COUNT(*) AS n FROM group_members WHERE chat_id=? AND active=1 AND role_key IS NOT NULL",(chat_id,)).fetchone())["n"]
    recent=group_db_op(lambda conn: conn.execute("SELECT h.user_id,h.role_name,h.event,h.created_at,COALESCE(m.username,m.first_name,CAST(h.user_id AS TEXT)) AS who FROM role_history h LEFT JOIN group_members m ON m.chat_id=h.chat_id AND m.user_id=h.user_id WHERE h.chat_id=? ORDER BY h.id DESC LIMIT 12",(chat_id,)).fetchall())
    lines=["𝗥𝗢𝗟𝗘 𝗔𝗨𝗗𝗜𝗧","",f"Основной чат: {chat_id}",f"Активных участников: {active}",f"С назначенной ролью: {assigned}",f"Без роли: {active-assigned}","","Последние изменения:"]
    if recent:
        for r in recent:
            who=f"@{r['who']}" if r['who'] and not str(r['who']).startswith('@') and not str(r['who']).isdigit() else str(r['who'])
            lines.append(f"• {who} — {r['role_name']} ({r['event']})")
    else:
        lines.append("• пока нет записей")
    await message.reply("\n".join(lines))


@dp.message(Command("syncroles"))
async def sync_roles_cmd(message: Message):
    if not require_primary_group(message): return
    if not is_group_admin_user(message):
        await message.reply("𝗔𝗖𝗖𝗘𝗦𝗦\n\nУ вас нет прав для этой команды."); return
    chat_id=PRIMARY_CHAT_ID
    rows=group_db_op(lambda conn: conn.execute("SELECT DISTINCT user_id FROM participant_roster WHERE chat_id=? AND user_id IS NOT NULL",(chat_id,)).fetchall())
    rows2=group_db_op(lambda conn: conn.execute("SELECT DISTINCT user_id FROM group_members WHERE chat_id=? AND user_id IS NOT NULL",(chat_id,)).fetchall())
    known={int(r["user_id"]) for r in rows}|{int(r["user_id"]) for r in rows2}
    try:
        admins=await bot.get_chat_administrators(chat_id)
        known.update(int(a.user.id) for a in admins if not getattr(a.user,"is_bot",False))
    except Exception: pass
    checked=recognized=adopted=without_role=bots=admins_checked=left=errors=0; details=[]
    for uid in sorted(known):
        try:
            member=await bot.get_chat_member(chat_id,uid)
            target=getattr(member,"user",None)
            if not target or getattr(target,"is_bot",False): bots+=1; continue
            if not _chat_member_is_active(member):
                left+=1; mark_member_left(chat_id,uid); continue
            checked+=1
            if getattr(member,"status","") in {"administrator","creator"}: admins_checked+=1
            tag=(getattr(member,"tag",None) or getattr(member,"custom_title",None) or "").strip()
            role=role_for_tag(tag)
            previous=get_member(chat_id,uid)
            if role:
                recognized+=1
                if not previous or previous["role_name"]!=role["name"] or previous["tag"]!=tag or int(previous["tag_set_by_bot"] or 0)==0:
                    adopted+=1
                await sync_member_tag(chat_id,uid,user=target,force=True)
            elif not tag:
                without_role+=1; await sync_member_tag(chat_id,uid,user=target,force=True)
            elif previous and previous["role_name"]:
                # Keep the known role until a trustworthy role tag is observed.
                upsert_group_member(chat_id,target,active=True,tag=tag,confirmed=bool(previous["confirmed"]),tag_set_by_bot=None)
            else:
                without_role+=1
        except (TelegramForbiddenError,TelegramNotFound,TelegramBadRequest):
            errors+=1; details.append(f"{uid}: участник недоступен через Telegram")
        except (TelegramNetworkError,TelegramServerError):
            errors+=1; details.append(f"{uid}: временная ошибка Telegram")
        except Exception as exc:
            errors+=1; details.append(f"{uid}: {type(exc).__name__}"); logger.exception("Role sync failed | chat=%s user=%s",chat_id,uid)
    with suppress(Exception): await update_role_snapshot(chat_id)
    lines=["𝗥𝗢𝗟𝗘 𝗦𝗬𝗡𝗖","",f"Проверено участников: {checked}",f"Ролей распознано: {recognized}",f"Новых/обновлённых ролей: {adopted}",f"Без роли: {without_role}",f"Ботов пропущено: {bots}",f"Администраторов проверено: {admins_checked}",f"Вышедших: {left}",f"Ошибок проверки: {errors}"]
    if details: lines += ["","Ошибки:"]+[f"• {x}" for x in details[:12]]
    await bot.send_message(ADMIN_ID,"\n".join(lines))



async def member_info_cmd(message: Message):
    if not _is_group_message(message):
        return
    if not is_group_admin_user(message):
        return
    target = message.reply_to_message.from_user if message.reply_to_message and message.reply_to_message.from_user else None
    if not target:
        parts = (message.text or "").split()
        row = find_group_member(message.chat.id, parts[1]) if len(parts) == 2 and parts[1].startswith("@") else None
    else:
        row = get_member(message.chat.id, target.id)
    if not row:
        await message.reply("𝗠𝗘𝗠𝗕𝗘𝗥\n\nУчастник не найден.")
        return
    # Read the real Telegram member tag before showing role information.
    # The local DB may know the participant but not yet know the tag->role mapping.
    try:
        await sync_member_tag(message.chat.id, int(row["user_id"]), force=True)
        row = get_member(message.chat.id, int(row["user_id"])) or row
    except Exception:
        logger.exception("Live member-role sync failed | chat=%s user=%s", message.chat.id, row["user_id"])
    await message.reply(
        f"𝗠𝗘𝗠𝗕𝗘𝗥\n\n"
        f"Участник: {('@'+row['username']) if row['username'] else row['first_name']}\n"
        f"ID: {row['user_id']}\n"
        f"Роль участника: {row['role_name'] or '—'}\n"
        f"Тег: {row['tag'] or '—'}\n"
        f"Подтверждение: {'да' if row['confirmed'] else 'нет'}\n"
        f"Активен: {'да' if row['active'] else 'нет'}"
    )



# =========================================================
# GAMES & WEEKLY SCHEDULE
# =========================================================

WEEKDAY_NAMES={0:"Понедельник",1:"Вторник",2:"Среда",3:"Четверг",4:"Пятница",5:"Суббота",6:"Воскресенье"}
SCHEDULE_CYCLE_DAYS=[("Вторник",1),("Четверг",3),("Суббота",5),("Понедельник",0),("Среда",2),("Пятница",4),("Воскресенье",6)]
SCHEDULE_DAY_TO_SLOT={name:i for i,(name,_) in enumerate(SCHEDULE_CYCLE_DAYS)}

GAME_DEFINITIONS = [
    ("Шпион", "Все участники, кроме одного, знают загаданную локацию. Игроки задают друг другу вопросы, пытаясь вычислить шпиона. Задача шпиона — понять локацию и назвать её.", ""),
    ("Жених и невеста", "Ведущий выбирает жениха, остальные получают номера. Жених задаёт вопросы, невесты отвечают в личке ведущего. Жених выбирает, чей ответ выбивает участницу. Побеждает последняя оставшаяся невеста.", ""),
    ("Правда или ложь", "Каждый участник рассказывает три факта о себе: два правдивых и один ложный. Остальные пытаются определить ложный факт и получают баллы за правильные ответы.", ""),
    ("Мафия", "Игроки получают роли и делятся на мирных и мафию. Днём обсуждение и голосование, ночью действует мафия. Наш бот только собирает игроков и передаёт запуск MafiaAzBot.", "/start@MafiaAzBot"),
    ("Снежный ком историй", "Первый игрок начинает историю одним предложением. Каждый следующий повторяет предыдущие предложения и добавляет своё. История постепенно превращается в общий абсурдный рассказ.", ""),
    ("Чёрный ящик", "Ведущий объявляет, что в воображаемом чёрном ящике лежит предмет. Игроки задают наводящие вопросы, а ведущий описывает свойства, функции, историю или ассоциации предмета.", ""),
    ("Бункер", "После катаклизма игроки пытаются попасть в бункер. Каждый получает случайные характеристики и постепенно раскрывает их, убеждая остальных, что именно он должен выжить.", ""),
]

DEFAULT_WEEKLY_SCHEDULE = [
    (0, "20:00", "Шпион"),
    (1, "20:00", "Жених и невеста"),
    (2, "20:00", "Правда или ложь"),
    (3, "20:00", "Мафия"),
    (4, "20:00", "Снежный ком историй"),
    (5, "20:00", "Чёрный ящик"),
    (6, "20:00", "Бункер"),
]

def schedule_anchor():
    return datetime.fromisoformat(SCHEDULE_ANCHOR_DATE).date()

def cycle_slot_for_local(dt):
    anchor=schedule_anchor()
    days=(dt.date()-anchor).days
    cycle_day=days % 14
    if cycle_day % 2 != 0:
        return None
    slot=cycle_day // 2
    if slot>6:
        return None
    return slot

def next_cycle_slot(chat_id, after_local):
    rows=group_db_op(lambda conn: conn.execute(
        "SELECT * FROM schedule_cycle WHERE chat_id=? AND enabled=1",(chat_id,)
    ).fetchall())
    if not rows:
        return None
    best=None
    for row in rows:
        slot=int(row["slot_index"])
        base=schedule_anchor()+timedelta(days=slot*2)
        delta_days=(after_local.date()-base).days
        cycles=max(0, (delta_days//14))
        candidate_date=base+timedelta(days=cycles*14)
        hour,minute=map(int,str(row["time_hm"]).split(":"))
        candidate=datetime.combine(candidate_date, datetime.min.time()).replace(hour=hour, minute=minute, tzinfo=after_local.tzinfo)
        if candidate <= after_local:
            candidate += timedelta(days=14)
        item=(candidate,slot,str(row["time_hm"]))
        if best is None or item[0]<best[0]:
            best=item
    return best

def ensure_default_schedule(chat_id):
    existing=group_db_op(lambda conn: conn.execute(
        "SELECT COUNT(*) AS c FROM schedule_cycle WHERE chat_id=? AND enabled=1",(chat_id,)
    ).fetchone())
    if existing and int(existing["c"])>0:
        return
    def op(conn):
        for slot,time_hm,game_name in DEFAULT_WEEKLY_SCHEDULE:
            conn.execute(
                "INSERT OR IGNORE INTO schedule_cycle(chat_id,slot_index,time_hm,game_name,note,enabled) VALUES(?,?,?,?,?,1)",
                (chat_id,slot,time_hm,game_name,"Базовый цикл через день · 20:00 МСК"),
            )
        conn.commit()
    group_db_op(op)

def schedule_text(chat_id):
    ensure_default_schedule(chat_id)
    rows=group_db_op(lambda conn: conn.execute(
        "SELECT * FROM schedule_cycle WHERE chat_id=? AND enabled=1 ORDER BY slot_index",(chat_id,)
    ).fetchall())
    lines=["𝗦𝗖𝗛𝗘𝗗𝗨𝗟𝗘","","Игры · МСК",""]
    for row in rows:
        slot=int(row["slot_index"])
        day_name=SCHEDULE_CYCLE_DAYS[slot][0] if 0<=slot<7 else str(slot)
        lines.append(f"{day_name} · {row['time_hm']} — {row['game_name']}")
    lines.extend([
        "",
        "После воскресенья цикл начинается со вторника.",
        "Интерактивы проходят через день.",
        "Расписание может изменяться.",
        "О переносах и новых мероприятиях сообщается отдельно.",
    ])
    return "\n".join(lines)

def set_schedule(chat_id,day_text,time_hm,game_name,note=""):
    key=(day_text or "").strip().casefold()
    slot=SCHEDULE_DAY_TO_SLOT.get(next((name for name,_ in SCHEDULE_CYCLE_DAYS if name.casefold()==key), ""))
    if slot is None or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',time_hm):
        raise ValueError("INVALID_SCHEDULE")
    group_db_op(lambda conn:(conn.execute(
        "INSERT INTO schedule_cycle(chat_id,slot_index,time_hm,game_name,note,enabled) VALUES(?,?,?,?,?,1) ON CONFLICT(chat_id,slot_index) DO UPDATE SET time_hm=excluded.time_hm,game_name=excluded.game_name,note=excluded.note,enabled=1",
        (chat_id,slot,time_hm,game_name,note),
    ),conn.commit()))

def delete_schedule(chat_id,day_text):
    key=(day_text or "").strip().casefold()
    name=next((name for name,_ in SCHEDULE_CYCLE_DAYS if name.casefold()==key),None)
    if name is None:
        return False
    slot=SCHEDULE_DAY_TO_SLOT[name]
    return group_db_op(lambda conn:(conn.execute("DELETE FROM schedule_cycle WHERE chat_id=? AND slot_index=?",(chat_id,slot)).rowcount>0,conn.commit())[0])


def get_games(enabled_only=True):
    sql="SELECT * FROM game_catalog"+(' WHERE enabled=1' if enabled_only else '')+" ORDER BY id"
    return group_db_op(lambda conn: conn.execute(sql).fetchall())

def get_game_by_id(game_id, enabled_only=False):
    """Return a single game by id; used by poll callbacks and winner resolution."""
    sql = "SELECT * FROM game_catalog WHERE id=?"
    params = (int(game_id),)
    if enabled_only:
        sql += " AND enabled=1"
    return group_db_op(lambda conn: conn.execute(sql, params).fetchone())

def add_game(name, description="", launch_text=""):
    name=(name or "").strip()
    if not name: raise ValueError("EMPTY_GAME")
    return group_db_op(lambda conn: (conn.execute("INSERT INTO game_catalog(name,description,launch_text,enabled,created_at,created_by) VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET description=excluded.description,launch_text=excluded.launch_text,enabled=1",(name,description,launch_text,1,now(),ADMIN_ID)),conn.commit(),True)[-1])

def remove_game(name):
    return group_db_op(lambda conn: (conn.execute("UPDATE game_catalog SET enabled=0 WHERE lower(name)=lower(?)",(name.strip(),)).rowcount>0, conn.commit())[0])

def compact_game_name(name, limit=24):
    value = str(name)
    return value if len(value) <= limit else value[: limit - 1] + "…"

def is_primary_chat(chat_id: int) -> bool:
    """Allow game/rolemembership features only in the single configured primary chat.
    If PRIMARY_CHAT_ID is unset, a single managed group is accepted as the fallback.
    """
    if not chat_id:
        return False
    if PRIMARY_CHAT_ID:
        return int(chat_id) == PRIMARY_CHAT_ID
    try:
        rows = group_db_op(lambda conn: conn.execute(
            "SELECT group_chat_id FROM managed_group ORDER BY group_chat_id"
        ).fetchall())
        ids = [int(r["group_chat_id"]) for r in rows]
        return len(ids) == 1 and ids[0] == int(chat_id)
    except Exception:
        return False

def require_primary_group(message: Message) -> bool:
    return _is_group_message(message) and is_primary_chat(message.chat.id)

def poll_games(poll) -> list:
    raw = (poll["options_json"] or "").strip() if poll else ""
    if not raw:
        return get_games()
    try:
        ids = [int(x) for x in json.loads(raw)]
    except Exception:
        return get_games()
    if not ids:
        return get_games()
    placeholders = ",".join("?" for _ in ids)
    return group_db_op(lambda conn: conn.execute(
        f"SELECT * FROM game_catalog WHERE enabled=1 AND id IN ({placeholders}) ORDER BY id", ids
    ).fetchall())

def user_mention(user_id: int, name: str, username: str | None = None) -> str:
    if username:
        return f"@{html.escape(username)}"
    safe_name = html.escape(name or f"ID {user_id}")
    return f'<a href="tg://user?id={int(user_id)}">{safe_name}</a>'

def poll_voter_mentions(poll_id: int, game_id: int) -> list[str]:
    rows = group_db_op(lambda conn: conn.execute(
        """SELECT v.user_id, COALESCE(m.first_name, u.first_name, '') AS first_name,
                  COALESCE(m.username, u.username, '') AS username
             FROM game_poll_votes v
             LEFT JOIN group_members m ON m.chat_id=(SELECT chat_id FROM game_polls WHERE id=?) AND m.user_id=v.user_id
             LEFT JOIN users u ON u.user_id=v.user_id
             WHERE v.poll_id=? AND v.game_id=? ORDER BY v.voted_at""",
        (poll_id, poll_id, game_id)
    ).fetchall())
    return [user_mention(int(r["user_id"]), r["first_name"], r["username"]) for r in rows]


def game_poll_keyboard(poll_id, games, current_vote=None):
    rows = []
    for game in games:
        gid = int(game["id"])
        label = compact_game_name(game["name"])
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"gp:v:{poll_id}:{gid}"),
            InlineKeyboardButton(text="ℹ", callback_data=f"gp:i:{poll_id}:{gid}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def poll_duration_keyboard():
    opts=[(15,"15 мин"),(30,"30 мин"),(60,"1 час"),(120,"2 часа"),(360,"6 часов"),(1440,"1 день"),(10080,"7 дней")]
    return InlineKeyboardMarkup(inline_keyboard=[ [InlineKeyboardButton(text=l,callback_data=f"gp:d:{m}") for m,l in opts[i:i+2]] for i in range(0,len(opts),2) ]+[[InlineKeyboardButton(text="Отмена",callback_data="gp:cancel")]])


@dp.message(Command("game_poll"))
async def game_poll_cmd(message: Message):
    if not require_primary_group(message) or not is_group_admin_user(message): return
    ensure_default_schedule(message.chat.id)
    games=get_games()
    if len(games)<2:
        await message.reply("𝗚𝗔𝗠𝗘 𝗣𝗢𝗟𝗟\n\nНужно минимум две игры в списке.")
        return
    open_poll=group_db_op(lambda conn: conn.execute("SELECT 1 FROM game_polls WHERE chat_id=? AND status='OPEN'",(message.chat.id,)).fetchone())
    if open_poll:
        await message.reply("𝗚𝗔𝗠𝗘 𝗣𝗢𝗟𝗟\n\nОпрос уже идёт.")
        return
    await message.reply("𝗚𝗔𝗠𝗘 𝗣𝗢𝗟𝗟\n\nНа сколько оставить опрос?",reply_markup=poll_duration_keyboard())


def game_poll_text(poll, games, counts):
    expires = datetime.fromisoformat(poll["expires_at"]).astimezone(timezone(timedelta(hours=DEFAULT_TIMEZONE_OFFSET_HOURS)))
    total_votes = sum(int(counts.get(int(g["id"]), 0)) for g in games)
    lines = [
        "𝗚𝗔𝗠𝗘 𝗣𝗢𝗟𝗟",
        "",
        "Выберите одну игру. Нажатие на название меняет ваш голос.",
        "Нажмите ℹ рядом с игрой, чтобы открыть описание.",
        "",
    ]
    for game in games:
        lines.append(f"{game['name']} — {counts.get(int(game['id']), 0)}")
    lines.extend([
        "",
        f"Всего голосов: {total_votes}",
        f"Опрос завершится: {expires.strftime('%d.%m %H:%M МСК')}."
    ])
    return "\n".join(lines)


async def refresh_game_poll_message(poll_id):
    poll = group_db_op(lambda conn: conn.execute(
        "SELECT * FROM game_polls WHERE id=?", (poll_id,)
    ).fetchone())
    if not poll or poll["status"] != "OPEN" or not poll["message_id"]:
        return
    counts = {int(r["game_id"]): int(r["votes"]) for r in group_db_op(lambda conn: conn.execute(
        "SELECT game_id, COUNT(*) votes FROM game_poll_votes WHERE poll_id=? GROUP BY game_id", (poll_id,)
    ).fetchall())}
    games = poll_games(poll)
    with suppress(Exception):
        await bot.edit_message_text(
            chat_id=poll["chat_id"],
            message_id=poll["message_id"],
            text=game_poll_text(poll, games, counts),
            reply_markup=game_poll_keyboard(poll_id, games, None),
        )


@dp.callback_query(F.data.startswith("gp:"))
async def game_poll_callback(callback: CallbackQuery):
    if not callback.message or not is_primary_chat(callback.message.chat.id):
        await safe_callback_answer(callback, 'Этот опрос работает только в основном чате.', True)
        return
    parts=(callback.data or '').split(':')
    action=parts[1] if len(parts)>1 else ''

    if action in {'cancel','d'}:
        if not await _assign_access(callback, callback.message.chat.id):
            await safe_callback_answer(callback, 'Создавать и отменять опрос может только администратор.', True)
            return

    if action=='cancel':
        with suppress(Exception):
            await callback.message.delete()
        await safe_callback_answer(callback,'Опрос отменён.')
        return

    if action=='i' and len(parts)==4:
        try:
            pid=int(parts[2]); gid=int(parts[3])
        except ValueError:
            await safe_callback_answer(callback,'Некорректная игра.',True); return
        row=group_db_op(lambda conn: conn.execute(
            "SELECT * FROM game_polls WHERE id=?",(pid,)
        ).fetchone())
        if not row:
            await safe_callback_answer(callback,'Опрос не найден.',True); return
        game=get_game_by_id(gid)
        allowed_ids={int(g["id"]) for g in poll_games(row)}
        if not game or gid not in allowed_ids:
            await safe_callback_answer(callback,'Игра не найдена.',True); return
        description = (str(game['description']).strip() or 'Для этой игры описание пока не добавлено.')[:190]
        await safe_callback_answer(callback, description, True)
        return

    if action=='d' and len(parts)==3:
        try: minutes=int(parts[2])
        except ValueError: await safe_callback_answer(callback,'Некорректная длительность.',True); return
        if minutes<1 or minutes>GAME_POLL_MAX_MINUTES:
            await safe_callback_answer(callback,'Некорректная длительность.',True); return
        expires=datetime.now(timezone.utc)+timedelta(minutes=minutes)
        games=get_games()
        def op(conn):
            cur=conn.execute(
                "INSERT INTO game_polls(chat_id,created_by,created_at,expires_at,status,options_json,tie_round,parent_poll_id) VALUES(?,?,?,?, 'OPEN', ?, 0, NULL)",
                (callback.message.chat.id,callback.from_user.id,now(),expires.isoformat(), json.dumps([int(g["id"]) for g in games])),
            )
            conn.commit(); return cur.lastrowid
        pid=group_db_op(op)
        poll_row = group_db_op(lambda conn: conn.execute(
            "SELECT * FROM game_polls WHERE id=?", (pid,)
        ).fetchone())
        msg=await callback.message.answer(
            game_poll_text(poll_row, games, {}),
            reply_markup=game_poll_keyboard(pid, games, None),
        )
        group_db_op(lambda conn:(conn.execute("UPDATE game_polls SET message_id=? WHERE id=?",(msg.message_id,pid)),conn.commit()))
        with suppress(Exception):
            await bot.pin_chat_message(callback.message.chat.id,msg.message_id,disable_notification=True)
        with suppress(Exception):
            await callback.message.delete()
        asyncio.create_task(expire_game_poll(pid,minutes*60))
        await safe_callback_answer(callback,'Опрос запущен.')
        return

    if len(parts)<3:
        await safe_callback_answer(callback,'Некорректный опрос.',True); return

    try:
        pid=int(parts[2])
    except ValueError:
        await safe_callback_answer(callback,'Некорректный опрос.',True); return

    if action == "r" and len(parts) == 4:
        if not await _assign_access(callback, callback.message.chat.id):
            await safe_callback_answer(callback, 'Решить ничью может только администратор.', True)
            return
        try:
            gid=int(parts[3])
        except ValueError:
            await safe_callback_answer(callback,'Некорректная игра.',True); return
        poll=group_db_op(lambda conn: conn.execute("SELECT * FROM game_polls WHERE id=?",(pid,)).fetchone())
        game=get_game_by_id(gid) if poll else None
        allowed_ids={int(g["id"]) for g in poll_games(poll)} if poll else set()
        if not poll or gid not in allowed_ids or not game:
            await safe_callback_answer(callback,'Игра недоступна.',True); return
        group_db_op(lambda conn:(conn.execute("UPDATE game_polls SET winner_game_id=?, status='CLOSED' WHERE id=?",(gid,pid)),conn.commit()))
        with suppress(Exception):
            await bot.unpin_chat_message(callback.message.chat.id, callback.message.message_id)
        selected_slot=assign_poll_winner_to_next_slot(int(poll["chat_id"]),game["name"],pid)
        voters=poll_voter_mentions(pid,gid)
        voter_block="\n".join(voters) if voters else "Никто не указан."
        schedule_line=(f"Начало по расписанию: {selected_slot[0]} · {selected_slot[1]} МСК" if selected_slot else "Время игры определяется расписанием.")
        final=("𝗚𝗔𝗠𝗘 𝗖𝗛𝗢𝗦𝗘𝗡\n\n"
               f"Победила игра: {game['name']}\n"
               "Результат определён администратором после повторной ничьей.\n\n"
               "За неё проголосовали:\n" + voter_block + "\n\n" + schedule_line + ".")
        with suppress(Exception): await bot.edit_message_text(chat_id=callback.message.chat.id,message_id=callback.message.message_id,text=final,parse_mode="HTML")
        await safe_callback_answer(callback,'Победитель выбран.')
        return

    poll=group_db_op(lambda conn: conn.execute("SELECT * FROM game_polls WHERE id=?",(pid,)).fetchone())
    if not poll or poll['status']!='OPEN':
        await safe_callback_answer(callback,'Опрос уже закрыт.',True); return

    if action=='v' and len(parts)==4:
        try: gid=int(parts[3])
        except ValueError: await safe_callback_answer(callback,'Некорректная игра.',True); return
        game=get_game_by_id(gid)
        allowed_ids={int(g["id"]) for g in poll_games(poll)}
        if not game or not game['enabled'] or gid not in allowed_ids:
            await safe_callback_answer(callback,'Эта игра больше недоступна.',True); return
        group_db_op(lambda conn:(conn.execute(
            "INSERT INTO game_poll_votes(poll_id,user_id,game_id,voted_at) VALUES(?,?,?,?) "
            "ON CONFLICT(poll_id,user_id) DO UPDATE SET game_id=excluded.game_id,voted_at=excluded.voted_at",
            (pid,callback.from_user.id,gid,now()),
        ),conn.commit()))
        await safe_callback_answer(callback,'Выбор сохранён. Его можно изменить.')
    else:
        await safe_callback_answer(callback,'Неизвестное действие.',True); return

    counts={int(r['game_id']):int(r['votes']) for r in group_db_op(lambda conn: conn.execute(
        "SELECT game_id,COUNT(*) votes FROM game_poll_votes WHERE poll_id=? GROUP BY game_id",(pid,)
    ).fetchall())}
    games=poll_games(poll)
    current=group_db_op(lambda conn: conn.execute(
        "SELECT game_id FROM game_poll_votes WHERE poll_id=? AND user_id=?",(pid,callback.from_user.id)
    ).fetchone())
    current_vote=int(current['game_id']) if current else None
    text = game_poll_text(poll, games, counts)
    await callback.message.edit_text(
        text,
        reply_markup=game_poll_keyboard(pid, games, current_vote),
    )

def next_scheduled_slot(chat_id, after_local):
    slot=next_cycle_slot(chat_id, after_local)
    if not slot:
        return None
    candidate,slot_index,time_hm=slot
    return candidate,time_hm


def assign_poll_winner_to_next_slot(chat_id, game_name, poll_id):
    local=datetime.now(timezone.utc)+timedelta(hours=DEFAULT_TIMEZONE_OFFSET_HOURS)
    slot=next_scheduled_slot(chat_id,local)
    if not slot:
        return None
    candidate, time_hm=slot
    run_date=candidate.date().isoformat()
    group_db_op(lambda conn:(conn.execute(
        "INSERT OR REPLACE INTO game_schedule_overrides(chat_id,run_date,time_hm,game_name,source_poll_id,created_at) VALUES(?,?,?,?,?,?)",
        (chat_id,run_date,time_hm,game_name,poll_id,now()),
    ),conn.commit()))
    return run_date,time_hm

async def expire_game_poll(poll_id, seconds):
    await asyncio.sleep(seconds)
    poll = group_db_op(lambda conn: conn.execute(
        "SELECT * FROM game_polls WHERE id=?", (poll_id,)
    ).fetchone())
    if not poll or poll["status"] != "OPEN":
        return

    games = poll_games(poll)
    counts = {int(r["game_id"]): int(r["votes"]) for r in group_db_op(lambda conn: conn.execute(
        "SELECT game_id, COUNT(*) votes FROM game_poll_votes WHERE poll_id=? GROUP BY game_id", (poll_id,)
    ).fetchall())}
    top = max(counts.values(), default=0)
    leaders = sorted([gid for gid, votes in counts.items() if votes == top and top > 0])

    # Close current poll first; the old poll remains as historical record.
    winner_id = leaders[0] if len(leaders) == 1 else None
    group_db_op(lambda conn: (
        conn.execute(
            "UPDATE game_polls SET status='CLOSED', winner_game_id=? WHERE id=? AND status='OPEN'",
            (winner_id, poll_id),
        ),
        conn.commit(),
    ))

    if poll["message_id"]:
        with suppress(Exception):
            await bot.unpin_chat_message(int(poll["chat_id"]), int(poll["message_id"]))

    # No votes: announce and stop.
    if not leaders:
        await bot.send_message(
            int(poll["chat_id"]),
            "𝗚𝗔𝗠𝗘 𝗣𝗢𝗟𝗟 · 𝗙𝗜𝗡𝗔𝗟\n\nНикто не проголосовал. Опрос закрыт.",
        )
        return

    # Tie: launch one short tie-break poll containing only the tied games.
    if len(leaders) > 1:
        if int(poll["tie_round"] or 0) >= 1:
            tie_games = [g for g in games if int(g["id"]) in leaders]
            rows = [
                [InlineKeyboardButton(text=g["name"], callback_data=f"gp:r:{poll_id}:{int(g['id'])}")]
                for g in tie_games
            ]
            await bot.send_message(
                int(poll["chat_id"]),
                "𝗧𝗜𝗘𝗕𝗥𝗘𝗔𝗞 𝗥𝗘𝗦𝗨𝗟𝗧\n\n"
                "Повторная ничья. Администратор выбирает победителя:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
            return
        tie_games = [g for g in games if int(g["id"]) in leaders]
        tie_minutes = min(10, GAME_POLL_MAX_MINUTES)
        expires = datetime.now(timezone.utc) + timedelta(minutes=tie_minutes)
        def op(conn):
            cur = conn.execute(
                """INSERT INTO game_polls(chat_id,created_by,created_at,expires_at,status,options_json,tie_round,parent_poll_id)
                   VALUES(?,?,?,?, 'OPEN', ?, ?, ?)""",
                (int(poll["chat_id"]), int(poll["created_by"]), now(), expires.isoformat(),
                 json.dumps(leaders), int(poll["tie_round"] or 0) + 1, int(poll["id"])),
            )
            conn.commit()
            return cur.lastrowid
        tie_id = group_db_op(op)
        tie_poll = group_db_op(lambda conn: conn.execute("SELECT * FROM game_polls WHERE id=?", (tie_id,)).fetchone())
        msg = await bot.send_message(
            int(poll["chat_id"]),
            game_poll_text(tie_poll, tie_games, {}),
            reply_markup=game_poll_keyboard(tie_id, tie_games),
        )
        group_db_op(lambda conn: (
            conn.execute("UPDATE game_polls SET message_id=? WHERE id=?", (msg.message_id, tie_id)),
            conn.commit(),
        ))
        with suppress(Exception):
            await bot.pin_chat_message(int(poll["chat_id"]), msg.message_id, disable_notification=True)
        asyncio.create_task(expire_game_poll(tie_id, tie_minutes * 60))
        await bot.send_message(
            int(poll["chat_id"]),
            "𝗧𝗜𝗘𝗕𝗥𝗘𝗔𝗞\n\n" + "\n".join(f"• {g['name']} — {counts.get(int(g['id']), 0)}" for g in tie_games)
            + f"\n\nЗапущен короткий дополнительный тур на {tie_minutes} минут.",
        )
        return

    winner = get_game_by_id(winner_id)
    if not winner:
        return

    selected_slot = assign_poll_winner_to_next_slot(int(poll["chat_id"]), winner["name"], poll_id)
    voters = poll_voter_mentions(poll_id, winner_id)
    voter_block = "\n".join(voters) if voters else "Никто не указан."
    schedule_line = (
        f"Начало по расписанию: {selected_slot[0]} · {selected_slot[1]} МСК"
        if selected_slot else "Время игры определяется расписанием."
    )
    final = (
        "𝗚𝗔𝗠𝗘 𝗖𝗛𝗢𝗦𝗘𝗡\n\n"
        f"Победила игра: {winner['name']}\n"
        f"Голосов: {counts.get(int(winner['id']), 0)}\n\n"
        "За неё проголосовали:\n"
        f"{voter_block}\n\n"
        f"{schedule_line}."
    )
    await bot.send_message(int(poll["chat_id"]), final, parse_mode="HTML")

async def game_poll_refresh_worker():
    while True:
        try:
            polls = group_db_op(lambda conn: conn.execute(
                "SELECT id FROM game_polls WHERE status='OPEN'"
            ).fetchall())
            for row in polls:
                await refresh_game_poll_message(int(row["id"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Game poll refresh worker failed")
        await asyncio.sleep(10)


async def game_poll_expiry_recovery_worker():
    while True:
        try:
            rows=group_db_op(lambda conn: conn.execute("SELECT id,expires_at FROM game_polls WHERE status='OPEN'").fetchall())
            now_utc=datetime.now(timezone.utc)
            for row in rows:
                try:
                    expires=datetime.fromisoformat(str(row["expires_at"]))
                    if expires.tzinfo is None: expires=expires.replace(tzinfo=timezone.utc)
                    if expires <= now_utc:
                        await expire_game_poll(int(row["id"]),0)
                except Exception:
                    logger.exception("Poll expiry recovery failed | poll=%s",row["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Game poll expiry recovery worker failed")
        await asyncio.sleep(5)


async def mafia_expiry_recovery_worker():
    while True:
        try:
            cutoff=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
            rows=group_db_op(lambda conn: conn.execute("SELECT id,chat_id,lobby_message_id FROM mafia_games WHERE status='WAITING' AND created_at<=?",(cutoff,)).fetchall())
            for row in rows:
                try:
                    if mafia_stop(int(row["id"])) and row["lobby_message_id"]:
                        with suppress(Exception): await bot.delete_message(int(row["chat_id"]),int(row["lobby_message_id"]))
                        with suppress(Exception): await bot.unpin_chat_message(int(row["chat_id"]),int(row["lobby_message_id"]))
                except Exception:
                    logger.exception("Mafia expiry recovery failed | game=%s",row["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Mafia expiry recovery worker failed")
        await asyncio.sleep(10)


async def schedule_worker():
    """Announce scheduled game time in the primary chat; never auto-launch a game."""
    while True:
        try:
            local = datetime.now(timezone.utc) + timedelta(hours=DEFAULT_TIMEZONE_OFFSET_HOURS)
            if PRIMARY_CHAT_ID:
                slot = next_cycle_slot(PRIMARY_CHAT_ID, local)
                if slot:
                    candidate, slot_index, time_hm = slot
                    delta = candidate - local
                    chat_id = PRIMARY_CHAT_ID
                    # 30-minute reminder. Claimed through schedule_runs to avoid repeats.
                    if timedelta(minutes=29, seconds=50) <= delta <= timedelta(minutes=30, seconds=10):
                        game_name = group_db_op(lambda conn: conn.execute(
                            "SELECT game_name FROM schedule_cycle WHERE chat_id=? AND slot_index=? AND enabled=1",
                            (chat_id, slot_index),
                        ).fetchone())
                        if game_name:
                            run_key = f"reminder30:{candidate.isoformat()}"
                            claimed = group_db_op(lambda conn: (
                                conn.execute(
                                    "INSERT OR IGNORE INTO schedule_runs(chat_id,run_date,time_hm,game_name,source,created_at) VALUES(?,?,?,?,?,?)",
                                    (chat_id, candidate.date().isoformat(), time_hm, game_name["game_name"], run_key, now()),
                                ).rowcount == 1
                            ))
                            if claimed:
                                await bot.send_message(
                                    chat_id,
                                    f"𝗚𝗔𝗠𝗘 𝗦𝗧𝗔𝗥𝗧𝗦\n\n{html.escape(game_name['game_name'])} начнётся через 30 минут.\nВремя начала: {time_hm} МСК.",
                                    parse_mode="HTML",
                                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Schedule worker failed")
        await asyncio.sleep(SCHEDULE_CHECK_SECONDS)


# =========================================================
# MAFIA
# =========================================================

async def _assign_access(callback, chat_id):
    if callback.from_user.id == ADMIN_ID:
        return True
    try:
        member=await bot.get_chat_member(chat_id,callback.from_user.id)
        return getattr(member,"status",None) in {"administrator","creator"}
    except Exception:
        return False


@dp.callback_query(F.data.startswith("fm:role:"))
async def role_assign_button(callback: CallbackQuery, state: FSMContext):
    if not callback.message or not is_primary_chat(callback.message.chat.id):
        await callback.answer("Этот чат не является основным.", show_alert=True)
        return
    parts=(callback.data or "").split(":")
    if len(parts)!=4:
        await callback.answer("Некорректная кнопка.",show_alert=True); return
    chat_id,target_id=int(parts[2]),int(parts[3])
    if not await _assign_access(callback,chat_id):
        await callback.answer("Назначать роль могут только администраторы.",show_alert=True); return
    await state.set_state(RoleAssignSearchState.waiting)
    await state.update_data(chat_id=chat_id,target_id=target_id)
    await callback.message.reply("Напишите название роли участника одним сообщением. Например: Кокоми")
    await callback.answer()


@dp.callback_query(F.data.startswith("fm:close:"))
async def role_assign_close(callback: CallbackQuery):
    await callback.answer("Закрыто.")


@dp.message(RoleAssignSearchState.waiting, F.text)
async def role_assign_from_text(message: Message, state: FSMContext):
    data=await state.get_data()
    chat_id=int(data.get("chat_id",0)); target_id=int(data.get("target_id",0))
    await state.clear()
    if message.chat.type not in {"group","supergroup"} or not is_group_admin_user(message) or message.chat.id!=chat_id:
        await message.reply("𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗠𝗘𝗡𝗧\n\nНет доступа к этому назначению.")
        return
    role=role_for((message.text or "").strip())
    if not role:
        await message.reply("𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗠𝗘𝗡𝗧\n\nТакой роли нет в каталоге 148 ролей.")
        return
    try:
        member=await bot.get_chat_member(chat_id,target_id)
        if not _chat_member_is_active(member):
            await message.reply("𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗠𝗘𝗡𝗧\n\nУчастник больше не находится в чате.")
            return
        target=member.user
        tag,key=assign_role_db_atomic(chat_id,target,role)
        ok,actual=await apply_member_tag(chat_id,target.id,tag)
        if not ok:
            release_role_assignment(chat_id,target.id,key)
            await message.reply("𝗧𝗘𝗟𝗘𝗚𝗥𝗔𝗠 𝗧𝗔𝗚\n\nНе удалось установить тег. Назначение отменено.")
            return
        finalize_role_assignment(chat_id,target.id,key,actual or tag)
        confirm_member(chat_id,target.id)
        lifted=await lift_member_restriction(chat_id,target.id)
        await message.reply(f"✅ Назначено\n{display_username_for_group(target)}\nРоль: {role['name']}\nТег: {actual or tag}\nОграничение снято: {'да' if lifted else 'нет'}")
    except ValueError as exc:
        await message.reply("𝗥𝗢𝗟𝗘 𝗢𝗖𝗖𝗨𝗣𝗜𝗘𝗗\n\nЭта роль уже занята." if str(exc)=="ROLE_OCCUPIED" else "𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗠𝗘𝗡𝗧\n\nНе удалось назначить роль.")
    except Exception:
        logger.exception("Role assignment from welcome button failed")
        await message.reply("𝗥𝗢𝗟𝗘 𝗔𝗦𝗦𝗜𝗚𝗡𝗠𝗘𝗡𝗧\n\nНе удалось назначить роль.")


def _message_with_text(message, text):
    return message.model_copy(update={"text": text, "entities": None})


async def sign_assigned_role_for_user(message: Message):
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return
    rows = group_db_op(lambda conn: conn.execute(
        """SELECT chat_id, role_name, tag FROM group_members
           WHERE user_id=? AND active=1 AND role_key IS NOT NULL
           ORDER BY joined_at DESC""",
        (user_id,),
    ).fetchall())
    if not rows:
        await message.reply(
            "𝗥𝗢𝗟𝗘 𝗧𝗔𝗚\n\n"
            "У тебя пока нет назначенной роли.\n"
            "Попроси администратора назначить её через «калл <роль>»."
        )
        return
    if len(rows) > 1:
        lines = ["𝗥𝗢𝗟𝗘 𝗧𝗔𝗚", "", "Найдено несколько ролей в активных чатах:", ""]
        for row in rows:
            lines.append(f"{row['role_name']} — {row['tag'] or 'тег не установлен'} — чат {row['chat_id']}")
        lines.append("\nВыбери нужную группу, если понадобится повторная установка тега.")
        await message.reply("\n".join(lines))
        return
    row = rows[0]
    role = role_for(row["role_name"])
    if not role:
        await message.reply("𝗥𝗢𝗟𝗘 𝗧𝗔𝗚\n\nНазначенная роль больше не найдена в каталоге.")
        return
    desired = make_tag(role["english"])
    ok, actual = await apply_member_tag(int(row["chat_id"]), user_id, desired)
    if not ok:
        await message.reply("𝗧𝗔𝗚 𝗘𝗥𝗥𝗢𝗥\n\nНе удалось установить Telegram-тег. Проверь права бота в чате.")
        return
    finalize_role_assignment(int(row["chat_id"]), user_id, normalize_role(role["name"]), actual or desired)
    await message.reply(
        "𝗥𝗢𝗟𝗘 𝗧𝗔𝗚\n\n"
        f"Роль: {role['name']}\n"
        f"Тег: {actual or desired}\n\n"
        "Готово."
    )


@dp.message(F.chat.type == "private", F.text.regexp(r"(?iu)^\s*подпиши\s+роль\s*$"))
async def sign_assigned_role_private(message: Message):
    await sign_assigned_role_for_user(message)


def mafia_open_game(chat_id):
    return group_db_op(lambda conn: conn.execute("SELECT * FROM mafia_games WHERE chat_id=? AND status='WAITING' LIMIT 1", (chat_id,)).fetchone())


def mafia_player_rows(game_id):
    return group_db_op(lambda conn: conn.execute("SELECT * FROM mafia_players WHERE game_id=? ORDER BY joined_at", (game_id,)).fetchall())


def mafia_is_banned(chat_id, user_id):
    return bool(group_db_op(lambda conn: conn.execute("SELECT 1 FROM mafia_bans WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()))


async def _pin_lobby_message(chat_id,message_id):
    try:
        await bot.pin_chat_message(chat_id=chat_id,message_id=message_id,disable_notification=True)
    except Exception:
        logger.exception("Could not pin mafia lobby | chat=%s message=%s",chat_id,message_id)


async def _update_mafia_lobby_message(game):
    if not game or not game["lobby_message_id"]:
        return
    players=mafia_player_rows(game["id"])
    try:
        await bot.edit_message_text(chat_id=game["chat_id"],message_id=game["lobby_message_id"],text=mafia_lobby_text(game),reply_markup=mafia_kb(game["id"],len(players)>=5,True))
    except Exception:
        logger.exception("Could not update mafia lobby | game=%s",game["id"])


def mafia_lobby_text(game):
    players=mafia_player_rows(game["id"])
    lines=["𝗠𝗔𝗙𝗜𝗔 𝗟𝗢𝗕𝗕𝗬","","Собираем минимум пять участников для запуска MafiaAzBot.","",f"Участники: {len(players)}/5",""]
    if players:
        lines.extend([f"{i}. {('@'+p['username']) if p['username'] else p['first_name']}" for i,p in enumerate(players,1)])
    else:
        lines.append("Никто не вошёл.")
    lines += ["","Минимум: 5 игроков.","Лобби автоматически закроется через 5 минут, если набор не состоится."]
    return "\n".join(lines)

def mafia_kb(game_id, can_start=False, can_stop=False):
    rows = [[
        InlineKeyboardButton(text="Войти", callback_data=f"mf:join:{game_id}"),
        InlineKeyboardButton(text="Выйти", callback_data=f"mf:leave:{game_id}"),
    ]]
    if can_stop:
        rows.append([InlineKeyboardButton(text="Закрыть", callback_data=f"mf:stop:{game_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mafia_create_game(chat_id, creator):
    def op(conn):
        existing = conn.execute("SELECT * FROM mafia_games WHERE chat_id=? AND status='WAITING' LIMIT 1", (chat_id,)).fetchone()
        if existing:
            return existing
        cur = conn.execute("INSERT INTO mafia_games(chat_id,creator_id,status,created_at,lobby_message_id) VALUES(?,?, 'WAITING',?,NULL)", (chat_id,creator.id,now()))
        game_id = cur.lastrowid
        conn.execute("INSERT INTO mafia_players(game_id,user_id,first_name,username,joined_at) VALUES(?,?,?,?,?)", (game_id,creator.id,creator.first_name or '',creator.username or '',now()))
        conn.commit()
        return conn.execute("SELECT * FROM mafia_games WHERE id=?", (game_id,)).fetchone()
    return group_db_op(op)


def mafia_join(game_id, user):
    def op(conn):
        game = conn.execute("SELECT * FROM mafia_games WHERE id=?", (game_id,)).fetchone()
        if not game or game["status"] != "WAITING": return False, "Игра уже началась или закрыта."
        banned = conn.execute("SELECT 1 FROM mafia_bans WHERE chat_id=? AND user_id=?", (game["chat_id"], user.id)).fetchone()
        if banned: return False, "Вам запрещено участвовать в мафии."
        exists = conn.execute("SELECT 1 FROM mafia_players WHERE game_id=? AND user_id=?", (game_id,user.id)).fetchone()
        if exists: return False, "Вы уже в лобби."
        conn.execute("INSERT INTO mafia_players(game_id,user_id,first_name,username,joined_at) VALUES(?,?,?,?,?)", (game_id,user.id,user.first_name or '',user.username or '',now()))
        conn.commit()
        return True, "Вы вошли в игру."
    return group_db_op(op)


def mafia_leave(game_id,user_id):
    def op(conn):
        game = conn.execute("SELECT * FROM mafia_games WHERE id=?", (game_id,)).fetchone()
        if not game or game["status"] != "WAITING": return False, "Игра уже началась."
        conn.execute("DELETE FROM mafia_players WHERE game_id=? AND user_id=?", (game_id,user_id))
        conn.commit(); return True, "Вы вышли из лобби."
    return group_db_op(op)


def mafia_start(game_id):
    """Compatibility wrapper: MafiaAzBot owns the actual game now."""
    return None, "Внутренняя игра отключена. Используйте передачу в MafiaAzBot."


def mafia_stop(game_id):
    return group_db_op(lambda conn: conn.execute(
        "UPDATE mafia_games SET status='FINISHED',finished_at=? WHERE id=? AND status='WAITING'",
        (now(),game_id)
    ).rowcount > 0)


async def expire_mafia_lobby(chat_id, game_id, message_id):
    await asyncio.sleep(300)
    game = group_db_op(lambda conn: conn.execute("SELECT * FROM mafia_games WHERE id=?", (game_id,)).fetchone())
    if not game or game["status"] != "WAITING":
        return
    if len(mafia_player_rows(game_id)) >= 5:
        return
    mafia_stop(game_id)
    with suppress(Exception):
        await bot.delete_message(chat_id, message_id)
    with suppress(Exception):
        await bot.unpin_chat_message(chat_id, message_id)


@dp.message(Command("mafia"))
async def mafia_cmd(message: Message):
    if message.chat.type not in {"group","supergroup"} or not message.from_user:
        return
    if mafia_is_banned(message.chat.id,message.from_user.id):
        await message.reply("Вам запрещено участвовать в мафии.")
        return
    game=mafia_open_game(message.chat.id)
    if not game:
        game=mafia_create_game(message.chat.id,message.from_user)
        sent=await message.answer(mafia_lobby_text(game),reply_markup=mafia_kb(game["id"],False,True))
        group_db_op(lambda conn:(conn.execute("UPDATE mafia_games SET lobby_message_id=? WHERE id=?",(sent.message_id,game["id"])),conn.commit()))
        await _pin_lobby_message(message.chat.id,sent.message_id)
        asyncio.create_task(expire_mafia_lobby(message.chat.id, game["id"], sent.message_id))
    else:
        if game["lobby_message_id"]:
            await _update_mafia_lobby_message(game)
        else:
            sent=await message.answer(mafia_lobby_text(game),reply_markup=mafia_kb(game["id"],len(mafia_player_rows(game["id"]))>=5,True))
            group_db_op(lambda conn:(conn.execute("UPDATE mafia_games SET lobby_message_id=? WHERE id=?",(sent.message_id,game["id"])),conn.commit()))
            await _pin_lobby_message(message.chat.id,sent.message_id)


@dp.message(Command("mafia_leave"))
async def mafia_leave_cmd(message: Message):
    if not require_primary_group(message):
        return
    game = mafia_open_game(message.chat.id)
    if not game:
        await message.reply("Сейчас нет активного лобби.")
        return
    ok,text=mafia_leave(game["id"], message.from_user.id)
    await message.reply(text)
    if ok:
        active=mafia_open_game(message.chat.id)
        if active:
            await _update_mafia_lobby_message(active)


@dp.callback_query(F.data.startswith("mf:"))
async def mafia_callback(callback: CallbackQuery):
    if not callback.message or not is_primary_chat(callback.message.chat.id):
        await safe_callback_answer(callback, 'Mafia работает только в основном чате.', True)
        return
    parts=(callback.data or '').split(':')
    if len(parts)!=3:
        await safe_callback_answer(callback,'Некорректная кнопка.',True); return
    action, game_raw = parts[1], parts[2]
    try: game_id=int(game_raw)
    except ValueError:
        await safe_callback_answer(callback,'Некорректная игра.',True); return
    game=group_db_op(lambda conn: conn.execute('SELECT * FROM mafia_games WHERE id=?',(game_id,)).fetchone())
    if not game:
        await safe_callback_answer(callback,'Лобби не найдено.',True); return
    if action=='join':
        ok,text=mafia_join(game_id,callback.from_user)
        if not ok:
            await safe_callback_answer(callback,text,True); return
        players=mafia_player_rows(game_id)
        if len(players) >= 5:
            claimed = group_db_op(lambda conn: conn.execute(
                "UPDATE mafia_games SET status='STARTING',started_at=? WHERE id=? AND status='WAITING'",
                (now(),game_id)
            ).rowcount == 1)
            if not claimed:
                await safe_callback_answer(callback,'Мафия уже запускается.')
                return
            try:
                await bot.send_message(callback.message.chat.id, '/start@MafiaAzBot')
                group_db_op(lambda conn: conn.execute(
                    "UPDATE mafia_games SET status='TRANSFERRED',finished_at=? WHERE id=? AND status='STARTING'",
                    (now(),game_id)
                ))
                with suppress(Exception): await callback.message.delete()
                with suppress(Exception): await bot.unpin_chat_message(callback.message.chat.id)
                await safe_callback_answer(callback,'Мафия запускается.')
            except Exception:
                logger.exception('Could not transfer mafia lobby to MafiaAzBot')
                group_db_op(lambda conn: conn.execute(
                    "UPDATE mafia_games SET status='WAITING',started_at=NULL WHERE id=? AND status='STARTING'",
                    (game_id,)
                ))
                await safe_callback_answer(callback,'Не удалось запустить MafiaAzBot. Попробуйте ещё раз.',True)
            return
        await safe_callback_answer(callback,text)
    elif action=='leave':
        ok,text=mafia_leave(game_id,callback.from_user.id); await safe_callback_answer(callback,text,not ok)
    elif action=='start':
        if callback.from_user.id not in {game['creator_id'], ADMIN_ID}:
            await safe_callback_answer(callback,'Нет доступа.',True); return
        players=mafia_player_rows(game_id)
        if len(players)<5:
            await safe_callback_answer(callback,'Нужно минимум 5 игроков.',True); return
        claimed=group_db_op(lambda conn: conn.execute(
            "UPDATE mafia_games SET status='STARTING',started_at=? WHERE id=? AND status='WAITING'",
            (now(),game_id)
        ).rowcount == 1)
        if not claimed:
            await safe_callback_answer(callback,'Мафия уже запускается.',True); return
        try:
            await bot.send_message(callback.message.chat.id, '/start@MafiaAzBot')
        except Exception:
            logger.exception('Could not transfer command to MafiaAzBot')
            group_db_op(lambda conn: conn.execute("UPDATE mafia_games SET status='WAITING',started_at=NULL WHERE id=? AND status='STARTING'",(game_id,)))
            await safe_callback_answer(callback,'Не удалось передать лобби MafiaAzBot.',True); return
        group_db_op(lambda conn: conn.execute("UPDATE mafia_games SET status='TRANSFERRED',finished_at=? WHERE id=? AND status='STARTING'", (now(),game_id)))
        with suppress(Exception): await callback.message.delete()
        with suppress(Exception): await bot.unpin_chat_message(callback.message.chat.id)
        await safe_callback_answer(callback,'Мафия запускается.')
        return
    elif action=='stop':
        if callback.from_user.id not in {game['creator_id'], ADMIN_ID}:
            await safe_callback_answer(callback,'Нет доступа.',True); return
        mafia_stop(game_id)
        with suppress(Exception):
            await callback.message.edit_text('𝗟𝗜𝗙𝗘 𝗢𝗙 𝗝𝗨𝗦𝗧𝗜𝗖𝗘 𝗙𝗔𝗜𝗧𝗘\n\nЛобби закрыто.')
        await safe_callback_answer(callback,'Лобби закрыто.')
        return
    active=mafia_open_game(callback.message.chat.id)
    if active:
        await _update_mafia_lobby_message(active)


# =========================================================
# COMMANDS
# =========================================================

def seed_default_games():
    for name, description, launch_text in GAME_DEFINITIONS:
        add_game(name, description, launch_text)


async def setup_commands():
    base_user = [
        BotCommand(command="start", description="Открыть анонимную обратную связь"),
        BotCommand(command="help", description="Помощь и команды"),
        BotCommand(command="mafia", description="Лобби MafiaAzBot"),
        BotCommand(command="mafia_leave", description="Выйти из мафии"),
        BotCommand(command="spy", description="Запустить игру «Шпион»"),
    ]
    base_admin = [
        *base_user,
        BotCommand(command="game_poll", description="Опрос на игру"),
        BotCommand(command="setrole", description="Назначить роль"),
        BotCommand(command="syncroles", description="Сверить и сохранить роли"),
    ]
    private = [
        BotCommand(command="manage_commands", description="Управление командами"),
        BotCommand(command="addcommand", description="Добавить команду"),
        BotCommand(command="delcommand", description="Удалить команду"),
        BotCommand(command="commands", description="Список команд"),
        BotCommand(command="bindrole", description="Привязать роль"),
        BotCommand(command="roles_audit", description="Аудит ролей"),
    ]
    user_commands=list(base_user)
    admin_commands=list(base_admin)
    private_commands=list(private)
    for row in get_custom_commands():
        try:
            cmd=BotCommand(command=row["command"], description=row["description"][:256])
        except Exception:
            continue
        if row["scope"] in {"all", "group"}:
            user_commands.append(cmd)
        if row["scope"] in {"all", "admin"}:
            admin_commands.append(cmd)
        if row["scope"] == "private":
            private_commands.append(cmd)
    # Clear stale command scopes from previous deployments first.
    with suppress(Exception):
        await bot.set_my_commands([], scope=BotCommandScopeDefault())
    with suppress(Exception):
        await bot.set_my_commands([], scope=BotCommandScopeAllGroupChats())
    with suppress(Exception):
        await bot.set_my_commands([], scope=BotCommandScopeAllChatAdministrators())
    if PRIMARY_CHAT_ID:
        with suppress(Exception):
            await bot.set_my_commands([], scope=BotCommandScopeChat(chat_id=PRIMARY_CHAT_ID))
        with suppress(Exception):
            await bot.set_my_commands([], scope=BotCommandScopeChatAdministrators(chat_id=PRIMARY_CHAT_ID))

    # Private/default user commands. No legacy /schedule command.
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
    if PRIMARY_CHAT_ID:
        # Everyone in the primary chat sees base user commands.
        await bot.set_my_commands(user_commands, scope=BotCommandScopeChat(chat_id=PRIMARY_CHAT_ID))
        # Keep the full useful menu visible in the primary chat; handlers enforce admin-only actions.
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=PRIMARY_CHAT_ID))
    else:
        # Safe fallback for first deployment before PRIMARY_CHAT_ID is configured.
        await bot.set_my_commands(user_commands, scope=BotCommandScopeAllGroupChats())
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeAllChatAdministrators())

    # Owner's private command menu.
    await bot.set_my_commands(admin_commands + private_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))


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


# =========================================================
# =========================================================
# SPYFALL — isolated, bot-led game module
# =========================================================
# This section owns only Spyfall state/tables/callbacks. It does not depend on
# role assignment, Mafia, polls, schedule state, or anonymous feedback.

SPY_MIN_PLAYERS = 3
SPY_MAX_PLAYERS = 10
SPY_ROUND_SECONDS = 8 * 60
SPY_DISCUSSION_SECONDS = 120
SPY_FINAL_VOTE_SECONDS = 120
SPY_LOCATIONS = {
    "Аэропорт": ["Пилот", "Стюард", "Пассажир", "Диспетчер", "Охранник", "Механик", "Таможенник", "Работник багажа", "VIP-гость", "Турист"],
    "Банк": ["Кассир", "Клиент", "Охранник", "Инкассатор", "Менеджер", "Финансист", "Стажёр", "Проверяющий", "Посетитель", "Владелец"],
    "Больница": ["Врач", "Медсестра", "Пациент", "Хирург", "Посетитель", "Санитар", "Администратор", "Лаборант", "Охранник", "Стажёр"],
    "Ресторан": ["Шеф-повар", "Официант", "Клиент", "Бармен", "Критик", "Менеджер", "Курьер", "Владелец", "Стажёр", "Уборщик"],
    "Казино": ["Крупье", "Игрок", "Охранник", "VIP-гость", "Бармен", "Менеджер", "Дилер", "Наблюдатель", "Уборщик", "Администратор"],
    "Пляж": ["Спасатель", "Сёрфер", "Турист", "Пловец", "Продавец", "Фотограф", "Инструктор", "Бармен", "Рыбак", "Работник пляжа"],
    "Школа": ["Учитель", "Ученик", "Директор", "Охранник", "Родитель", "Завуч", "Уборщик", "Психолог", "Практикант", "Библиотекарь"],
    "Театр": ["Актёр", "Режиссёр", "Зритель", "Гример", "Кассир", "Костюмер", "Охранник", "Осветитель", "Критик", "Суфлёр"],
    "Космическая станция": ["Командир", "Инженер", "Учёный", "Врач", "Астронавт", "Техник", "Пилот", "Исследователь", "Связист", "Турист"],
    "Круизный лайнер": ["Капитан", "Матрос", "Пассажир", "Повар", "Бармен", "Музыкант", "Механик", "Врач", "Аниматор", "Уборщик"],
    "Железнодорожный вокзал": ["Машинист", "Пассажир", "Кассир", "Проводник", "Диспетчер", "Охранник", "Уборщик", "Турист", "Начальник станции", "Продавец"],
    "Тюрьма": ["Заключённый", "Охранник", "Начальник", "Адвокат", "Врач", "Посетитель", "Следователь", "Психолог", "Сотрудник", "Журналист"],
}

def _spy_ensure_schema():
    def op(conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(spy_players)").fetchall()}
        if "turn_order" not in cols:
            conn.execute("ALTER TABLE spy_players ADD COLUMN turn_order INTEGER NOT NULL DEFAULT 0")
        if "dm_ready" not in cols:
            conn.execute("ALTER TABLE spy_players ADD COLUMN dm_ready INTEGER NOT NULL DEFAULT 0")
        game_cols = {r["name"] for r in conn.execute("PRAGMA table_info(spy_games)").fetchall()}
        if "turn_count" not in game_cols:
            conn.execute("ALTER TABLE spy_games ADD COLUMN turn_count INTEGER NOT NULL DEFAULT 0")
        if "round_target" not in game_cols:
            conn.execute("ALTER TABLE spy_games ADD COLUMN round_target INTEGER NOT NULL DEFAULT 2")
        conn.commit()
    group_db_op(op)

def _spy_get_game(game_id):
    return group_db_op(lambda conn: conn.execute("SELECT * FROM spy_games WHERE id=?", (int(game_id),)).fetchone())

def _spy_open_game(chat_id):
    return group_db_op(lambda conn: conn.execute("SELECT * FROM spy_games WHERE chat_id=? AND status IN ('WAITING','PLAYING','DISCUSSION','VOTING') ORDER BY id DESC LIMIT 1", (int(chat_id),)).fetchone())

def _spy_players(game_id):
    return group_db_op(lambda conn: conn.execute("SELECT * FROM spy_players WHERE game_id=? AND active=1 ORDER BY turn_order, joined_at", (int(game_id),)).fetchall())

def _spy_player(game_id, user_id):
    return group_db_op(lambda conn: conn.execute("SELECT * FROM spy_players WHERE game_id=? AND user_id=?", (int(game_id), int(user_id))).fetchone())

def _spy_display(row):
    if not row: return "участник"
    return f"@{row['username']}" if row["username"] else (row["display_name"] or str(row["user_id"]))

def _spy_lobby_keyboard(game_id, can_start=False):
    rows = [[InlineKeyboardButton(text="🎮 Участвовать", callback_data=f"sp:join:{game_id}"), InlineKeyboardButton(text="🚪 Выйти", callback_data=f"sp:leave:{game_id}")]]
    if can_start:
        rows.append([InlineKeyboardButton(text="▶️ Начать игру", callback_data=f"sp:start:{game_id}")])
    rows.append([InlineKeyboardButton(text="🔐 Открыть ЛС с ботом", url=f"https://t.me/{BOT_USERNAME}?start=spy_{int(game_id)}")])
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data=f"sp:cancel:{game_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _spy_lobby_text(game_id):
    players = _spy_players(game_id)
    lines = ["𝗦𝗣𝗬𝗙𝗔𝗟𝗟", "", "Нажмите «Участвовать», чтобы войти в игру.", "", f"Участники: {len(players)}/{SPY_MAX_PLAYERS}", "Минимум: 3", ""]
    lines += [f"{i}. {_spy_display(p)}" for i, p in enumerate(players, 1)] or ["Пока никто не присоединился."]
    lines += ["", "После старта присоединиться уже нельзя."]
    return "\n".join(lines)

def _spy_group_controls(game_id):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ℹ️ Правила", callback_data=f"sp:rules:{game_id}")]])

def _spy_guess_keyboard(game_id):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎯 Угадать локацию", callback_data=f"sp:guess:{game_id}")]])

def _spy_question_keyboard(game_id, asker_id):
    game = _spy_get_game(game_id)
    if not game or game["phase"] != "QUESTION" or int(game["current_asker_id"] or 0) != int(asker_id):
        return InlineKeyboardMarkup(inline_keyboard=[])
    target = _spy_player(game_id, int(game["current_target_id"] or 0))
    if not target: return InlineKeyboardMarkup(inline_keyboard=[])
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"❓ Задать вопрос {_spy_display(target)}", callback_data=f"sp:ask:{game_id}")]])

def _spy_answer_keyboard(game_id):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да", callback_data=f"sp:answer:{game_id}:1"), InlineKeyboardButton(text="❌ Нет", callback_data=f"sp:answer:{game_id}:0")]])

def _spy_final_vote_keyboard(game_id):
    rows = []
    for p in _spy_players(game_id):
        rows.append([InlineKeyboardButton(text=f"🕵️ {_spy_display(p)}", callback_data=f"sp:finalvote:{game_id}:{p['user_id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _spy_set_state(game_id, **fields):
    if not fields: return
    allowed = {"status","phase","current_asker_id","current_target_id","expires_at","accusation_suspect_id","updated_at"}
    clean = {k:v for k,v in fields.items() if k in allowed}
    clean["updated_at"] = now()
    sets = ", ".join(f"{k}=?" for k in clean)
    values = list(clean.values()) + [int(game_id)]
    group_db_op(lambda conn: (conn.execute(f"UPDATE spy_games SET {sets} WHERE id=?", values), conn.commit()))

def _spy_next_player(game_id, user_id):
    players = _spy_players(game_id)
    if not players: return None
    for i, p in enumerate(players):
        if int(p["user_id"]) == int(user_id): return players[(i + 1) % len(players)]
    return players[0]

def _spy_round_target(player_count):
    return 2 if player_count <= 5 else 3

def _spy_create_game(chat_id, creator_id):
    return group_db_op(lambda conn: (conn.execute("INSERT INTO spy_games(chat_id,created_by,created_at,status,phase,updated_at) VALUES(?,?,?,?,?,?)", (chat_id, creator_id, now(), "WAITING", "LOBBY", now())), conn.commit(), conn.execute("SELECT * FROM spy_games WHERE id=last_insert_rowid()").fetchone())[-1])

def _spy_join(game_id, user):
    if getattr(user, "is_bot", False): return False, "Боты не участвуют."
    game = _spy_get_game(game_id)
    if not game or game["status"] != "WAITING": return False, "Игра уже началась. Войти больше нельзя."
    if _spy_player(game_id, user.id): return True, "Вы уже участвуете."
    players = _spy_players(game_id)
    if len(players) >= SPY_MAX_PLAYERS: return False, "Достигнут максимум — 10 участников."
    group_db_op(lambda conn: (conn.execute("INSERT INTO spy_players(game_id,user_id,username,display_name,joined_at,turn_order,dm_ready) VALUES(?,?,?,?,?,?,0)", (game_id, int(user.id), (user.username or "").lstrip("@"), user.first_name or "участник", now(), len(players)+1)), conn.commit()))
    return True, "Вы в игре. Откройте ЛС с ботом до старта, чтобы получить секретную карточку."

def _spy_leave(game_id, user_id):
    return group_db_op(lambda conn: (conn.execute("DELETE FROM spy_players WHERE game_id=? AND user_id=?", (game_id,int(user_id))).rowcount>0, conn.commit())[0])

def _spy_mark_dm_ready(game_id, user_id):
    group_db_op(lambda conn: (conn.execute("UPDATE spy_players SET dm_ready=1 WHERE game_id=? AND user_id=?", (int(game_id),int(user_id))), conn.commit()))

def _spy_missing_dm(game_id):
    return group_db_op(lambda conn: conn.execute("SELECT * FROM spy_players WHERE game_id=? AND active=1 AND dm_ready=0 ORDER BY turn_order", (int(game_id),)).fetchall())

def _spy_start_db(game_id, location, spy_id, assignments, order):
    target_rounds = _spy_round_target(len(assignments))
    def op(conn):
        for idx,(uid,role_name,is_spy) in enumerate(assignments):
            conn.execute("UPDATE spy_players SET role_name=?,is_spy=?,active=1,turn_order=? WHERE game_id=? AND user_id=?", (role_name,int(bool(is_spy)),idx,int(game_id),int(uid)))
        first, second = order[0], order[1 % len(order)]
        conn.execute("UPDATE spy_games SET status='PLAYING',phase='QUESTION',started_at=?,expires_at=?,location=?,spy_user_id=?,current_asker_id=?,current_target_id=?,turn_count=0,round_target=?,updated_at=? WHERE id=?", (now(),(datetime.now(timezone.utc)+timedelta(seconds=SPY_ROUND_SECONDS)).isoformat(),location,int(spy_id),int(first),int(second),target_rounds,now(),int(game_id)))
        conn.commit()
    group_db_op(op)

async def _spy_send_card(user_id,location,role_name,is_spy):
    if is_spy:
        text="𝗦𝗣𝗬𝗙𝗔𝗟𝗟\n\nТы — 🕵️ ШПИОН.\n\nТвоя задача — вычислить локацию и не выдать себя. Отвечай и задавай вопросы так, чтобы не раскрыть свою роль.\n\nХорошей игры!"
    else:
        text=f"𝗦𝗣𝗬𝗙𝗔𝗟𝗟\n\nЛокация: {location}\nТвоя роль: {role_name}\n\nНе показывай эту информацию другим игрокам. Задавай вопросы так, чтобы проверить знание места, но не назвать его напрямую.\n\nХорошей игры!"
    await bot.send_message(int(user_id),text)

def _spy_start_round_sync(game_id):
    game=_spy_get_game(game_id); players=_spy_players(game_id)
    if not game or len(players)<SPY_MIN_PLAYERS: return None,"Нужно минимум 3 участника."
    location,role_pool=random.choice(list(SPY_LOCATIONS.items()))
    order=[int(p["user_id"]) for p in players]; random.shuffle(order); spy_id=random.choice(order)
    roles=list(role_pool); random.shuffle(roles); assignments=[]; ri=0
    for uid in order:
        if uid==spy_id: assignments.append((uid,"",True))
        else: assignments.append((uid,roles[ri%len(roles)],False)); ri+=1
    _spy_start_db(game_id,location,spy_id,assignments,order)
    return assignments,None

async def _spy_start_round(game_id):
    missing=_spy_missing_dm(game_id)
    if missing:
        names=", ".join(_spy_display(p) for p in missing[:8])
        more=f" и ещё {len(missing)-8}" if len(missing)>8 else ""
        return False, f"Этим игрокам нужно открыть ЛС с ботом: {names}{more}"
    assignments,error=_spy_start_round_sync(game_id)
    if error: return False,error
    for uid,role_name,is_spy in assignments:
        try:
            await _spy_send_card(uid,_spy_get_game(game_id)["location"],role_name,is_spy)
        except Exception as exc:
            logger.exception("Spy secret card delivery failed | game=%s user=%s",game_id,uid)
            _spy_set_state(game_id,status="WAITING",phase="LOBBY",current_asker_id=None,current_target_id=None,expires_at=None)
            group_db_op(lambda conn: (conn.execute("UPDATE spy_players SET role_name='',is_spy=0 WHERE game_id=?",(game_id,)),conn.commit()))
            return False, f"Не удалось открыть ЛС с {_spy_display(_spy_player(game_id,uid))}. Попросите его снова открыть ЛС с ботом."
    return True,"Игра началась."

async def _spy_prepare_turn_dm(game_id,asker_id):
    game=_spy_get_game(game_id); asker=_spy_player(game_id,asker_id); target=_spy_player(game_id,int(game["current_target_id"] or 0)) if game else None
    if not game or not asker or not target: return
    await bot.send_message(int(asker_id),f"𝗦𝗣𝗬𝗙𝗔𝗟𝗟\n\nТвой ход.\nТы задаёшь вопрос: {_spy_display(target)}\n\nВопрос должен допускать ответ только «Да» или «Нет».",reply_markup=_spy_question_keyboard(game_id,asker_id))
    with suppress(Exception): await bot.send_message(int(game["chat_id"]),f"𝗦𝗣𝗬𝗙𝗔𝗟𝗟 · ХОД\n\nСейчас ходит {_spy_display(asker)}.\nОн задаёт вопрос {_spy_display(target)}.")

async def _spy_start_discussion(game_id):
    game=_spy_get_game(game_id)
    if not game: return
    _spy_set_state(game_id,status="DISCUSSION",phase="DISCUSSION",expires_at=(datetime.now(timezone.utc)+timedelta(seconds=SPY_DISCUSSION_SECONDS)).isoformat())
    with suppress(Exception): await bot.send_message(int(game["chat_id"]),"𝗦𝗣𝗬𝗙𝗔𝗟𝗟 · ОБСУЖДЕНИЕ\n\nВопросы завершены. У вас 2 минуты на обсуждение.\n\nОбсудите подозрения. После этого бот откроет голосование.")

async def _spy_start_final_vote(game_id):
    game=_spy_get_game(game_id)
    if not game:return
    _spy_set_state(game_id,status="VOTING",phase="VOTING",expires_at=(datetime.now(timezone.utc)+timedelta(seconds=SPY_FINAL_VOTE_SECONDS)).isoformat())
    group_db_op(lambda conn:(conn.execute("DELETE FROM spy_votes WHERE game_id=?",(int(game_id),)),conn.commit()))
    with suppress(Exception): await bot.send_message(int(game["chat_id"]),"𝗦𝗣𝗬𝗙𝗔𝗟𝗟 · ГОЛОСОВАНИЕ\n\nВыберите, кого считаете шпионом. Каждый участник голосует один раз.",reply_markup=_spy_final_vote_keyboard(game_id))

async def _spy_finish_message(game_id,winner,reason,location_override=None):
    game=_spy_get_game(game_id)
    if not game:return
    players=_spy_players(game_id); spy_id=int(game["spy_user_id"] or 0); location=location_override or game["location"]; spy=_spy_player(game_id,spy_id)
    _spy_set_state(game_id,status="FINISHED",phase="FINISHED",current_asker_id=None,current_target_id=None)
    with suppress(Exception): await bot.send_message(int(game["chat_id"]),f"𝗦𝗣𝗬𝗙𝗔𝗟𝗟 · ФИНАЛ\n\nПобедитель: {winner}\nПричина: {reason}\n\nЛокация: {location}\nШпион: {_spy_display(spy)}")
    for p in players:
        with suppress(Exception): await bot.send_message(int(p["user_id"]),f"𝗦𝗣𝗬𝗙𝗔𝗟𝗟\n\nРаунд завершён.\nЛокация: {location}\nШпион: {_spy_display(spy)}")

@dp.message(Command("spy"))
async def spy_cmd(message: Message):
    if not require_primary_group(message) or not is_group_admin_user(message): return
    existing=_spy_open_game(message.chat.id)
    if existing:
        await message.reply("𝗦𝗣𝗬𝗙𝗔𝗟𝗟\n\nВ этом чате уже есть активная игра или лобби.",reply_markup=_spy_lobby_keyboard(existing["id"],len(_spy_players(existing["id"]))>=SPY_MIN_PLAYERS) if existing["status"]=="WAITING" else _spy_group_controls(existing["id"]))
        return
    game=_spy_create_game(message.chat.id,message.from_user.id)
    sent=await message.answer(_spy_lobby_text(game["id"]),reply_markup=_spy_lobby_keyboard(game["id"],False))
    with suppress(Exception): await bot.pin_chat_message(message.chat.id,sent.message_id,disable_notification=True)
    group_db_op(lambda conn:(conn.execute("UPDATE spy_games SET lobby_message_id=? WHERE id=?",(sent.message_id,game["id"])),conn.commit()))

async def spy_handle_deep_link(message: Message,game_id:int):
    game=_spy_get_game(game_id)
    if not game or message.chat.type!="private" or not message.from_user:return
    ok,text=_spy_join(game_id,message.from_user) if game["status"]=="WAITING" else (False,"Игра уже началась.")
    if ok:
        _spy_mark_dm_ready(game_id,message.from_user.id)
        await message.answer("𝗦𝗣𝗬𝗙𝗔𝗟𝗟\n\nЛС подключено к игре. Ты готов к старту.\n\nВернись в чат игры — когда ведущий начнёт раунд, я пришлю тебе секретную карточку сюда.")
    else:
        await message.answer(f"𝗦𝗣𝗬𝗙𝗔𝗟𝗟\n\n{text}")

@dp.callback_query(F.data.startswith("sp:"))
async def spy_callback(callback: CallbackQuery):
    parts=(callback.data or "").split(":")
    if len(parts)<3: await callback.answer(); return
    try: action=parts[1]; game_id=int(parts[2]); uid=int(callback.from_user.id)
    except (ValueError,TypeError): await callback.answer("Некорректное действие.",show_alert=True); return
    game=_spy_get_game(game_id)
    if not game: await callback.answer("Игра не найдена.",show_alert=True); return
    if action=="rules":
        await callback.answer("Порядок фиксирован по случайному кругу. Один игрок задаёт один вопрос другому, адресат отвечает только «Да» или «Нет» и автоматически получает следующий ход. После 2–3 кругов — обсуждение и голосование.",show_alert=True); return
    if action=="join":
        if callback.message.chat.id!=game["chat_id"] or game["status"]!="WAITING": await callback.answer("Игра уже началась.",show_alert=True); return
        ok,text=_spy_join(game_id,callback.from_user); players=_spy_players(game_id); await callback.answer(text or "Готово.")
        with suppress(Exception): await callback.message.edit_text(_spy_lobby_text(game_id),reply_markup=_spy_lobby_keyboard(game_id,len(players)>=SPY_MIN_PLAYERS))
        return
    if action=="leave":
        if game["status"]!="WAITING": await callback.answer("Сейчас выйти из игры нельзя.",show_alert=True); return
        _spy_leave(game_id,uid); players=_spy_players(game_id); await callback.answer("Вы вышли.")
        with suppress(Exception): await callback.message.edit_text(_spy_lobby_text(game_id),reply_markup=_spy_lobby_keyboard(game_id,len(players)>=SPY_MIN_PLAYERS))
        return
    if action=="cancel":
        if uid not in {int(game["created_by"]),int(ADMIN_ID)}: await callback.answer("Нет доступа.",show_alert=True); return
        _spy_set_state(game_id,status="CANCELLED",phase="FINISHED")
        with suppress(Exception): await callback.message.edit_text("𝗦𝗣𝗬𝗙𝗔𝗟𝗟\n\nЛобби отменено.",reply_markup=None)
        with suppress(Exception): await bot.unpin_chat_message(callback.message.chat.id,callback.message.message_id)
        await callback.answer("Лобби отменено."); return
    if action=="start":
        if callback.message.chat.id!=game["chat_id"]: await callback.answer("Неверный чат.",show_alert=True); return
        if uid not in {int(game["created_by"]),int(ADMIN_ID)}: await callback.answer("Начать игру может только ведущий или администратор.",show_alert=True); return
        if game["status"]!="WAITING": await callback.answer("Игра уже запущена.",show_alert=True); return
        players=_spy_players(game_id)
        if len(players)<SPY_MIN_PLAYERS: await callback.answer("Нужно минимум 3 участника.",show_alert=True); return
        missing=_spy_missing_dm(game_id)
        if missing:
            names=", ".join(_spy_display(p) for p in missing[:6]); more=f" и ещё {len(missing)-6}" if len(missing)>6 else ""
            await callback.answer(f"Сначала откройте ЛС с ботом: {names}{more}",show_alert=True); return
        ok,msg=await _spy_start_round(game_id)
        if not ok: await callback.answer(msg,show_alert=True); return
        with suppress(Exception): await bot.unpin_chat_message(callback.message.chat.id,callback.message.message_id)
        with suppress(Exception): await callback.message.edit_text("𝗦𝗣𝗬𝗙𝗔𝗟𝗟\n\nРаунд начался.\nПорядок игроков определён случайно.\nСекретные карточки отправлены в ЛС.\nНовые участники больше не принимаются.",reply_markup=_spy_group_controls(game_id))
        await _spy_prepare_turn_dm(game_id,_spy_get_game(game_id)["current_asker_id"])
        await callback.answer("Игра началась."); return
    if action=="ask":
        if callback.message.chat.type!="private" or int(game["current_asker_id"] or 0)!=uid or game["phase"]!="QUESTION": await callback.answer("Сейчас не ваш ход.",show_alert=True); return
        target=_spy_player(game_id,int(game["current_target_id"] or 0))
        if not target: await callback.answer("Цель не найдена.",show_alert=True); return
        _spy_set_state(game_id,phase="ASKING"); await callback.message.answer(f"Напиши один вопрос для {_spy_display(target)}. На него должно быть возможно ответить только «Да» или «Нет»."); await callback.answer("Жду вопрос."); return
    if action=="guess":
        if callback.message.chat.type != "private" or int(game["spy_user_id"] or 0) != uid or game["status"] != "PLAYING" or game["phase"] != "GUESS":
            await callback.answer("Эта кнопка сейчас недоступна.", show_alert=True); return
        _spy_set_state(game_id, phase="GUESS_INPUT")
        await callback.message.answer("Напиши одним сообщением, какую локацию ты угадываешь.")
        await callback.answer()
        return
    if action=="answer":
        if callback.message.chat.type!="private" or game["phase"]!="ANSWERING" or int(game["current_target_id"] or 0)!=uid: await callback.answer("Сейчас не ваш ответ.",show_alert=True); return
        val=parts[3]=="1" if len(parts)>3 else False; target=_spy_player(game_id,uid); next_target=_spy_next_player(game_id,uid); ans="Да" if val else "Нет"
        with suppress(Exception): await bot.send_message(int(game["chat_id"]),f"💬 {_spy_display(target)} → {ans}")
        group_db_op(lambda conn:(conn.execute("UPDATE spy_games SET current_asker_id=?,current_target_id=?,phase='QUESTION',turn_count=turn_count+1,updated_at=? WHERE id=?",(uid,int(next_target["user_id"]),now(),game_id)),conn.commit()))
        with suppress(Exception): await callback.message.edit_reply_markup(reply_markup=None)
        updated=_spy_get_game(game_id)
        if int(updated["turn_count"] or 0)>=len(_spy_players(game_id))*int(updated["round_target"] or 2):
            await _spy_start_discussion(game_id)
        else:
            await _spy_prepare_turn_dm(game_id,uid)
        await callback.answer("Ответ принят. Теперь твой ход — задавай следующий вопрос."); return
    if action=="finalvote":
        if game["phase"]!="VOTING" or not _spy_player(game_id,uid): await callback.answer("Голосование сейчас недоступно.",show_alert=True); return
        suspect_id=int(parts[3])
        if not _spy_player(game_id,suspect_id): await callback.answer("Игрок не найден.",show_alert=True); return
        group_db_op(lambda conn:(conn.execute("INSERT OR REPLACE INTO spy_votes(game_id,voter_id,suspect_id,created_at) VALUES(?,?,?,?)",(game_id,uid,suspect_id,now())),conn.commit()))
        await callback.answer("Голос принят.")
        voters=group_db_op(lambda conn: conn.execute("SELECT COUNT(*) AS c FROM spy_votes WHERE game_id=?",(game_id,)).fetchone())
        if int(voters["c"])>=len(_spy_players(game_id)):
            votes=group_db_op(lambda conn: conn.execute("SELECT suspect_id,COUNT(*) AS c FROM spy_votes WHERE game_id=? GROUP BY suspect_id ORDER BY c DESC,suspect_id",(game_id,)).fetchall())
            top=int(votes[0]["suspect_id"]) if votes else 0; top_count=int(votes[0]["c"]) if votes else 0
            tied=len(votes)>1 and int(votes[1]["c"])==top_count
            suspect=_spy_player(game_id,top) if top else None
            if suspect and int(suspect["is_spy"])==1 and not tied:
                _spy_set_state(game_id,status="PLAYING",phase="GUESS",current_asker_id=None,current_target_id=None,expires_at=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat())
                with suppress(Exception): await bot.send_message(int(game["chat_id"]),f"𝗦𝗣𝗬𝗙𝗔𝗟𝗟\n\nШпион найден: {_spy_display(suspect)}\n\nТеперь у шпиона последняя попытка угадать локацию.")
                with suppress(Exception): await bot.send_message(int(game["spy_user_id"]),"𝗦𝗣𝗬𝗙𝗔𝗟𝗟\n\nТебя вычислили. У тебя есть последняя попытка угадать локацию.",reply_markup=_spy_guess_keyboard(game_id))
            else:
                await _spy_finish_message(game_id,"шпион","Игроки не смогли однозначно вычислить шпиона.")
        return
    await callback.answer()

@dp.message(F.chat.type == "private", F.text)
async def spy_private_text(message: Message):
    if not message.from_user or not message.text or message.text.startswith("/"): return
    game=group_db_op(lambda conn: conn.execute("SELECT * FROM spy_games WHERE status IN ('PLAYING','DISCUSSION','VOTING') AND (current_asker_id=? OR current_target_id=? OR spy_user_id=?) ORDER BY id DESC LIMIT 1",(message.from_user.id,message.from_user.id,message.from_user.id)).fetchone())
    if not game:return
    uid=int(message.from_user.id)
    if game["phase"]=="ASKING" and int(game["current_asker_id"] or 0)==uid:
        text=message.text.strip()
        if not text or len(text)>300 or "?" not in text:
            await message.answer("Напиши один короткий вопрос со знаком ?, на который можно ответить только «Да» или «Нет»."); return
        target=_spy_player(game["id"],int(game["current_target_id"] or 0))
        if not target:return
        await bot.send_message(int(game["chat_id"]),f"❓ {_spy_display(_spy_player(game['id'],uid))} → {_spy_display(target)}\n\n{text}")
        _spy_set_state(game["id"],phase="ANSWERING")
        await bot.send_message(int(target["user_id"]),f"𝗦𝗣𝗬𝗙𝗔𝗟𝗟\n\nТебе задали вопрос:\n\n«{text}»\n\nОтветь кнопкой:",reply_markup=_spy_answer_keyboard(game["id"]))
        return
    if game["phase"]=="GUESS_INPUT" and int(game["spy_user_id"] or 0)==uid:
        guess=unicodedata.normalize("NFKC",message.text.strip()).casefold(); location=unicodedata.normalize("NFKC",game["location"]).casefold()
        if guess==location: await _spy_finish_message(game["id"],"шпион",f"Шпион угадал локацию: {game['location']}")
        else: await _spy_finish_message(game["id"],"мирные игроки",f"Шпион не угадал локацию. Его ответ: {message.text.strip()}")

async def _spy_recovery_worker():
    await asyncio.sleep(5)
    while True:
        try:
            games=group_db_op(lambda conn: conn.execute("SELECT * FROM spy_games WHERE status IN ('PLAYING','DISCUSSION','VOTING')").fetchall())
            current=datetime.now(timezone.utc)
            for game in games:
                if not game["expires_at"]: continue
                try:
                    expires=datetime.fromisoformat(str(game["expires_at"]))
                    if expires.tzinfo is None: expires=expires.replace(tzinfo=timezone.utc)
                    if expires>current: continue
                    if game["phase"] in {"QUESTION","ASKING","ANSWERING","PLAYING"}: await _spy_start_discussion(int(game["id"]))
                    elif game["phase"]=="DISCUSSION": await _spy_start_final_vote(int(game["id"]))
                    elif game["phase"]=="VOTING": await _spy_finish_message(int(game["id"]),"шпион","Время голосования вышло.")
                    elif game["phase"] in {"GUESS","GUESS_INPUT"}: await _spy_finish_message(int(game["id"]),"мирные игроки","Шпион не успел назвать локацию.")
                except Exception: logger.exception("Spy recovery expiry failed | game=%s",game["id"])
        except asyncio.CancelledError: raise
        except Exception: logger.exception("Spy recovery worker failed")
        await asyncio.sleep(5)

# Custom slash-command fallback MUST be registered after all built-in handlers.
# aiogram stops on the first matching handler, so registering this before /mafia,
# /roles, etc. would swallow native commands that are not custom commands.
@dp.message(F.text.regexp(r"^\s*/[A-Za-z][A-Za-z0-9_]{0,31}(?:@[A-Za-z0-9_]+)?(?:\s.*)?$"))
async def custom_command_dispatch(message: Message):
    if message.chat.type in {"group", "supergroup"} and not is_primary_chat(message.chat.id):
        return
    text = message.text or ""
    m = re.match(r"^\s*/([A-Za-z][A-Za-z0-9_]{0,31})", text)
    if not m:
        return
    command = m.group(1).lower()
    row = get_custom_command(command)
    if not row or not _custom_command_allowed(row, message):
        return
    await message.reply(render_custom_response(row["response"], message))


async def main():
    global DELIVERY_QUEUE, BROADCAST_QUEUE, FATAL_EVENT

    logger.info("Starting Anonymous Feedback Bot...")

    init_db()
    migrate_db()
    init_group_db()
    seed_role_catalog()
    seed_default_games()
    migrate_group_state()

    _spy_ensure_schema()
    asyncio.create_task(_spy_recovery_worker())

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
    asyncio.create_task(game_poll_refresh_worker())
    asyncio.create_task(game_poll_expiry_recovery_worker())
    asyncio.create_task(mafia_expiry_recovery_worker())
    asyncio.create_task(schedule_worker())

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


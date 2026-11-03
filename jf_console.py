"""Owner-only Telegram console, project memory, owner message journal and DB backup.

The owner DM is a human-readable control surface. SQLite remains the source of
truth. A pinned, replaceable SQLite backup document in the owner's private chat
allows recovery when a free host loses its ephemeral filesystem.
"""
from __future__ import annotations

import gzip
import hashlib
import html
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger("justice-faite-console")
PROJECT_VERSION = "0.7.6"
BACKUP_MARKER = "JF_DB_BACKUP_V1"
BACKUP_INTERVAL_SECONDS = 300
CHAT_LOG_RETENTION_DAYS = max(1, min(int(os.getenv("JF_CHAT_LOG_RETENTION_DAYS", "30")), 365))
CHAT_LOG_MAX_ROWS = max(1000, min(int(os.getenv("JF_CHAT_LOG_MAX_ROWS", "25000")), 200000))
RUNTIME_LOG_MAX_ROWS = max(500, min(int(os.getenv("JF_RUNTIME_LOG_MAX_ROWS", "10000")), 100000))
CHAT_ARCHIVE_ENABLED = os.getenv("JF_CHAT_ARCHIVE_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _pack_html_blocks(blocks, max_chars: int = 3500):
    """Pack complete HTML blocks without slicing tags/entities in half."""
    chunks, current = [], ""
    for block in blocks:
        block = str(block)
        if len(block) > max_chars:
            block = block[:max_chars - 40] + "\n<i>…запись сокращена…</i>"
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _is_sensitive_message(value: str) -> bool:
    """Detect common credentials before any chat text is stored in the archive."""
    patterns = (
        r"\b\d{5,12}:[A-Za-z0-9_-]{25,}\b",  # Telegram bot token
        r"(?i)\b(?:password|passwd|пароль|bot[_ -]?token|api[_ -]?key|secret|access[_ -]?token|refresh[_ -]?token)\s*[:=]",
        r"(?i)\b(?:ключ доступа|секретный ключ)\s*[:=]",
        r"(?i)\bauthorization\s*[:=]\s*bearer\s+\S+",
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}",
        r"(?i)\b(?:x-api-key|client_secret|private_key)\s*[:=]",
        r"(?i)[?&](?:token|access_token|api_key|secret)=([^&\s]+)",
    )
    return any(re.search(pattern, value or "") for pattern in patterns)


SEED_MEMORY = (
    ("core", "Цель проекта", "Единый Telegram-бот Justice Faite Manager: игра «Шпион», инструменты управления чатом, профили, статистика, сообщества, события и личная панель владельца. Общие пользователи, права и настройки; модули не должны дублировать друг друга.", "Требования владельца"),
    ("spy", "Игра «Шпион»", "Лобби на 3–15 игроков; настраиваемые 1–3 шпиона; категории секретных слов/локаций; приватная раздача ролей; последовательные круги вопросов и ответов; обсуждение; голосование; финальная догадка шпиона; таймеры, ничьи, выходы, отмена и восстановление после перезапуска. Игровая панель в группе редактируется, а личные карточки ролей отправляются отдельно.", "Требования владельца"),
    ("console", "Личная консоль и память", "В личке владельца отдельные редактируемые сообщения: состояние системы, активные игры, модули, роли участников и память проекта. Сообщения владельца в личном чате архивируются (кроме команд и очевидных секретов), а база SQLite остаётся источником истины. Резервная копия базы обновляется примерно каждые 5 минут и хранится в закреплённом документе личного чата, чтобы восстановить данные после потери локального диска.", "Требования владельца"),
    ("roles", "Роли участников и Telegram-теги", "Роль участника определяется по Telegram member tag / sender_tag, если он задан и сопоставляется с каталогом ролей. Тег из сообщения сохраняется сразу, без лишнего API-запроса. Если поле отсутствует, используется синхронизация getChatMember как резервный способ. Роли, теги и история должны храниться в базе и попадать в резервную копию.", "Требования владельца и Telegram Bot API"),
    ("moderation", "Модерация и безопасность", "Внутренние ранги и наследование прав; журнал действий; баны, муты, предупреждения, кики; доступ к командам по рангам и персональные исключения; триггеры; антиспам/антирейд; фильтры входа; репорты и голосование за бан. Все опасные административные действия подтверждаются и проверяют реальные права Telegram.", "Анализ Iris"),
    ("community", "Профили и сообщества", "Профили с добровольными полями и настройкой приватности; ник, звание и девиз; статистика и рейтинги; репутация, награды, заметки, закладки; кланы и кружки; сетка чатов. Данные пользователя общие, а данные конкретного чата ограничены этим чатом.", "Анализ Iris"),
    ("games", "Развлечения и события", "Дуэли и кубы без денежных ставок и наказаний; случайные команды; сезонные и тематические события с ресурсами, коллекциями, лимитами, добровольным участием, чёрными списками и рейтингами. Нужны защита от накрутки и журнал начислений.", "Анализ Iris"),
    ("style", "Стиль сообщений", "Не использовать декоративные разделительные линии и лишние тире. Отделять смысловые блоки пустой строкой. В начале пунктов использовать подходящие эмодзи.", "Предпочтение владельца"),
    ("platform", "Ограничения Telegram", "Не выдавать сообщения бота за личные сообщения владельца в группах. Business-подключение годится для части личных диалогов, но не заменяет полноценный пользовательский аккаунт. Приоритет — нативные Bot API-возможности и собственные аналоги команд других ботов.", "Проверка Telegram Bot API"),
    ("iris_moderation", "Полный план модерации Iris", "Адаптировать ранги 0–5 и иерархию прав; назначение, повышение, понижение и снятие; историю рангов; предупреждения с лимитами и сроками; временные/постоянные баны; муты; кики; причины и списки наказаний; доступ команд по рангу и личные исключения; журнал ДК; триггеры и автоматические наказания; безопасную чистку; фильтры входа, антиспам и антирейд; репорты и голосование за бан. Все действия должны проверять реальные права Telegram и оставлять аудит-запись.", "Материалы Iris, присланные владельцем"),
    ("iris_chat", "План управления Telegram-чатами", "Правила, приветствия, описание и название чата, закрепы, ссылки-приглашения, запрет/разрешение типов ссылок, лимиты приглашений, фильтры новых участников, чистка неактива и новичков, настройки доступа, журнал изменений и сеть связанных чатов с общими ролями и блокировками. VK Callback API, дежурные и VK-специфичные команды не копировать напрямую.", "Материалы Iris, присланные владельцем"),
    ("iris_profiles", "Профили и статистика участников", "Карточка пользователя; никнейм, звание, описание и девиз; скрытие/открытие профиля; гражданство чата и его приватность; подписки и достижения; статистика сообщений за сутки/неделю/месяц/всё время; почасовые графики, активность, олды, новички, молчуны и рейтинги. Персональные сведения должны быть добровольными и управляться настройками приватности.", "Материалы Iris, присланные владельцем"),
    ("iris_social", "Социальные модули Iris", "Кланы и кружки с приглашениями, вступлением, выходом, списками участников и правами лидеров; репутация; награды с описанием и журналом выдачи; заметки и закладки с чатбуком; таймеры; локальные и глобальные рейтинги. Разные чаты не должны случайно смешивать локальные данные.", "Материалы Iris, присланные владельцем"),
    ("iris_events", "Сезонные и тематические события", "Вдохновение: Валентинки и «Сладость или гадость». Нужны добровольное участие и выход без потери честно заработанной статистики, коллекции/ресурсы, лимиты и кулдауны, подарки (в том числе анонимные), личный чёрный список, уведомления, дневные и общие рейтинги, команды/лиги, защита от повторной выдачи наград и накрутки. Не использовать реальные денежные ставки или платёжные механики без отдельного требования и правовой проверки.", "Материалы Iris, присланные владельцем"),
    ("spy_engine", "Архитектура игры «Шпион»", "Сделать игру как конечный автомат с явными состояниями и допустимыми переходами. Все ходы должны иметь номер круга и текущего игрока; callback-кнопки должны проверять game_id, версию состояния и право игрока. Таймеры и результаты сохранять в SQLite; после рестарта восстанавливать только непросроченные игры. Защититься от двойного старта, повторного голосования, устаревших кнопок, одновременных нажатий и дублирующих задач.", "Требования владельца и технический план"),
)


class Store:
    def __init__(self, path: str, lock=None):
        self.path = str(path)
        self.lock = lock or threading.RLock()

    def run(self, callback):
        with self.lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                result = callback(conn)
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
                CREATE TABLE IF NOT EXISTS jf_console_messages (
                    section TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jf_project_memory (
                    key TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jf_owner_message_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_jf_owner_message_log_created
                    ON jf_owner_message_log(created_at DESC);
                CREATE TABLE IF NOT EXISTS jf_chat_message_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'in',
                    sender_user_id INTEGER NOT NULL DEFAULT 0,
                    sender_chat_id INTEGER,
                    sender_username TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    message_type TEXT NOT NULL DEFAULT 'text',
                    body TEXT NOT NULL DEFAULT '',
                    reply_to_message_id INTEGER,
                    message_at TEXT NOT NULL,
                    edited_at TEXT,
                    stored_at TEXT NOT NULL,
                    UNIQUE(chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_jf_chat_message_log_date
                    ON jf_chat_message_log(chat_id, message_at DESC);
                CREATE INDEX IF NOT EXISTS idx_jf_chat_message_log_sender
                    ON jf_chat_message_log(chat_id, sender_user_id, message_at DESC);
                CREATE TABLE IF NOT EXISTS jf_runtime_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    logger_name TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jf_runtime_log_date
                    ON jf_runtime_log(created_at DESC);
            """)
            self._prune_archive_conn(conn)
            for category, title, body, source in SEED_MEMORY:
                conn.execute(
                    "INSERT OR IGNORE INTO jf_project_memory(key,category,title,body,source,updated_at) VALUES(?,?,?,?,?,?)",
                    (category, category, title, body, source, utcnow()),
                )
        self.run(op)

    def memory(self):
        return self.run(lambda c: c.execute(
            "SELECT category,title,body,source,updated_at FROM jf_project_memory ORDER BY rowid"
        ).fetchall())

    def save_memory(self, category: str, title: str, body: str, source: str = "Owner via Telegram"):
        self.run(lambda c: c.execute(
            "INSERT INTO jf_project_memory(key,category,title,body,source,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET category=excluded.category,title=excluded.title,"
            "body=excluded.body,source=excluded.source,updated_at=excluded.updated_at",
            (category, category, title, body, source, utcnow()),
        ))

    def archive_owner_message(self, chat_id: int, user_id: int, message_id: int, body: str, created_at: str | None = None) -> bool:
        text = (body or "").strip()
        if not text or text.startswith("/") or len(text) > 8000 or _is_sensitive_message(text):
            return False
        def op(conn):
            cur = conn.execute(
                "INSERT OR IGNORE INTO jf_owner_message_log(chat_id,user_id,message_id,body,created_at) VALUES(?,?,?,?,?)",
                (int(chat_id), int(user_id), int(message_id), text, created_at or utcnow()),
            )
            return cur.rowcount == 1
        return bool(self.run(op))

    def recent_owner_messages(self, limit: int = 12):
        return self.run(lambda c: c.execute(
            "SELECT body,created_at FROM jf_owner_message_log ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 50)),),
        ).fetchall())

    def search_owner_messages(self, query: str, limit: int = 20):
        needle = query.strip().casefold()
        if not needle:
            return []
        rows = self.run(lambda c: c.execute(
            "SELECT body,created_at FROM jf_owner_message_log ORDER BY id DESC LIMIT 5000"
        ).fetchall())
        found = [row for row in rows if needle in str(row["body"]).casefold()]
        return found[:max(1, min(int(limit), 50))]

    def search_project_memory(self, query: str, limit: int = 20):
        needle = query.strip().casefold()
        if not needle:
            return []
        rows = self.memory()
        found = [row for row in rows if needle in (str(row["title"]) + "\n" + str(row["body"])).casefold()]
        return found[:max(1, min(int(limit), 50))]

    def owner_message_count(self) -> int:
        try:
            return int(self.run(lambda c: c.execute("SELECT COUNT(*) FROM jf_owner_message_log").fetchone()[0]))
        except sqlite3.Error:
            return 0

    @staticmethod
    def _prune_archive_conn(conn):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=CHAT_LOG_RETENTION_DAYS)).isoformat(timespec="seconds")
        # Retention follows the original message timestamp; editing an old
        # message must not extend the archive lifetime for that content.
        conn.execute("DELETE FROM jf_chat_message_log WHERE message_at < ?", (cutoff,))
        conn.execute("DELETE FROM jf_runtime_log WHERE created_at < ?", (cutoff,))
        conn.execute(
            "DELETE FROM jf_chat_message_log WHERE id NOT IN "
            "(SELECT id FROM jf_chat_message_log ORDER BY id DESC LIMIT ?)",
            (CHAT_LOG_MAX_ROWS,),
        )
        conn.execute(
            "DELETE FROM jf_runtime_log WHERE id NOT IN "
            "(SELECT id FROM jf_runtime_log ORDER BY id DESC LIMIT ?)",
            (RUNTIME_LOG_MAX_ROWS,),
        )

    def archive_chat_message(self, *, chat_id: int, message_id: int, direction: str,
                             sender_user_id: int = 0, sender_chat_id: int | None = None,
                             sender_username: str = "", sender_name: str = "",
                             message_type: str = "text", body: str = "",
                             reply_to_message_id: int | None = None,
                             message_at: str | None = None, edited_at: str | None = None) -> bool:
        if not CHAT_ARCHIVE_ENABLED or int(message_id or 0) <= 0:
            return False
        text = str(body or "").strip()
        if _is_sensitive_message(text):
            text = "[сообщение скрыто: похоже на секрет или учётные данные]"
        if len(text) > 4000:
            text = text[:3980] + "… [обрезано]"
        try:
            def op(conn):
                cur = conn.execute(
                    "INSERT INTO jf_chat_message_log "
                    "(chat_id,message_id,direction,sender_user_id,sender_chat_id,sender_username,sender_name,"
                    "message_type,body,reply_to_message_id,message_at,edited_at,stored_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(chat_id,message_id) DO UPDATE SET "
                    "direction=excluded.direction,sender_user_id=excluded.sender_user_id,"
                    "sender_chat_id=excluded.sender_chat_id,sender_username=excluded.sender_username,"
                    "sender_name=excluded.sender_name,message_type=excluded.message_type,body=excluded.body,"
                    "reply_to_message_id=excluded.reply_to_message_id,"
                    "edited_at=COALESCE(excluded.edited_at,jf_chat_message_log.edited_at),stored_at=excluded.stored_at",
                    (int(chat_id), int(message_id), direction if direction in {"in", "out"} else "in",
                     int(sender_user_id or 0), int(sender_chat_id) if sender_chat_id is not None else None,
                     str(sender_username or "")[:128], str(sender_name or "")[:256],
                     str(message_type or "unknown")[:40], text, int(reply_to_message_id) if reply_to_message_id else None,
                     message_at or utcnow(), edited_at, utcnow()),
                )
                self._prune_archive_conn(conn)
                return cur.rowcount > 0
            return bool(self.run(op))
        except sqlite3.Error:
            return False

    def archive_runtime_log(self, created_at: str, level: str, logger_name: str, message: str) -> bool:
        text = str(message or "").strip()
        if not text or _is_sensitive_message(text):
            return False
        text = text[:6000]
        try:
            def op(conn):
                cur = conn.execute(
                    "INSERT INTO jf_runtime_log(created_at,level,logger_name,message) VALUES(?,?,?,?)",
                    (created_at or utcnow(), str(level or "INFO")[:20], str(logger_name or "")[:160], text),
                )
                self._prune_archive_conn(conn)
                return cur.rowcount == 1
            return bool(self.run(op))
        except sqlite3.Error:
            return False

    def chat_message_count(self, chat_id: int | None = None) -> int:
        try:
            if chat_id is None:
                return int(self.run(lambda c: c.execute("SELECT COUNT(*) FROM jf_chat_message_log").fetchone()[0]))
            return int(self.run(lambda c: c.execute("SELECT COUNT(*) FROM jf_chat_message_log WHERE chat_id=?", (int(chat_id),)).fetchone()[0]))
        except sqlite3.Error:
            return 0

    def runtime_log_count(self) -> int:
        try:
            return int(self.run(lambda c: c.execute("SELECT COUNT(*) FROM jf_runtime_log").fetchone()[0]))
        except sqlite3.Error:
            return 0

    def recent_chat_messages(self, chat_id: int, limit: int = 12):
        return self.run(lambda c: c.execute(
            "SELECT direction,sender_username,sender_name,message_type,body,message_at,edited_at "
            "FROM jf_chat_message_log WHERE chat_id=? ORDER BY message_at DESC,id DESC LIMIT ?",
            (int(chat_id), max(1, min(int(limit), 100))),
        ).fetchall())

    def search_chat_messages(self, chat_id: int, query: str, limit: int = 20):
        needle = query.strip().casefold()
        if not needle:
            return []
        rows = self.run(lambda c: c.execute(
            "SELECT direction,sender_username,sender_name,message_type,body,message_at "
            "FROM jf_chat_message_log WHERE chat_id=? ORDER BY message_at DESC LIMIT 10000",
            (int(chat_id),),
        ).fetchall())
        found = [row for row in rows if needle in str(row["body"]).casefold() or needle in str(row["sender_name"]).casefold() or needle in str(row["sender_username"]).casefold()]
        return found[:max(1, min(int(limit), 50))]

    def recent_runtime_logs(self, limit: int = 20):
        return self.run(lambda c: c.execute(
            "SELECT created_at,level,logger_name,message FROM jf_runtime_log ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall())

    def search_runtime_logs(self, query: str, limit: int = 20):
        needle = query.strip().casefold()
        if not needle:
            return []
        rows = self.recent_runtime_logs(500)
        found = [row for row in rows if needle in (str(row["logger_name"]) + " " + str(row["message"])).casefold()]
        return found[:max(1, min(int(limit), 50))]

    def set_message(self, section: str, chat_id: int, message_id: int):
        self.run(lambda c: c.execute(
            "INSERT INTO jf_console_messages(section,chat_id,message_id,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(section) DO UPDATE SET chat_id=excluded.chat_id,message_id=excluded.message_id,updated_at=excluded.updated_at",
            (section, int(chat_id), int(message_id), utcnow()),
        ))

    def get_message(self, section: str):
        try:
            return self.run(lambda c: c.execute(
                "SELECT chat_id,message_id,updated_at FROM jf_console_messages WHERE section=?", (section,)
            ).fetchone())
        except sqlite3.Error:
            return None


class SQLiteRuntimeLogHandler(logging.Handler):
    """Copy application diagnostics into SQLite so the pinned DB backup contains them."""
    def __init__(self, store: Store):
        super().__init__(level=logging.INFO)
        self.store = store

    def emit(self, record):
        try:
            message = record.getMessage()
            if record.exc_info:
                import traceback
                message += "\n" + "".join(traceback.format_exception(*record.exc_info))
            created_at = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds")
            self.store.archive_runtime_log(created_at, record.levelname, record.name, message)
        except Exception:
            # Logging must never break a bot handler or recursively log its own failure.
            return


class FloodOutgoingArchiveMiddleware:
    """Capture the bot's successful outgoing group messages at the aiogram API layer."""
    def __init__(self, console):
        self.console = console

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        try:
            self.console._archive_outgoing_api_result(method, result)
        except Exception:
            logger.exception("Could not archive an outgoing Telegram message")
        return result


class Console:
    def __init__(self, dp, bot, F, db_path, lock=None, owner_id=0, primary_chat_id=0):
        self.dp, self.bot, self.F = dp, bot, F
        self.store = Store(db_path, lock)
        self.owner_id = int(owner_id or 0)
        self.primary_chat_id = int(primary_chat_id or 0)
        self.registered = False
        self._last_backup_signature = None
        self._last_backup_error = None
        self._roles_page = 0
        self._roles_page_size = 20
        # Wired by bot.py after the role synchronizer is defined.
        self.role_sync = None

    def init(self):
        self.store.init()
        if self.bot is not None:
            root = logging.getLogger()
            marker = f"_jf_sqlite_runtime_log_{os.path.abspath(self.store.path)}"
            if not any(getattr(handler, marker, False) for handler in root.handlers):
                handler = SQLiteRuntimeLogHandler(self.store)
                setattr(handler, marker, True)
                root.addHandler(handler)

    def register(self):
        if self.registered:
            return
        private = self.F.chat.type == "private"
        self.dp.message.register(self.handle_open, private, Command("jf_console"))
        self.dp.message.register(self.handle_refresh, private, Command("jf_console_refresh"))
        self.dp.message.register(self.handle_memory, private, Command("jf_memory"))
        self.dp.message.register(self.handle_memory_save, private, Command("jf_memory_save"))
        self.dp.message.register(self.handle_memory_search, private, Command("jf_memory_search"))
        self.dp.message.register(self.handle_chatlog, private, Command("jf_chatlog"))
        # Outer middlewares archive context without consuming or changing messages.
        self.dp.message.outer_middleware(self.owner_message_middleware)
        self.dp.message.outer_middleware(self.group_message_middleware)
        # Edited messages are a separate Telegram update type; a passive handler
        # keeps that update enabled and stores revisions of flood messages.
        edited_observer = getattr(self.dp, "edited_message", None)
        if edited_observer is not None:
            edited_observer.register(self.archive_edited_message_event)
        self._install_outgoing_message_archive()
        self.dp.callback_query.register(self.handle_callback, self.F.data.startswith("jfcon:"))
        self.registered = True

    def is_owner_private(self, event) -> bool:
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None)
        if chat is None:
            chat = getattr(getattr(event, "message", None), "chat", None)
        return bool(self.owner_id and user and int(getattr(user, "id", 0) or 0) == self.owner_id and chat and getattr(chat, "type", None) == "private")

    async def owner_message_middleware(self, handler, event, data):
        try:
            if self.is_owner_private(event):
                body = getattr(event, "text", None) or getattr(event, "caption", None) or ""
                event_date = getattr(event, "date", None)
                if isinstance(event_date, datetime):
                    aware_date = event_date if event_date.tzinfo else event_date.replace(tzinfo=timezone.utc)
                    created_at = aware_date.astimezone(timezone.utc).isoformat(timespec="seconds")
                elif isinstance(event_date, (int, float)):
                    created_at = datetime.fromtimestamp(event_date, timezone.utc).isoformat(timespec="seconds")
                else:
                    created_at = None
                self.store.archive_owner_message(
                    int(event.chat.id), int(event.from_user.id), int(getattr(event, "message_id", 0) or 0), body,
                    created_at=created_at,
                )
        except Exception:
            logger.exception("Could not archive owner DM message")
        return await handler(event, data)

    @staticmethod
    def _date_iso(value) -> str | None:
        if isinstance(value, datetime):
            aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            return aware.astimezone(timezone.utc).isoformat(timespec="seconds")
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
        return None

    def _archive_flood_event(self, event, *, direction: str = "in", edited: bool = False):
        if not CHAT_ARCHIVE_ENABLED:
            return False
        chat = getattr(event, "chat", None)
        if not chat or getattr(chat, "type", None) not in {"group", "supergroup"}:
            return False
        chat_id = int(getattr(chat, "id", 0) or 0)
        if not chat_id or chat_id != self._effective_primary_chat_id():
            return False
        user = getattr(event, "from_user", None)
        sender_chat = getattr(event, "sender_chat", None)
        body = getattr(event, "text", None) or getattr(event, "caption", None) or ""
        content_type = getattr(event, "content_type", None)
        message_type = str(getattr(content_type, "value", content_type) or "text")
        if not body:
            body = f"[{message_type}]"
        reply = getattr(event, "reply_to_message", None)
        message_date = self._date_iso(getattr(event, "date", None)) or utcnow()
        edited_date = self._date_iso(getattr(event, "edit_date", None)) if edited else None
        return self.store.archive_chat_message(
            chat_id=chat_id,
            message_id=int(getattr(event, "message_id", 0) or 0),
            direction=direction,
            sender_user_id=int(getattr(user, "id", 0) or 0),
            sender_chat_id=int(getattr(sender_chat, "id", 0) or 0) or None,
            sender_username=str(getattr(user, "username", "") or ""),
            sender_name=" ".join(filter(None, [str(getattr(user, "first_name", "") or ""), str(getattr(user, "last_name", "") or "")])) or str(getattr(sender_chat, "title", "") or ""),
            message_type=message_type,
            body=str(body),
            reply_to_message_id=int(getattr(reply, "message_id", 0) or 0) or None,
            message_at=message_date,
            edited_at=edited_date,
        )

    async def group_message_middleware(self, handler, event, data):
        try:
            self._archive_flood_event(event, direction="in")
        except Exception:
            logger.exception("Could not archive a flood message")
        return await handler(event, data)

    async def archive_edited_message_event(self, message):
        try:
            self._archive_flood_event(message, direction="in", edited=True)
        except Exception:
            logger.exception("Could not archive an edited flood message")

    def _archive_outgoing_api_result(self, method, result):
        """Archive text/captions produced by any aiogram API send/edit request."""
        if not CHAT_ARCHIVE_ENABLED:
            return
        method_name = type(method).__name__
        chat_id_value = getattr(method, "chat_id", None)
        try:
            chat_id = int(chat_id_value or 0)
        except (TypeError, ValueError):
            return
        if not chat_id or chat_id != self._effective_primary_chat_id():
            return

        edit_methods = {"EditMessageText", "EditMessageCaption", "EditMessageMedia"}
        send_methods = {
            "SendMessage", "SendPhoto", "SendVideo", "SendAnimation", "SendDocument",
            "SendAudio", "SendVoice", "SendLivePhoto", "SendPaidMedia", "SendVideoNote",
            "CopyMessage", "ForwardMessage", "SendRichMessage",
        }
        if method_name not in edit_methods | send_methods and method_name != "SendMediaGroup":
            return

        method_text = getattr(method, "text", None)
        method_caption = getattr(method, "caption", None)
        input_media = getattr(method, "media", None)

        def archive_one(message_obj=None, *, message_id=None, body=None, message_type=None, edited=False):
            message_obj = message_obj if message_obj is not None else None
            actual_chat = getattr(getattr(message_obj, "chat", None), "id", None)
            actual_id = getattr(message_obj, "message_id", None) or message_id
            actual_text = body
            if actual_text is None and message_obj is not None:
                actual_text = getattr(message_obj, "text", None) or getattr(message_obj, "caption", None)
            if message_type is None:
                message_type = "text" if method_name in {"SendMessage", "EditMessageText", "SendRichMessage"} else method_name.removeprefix("Send").removeprefix("Edit").lower()
            if not actual_text:
                actual_text = f"[{message_type or 'message'}]"
            sender = getattr(message_obj, "from_user", None)
            self.store.archive_chat_message(
                chat_id=int(actual_chat or chat_id),
                message_id=int(actual_id or 0),
                direction="out",
                sender_user_id=int(getattr(sender, "id", 0) or 0),
                sender_username=str(getattr(sender, "username", "") or ""),
                sender_name=str(getattr(sender, "first_name", "Justice Faite bot") or "Justice Faite bot"),
                message_type=str(message_type or "text"),
                body=str(actual_text),
                reply_to_message_id=int(getattr(method, "reply_to_message_id", 0) or 0) or None,
                message_at=self._date_iso(getattr(message_obj, "date", None)) or utcnow(),
                edited_at=utcnow() if edited else None,
            )

        # SendMediaGroup returns several Message objects; each gets its own row.
        if method_name == "SendMediaGroup":
            returned = result if isinstance(result, (list, tuple)) else []
            media_items = input_media if isinstance(input_media, (list, tuple)) else []
            for index, sent in enumerate(returned):
                item = media_items[index] if index < len(media_items) else None
                caption = getattr(item, "caption", None) or getattr(sent, "caption", None)
                media_type = getattr(item, "type", None) or (type(item).__name__.removeprefix("InputMedia").lower() if item else "media")
                archive_one(sent, body=caption or f"[{media_type}]", message_type=str(media_type))
            return

        if method_name in edit_methods:
            msg_id = getattr(method, "message_id", None)
            body = method_text or method_caption or getattr(getattr(method, "media", None), "caption", None)
            media_type = "text" if method_name == "EditMessageText" else "caption"
            archive_one(result if hasattr(result, "message_id") else None,
                        message_id=msg_id, body=body, message_type=media_type, edited=True)
            return

        archive_one(result if hasattr(result, "message_id") else None,
                    body=method_text or method_caption,
                    message_type="text" if method_name in {"SendMessage", "SendRichMessage"} else method_name.removeprefix("Send").lower())

    def _install_outgoing_message_archive(self):
        if self.bot is None or getattr(self, "_outgoing_archive_installed", False):
            return
        session = getattr(self.bot, "session", None)
        register_middleware = getattr(session, "middleware", None)
        if callable(register_middleware):
            middleware = FloodOutgoingArchiveMiddleware(self)
            register_middleware(middleware)
            self._outgoing_archive_installed = True
            return

        # Compatibility fallback for tiny test doubles or non-aiogram adapters.
        original_send = getattr(self.bot, "send_message", None)
        if callable(original_send):
            async def tracked_send(*args, **kwargs):
                sent = await original_send(*args, **kwargs)
                try:
                    chat_id = kwargs.get("chat_id", args[0] if args else 0)
                    body = kwargs.get("text", args[1] if len(args) > 1 else "")
                    if int(chat_id or 0) == self._effective_primary_chat_id() and body:
                        self.store.archive_chat_message(
                            chat_id=int(chat_id), message_id=int(getattr(sent, "message_id", 0) or 0),
                            direction="out", sender_user_id=int(getattr(getattr(sent, "from_user", None), "id", 0) or 0),
                            sender_username=str(getattr(getattr(sent, "from_user", None), "username", "") or ""),
                            sender_name=str(getattr(getattr(sent, "from_user", None), "first_name", "Justice Faite bot") or "Justice Faite bot"),
                            message_type="text", body=str(body),
                            reply_to_message_id=int(kwargs.get("reply_to_message_id", 0) or 0) or None,
                            message_at=self._date_iso(getattr(sent, "date", None)) or utcnow(),
                        )
                except Exception:
                    logger.exception("Could not archive a bot reply in the flood")
                return sent
            self.bot.send_message = tracked_send
        original_edit = getattr(self.bot, "edit_message_text", None)
        if callable(original_edit):
            async def tracked_edit(*args, **kwargs):
                edited = await original_edit(*args, **kwargs)
                try:
                    body = kwargs.get("text", args[0] if args else "")
                    chat_id = kwargs.get("chat_id", args[1] if len(args) > 1 else 0)
                    message_id = kwargs.get("message_id", args[2] if len(args) > 2 else 0)
                    if int(chat_id or 0) == self._effective_primary_chat_id() and body and message_id:
                        self.store.archive_chat_message(
                            chat_id=int(chat_id), message_id=int(message_id), direction="out",
                            sender_name="Justice Faite bot", message_type="text", body=str(body),
                            edited_at=utcnow(),
                        )
                except Exception:
                    logger.exception("Could not archive an edited bot message in the flood")
                return edited
            self.bot.edit_message_text = tracked_edit
        self._outgoing_archive_installed = True

    async def handle_chatlog(self, message):
        if not self.is_owner_private(message):
            return await self.deny(message)
        chat_id = self._effective_primary_chat_id()
        total = self.store.chat_message_count(chat_id)
        runtime = self.store.runtime_log_count()
        latest = self.store.recent_chat_messages(chat_id, 1)
        last = latest[0]["message_at"] if latest else "пока нет записей"
        await message.answer(
            "📚 <b>АРХИВ JUSTICE FAITE</b>\n\n"
            f"Сообщений флуда: <b>{total}</b>\n"
            f"Технических записей: <b>{runtime}</b>\n"
            f"Срок хранения переписки: <b>{CHAT_LOG_RETENTION_DAYS} дней</b>\n"
            f"Последнее сообщение: <code>{esc(last)}</code>\n\n"
            "Поиск: <code>/jf_memory_search слово</code>. Архив входит в закреплённый JF_DB_BACKUP_V1.",
            parse_mode="HTML",
        )

    async def deny(self, message):
        await message.answer("⛔ Эта консоль доступна только владельцу в личных сообщениях.")

    async def handle_open(self, message):
        if not self.is_owner_private(message):
            return await self.deny(message)
        await self.refresh_all(int(message.chat.id))
        await message.answer("✅ Личная консоль готова. Разделы обновляются автоматически каждые 5 минут.")

    async def handle_refresh(self, message):
        if not self.is_owner_private(message):
            return await self.deny(message)
        await self.refresh_all(int(message.chat.id))
        await message.answer("🔄 Все блоки консоли обновлены.")

    async def handle_memory(self, message):
        if not self.is_owner_private(message):
            return await self.deny(message)
        blocks = ["🧠 <b>ПАМЯТЬ ПРОЕКТА</b>"]
        for row in self.store.memory():
            blocks.append(f"<b>{esc(row['title'])}</b>\n{esc(row['body'])}")
        recent = self.store.recent_owner_messages(8)
        blocks.append(f"<b>📝 Архив личных сообщений владельца</b>\nСохранено: {self.store.owner_message_count()} записей")
        for row in recent:
            blocks.append(f"<i>{esc(row['created_at'])}</i>\n{esc(row['body'][:700])}")
        flood_id = self._effective_primary_chat_id()
        flood_recent = self.store.recent_chat_messages(flood_id, 8)
        blocks.append(
            f"<b>💬 Архив переписки флуда</b>\nСохранено: {self.store.chat_message_count(flood_id)} сообщений "
            f"(хранение {CHAT_LOG_RETENTION_DAYS} дней). Технических логов: {self.store.runtime_log_count()}."
        )
        for row in flood_recent:
            person = row['sender_username'] and ('@' + row['sender_username']) or row['sender_name'] or 'участник/бот'
            direction = 'бот' if row['direction'] == 'out' else 'чат'
            blocks.append(f"<i>{esc(row['message_at'])} · {esc(direction)} · {esc(person)}</i>\n{esc(str(row['body'])[:500])}")
        runtime_recent = self.store.recent_runtime_logs(5)
        if runtime_recent:
            blocks.append("<b>⚙️ Последние технические события</b>")
            for row in runtime_recent:
                blocks.append(f"<i>{esc(row['created_at'])} · {esc(row['level'])} · {esc(row['logger_name'])}</i>\n{esc(str(row['message'])[:500])}")
        for chunk in _pack_html_blocks(blocks):
            await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)

    async def handle_memory_search(self, message):
        if not self.is_owner_private(message):
            return await self.deny(message)
        raw = str(getattr(message, "text", "") or "")
        payload = raw.split(maxsplit=1)[1].strip() if len(raw.split(maxsplit=1)) > 1 else ""
        if len(payload) < 2:
            return await message.answer("🔎 Формат: <code>/jf_memory_search текст</code>", parse_mode="HTML")
        memory_rows = self.store.search_project_memory(payload, 20)
        journal_rows = self.store.search_owner_messages(payload, 20)
        flood_rows = self.store.search_chat_messages(self._effective_primary_chat_id(), payload, 20)
        runtime_rows = self.store.search_runtime_logs(payload, 20)
        if not memory_rows and not journal_rows and not flood_rows and not runtime_rows:
            return await message.answer("🔎 Совпадений в проектной памяти, переписке флуда и технических логах нет.")
        blocks = ["🔎 <b>РЕЗУЛЬТАТЫ ПОИСКА В ПАМЯТИ</b>"]
        if memory_rows:
            blocks.append("📌 <b>Структурированные требования</b>")
            for row in memory_rows:
                blocks.append(f"<b>{esc(row['title'])}</b>\n{esc(row['body'][:900])}")
        if journal_rows:
            blocks.append("📝 <b>Архив личных сообщений владельца</b>")
            for row in journal_rows:
                blocks.append(f"<i>{esc(row['created_at'])}</i>\n{esc(row['body'][:700])}")
        if flood_rows:
            blocks.append("💬 <b>Архив переписки флуда</b>")
            for row in flood_rows:
                person = row['sender_username'] and ('@' + row['sender_username']) or row['sender_name'] or 'участник/бот'
                blocks.append(f"<i>{esc(row['message_at'])} · {esc(person)} · {esc(row['direction'])}</i>\n{esc(str(row['body'])[:900])}")
        if runtime_rows:
            blocks.append("⚙️ <b>Технические логи</b>")
            for row in runtime_rows:
                blocks.append(f"<i>{esc(row['created_at'])} · {esc(row['level'])} · {esc(row['logger_name'])}</i>\n{esc(str(row['message'])[:900])}")
        for chunk in _pack_html_blocks(blocks):
            await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)

    async def handle_memory_save(self, message):
        if not self.is_owner_private(message):
            return await self.deny(message)
        raw = str(getattr(message, "text", "") or "")
        payload = raw.split(maxsplit=1)[1] if len(raw.split(maxsplit=1)) > 1 else ""
        parts = [part.strip() for part in payload.split("|", 2)]
        if len(parts) != 3:
            return await message.answer(
                "Формат:\n<code>/jf_memory_save категория | заголовок | текст</code>\n\n"
                "Категория — короткий ключ, например <code>spy</code> или <code>events</code>. Повторный ключ обновит блок памяти.",
                parse_mode="HTML",
            )
        category, title, body = parts
        if not re.fullmatch(r"[a-z0-9_-]{2,32}", category):
            return await message.answer("Категория должна содержать 2–32 символа: латинские буквы, цифры, _ или -.")
        if not title or len(title) > 100 or not body or len(body) > 3000:
            return await message.answer("Заголовок должен быть до 100 символов, текст — от 1 до 3000 символов.")
        self.store.save_memory(category, title, body)
        await self.refresh_all(int(message.chat.id))
        await message.answer(f"✅ Блок памяти <code>{esc(category)}</code> сохранён.", parse_mode="HTML")

    async def handle_callback(self, callback):
        if not self.is_owner_private(callback):
            with suppress(Exception):
                await callback.answer("Нет доступа", show_alert=True)
            return
        parts = str(getattr(callback, "data", "")).split(":")
        section = parts[1] if len(parts) > 1 else "all"
        action = parts[2] if len(parts) > 2 else "refresh"
        if section == "roles" and action == "sync":
            if not callable(self.role_sync):
                with suppress(Exception):
                    await callback.answer("Синхронизация пока недоступна", show_alert=True)
                return
            try:
                report = await self.role_sync()
                if report:
                    await self.bot.send_message(int(callback.message.chat.id), str(report))
            except Exception:
                logger.exception("Role sync from owner console failed")
                with suppress(Exception):
                    await callback.answer("Не удалось синхронизировать роли", show_alert=True)
                return
        if section == "roles" and action in {"prev", "next"}:
            chat_id = self._effective_primary_chat_id()
            total = self._table_count("group_members", f"WHERE chat_id={chat_id} AND active=1 AND role_key IS NOT NULL")
            pages = max(1, (total + self._roles_page_size - 1) // self._roles_page_size)
            if action == "prev":
                self._roles_page = max(0, self._roles_page - 1)
            else:
                self._roles_page = min(pages - 1, self._roles_page + 1)
        await self.refresh_all(int(callback.message.chat.id), only=section if section != "all" else None)
        with suppress(Exception):
            await callback.answer("Обновлено")

    def _database_summary(self):
        return self.store.run(lambda conn: {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()})

    def _table_count(self, table: str, where: str = "") -> int:
        if not table.replace("_", "").isalnum():
            return 0
        try:
            return self.store.run(lambda conn: int(conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]))
        except sqlite3.Error:
            return 0

    def _sections(self):
        tables = self._database_summary()
        try:
            size = sum(os.path.getsize(path) for path in (self.store.path, self.store.path + "-wal") if os.path.isfile(path))
            size_label = f"{size / 1024 / 1024:.2f} МБ"
        except OSError:
            size_label = "ещё не создана / недоступна"
        memory_count = self._table_count("jf_project_memory")
        journal_count = self.store.owner_message_count()
        flood_log_count = self.store.chat_message_count(self._effective_primary_chat_id())
        runtime_log_count = self.store.runtime_log_count()
        backup = self.store.get_message("db_backup")
        backup_status = f"закреплённое сообщение #{backup['message_id']}" if backup else "создаётся при первом цикле"
        backup_warning = f"\n⚠️ <b>Проблема копирования:</b> {esc(self._last_backup_error)}" if self._last_backup_error else ""
        return {
            "system": (
                f"🧠 <b>JUSTICE FAITE · ЛИЧНАЯ КОНСОЛЬ v{PROJECT_VERSION}</b>\n\n"
                "<b>Состояние базы:</b> доступна ✅\n"
                f"<b>Файл базы:</b> {esc(size_label)}\n"
                f"<b>Блоков памяти:</b> {memory_count}\n"
                f"<b>Архив личных сообщений владельца:</b> {journal_count}\n"
                f"<b>Архив переписки флуда:</b> {flood_log_count}\n"
                f"<b>Технический журнал:</b> {runtime_log_count}\n"
                f"<b>Срок хранения переписки:</b> {CHAT_LOG_RETENTION_DAYS} дней\n"
                f"<b>Резервная копия:</b> {esc(backup_status)}\n"
                f"<b>Обновлено:</b> {esc(utcnow())}{backup_warning}\n\n"
                "Панель и резервный снимок обновляются примерно каждые 5 минут; записи SQLite фиксируются сразу."
            ),
            "games": self._games_text(tables),
            "modules": self._modules_text(tables),
            "roles": self._roles_text(tables),
            "memory": self._memory_text(),
        }

    def _games_text(self, tables):
        lines = ["🎮 <b>ИГРОВОЙ ЦЕНТР</b>"]
        if "spy_games" in tables:
            rows = self.store.run(lambda c: c.execute(
                "SELECT id,chat_id,status,phase,created_at FROM spy_games WHERE status IN ('WAITING','STARTING','PLAYING','DISCUSSION','VOTING') ORDER BY id DESC LIMIT 8"
            ).fetchall())
            lines.append(f"\n🕵️ <b>Шпион:</b> {len(rows)} активных игр")
            for row in rows:
                players = self._table_count("spy_players", "WHERE game_id=" + str(int(row["id"])))
                lines.append(f"• Игра #{row['id']} · {esc(row['status'])}/{esc(row['phase'])} · {players} игроков · чат <code>{row['chat_id']}</code>")
        if "mafia_games" in tables:
            rows = self.store.run(lambda c: c.execute(
                "SELECT id,chat_id,status FROM mafia_games WHERE status IN ('WAITING','STARTING') ORDER BY id DESC LIMIT 8"
            ).fetchall())
            lines.append(f"\n🎭 <b>Мафия:</b> {len(rows)} открытых лобби")
            for row in rows:
                lines.append(f"• Лобби #{row['id']} · {esc(row['status'])} · чат <code>{row['chat_id']}</code>")
        if len(lines) == 1:
            lines.append("\nПока активных игровых таблиц нет.")
        lines.append("\n<i>Здесь отображается сохранённое состояние игр.</i>")
        return "\n".join(lines)

    def _modules_text(self, tables):
        modules = [
            ("🛡️", "Модерация", "jf_manager_"), ("👥", "Кланы, кружки, заметки, закладки", "jf_community_"),
            ("⚙️", "Настройки и фильтры чата", "jf_chatconfig_"), ("📊", "Статистика", "jf_stats_"),
            ("👤", "Профили", "jf_profiles_"), ("✨", "Репутация", "jf_reputation_"),
            ("🚩", "Жалобы", "jf_reports"), ("⏰", "Таймеры", "jf_timers"),
            ("🧩", "Автомодерация", "jf_automod"), ("🌐", "Сетка чатов", "jf_network"),
            ("🎉", "Розыгрыши", "jf_raffles"), ("🧹", "Чистка", "jf_clean"), ("🕵️", "Шпион", "spy_"),
        ]
        lines = ["🧩 <b>МОДУЛИ БОТА</b>"]
        for emoji, name, prefix in modules:
            found = any(t.startswith(prefix) for t in tables)
            lines.append(f"{emoji} {esc(name)}: {'✅ таблицы обнаружены' if found else '⚪️ проверяется при запуске'}")
        lines.append("\n<i>Наличие таблиц не означает, что каждый пункт уже завершён полностью.</i>")
        return "\n".join(lines)

    def _roles_text(self, tables):
        lines = ["🏷️ <b>РОЛИ УЧАСТНИКОВ</b>"]
        if not {"group_members", "role_state"} <= tables:
            lines.append("\nТаблицы ролей ещё не созданы.")
            return "\n".join(lines)
        chat_id = self._effective_primary_chat_id()
        stats = self.store.run(lambda c: c.execute(
            "SELECT COUNT(*) AS active, SUM(CASE WHEN role_key IS NOT NULL THEN 1 ELSE 0 END) AS assigned "
            "FROM group_members WHERE chat_id=? AND active=1", (chat_id,)
        ).fetchone())
        active_count = int(stats["active"] or 0)
        assigned_count = int(stats["assigned"] or 0)
        lines.append(f"\n👥 Активных участников: {active_count}")
        lines.append(f"🎭 С распознанной ролью: {assigned_count}")
        lines.append(f"▫️ Пока без распознанной роли: {max(0, active_count - assigned_count)}")

        if "participant_roster" in tables:
            pending_count = self.store.run(lambda c: c.execute(
                "SELECT COUNT(*) FROM participant_roster WHERE chat_id=? AND user_id IS NULL", (chat_id,)
            ).fetchone()[0])
            pending_names = self.store.run(lambda c: c.execute(
                "SELECT username FROM participant_roster WHERE chat_id=? AND user_id IS NULL "
                "ORDER BY username COLLATE NOCASE LIMIT 10", (chat_id,)
            ).fetchall())
            if pending_count:
                names = ", ".join("@" + esc(r["username"]) for r in pending_names if r["username"])
                remaining = max(0, int(pending_count) - len(pending_names))
                lines.append(f"\n📥 В предварительном списке без Telegram ID: {int(pending_count)}")
                if names:
                    lines.append(f"Ожидают первого сообщения: {names}" + (f" и ещё {remaining}" if remaining else ""))

        unassigned_rows = self.store.run(lambda c: c.execute(
            "SELECT first_name,username,tag FROM group_members WHERE chat_id=? AND active=1 AND role_key IS NULL "
            "ORDER BY lower(username),lower(first_name) LIMIT 8", (chat_id,)
        ).fetchall())
        if unassigned_rows:
            lines.append("\n<b>Участники без распознанной роли:</b>")
            for row in unassigned_rows:
                who = ("@" + row["username"]) if row["username"] else (row["first_name"] or "участник")
                tag = row["tag"] or "тег не получен"
                lines.append(f"• {esc(who)} · <code>{esc(tag)}</code>")

        total = assigned_count
        page_count = max(1, (total + self._roles_page_size - 1) // self._roles_page_size)
        self._roles_page = max(0, min(self._roles_page, page_count - 1))
        offset = self._roles_page * self._roles_page_size
        rows = self.store.run(lambda c: c.execute(
            "SELECT user_id,first_name,username,role_name,tag FROM group_members WHERE chat_id=? AND active=1 AND role_key IS NOT NULL ORDER BY lower(role_name),lower(first_name) LIMIT ? OFFSET ?",
            (chat_id, self._roles_page_size, offset)
        ).fetchall())
        if rows:
            lines.append(f"\n<b>Карта ролей · страница {self._roles_page + 1}/{page_count}</b>")
            for row in rows:
                who = ("@" + row["username"]) if row["username"] else (row["first_name"] or str(row["user_id"]))
                lines.append(f"• {esc(row['role_name'])} · {esc(who)} · <code>{esc(row['tag'] or 'тег не указан')}</code>")
            lines.append(f"\nВсего в карте ролей: {total}. Перелистывай кнопками под сообщением.")
        else:
            lines.append("\nПока нет участников с распознанными ролями.")
        lines.append(
            "\n<i>Важно: Telegram Bot API не выдаёт полный список обычных участников и не ищет их ID по @username. "
            "Чтобы собирать новых участников автоматически, бот должен получать их сообщения: добавьте его администратором "
            "или отключите Privacy Mode. Синхронизация проверяет только уже известные Telegram ID.</i>"
        )
        return "\n".join(lines)

    def _effective_primary_chat_id(self) -> int:
        """Use the sole persisted managed group if the console ID is stale."""
        configured = int(self.primary_chat_id or 0)
        try:
            ids = self.store.run(lambda conn: [
                int(row[0]) for row in conn.execute(
                    "SELECT group_chat_id FROM managed_group ORDER BY group_chat_id"
                ).fetchall()
            ])
        except sqlite3.Error:
            return configured
        if configured in ids:
            return configured
        if len(ids) == 1:
            self.primary_chat_id = ids[0]
            logger.warning(
                "Owner console PRIMARY_CHAT_ID corrected from %s to persisted group %s",
                configured, ids[0],
            )
            return ids[0]
        return configured

    def _memory_text(self):
        rows = self.store.memory()
        lines = ["🗂️ <b>ПАМЯТЬ И ТЕХНИЧЕСКОЕ ЗАДАНИЕ</b>"]
        for row in rows[:10]:
            preview = str(row["body"] or "")
            if len(preview) > 240:
                preview = preview[:237] + "…"
            lines.append(f"\n🔹 <b>{esc(row['title'])}</b>\n{esc(preview)}")
        if len(rows) > 10:
            lines.append(f"\nЕщё блоков в полной памяти: {len(rows) - 10}. Команда: <code>/jf_memory</code>.")
        lines.append(f"\n📝 Архив ЛС владельца: {self.store.owner_message_count()} записей.")
        flood_id = self._effective_primary_chat_id()
        lines.append(f"💬 Архив флуда: {self.store.chat_message_count(flood_id)} сообщений · ⚙️ техлогов: {self.store.runtime_log_count()}.")
        lines.append(f"Срок хранения: {CHAT_LOG_RETENTION_DAYS} дней. Проверка: <code>/jf_chatlog</code> · поиск: <code>/jf_memory_search запрос</code>.")
        return "\n".join(lines)

    async def _edit_or_send(self, chat_id: int, section: str, text: str):
        if section == "roles":
            chat_id = self._effective_primary_chat_id()
            total = self._table_count("group_members", f"WHERE chat_id={chat_id} AND active=1 AND role_key IS NOT NULL")
            pages = max(1, (total + self._roles_page_size - 1) // self._roles_page_size)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="◀️", callback_data="jfcon:roles:prev"),
                    InlineKeyboardButton(text=f"{self._roles_page + 1}/{pages}", callback_data="jfcon:roles:refresh"),
                    InlineKeyboardButton(text="▶️", callback_data="jfcon:roles:next"),
                ],
                [InlineKeyboardButton(text="🔄 Обновить карту", callback_data="jfcon:roles:refresh")],
                [InlineKeyboardButton(text="🔎 Синхронизировать известные ID", callback_data="jfcon:roles:sync")],
            ])
        else:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Обновить блок", callback_data=f"jfcon:{section}:refresh")
            ]])
        old = self.store.get_message(section)
        if old and int(old["chat_id"]) == int(chat_id):
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id, message_id=int(old["message_id"]), text=text[:3900],
                    parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True,
                )
                return
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    return
                logger.info("Console block %s will be recreated: %s", section, exc)
        sent = await self.bot.send_message(chat_id, text[:3900], parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
        self.store.set_message(section, chat_id, int(sent.message_id))

    async def refresh_all(self, chat_id: int, only: str | None = None):
        sections = self._sections()
        order = [only] if only in sections else ["system", "games", "modules", "roles", "memory"]
        for key in order:
            await self._edit_or_send(chat_id, key, sections[key])

    @staticmethod
    def _valid_database(path: str) -> bool:
        if not os.path.isfile(path) or os.path.getsize(path) < 100:
            return False
        try:
            with sqlite3.connect(path, timeout=5) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()
                tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            return bool(integrity and integrity[0] == "ok" and int(tables) > 0)
        except sqlite3.Error:
            return False

    def _quarantine_invalid_database(self) -> str | None:
        """Move a corrupt local DB aside so SQLite initialization can start cleanly."""
        if not os.path.exists(self.store.path):
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine = f"{self.store.path}.corrupt.{stamp}"
        try:
            os.replace(self.store.path, quarantine)
            for suffix in ("-wal", "-shm"):
                with suppress(OSError):
                    os.replace(self.store.path + suffix, quarantine + suffix)
            logger.error("Invalid local SQLite database was quarantined at %s", quarantine)
            return quarantine
        except OSError:
            logger.exception("Could not quarantine invalid SQLite database %s", self.store.path)
            return None

    async def restore_if_missing(self) -> bool:
        """Restore an ephemeral local SQLite DB from the latest pinned owner-DM backup."""
        if self._valid_database(self.store.path):
            return False
        if not self.owner_id:
            return False
        temp_dir = tempfile.mkdtemp(prefix="jf_restore_")
        compressed = os.path.join(temp_dir, "backup.sqlite3.gz")
        restored = os.path.join(temp_dir, "restored.sqlite3")
        restored_ok = False
        try:
            chat = await self.bot.get_chat(self.owner_id)
            pinned = getattr(chat, "pinned_message", None)
            document = getattr(pinned, "document", None) if pinned else None
            caption = getattr(pinned, "caption", "") if pinned else ""
            if not document or BACKUP_MARKER not in str(caption):
                logger.warning("Local DB is missing/invalid and latest pinned owner-DM message is not a Justice Faite backup")
                return False
            file_info = await self.bot.get_file(document.file_id)
            await self.bot.download_file(file_info.file_path, destination=compressed)
            with gzip.open(compressed, "rb") as source, open(restored, "wb") as target:
                shutil.copyfileobj(source, target)
            if not self._valid_database(restored):
                raise sqlite3.DatabaseError("Pinned backup failed SQLite integrity validation")
            os.makedirs(os.path.dirname(os.path.abspath(self.store.path)), exist_ok=True)
            for suffix in ("-wal", "-shm"):
                with suppress(OSError):
                    os.remove(self.store.path + suffix)
            if os.path.exists(self.store.path):
                self._quarantine_invalid_database()
            os.replace(restored, self.store.path)
            message_id = int(getattr(pinned, "message_id", 0) or 0)
            if message_id:
                with suppress(Exception):
                    self.store.set_message("db_backup", self.owner_id, message_id)
            logger.warning("Recovered SQLite database from the pinned owner-DM backup")
            restored_ok = True
            return True
        except Exception:
            logger.exception("Could not restore SQLite DB from Telegram owner-DM backup")
            return False
        finally:
            if not restored_ok and os.path.exists(self.store.path) and not self._valid_database(self.store.path):
                # Do not leave a corrupt file at the configured path: the normal
                # initializers would otherwise fail before the bot can start.
                self._quarantine_invalid_database()
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _backup_caption(self, digest: str) -> str:
        flood_id = self._effective_primary_chat_id()
        messages = self.store.chat_message_count(flood_id)
        runtime = self.store.runtime_log_count()
        return (
            f"💾 {BACKUP_MARKER}\n"
            f"Обновлено: {esc(utcnow())}\n"
            f"Переписка флуда: {messages} · техлоги: {runtime}\n"
            f"SHA-256: <code>{digest}</code>"
        )

    def _make_compressed_snapshot(self, directory: str) -> tuple[str, str]:
        snapshot = os.path.join(directory, "justice_faite.sqlite3")
        compressed = os.path.join(directory, "justice_faite.sqlite3.gz")
        with self.store.lock:
            with sqlite3.connect(self.store.path, timeout=30) as source, sqlite3.connect(snapshot) as target:
                source.execute("PRAGMA busy_timeout=30000")
                source.backup(target)
                check = target.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise sqlite3.DatabaseError("SQLite backup integrity check failed")
        with open(snapshot, "rb") as source, gzip.open(compressed, "wb", compresslevel=6) as target:
            shutil.copyfileobj(source, target)
        digest = hashlib.sha256(Path(compressed).read_bytes()).hexdigest()
        return compressed, digest

    async def backup_once(self) -> bool:
        """Maintain one latest pinned document; create a new one if another pin displaced it."""
        if not self.owner_id or not self._valid_database(self.store.path):
            return False
        temp_dir = tempfile.mkdtemp(prefix="jf_backup_")
        try:
            compressed, digest = self._make_compressed_snapshot(temp_dir)
            compressed_size = os.path.getsize(compressed)
            # Standard Bot API file downloads are limited; leave headroom below 20 MB.
            if compressed_size > 18 * 1024 * 1024:
                self._last_backup_error = f"Сжатая копия слишком велика: {compressed_size / 1024 / 1024:.2f} МБ (лимит 18 МБ)."
                logger.error("%s", self._last_backup_error)
                return False
            signature = f"{compressed_size}:{digest}"
            old = self.store.get_message("db_backup")
            chat = await self.bot.get_chat(self.owner_id)
            pinned = getattr(chat, "pinned_message", None)
            pinned_id = int(getattr(pinned, "message_id", 0) or 0) if pinned else 0
            pinned_caption = str(getattr(pinned, "caption", "") or "") if pinned else ""
            old_id = int(old["message_id"]) if old and int(old["chat_id"]) == self.owner_id else 0
            old_is_latest_backup = bool(old_id and pinned_id == old_id and BACKUP_MARKER in pinned_caption)
            backup_caption = self._backup_caption(digest)

            if signature == self._last_backup_signature and old_is_latest_backup:
                return True

            from aiogram.types import FSInputFile, InputMediaDocument
            sent_or_edited = False
            if old_is_latest_backup:
                try:
                    media = InputMediaDocument(
                        media=FSInputFile(compressed, filename="justice_faite_memory.sqlite3.gz"),
                        caption=backup_caption,
                        parse_mode="HTML",
                    )
                    await self.bot.edit_message_media(
                        chat_id=self.owner_id, message_id=old_id, media=media,
                    )
                    sent_or_edited = True
                except Exception as exc:
                    if "message is not modified" in str(exc).lower():
                        sent_or_edited = True
                    else:
                        logger.info("Could not edit latest pinned DB backup; replacing it: %s", exc)

            if not sent_or_edited:
                sent = await self.bot.send_document(
                    chat_id=self.owner_id,
                    document=FSInputFile(compressed, filename="justice_faite_memory.sqlite3.gz"),
                    caption=backup_caption,
                    parse_mode="HTML",
                    disable_notification=True,
                )
                new_message_id = int(sent.message_id)
                await self.bot.pin_chat_message(chat_id=self.owner_id, message_id=new_message_id, disable_notification=True)
                self.store.set_message("db_backup", self.owner_id, new_message_id)
                if old_id and old_id != new_message_id:
                    # Delete only a known backup created by this module. If it was
                    # displaced from the pinned slot, it is still safe to remove.
                    with suppress(Exception):
                        await self.bot.delete_message(chat_id=self.owner_id, message_id=old_id)
            self._last_backup_signature = signature
            self._last_backup_error = None
            return True
        except Exception as exc:
            self._last_backup_error = str(exc)[:300]
            logger.exception("Could not update owner-DM database backup")
            return False
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def run_forever(self):
        import asyncio
        while True:
            try:
                await self.refresh_all(self.owner_id)
            except Exception:
                logger.exception("Console panel refresh failed")
            # A broken/deleted console block must not prevent the DB snapshot.
            try:
                await self.backup_once()
            except Exception:
                logger.exception("Console backup cycle failed")
            await asyncio.sleep(BACKUP_INTERVAL_SECONDS)


def register(dp, bot, F, db_path, lock=None, owner_id=0, primary_chat_id=0):
    module = Console(dp, bot, F, db_path, lock=lock, owner_id=owner_id, primary_chat_id=primary_chat_id)
    module.register()
    return module

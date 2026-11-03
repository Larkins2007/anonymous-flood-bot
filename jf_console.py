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
from datetime import datetime, timezone
from pathlib import Path

from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger("justice-faite-console")
PROJECT_VERSION = "0.6.0"
BACKUP_MARKER = "JF_DB_BACKUP_V1"
BACKUP_INTERVAL_SECONDS = 300


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
    """Avoid archiving obvious credentials accidentally pasted into the owner DM."""
    patterns = (
        r"\b\d{5,12}:[A-Za-z0-9_-]{25,}\b",  # Telegram bot token
        r"(?i)\b(?:password|passwd|пароль|bot[_ -]?token|api[_ -]?key|secret)\s*[:=]",
        r"(?i)\b(?:ключ доступа|секретный ключ)\s*[:=]",
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
            """)
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


class Console:
    def __init__(self, dp, bot, F, db_path, lock=None, owner_id=0):
        self.dp, self.bot, self.F = dp, bot, F
        self.store = Store(db_path, lock)
        self.owner_id = int(owner_id or 0)
        self.primary_chat_id = int(os.getenv("PRIMARY_CHAT_ID", "0") or "0")
        self.registered = False
        self._last_backup_signature = None
        self._last_backup_error = None
        self._roles_page = 0
        self._roles_page_size = 20

    def init(self):
        self.store.init()

    def register(self):
        if self.registered:
            return
        private = self.F.chat.type == "private"
        self.dp.message.register(self.handle_open, private, Command("jf_console"))
        self.dp.message.register(self.handle_refresh, private, Command("jf_console_refresh"))
        self.dp.message.register(self.handle_memory, private, Command("jf_memory"))
        self.dp.message.register(self.handle_memory_save, private, Command("jf_memory_save"))
        self.dp.message.register(self.handle_memory_search, private, Command("jf_memory_search"))
        # Outer middleware archives owner notes without blocking any command handler.
        self.dp.message.outer_middleware(self.owner_message_middleware)
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
        blocks.append(f"<b>📝 Архив личных сообщений</b>\nСохранено: {self.store.owner_message_count()} записей")
        for row in recent:
            blocks.append(f"<i>{esc(row['created_at'])}</i>\n{esc(row['body'][:700])}")
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
        if not memory_rows and not journal_rows:
            return await message.answer("🔎 Совпадений в проектной памяти и архиве личных сообщений нет.")
        blocks = ["🔎 <b>РЕЗУЛЬТАТЫ ПОИСКА В ПАМЯТИ</b>"]
        if memory_rows:
            blocks.append("📌 <b>Структурированные требования</b>")
            for row in memory_rows:
                blocks.append(f"<b>{esc(row['title'])}</b>\n{esc(row['body'][:900])}")
        if journal_rows:
            blocks.append("📝 <b>Архив личных сообщений</b>")
            for row in journal_rows:
                blocks.append(f"<i>{esc(row['created_at'])}</i>\n{esc(row['body'][:700])}")
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
        if section == "roles" and action in {"prev", "next"}:
            total = self._table_count("group_members", f"WHERE chat_id={self.primary_chat_id} AND active=1 AND role_key IS NOT NULL")
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
        backup = self.store.get_message("db_backup")
        backup_status = f"закреплённое сообщение #{backup['message_id']}" if backup else "создаётся при первом цикле"
        backup_warning = f"\n⚠️ <b>Проблема копирования:</b> {esc(self._last_backup_error)}" if self._last_backup_error else ""
        return {
            "system": (
                f"🧠 <b>JUSTICE FAITE · ЛИЧНАЯ КОНСОЛЬ v{PROJECT_VERSION}</b>\n\n"
                "<b>Состояние базы:</b> доступна ✅\n"
                f"<b>Файл базы:</b> {esc(size_label)}\n"
                f"<b>Блоков памяти:</b> {memory_count}\n"
                f"<b>Архив личных сообщений:</b> {journal_count}\n"
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
        chat_id = self.primary_chat_id
        stats = self.store.run(lambda c: c.execute(
            "SELECT COUNT(*) AS active, SUM(CASE WHEN role_key IS NOT NULL THEN 1 ELSE 0 END) AS assigned "
            "FROM group_members WHERE chat_id=? AND active=1", (chat_id,)
        ).fetchone())
        lines.append(f"\n👥 Активных участников: {int(stats['active'] or 0)}")
        lines.append(f"🎭 С распознанной ролью: {int(stats['assigned'] or 0)}")
        total = int(stats["assigned"] or 0)
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
            lines.append("\nРоли появятся здесь после того, как участники напишут в чате или будут синхронизированы.")
        lines.append("\n<i>Telegram sender_tag обновляется на сообщениях; getChatMember используется как резерв.</i>")
        return "\n".join(lines)

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
        lines.append(f"\n📝 Архив ЛС: {self.store.owner_message_count()} записей. Поиск: <code>/jf_memory_search запрос</code>.")
        return "\n".join(lines)

    async def _edit_or_send(self, chat_id: int, section: str, text: str):
        if section == "roles":
            total = self._table_count("group_members", f"WHERE chat_id={self.primary_chat_id} AND active=1 AND role_key IS NOT NULL")
            pages = max(1, (total + self._roles_page_size - 1) // self._roles_page_size)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="◀️", callback_data="jfcon:roles:prev"),
                    InlineKeyboardButton(text=f"{self._roles_page + 1}/{pages}", callback_data="jfcon:roles:refresh"),
                    InlineKeyboardButton(text="▶️", callback_data="jfcon:roles:next"),
                ],
                [InlineKeyboardButton(text="🔄 Обновить карту", callback_data="jfcon:roles:refresh")],
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

            if signature == self._last_backup_signature and old_is_latest_backup:
                return True

            from aiogram.types import FSInputFile, InputMediaDocument
            sent_or_edited = False
            if old_is_latest_backup:
                try:
                    media = InputMediaDocument(
                        media=FSInputFile(compressed, filename="justice_faite_memory.sqlite3.gz"),
                        caption=f"💾 {BACKUP_MARKER}\nОбновлено: {esc(utcnow())}\nSHA-256: <code>{digest}</code>",
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
                    caption=f"💾 {BACKUP_MARKER}\nОбновлено: {esc(utcnow())}\nSHA-256: <code>{digest}</code>",
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


def register(dp, bot, F, db_path, lock=None, owner_id=0):
    module = Console(dp, bot, F, db_path, lock=lock, owner_id=owner_id)
    module.register()
    return module

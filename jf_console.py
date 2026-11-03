"""Owner-only private control console and persistent project memory.

The Telegram messages are a dashboard cache, not the source of truth. All
project notes and message pointers are stored in the shared SQLite database.
"""
from __future__ import annotations

import html
import os
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timezone

from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


SEED_MEMORY = (
    (
        "core", "Цель проекта",
        "Единый Telegram-бот Justice Faite Manager: игра «Шпион», инструменты управления чатом, профили, статистика, сообщества, события и личная панель владельца. Общие пользователи, права и настройки; модули не должны дублировать друг друга.",
        "Требования владельца",
    ),
    (
        "spy", "Игра «Шпион»",
        "Лобби на 3–15 игроков; настраиваемые 1–3 шпиона; категории секретных слов/локаций; приватная раздача ролей; последовательные круги вопросов и ответов; обсуждение; голосование; финальная догадка шпиона; таймеры, ничьи, выходы, отмена и восстановление после перезапуска. Игровая панель в группе редактируется, а личные карточки ролей отправляются отдельно.",
        "Требования владельца",
    ),
    (
        "console", "Личная консоль",
        "В личке владельца отдельные редактируемые сообщения: состояние системы, активные игры, модули и память проекта. Каждое сообщение — отдельный блок. Источник истины — постоянная БД; сообщения Telegram — только отображение и быстрый доступ. Доступ только ADMIN_ID.",
        "Требования владельца",
    ),
    (
        "moderation", "Модерация и безопасность",
        "Внутренние ранги и наследование прав; журнал действий; баны, муты, предупреждения, кики; доступ к командам по рангам и персональные исключения; триггеры; антиспам/антирейд; фильтры входа; репорты и голосование за бан. Все опасные административные действия подтверждаются и проверяют реальные права Telegram.",
        "Анализ Iris",
    ),
    (
        "community", "Профили и сообщества",
        "Профили с добровольными полями и настройкой приватности; ник, звание и девиз; статистика и рейтинги; репутация, награды, заметки, закладки; кланы и кружки; сетка чатов. Данные пользователя общие, а данные конкретного чата ограничены этим чатом.",
        "Анализ Iris",
    ),
    (
        "games", "Развлечения и события",
        "Дуэли и кубы без денежных ставок и наказаний; случайные команды; сезонные и тематические события с ресурсами, коллекциями, лимитами, добровольным участием, чёрными списками и рейтингами. Нужны защита от накрутки и журнал начислений.",
        "Анализ Iris",
    ),
    (
        "style", "Стиль сообщений",
        "Не использовать декоративные разделительные линии и лишние тире. Отделять смысловые блоки пустой строкой. В начале пунктов использовать подходящие эмодзи.",
        "Предпочтение владельца",
    ),
    (
        "platform", "Ограничения Telegram",
        "Не выдавать сообщения бота за личные сообщения владельца в группах. Business-подключение годится для части личных диалогов, но не заменяет полноценный пользовательский аккаунт. Приоритет — нативные Bot API-возможности и собственные аналоги команд других ботов.",
        "Проверка Telegram Bot API",
    ),
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
            """)
            for category, title, body, source in SEED_MEMORY:
                key = category
                conn.execute(
                    "INSERT OR IGNORE INTO jf_project_memory(key,category,title,body,source,updated_at) VALUES(?,?,?,?,?,?)",
                    (key, category, title, body, source, utcnow()),
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

    def set_message(self, section: str, chat_id: int, message_id: int):
        self.run(lambda c: c.execute(
            "INSERT INTO jf_console_messages(section,chat_id,message_id,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(section) DO UPDATE SET chat_id=excluded.chat_id,message_id=excluded.message_id,updated_at=excluded.updated_at",
            (section, int(chat_id), int(message_id), utcnow()),
        ))

    def get_message(self, section: str):
        return self.run(lambda c: c.execute(
            "SELECT chat_id,message_id FROM jf_console_messages WHERE section=?", (section,)
        ).fetchone())


class Console:
    def __init__(self, dp, bot, F, db_path, lock=None, owner_id=0):
        self.dp, self.bot, self.F = dp, bot, F
        self.store = Store(db_path, lock)
        self.owner_id = int(owner_id or 0)
        self.registered = False

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
        self.dp.callback_query.register(self.handle_callback, self.F.data.startswith("jfcon:"))
        self.registered = True

    def is_owner_private(self, event) -> bool:
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None)
        if chat is None:
            chat = getattr(getattr(event, "message", None), "chat", None)
        return bool(
            self.owner_id
            and user
            and int(getattr(user, "id", 0) or 0) == self.owner_id
            and chat
            and getattr(chat, "type", None) == "private"
        )

    async def deny(self, message):
        await message.answer("⛔ Эта консоль доступна только владельцу в личных сообщениях.")

    async def handle_open(self, message):
        if not self.is_owner_private(message):
            return await self.deny(message)
        await self.refresh_all(int(message.chat.id))
        await message.answer("✅ Личная консоль обновлена. Каждый раздел находится в отдельном редактируемом сообщении.")

    async def handle_refresh(self, message):
        if not self.is_owner_private(message):
            return await self.deny(message)
        await self.refresh_all(int(message.chat.id))
        await message.answer("🔄 Все блоки консоли обновлены.")

    async def handle_memory(self, message):
        if not self.is_owner_private(message):
            return await self.deny(message)
        rows = self.store.memory()
        parts = ["🧠 <b>ПАМЯТЬ ПРОЕКТА</b>"]
        for row in rows:
            parts.append(f"<b>{esc(row['title'])}</b>\n{esc(row['body'])}")
        text = "\n\n".join(parts)
        for start in range(0, len(text), 3500):
            await message.answer(text[start:start + 3500], parse_mode="HTML", disable_web_page_preview=True)

    async def handle_memory_save(self, message):
        if not self.is_owner_private(message):
            return await self.deny(message)
        raw = str(getattr(message, "text", "") or "")
        payload = raw.split(maxsplit=1)[1] if len(raw.split(maxsplit=1)) > 1 else ""
        parts = [part.strip() for part in payload.split("|", 2)]
        if len(parts) != 3:
            return await message.answer(
                "Формат:\n<code>/jf_memory_save категория | заголовок | текст</code>\n\n"
                "Категория — короткий ключ, например <code>spy</code> или <code>events</code>. "
                "Повторный ключ обновит существующий блок памяти.",
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
        data = str(getattr(callback, "data", ""))
        section = data.split(":", 1)[1] if ":" in data else "all"
        await self.refresh_all(int(callback.message.chat.id), only=section if section != "all" else None)
        with suppress(Exception):
            await callback.answer("Обновлено")

    def _database_summary(self):
        return self.store.run(lambda conn: {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        })

    def _table_count(self, table: str, where: str = "") -> int:
        # Table names and WHERE fragments are internal constants built by this module.
        if not table.replace("_", "").isalnum():
            return 0
        try:
            return self.store.run(lambda conn: int(
                conn.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]
            ))
        except sqlite3.Error:
            return 0

    def _sections(self):
        tables = self._database_summary()
        try:
            size = os.path.getsize(self.store.path)
            size_label = f"{size / 1024 / 1024:.2f} МБ"
        except OSError:
            size_label = "ещё не создана / недоступна"
        memory_count = self._table_count("jf_project_memory")
        return {
            "system": (
                "🧠 <b>JUSTICE FAITE · КОНСОЛЬ</b>\n\n"
                "<b>Состояние:</b> база доступна ✅\n"
                f"<b>Память проекта:</b> {memory_count} блоков\n"
                f"<b>Файл БД:</b> {esc(size_label)}\n"
                f"<b>Обновлено:</b> {esc(utcnow())}\n\n"
                "Команды: <code>/jf_console</code> · <code>/jf_console_refresh</code> · <code>/jf_memory</code>"
            ),
            "games": self._games_text(tables),
            "modules": self._modules_text(tables),
            "memory": self._memory_text(),
        }

    def _games_text(self, tables):
        lines = ["🎮 <b>ИГРОВОЙ ЦЕНТР</b>"]
        if "spy_games" in tables:
            rows = self.store.run(lambda c: c.execute(
                "SELECT id,chat_id,status,phase,created_at FROM spy_games "
                "WHERE status IN ('WAITING','STARTING','PLAYING','DISCUSSION','VOTING') ORDER BY id DESC LIMIT 8"
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
        lines.append("\n<i>Этот блок показывает сохранённое состояние, а не управляет ходами игры.</i>")
        return "\n".join(lines)

    def _modules_text(self, tables):
        modules = [
            ("🛡️", "Модерация", "jf_manager_"),
            ("👥", "Кланы, кружки, заметки, закладки", "jf_community_"),
            ("⚙️", "Настройки и фильтры чата", "jf_chatconfig_"),
            ("📊", "Статистика", "jf_stats_"),
            ("👤", "Профили", "jf_profiles_"),
            ("✨", "Репутация", "jf_reputation_"),
            ("🚩", "Жалобы", "jf_reports"),
            ("⏰", "Таймеры", "jf_timers"),
            ("🧩", "Автомодерация", "jf_automod"),
            ("🎲", "Развлечения", "jf_"),
            ("🌐", "Сетка чатов", "jf_network"),
            ("🎉", "Розыгрыши", "jf_raffles"),
            ("🧹", "Чистка", "jf_clean"),
            ("🕵️", "Шпион", "spy_"),
        ]
        lines = ["🧩 <b>МОДУЛИ БОТА</b>"]
        for emoji, name, prefix in modules:
            found = any(t.startswith(prefix) for t in tables)
            if prefix == "jf_":
                found = "jf_reports" in tables or "jf_timers" in tables
            lines.append(f"{emoji} {esc(name)}: {'✅ таблицы обнаружены' if found else '⚪️ проверяется при запуске'}")
        lines.append("\n<i>Наличие таблиц не означает, что каждая команда уже полностью реализована.</i>")
        return "\n".join(lines)

    def _memory_text(self):
        rows = self.store.memory()
        lines = ["🗂️ <b>ПАМЯТЬ И ТЕХНИЧЕСКОЕ ЗАДАНИЕ</b>"]
        for row in rows:
            lines.append(f"\n🔹 <b>{esc(row['title'])}</b>\n{esc(row['body'])}")
        return "\n".join(lines)

    async def _edit_or_send(self, chat_id: int, section: str, text: str):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить блок", callback_data=f"jfcon:{section}")
        ]])
        old = self.store.get_message(section)
        if old and int(old["chat_id"]) == int(chat_id):
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=int(old["message_id"]),
                    text=text[:3900],
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                self.store.set_message(section, chat_id, int(old["message_id"]))
                return
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    self.store.set_message(section, chat_id, int(old["message_id"]))
                    return
        sent = await self.bot.send_message(
            chat_id,
            text[:3900],
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        self.store.set_message(section, chat_id, int(sent.message_id))

    async def refresh_all(self, chat_id: int, only: str | None = None):
        sections = self._sections()
        order = [only] if only in sections else ["system", "games", "modules", "memory"]
        for key in order:
            await self._edit_or_send(chat_id, key, sections[key])

    async def run_forever(self):
        # Re-edit the dashboard periodically so it remains a live view of saved state.
        import asyncio
        while True:
            try:
                row = self.store.get_message("system")
                if row and int(row["chat_id"]) == self.owner_id:
                    await self.refresh_all(self.owner_id)
            except Exception:
                pass
            await asyncio.sleep(300)


def register(dp, bot, F, db_path, lock=None, owner_id=0):
    module = Console(dp, bot, F, db_path, lock=lock, owner_id=owner_id)
    module.register()
    return module

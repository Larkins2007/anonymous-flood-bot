import os
import asyncio
import sqlite3
import logging
from datetime import datetime, timezone

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_ID = 1682289834

DB_PATH = os.getenv("DB_PATH", "users.db")

REPORT_COOLDOWN_SECONDS = 600

MAX_MESSAGE_LENGTH = 4000
MAX_REPORT_REASON_LENGTH = 2000


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

bot = Bot(BOT_TOKEN)

dp = Dispatcher(
    storage=MemoryStorage()
)


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
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
    )

    conn.row_factory = sqlite3.Row

    return conn


def now():
    return datetime.now(
        timezone.utc
    ).isoformat()


def init_db():
    conn = db()

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
            blocked INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_id INTEGER NOT NULL,
            target_text TEXT NOT NULL,
            target_user_id INTEGER,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new'
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_reporter_target
        ON reports(reporter_id, target_user_id)
        WHERE target_user_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS sent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            sent_at TEXT NOT NULL
        );
        """
    )

    conn.commit()
    conn.close()


def migrate_db():
    conn = db()

    cols = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(users)"
        ).fetchall()
    }

    if "reports_count" not in cols:
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN reports_count INTEGER NOT NULL DEFAULT 0
            """
        )

    if "blocked" not in cols:
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0
            """
        )

    conn.commit()
    conn.close()


def register_user(user):
    stamp = now()

    conn = db()

    conn.execute(
        """
        INSERT INTO users (
            user_id,
            first_name,
            last_name,
            username,
            first_seen,
            last_seen
        )
        VALUES (?, ?, ?, ?, ?, ?)

        ON CONFLICT(user_id) DO UPDATE SET
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            username = excluded.username,
            last_seen = excluded.last_seen
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
    conn.close()


def get_user(user_id):
    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()

    conn.close()

    return row


def is_blocked(user_id):
    row = get_user(user_id)

    return bool(
        row and row["blocked"]
    )


def set_blocked(
    user_id,
    value,
):
    conn = db()

    conn.execute(
        """
        UPDATE users
        SET blocked = ?
        WHERE user_id = ?
        """,
        (
            1 if value else 0,
            user_id,
        ),
    )

    conn.commit()
    conn.close()


def increment_messages(user_id):
    conn = db()

    conn.execute(
        """
        UPDATE users
        SET messages_count = messages_count + 1
        WHERE user_id = ?
        """,
        (user_id,),
    )

    conn.execute(
        """
        INSERT INTO sent_messages (
            user_id,
            sent_at
        )
        VALUES (?, ?)
        """,
        (
            user_id,
            now(),
        ),
    )

    conn.commit()
    conn.close()


def last_report_timestamp(user_id):
    conn = db()

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

    conn.close()

    return (
        row["created_at"]
        if row
        else None
    )


def report_cooldown_left(user_id):
    stamp = last_report_timestamp(
        user_id
    )

    if not stamp:
        return 0

    try:
        elapsed = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(stamp)
        ).total_seconds()

    except (
        ValueError,
        TypeError,
    ):
        return 0

    return max(
        0,
        int(
            REPORT_COOLDOWN_SECONDS
            - elapsed
        ),
    )


def increment_reports(user_id):
    conn = db()

    conn.execute(
        """
        UPDATE users
        SET reports_count = reports_count + 1
        WHERE user_id = ?
        """,
        (user_id,),
    )

    conn.commit()
    conn.close()


def has_reported_target(
    reporter_id,
    target_user_id,
    target_text=None,
):
    conn = db()

    if target_user_id is not None:

        row = conn.execute(
            """
            SELECT 1
            FROM reports
            WHERE reporter_id = ?
              AND target_user_id = ?
            LIMIT 1
            """,
            (
                reporter_id,
                target_user_id,
            ),
        ).fetchone()

    else:
        normalized = (
            (target_text or "")
            .strip()
            .lstrip("@")
            .lower()
        )

        row = conn.execute(
            """
            SELECT 1
            FROM reports
            WHERE reporter_id = ?
              AND lower(
                    ltrim(
                        target_text,
                        '@'
                    )
                  ) = ?
            LIMIT 1
            """,
            (
                reporter_id,
                normalized,
            ),
        ).fetchone()

    conn.close()

    return row is not None


def create_report(
    reporter_id,
    target_text,
    target_user_id,
    reason,
):
    conn = db()

    try:

        cur = conn.execute(
            """
            INSERT INTO reports (
                reporter_id,
                target_text,
                target_user_id,
                reason,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                reporter_id,
                target_text,
                target_user_id,
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

    finally:
        conn.close()


def get_report(report_id):
    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM reports
        WHERE id = ?
        """,
        (report_id,),
    ).fetchone()

    conn.close()

    return row


def close_report(report_id):
    conn = db()

    conn.execute(
        """
        UPDATE reports
        SET status = 'closed'
        WHERE id = ?
        """,
        (report_id,),
    )

    conn.commit()
    conn.close()


def user_count():
    conn = db()

    value = conn.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    conn.close()

    return value


def blocked_count():
    conn = db()

    value = conn.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE blocked = 1
        """
    ).fetchone()[0]

    conn.close()

    return value


def message_count():
    conn = db()

    value = conn.execute(
        "SELECT COUNT(*) FROM sent_messages"
    ).fetchone()[0]

    conn.close()

    return value


def new_reports_count():
    conn = db()

    value = conn.execute(
        """
        SELECT COUNT(*)
        FROM reports
        WHERE status = 'new'
        """
    ).fetchone()[0]

    conn.close()

    return value


def list_users(
    limit=10,
    offset=0,
):
    conn = db()

    rows = conn.execute(
        """
        SELECT *
        FROM users
        ORDER BY last_seen DESC
        LIMIT ? OFFSET ?
        """,
        (
            limit,
            offset,
        ),
    ).fetchall()

    conn.close()

    return rows


def search_users(
    query,
    limit=15,
):
    conn = db()

    q = f"%{query}%"

    rows = conn.execute(
        """
        SELECT *
        FROM users
        WHERE CAST(user_id AS TEXT) LIKE ?
           OR username LIKE ?
           OR first_name LIKE ?
           OR last_name LIKE ?
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (
            q,
            q,
            q,
            q,
            limit,
        ),
    ).fetchall()

    conn.close()

    return rows


def recent_reports(
    limit=20,
):
    conn = db()

    rows = conn.execute(
        """
        SELECT *
        FROM reports
        WHERE status = 'new'
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    conn.close()

    return rows


def display_name(row):
    if not row:
        return "Без имени"

    value = " ".join(
        x
        for x in (
            row["first_name"],
            row["last_name"],
        )
        if x
    ).strip()

    return value or "Без имени"


# =========================================================
# SCREEN HELPERS
# =========================================================

async def delete_user_message(
    message: Message,
):
    """
    Удаляем сообщение самого пользователя,
    когда это сообщение является вводом
    для текущего состояния.
    """

    try:
        await message.delete()
    except Exception:
        pass


async def edit_screen(
    callback: CallbackQuery,
    text: str,
    reply_markup=None,
):
    """
    Главное правило нового интерфейса:

    НИЧЕГО НЕ УДАЛЯЕМ при нажатии inline-кнопок.

    Старое сообщение бота просто редактируется.
    """

    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
        )

    except Exception as exc:

        # Если сообщение нельзя изменить,
        # пытаемся показать экран как новое сообщение.
        logger.warning(
            "Could not edit screen: %s",
            exc,
        )

        try:
            await callback.message.answer(
                text,
                reply_markup=reply_markup,
            )
        except Exception:
            logger.exception(
                "Could not send fallback screen"
            )


async def edit_state_screen(
    state: FSMContext,
    bot_instance: Bot,
    chat_id: int,
    text: str,
    reply_markup=None,
):
    """
    Редактирует сохранённое экранное сообщение,
    если действие пришло обычным текстом пользователя.
    """

    data = await state.get_data()

    screen_message_id = data.get(
        "screen_message_id"
    )

    if not screen_message_id:
        return False

    try:

        await bot_instance.edit_message_text(
            chat_id=chat_id,
            message_id=screen_message_id,
            text=text,
            reply_markup=reply_markup,
        )

        return True

    except Exception as exc:

        logger.debug(
            "Could not edit state screen: %s",
            exc,
        )

        return False


async def save_screen_message(
    state: FSMContext,
    message: Message,
):
    """
    Запоминаем одно активное сообщение бота.
    """

    await state.update_data(
        screen_message_id=message.message_id
    )


async def send_screen(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup=None,
):
    """
    Создаём единственный активный экран
    и запоминаем его ID.
    """

    sent = await message.answer(
        text,
        reply_markup=reply_markup,
    )

    await save_screen_message(
        state,
        sent,
    )

    return sent


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
    await state.clear()

    register_user(
        message.from_user
    )

    if is_blocked(
        message.from_user.id
    ):

        await delete_user_message(
            message
        )

        await message.answer(
            f"{title('Доступ ограничен')}\n\n"
            f"{bullet('Для вашего аккаунта отправка сообщений отключена.')}"
        )

        return

    await delete_user_message(
        message
    )

    await message.answer(
        home_text(),
        reply_markup=main_kb(),
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

    await edit_screen(
        callback,
        home_text(),
        main_kb(),
    )

    await callback.answer()


# =========================================================
# INFORMATION
# =========================================================

@dp.callback_query(F.data == "u:info")
async def user_info(
    callback: CallbackQuery,
):
    await edit_screen(
        callback,

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

    await callback.answer()


@dp.callback_query(F.data == "u:send_info")
async def send_info(
    callback: CallbackQuery,
):
    await edit_screen(
        callback,

        f"♡₊˚ О сообщениях ˚₊♡\n\n"
        "Здесь можно высказать своё мнение о флуде, "
        "предложить идею, поделиться советом "
        "или рассказать о своих предпочтениях.\n\n"
        f"{divider()}\n"
        "♡ Напишите сообщение.\n"
        "♡ После отправки вы получите уведомление.\n\n"
        "₊˚♡ Делитесь мыслями свободно. ♡˚₊",

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

    await callback.answer()


@dp.callback_query(F.data == "u:report_info")
async def report_info(
    callback: CallbackQuery,
):
    await edit_screen(
        callback,

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

    await callback.answer()


# =========================================================
# SEND
# =========================================================

@dp.callback_query(F.data == "u:send")
async def user_send(
    callback: CallbackQuery,
    state: FSMContext,
):
    if is_blocked(
        callback.from_user.id
    ):
        await callback.answer(
            "Доступ ограничен.",
            show_alert=True,
        )

        return

    await state.set_state(
        FeedbackState.waiting
    )

    await edit_screen(
        callback,

        f"{title('Новое сообщение')}\n\n"
        f"{bullet('Напишите сообщение следующим сообщением.')}\n"
        f"{bullet('Максимальная длина — 4000 символов.')}\n\n"
        f"{divider()}\n"
        "♡ Вы можете написать всё, что хотите сообщить.",

        cancel_kb(),
    )

    await callback.answer()


@dp.callback_query(F.data == "u:cancel")
async def user_cancel(
    callback: CallbackQuery,
    state: FSMContext,
):
    await state.clear()

    await edit_screen(
        callback,
        home_text(),
        main_kb(),
    )

    await callback.answer()


@dp.message(Command("cancel"))
async def command_cancel(
    message: Message,
    state: FSMContext,
):
    await state.clear()

    await delete_user_message(
        message
    )

    await message.answer(
        home_text(),
        reply_markup=main_kb(),
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
    register_user(
        message.from_user
    )

    if is_blocked(
        message.from_user.id
    ):

        await state.clear()

        await delete_user_message(
            message
        )

        await message.answer(
            f"{title('Доступ ограничен')}\n\n"
            f"{bullet('Для вашего аккаунта отправка сообщений отключена.')}"
        )

        return

    text = message.text.strip()

    if not text:

        await delete_user_message(
            message
        )

        await edit_state_screen(
            state,
            bot,
            message.chat.id,

            f"{title('Новое сообщение')}\n\n"
            f"{bullet('Сообщение не может быть пустым.')}\n\n"
            "Попробуйте ещё раз.",

            cancel_kb(),
        )

        return

    if len(text) > MAX_MESSAGE_LENGTH:

        await delete_user_message(
            message
        )

        await edit_state_screen(
            state,
            bot,
            message.chat.id,

            f"{title('Новое сообщение')}\n\n"
            f"{bullet('Сообщение слишком длинное.')}\n"
            f"{bullet('Максимум — 4000 символов.')}",

            cancel_kb(),
        )

        return

    sender_name = " ".join(
        x
        for x in (
            message.from_user.first_name,
            message.from_user.last_name,
        )
        if x
    ).strip() or "Без имени"

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "нет username"
    )

    increment_messages(
        message.from_user.id
    )

    await state.clear()

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

        await bot.send_message(
            ADMIN_ID,
            admin_text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="↳ Ответить",
                            callback_data=(
                                f"reply:{message.from_user.id}"
                            ),
                        ),
                        InlineKeyboardButton(
                            text="⊘ Заблокировать",
                            callback_data=(
                                f"a:block:{message.from_user.id}"
                            ),
                        ),
                    ]
                ]
            ),
        )

    except Exception:

        logger.exception(
            "Failed to send feedback"
        )

        await delete_user_message(
            message
        )

        await message.answer(
            f"{title('Ошибка')}\n\n"
            f"{bullet('Не удалось передать сообщение.')}\n"
            f"{bullet('Попробуйте немного позже.')}",

            reply_markup=main_kb(),
        )

        return

    await delete_user_message(
        message
    )

    screen_updated = await edit_state_screen(
        state,
        bot,
        message.chat.id,

        f"{title('Сообщение отправлено')}\n\n"
        f"{bullet('Ваше сообщение успешно передано.')}\n"
        f"{bullet('При необходимости вы получите ответ через бота.')}\n\n"
        f"{divider()}\n"
        "♡ Спасибо за обратную связь.",

        after_send_kb(),
    )

    if not screen_updated:

        await message.answer(
            f"{title('Сообщение отправлено')}\n\n"
            f"{bullet('Ваше сообщение успешно передано.')}\n"
            f"{bullet('При необходимости вы получите ответ через бота.')}\n\n"
            f"{divider()}\n"
            "♡ Спасибо за обратную связь.",

            reply_markup=after_send_kb(),
        )


@dp.message(
    FeedbackState.waiting
)
async def feedback_non_text(
    message: Message,
):
    await delete_user_message(
        message
    )

    await message.answer(
        f"{title('Новое сообщение')}\n\n"
        f"{bullet('Пожалуйста, отправьте сообщение текстом.')}",

        reply_markup=cancel_kb(),
    )


# =========================================================
# REPORT START
# =========================================================

@dp.callback_query(F.data == "u:report")
async def report_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    if is_blocked(
        callback.from_user.id
    ):
        await callback.answer(
            "Доступ ограничен.",
            show_alert=True,
        )

        return

    left = report_cooldown_left(
        callback.from_user.id
    )

    if left:

        minutes = (
            left + 59
        ) // 60

        await callback.answer(
            f"Следующую жалобу можно отправить примерно через {minutes} мин.",
            show_alert=True,
        )

        return

    await state.set_state(
        ReportTargetState.waiting
    )

    await edit_screen(
        callback,

        f"{title('Жалоба на пользователя')}\n\n"
        f"{bullet('Укажите username или Telegram ID пользователя.')}\n\n"
        f"{divider()}\n"
        f"{warning('Жалоба не является анонимной.')}",

        cancel_kb(),
    )

    await callback.answer()


# =========================================================
# REPORT TARGET
# =========================================================

@dp.message(
    ReportTargetState.waiting,
    F.text,
)
async def report_target(
    message: Message,
    state: FSMContext,
):
    value = message.text.strip()

    if (
        len(value) < 2
        or len(value) > 100
    ):

        await delete_user_message(
            message
        )

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

    target_user_id = (
        int(cleaned)
        if cleaned.isdigit()
        else None
    )

    if target_user_id is not None:

        if (
            target_user_id
            == message.from_user.id
        ):

            await state.clear()

            await delete_user_message(
                message
            )

            await message.answer(
                f"{title('Жалоба')}\n\n"
                f"{bullet('Нельзя пожаловаться на самого себя.')}",

                reply_markup=main_kb(),
            )

            return

        if has_reported_target(
            message.from_user.id,
            target_user_id,
            value,
        ):

            await state.clear()

            await delete_user_message(
                message
            )

            await message.answer(
                f"{title('Жалоба уже отправлена')}\n\n"
                f"{bullet('Вы уже отправляли жалобу на этого пользователя.')}",

                reply_markup=main_kb(),
            )

            return

    await state.update_data(
        target_text=value,
        target_user_id=target_user_id,
    )

    await state.set_state(
        ReportReasonState.waiting
    )

    await delete_user_message(
        message
    )

    await edit_state_screen(
        state,
        bot,
        message.chat.id,

        f"{title('Причина жалобы')}\n\n"
        f"{bullet('Кратко опишите причину обращения.')}\n"
        f"{bullet('Максимум — 2000 символов.')}",

        cancel_kb(),
    )


# =========================================================
# REPORT REASON
# =========================================================

@dp.message(
    ReportReasonState.waiting,
    F.text,
)
async def report_reason(
    message: Message,
    state: FSMContext,
):
    reason = message.text.strip()

    if len(reason) < 3:

        await delete_user_message(
            message
        )

        await edit_state_screen(
            state,
            bot,
            message.chat.id,

            f"{title('Причина жалобы')}\n\n"
            f"{bullet('Опишите причину немного подробнее.')}",

            cancel_kb(),
        )

        return

    if (
        len(reason)
        > MAX_REPORT_REASON_LENGTH
    ):

        await delete_user_message(
            message
        )

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

    target = data.get(
        "target_text",
        "",
    )

    await state.update_data(
        reason=reason
    )

    await delete_user_message(
        message
    )

    await edit_state_screen(
        state,
        bot,
        message.chat.id,

        f"{title('Проверьте жалобу')}\n\n"
        f"{section('Пользователь')}\n"
        f"{bullet(target)}\n\n"
        f"{section('Причина')}\n"
        f"{reason}\n\n"
        f"{divider()}\n"
        f"{warning('Перед отправкой убедитесь, что всё указано верно.')}",

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
async def report_edit(
    callback: CallbackQuery,
    state: FSMContext,
):
    data = await state.get_data()

    if not data.get(
        "target_text"
    ):

        await callback.answer(
            "Данные жалобы устарели.",
            show_alert=True,
        )

        return

    await state.set_state(
        ReportReasonState.waiting
    )

    await edit_screen(
        callback,

        f"{title('Причина жалобы')}\n\n"
        f"{bullet('Напишите причину заново.')}",

        cancel_kb(),
    )

    await callback.answer()


# =========================================================
# REPORT CONFIRM
# =========================================================

@dp.callback_query(F.data == "u:report_confirm")
async def report_confirm(
    callback: CallbackQuery,
    state: FSMContext,
):
    if is_blocked(
        callback.from_user.id
    ):

        await state.clear()

        await callback.answer(
            "Доступ ограничен.",
            show_alert=True,
        )

        return

    data = await state.get_data()

    target = data.get(
        "target_text"
    )

    reason = data.get(
        "reason"
    )

    target_id = data.get(
        "target_user_id"
    )

    if not target or not reason:

        await state.clear()

        await callback.answer(
            "Данные жалобы устарели.",
            show_alert=True,
        )

        return

    left = report_cooldown_left(
        callback.from_user.id
    )

    if left:

        await state.clear()

        minutes = (
            left + 59
        ) // 60

        await edit_screen(
            callback,

            f"{title('Слишком часто')}\n\n"
            f"{bullet(f'Следующую жалобу можно отправить примерно через {minutes} мин.')}",

            main_kb(),
        )

        await callback.answer()

        return

    if (
        target_id
        == callback.from_user.id
    ):

        await state.clear()

        await edit_screen(
            callback,

            f"{title('Жалоба')}\n\n"
            f"{bullet('Нельзя пожаловаться на самого себя.')}",

            main_kb(),
        )

        await callback.answer()

        return

    if has_reported_target(
        callback.from_user.id,
        target_id,
        target,
    ):

        await state.clear()

        await edit_screen(
            callback,

            f"{title('Жалоба уже отправлена')}\n\n"
            f"{bullet('Вы уже отправляли жалобу на этого пользователя.')}",

            main_kb(),
        )

        await callback.answer()

        return

    report_id = create_report(
        callback.from_user.id,
        target,
        target_id,
        reason,
    )

    if report_id is None:

        await state.clear()

        await edit_screen(
            callback,

            f"{title('Жалоба уже отправлена')}\n\n"
            f"{bullet('Вы уже отправляли жалобу на этого пользователя.')}",

            main_kb(),
        )

        await callback.answer()

        return

    increment_reports(
        callback.from_user.id
    )

    reporter = get_user(
        callback.from_user.id
    )

    reporter_name = display_name(
        reporter
    )

    reporter_username = (
        f"@{reporter['username']}"
        if reporter
        and reporter["username"]
        else "нет username"
    )

    target_id_text = (
        str(target_id)
        if target_id
        else "не указан"
    )

    admin_text = (
        f"{title(f'Жалоба #{report_id}')}\n\n"
        f"{section('Пользователь')}\n"
        f"{bullet('Указано: ' + target)}\n"
        f"{bullet('ID: ' + target_id_text)}\n\n"
        f"{section('Причина')}\n"
        f"{reason}\n\n"
        f"{divider()}\n"
        f"{section('Заявитель')}\n"
        f"{bullet('Имя: ' + reporter_name)}\n"
        f"{bullet('Username: ' + reporter_username)}\n"
        f"{bullet('ID: ' + str(callback.from_user.id))}"
    )

    try:

        await bot.send_message(
            ADMIN_ID,
            admin_text,
            reply_markup=report_admin_kb(
                report_id,
                target_id,
            ),
        )

    except Exception:

        logger.exception(
            "Failed to send report"
        )

        await edit_screen(
            callback,

            f"{title('Ошибка')}\n\n"
            f"{bullet('Не удалось передать жалобу.')}",

            main_kb(),
        )

        await callback.answer()

        return

    await state.clear()

    await edit_screen(
        callback,

        f"{title('Жалоба отправлена')}\n\n"
        f"{bullet('Ваше обращение передано.')}\n"
        f"{bullet('Следующую жалобу можно отправить через 10 минут.')}\n\n"
        f"{divider()}\n"
        "♡ Спасибо за обращение.",

        main_kb(),
    )

    await callback.answer(
        "Жалоба отправлена"
    )


@dp.message(
    ReportTargetState.waiting
)
@dp.message(
    ReportReasonState.waiting
)
async def report_non_text(
    message: Message,
):
    await delete_user_message(
        message
    )

    await message.answer(
        f"{title('Жалоба')}\n\n"
        f"{bullet('Пожалуйста, отправьте ответ текстом.')}",
        reply_markup=cancel_kb(),
    )


# =========================================================
# ADMIN REPLY
# =========================================================

@dp.callback_query(
    F.data.startswith("reply:")
)
async def admin_reply_start(
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
            callback.data.split(":")[1]
        )

    except (
        ValueError,
        IndexError,
    ):

        await callback.answer(
            "Некорректный пользователь.",
            show_alert=True,
        )

        return

    if not get_user(user_id):

        await callback.answer(
            "Пользователь не найден.",
            show_alert=True,
        )

        return

    await state.update_data(
        reply_to=user_id
    )

    await state.set_state(
        AdminReplyState.waiting
    )

    sent = await callback.message.answer(
        f"{title('Ответ пользователю')}\n\n"
        f"{bullet('Напишите текст ответа.')}",
        reply_markup=cancel_kb(),
    )

    await save_screen_message(
        state,
        sent,
    )

    await callback.answer()


@dp.callback_query(
    F.data.startswith("a:replyuser:")
)
async def admin_reply_user_start(
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
            "Некорректный пользователь.",
            show_alert=True,
        )

        return

    if not get_user(user_id):

        await callback.answer(
            "Пользователь не найден.",
            show_alert=True,
        )

        return

    await state.update_data(
        reply_to=user_id
    )

    await state.set_state(
        AdminReplyState.waiting
    )

    sent = await callback.message.answer(
        f"{title('Ответ пользователю')}\n\n"
        f"{bullet('Напишите текст ответа.')}",
        reply_markup=cancel_kb(),
    )

    await save_screen_message(
        state,
        sent,
    )

    await callback.answer()


@dp.message(
    AdminReplyState.waiting,
    F.text,
)
async def admin_reply(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()

    user_id = data.get(
        "reply_to"
    )

    text = message.text.strip()

    await delete_user_message(
        message
    )

    await state.clear()

    if not user_id:

        await message.answer(
            f"{title('Ошибка')}\n\n"
            f"{bullet('Получатель не найден.')}",
            reply_markup=admin_kb(),
        )

        return

    try:

        await bot.send_message(
            user_id,

            f"{title('Ответ администрации')}\n\n"
            f"{text}\n\n"
            f"{divider()}\n"
            "♡ Вы можете ответить через бота.",

            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="♡ Ответить",
                            callback_data="u:send",
                        )
                    ]
                ]
            ),
        )

        screen_updated = await edit_state_screen(
            state,
            bot,
            message.chat.id,

            f"{title('Ответ отправлен')}\n\n"
            f"{bullet('Сообщение доставлено пользователю.')}",

            admin_kb(),
        )

        if not screen_updated:

            await message.answer(
                f"{title('Ответ отправлен')}\n\n"
                f"{bullet('Сообщение доставлено пользователю.')}",
                reply_markup=admin_kb(),
            )

    except Exception:

        logger.exception(
            "Failed to send admin reply"
        )

        await message.answer(
            f"{title('Ошибка доставки')}\n\n"
            f"{bullet('Не удалось доставить ответ.')}",
            reply_markup=admin_kb(),
        )


@dp.message(
    AdminReplyState.waiting
)
async def admin_reply_non_text(
    message: Message,
):
    if message.from_user.id != ADMIN_ID:
        return

    await delete_user_message(
        message
    )

    await message.answer(
        f"{title('Ответ пользователю')}\n\n"
        f"{bullet('Пожалуйста, отправьте ответ текстом.')}",
        reply_markup=cancel_kb(),
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

    register_user(
        message.from_user
    )

    await state.clear()

    await delete_user_message(
        message
    )

    await message.answer(
        admin_home_text(),
        reply_markup=admin_kb(),
    )


@dp.callback_query(F.data == "a:home")
async def admin_home(
    callback: CallbackQuery,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    await edit_screen(
        callback,
        admin_home_text(),
        admin_kb(),
    )

    await callback.answer()


# =========================================================
# ADMIN STATS
# =========================================================

@dp.callback_query(F.data == "a:stats")
async def admin_stats(
    callback: CallbackQuery,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

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
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

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
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

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

@dp.callback_query(
    F.data == "a:search"
)
async def admin_search_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    await state.set_state(
        AdminSearchState.waiting
    )

    sent = await callback.message.answer(
        f"{title('Поиск пользователя')}\n\n"
        f"{bullet('Введите имя, username или Telegram ID.')}",
        reply_markup=cancel_kb(),
    )

    await save_screen_message(
        state,
        sent,
    )

    await callback.answer()


@dp.message(
    AdminSearchState.waiting,
    F.text,
)
async def admin_search(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return

    rows = search_users(
        message.text.strip()
    )

    await delete_user_message(
        message
    )

    await state.clear()

    if not rows:

        await message.answer(
            f"{title('Поиск')}\n\n"
            f"{bullet('Пользователи не найдены.')}",
            reply_markup=admin_kb(),
        )

        return

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

    buttons.append(
        [
            InlineKeyboardButton(
                text="‹ В админку",
                callback_data="a:home",
            )
        ]
    )

    await message.answer(
        f"{title('Результаты поиска')}\n\n"
        f"{bullet(f'Найдено: {len(rows)}')}",

        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=buttons
        ),
    )


@dp.message(
    AdminSearchState.waiting
)
async def admin_search_non_text(
    message: Message,
):
    if message.from_user.id != ADMIN_ID:
        return

    await delete_user_message(
        message
    )

    await message.answer(
        f"{title('Поиск пользователя')}\n\n"
        f"{bullet('Введите имя, username или Telegram ID.')}",
        reply_markup=cancel_kb(),
    )


# =========================================================
# ADMIN USER CARD
# =========================================================

@dp.callback_query(
    F.data.startswith("a:user:")
)
async def admin_user(
    callback: CallbackQuery,
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

    await edit_screen(
        callback,
        text,
        user_admin_kb(
            user_id,
            bool(row["blocked"]),
        ),
    )

    await callback.answer()


# =========================================================
# TOGGLE BLOCK
# =========================================================

@dp.callback_query(
    F.data.startswith("a:toggle:")
)
async def admin_toggle(
    callback: CallbackQuery,
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
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    conn = db()

    rows = conn.execute(
        """
        SELECT *
        FROM users
        WHERE blocked = 1
        ORDER BY last_seen DESC
        LIMIT 50
        """
    ).fetchall()

    conn.close()

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
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

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
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

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
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

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

    close_report(
        report_id
    )

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

@dp.callback_query(
    F.data == "a:broadcast"
)
async def admin_broadcast_start(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Нет доступа.",
            show_alert=True,
        )

        return

    await state.set_state(
        BroadcastState.waiting
    )

    sent = await callback.message.answer(
        f"{title('Рассылка')}\n\n"
        f"{bullet('Напишите текст для зарегистрированных пользователей.')}\n"
        f"{bullet('Заблокированные пользователи рассылку не получают.')}\n\n"
        f"{divider()}\n"
        "♡ Используйте рассылку для важных объявлений.",

        reply_markup=cancel_kb(),
    )

    await save_screen_message(
        state,
        sent,
    )

    await callback.answer()


@dp.message(
    BroadcastState.waiting,
    F.text,
)
async def admin_broadcast(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return

    text = message.text.strip()

    if not text:

        await delete_user_message(
            message
        )

        await edit_state_screen(
            state,
            bot,
            message.chat.id,

            f"{title('Рассылка')}\n\n"
            f"{bullet('Текст не может быть пустым.')}",

            cancel_kb(),
        )

        return

    if len(text) > MAX_MESSAGE_LENGTH:

        await delete_user_message(
            message
        )

        await edit_state_screen(
            state,
            bot,
            message.chat.id,

            f"{title('Рассылка')}\n\n"
            f"{bullet('Текст слишком длинный.')}\n"
            f"{bullet('Максимум — 4000 символов.')}",

            cancel_kb(),
        )

        return

    conn = db()

    ids = [
        row["user_id"]
        for row in conn.execute(
            """
            SELECT user_id
            FROM users
            WHERE blocked = 0
            """
        ).fetchall()
    ]

    conn.close()

    sent_count = 0
    failed_count = 0

    for user_id in ids:

        try:

            await bot.send_message(
                user_id,

                f"{title('Сообщение от администрации')}\n\n"
                f"{text}",
            )

            sent_count += 1

            await asyncio.sleep(
                0.05
            )

        except Exception:

            failed_count += 1

    await delete_user_message(
        message
    )

    await state.clear()

    await message.answer(
        f"{title('Рассылка завершена')}\n\n"
        f"{bullet('Отправлено: ' + str(sent_count))}\n"
        f"{bullet('Не доставлено: ' + str(failed_count))}\n\n"
        f"{divider()}\n"
        "♡ Рассылка завершена.",

        reply_markup=admin_kb(),
    )


@dp.message(
    BroadcastState.waiting
)
async def admin_broadcast_non_text(
    message: Message,
):
    if message.from_user.id != ADMIN_ID:
        return

    await delete_user_message(
        message
    )

    await message.answer(
        f"{title('Рассылка')}\n\n"
        f"{bullet('Пожалуйста, отправьте текст рассылки.')}",

        reply_markup=cancel_kb(),
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
    return web.Response(
        text="OK"
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

    logger.info(
        "Removing old Telegram webhook..."
    )

    try:

        await bot.delete_webhook(
            drop_pending_updates=False
        )

        logger.info(
            "Old webhook removed."
        )

    except Exception:

        logger.exception(
            "Could not remove old webhook"
        )

    logger.info(
        "Starting polling..."
    )

    while True:

        try:

            await dp.start_polling(
                bot,
                allowed_updates=(
                    dp.resolve_used_update_types()
                ),
                handle_signals=False,
            )

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(
                "Polling crashed. "
                "Restarting in 5 seconds..."
            )

            await asyncio.sleep(
                5
            )


# =========================================================
# MAIN
# =========================================================

async def main():

    logger.info(
        "Starting Anonymous Feedback Bot..."
    )

    init_db()
    migrate_db()

    logger.info(
        "SQLite initialized: %s",
        DB_PATH,
    )

    try:

        await setup_commands()

    except Exception:

        logger.exception(
            "Could not configure commands"
        )

    http_runner = (
        await start_http_server()
    )

    try:

        await polling_loop()

    finally:

        logger.info(
            "Stopping bot..."
        )

        try:
            await http_runner.cleanup()

        except Exception:

            logger.exception(
                "HTTP cleanup error"
            )

        try:
            await bot.session.close()

        except Exception:

            logger.exception(
                "Bot session cleanup error"
            )


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped."
        )

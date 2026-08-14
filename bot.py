import os
import logging
import sqlite3
from datetime import datetime, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = 1682289834
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")
DB_PATH = os.getenv("DB_PATH", "users.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

logging.basicConfig(level=logging.INFO)

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class SendAnon(StatesGroup):
    waiting = State()


class AdminReply(StatesGroup):
    waiting = State()


class AdminSearch(StatesGroup):
    waiting = State()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            username TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            messages_count INTEGER DEFAULT 0,
            blocked INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def register_user(user):
    now = datetime.now(timezone.utc).isoformat()
    conn = db()
    conn.execute(
        """
        INSERT INTO users
        (user_id, first_name, last_name, username, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            username=excluded.username,
            last_seen=excluded.last_seen
        """,
        (
            user.id,
            user.first_name or "",
            user.last_name or "",
            user.username or "",
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def is_blocked(user_id):
    row = get_user(user_id)
    return bool(row and row["blocked"])


def set_blocked(user_id, blocked):
    conn = db()
    conn.execute(
        "UPDATE users SET blocked = ? WHERE user_id = ?",
        (1 if blocked else 0, user_id),
    )
    conn.commit()
    conn.close()


def increment_messages(user_id):
    conn = db()
    conn.execute(
        "UPDATE users SET messages_count = messages_count + 1 WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def user_count():
    conn = db()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return count


def blocked_count():
    conn = db()
    count = conn.execute("SELECT COUNT(*) FROM users WHERE blocked = 1").fetchone()[0]
    conn.close()
    return count


def message_count():
    conn = db()
    count = conn.execute(
        "SELECT COALESCE(SUM(messages_count), 0) FROM users"
    ).fetchone()[0]
    conn.close()
    return count


def get_users(limit=10, offset=0):
    conn = db()
    rows = conn.execute(
        """
        SELECT * FROM users
        ORDER BY last_seen DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    conn.close()
    return rows


def find_users(query, limit=10):
    conn = db()
    like = f"%{query}%"
    rows = conn.execute(
        """
        SELECT * FROM users
        WHERE CAST(user_id AS TEXT) LIKE ?
           OR username LIKE ?
           OR first_name LIKE ?
           OR last_name LIKE ?
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (like, like, like, like, limit),
    ).fetchall()
    conn.close()
    return rows


def display_name(row):
    name = " ".join(
        part for part in (row["first_name"], row["last_name"]) if part
    ).strip()
    return name or "Без имени"


def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отправить сообщение", callback_data="send")],
            [InlineKeyboardButton(text="Как это работает", callback_data="info")],
        ]
    )


def cancel_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")]
        ]
    )


def after_send_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отправить ещё", callback_data="send")],
            [InlineKeyboardButton(text="В главное меню", callback_data="home")],
        ]
    )


def admin_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пользователи", callback_data="admin:users:0")],
            [
                InlineKeyboardButton(text="Сообщения", callback_data="admin:messages"),
                InlineKeyboardButton(text="Статистика", callback_data="admin:stats"),
            ],
            [InlineKeyboardButton(text="Заблокированные", callback_data="admin:blocked")],
        ]
    )


def admin_user_keyboard(user_id, blocked):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Разблокировать" if blocked else "Заблокировать",
                    callback_data=f"admin:toggle:{user_id}",
                )
            ],
            [InlineKeyboardButton(text="Назад к пользователям", callback_data="admin:users:0")],
        ]
    )


def format_admin_home():
    return (
        "ПАНЕЛЬ АДМИНИСТРАТОРА\n"
        "\n"
        "────────────────────\n"
        f"Пользователей: {user_count()}\n"
        f"Заблокировано: {blocked_count()}\n"
        f"Сообщений получено: {message_count()}\n"
        "────────────────────\n"
        "\n"
        "Выберите раздел."
    )


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    register_user(message.from_user)

    if is_blocked(message.from_user.id):
        await message.answer("Доступ к анонимной обратной связи для вас ограничен.")
        return

    await message.answer(
        "АНОНИМНАЯ ОБРАТНАЯ СВЯЗЬ\n"
        "\n"
        "Здесь вы можете оставить сообщение администрации флуда.\n"
        "\n"
        "Ваше сообщение будет передано без отображения профиля отправителя другим пользователям.\n"
        "\n"
        "Выберите действие:",
        reply_markup=main_menu(),
    )


@dp.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    await state.clear()
    await message.answer(format_admin_home(), reply_markup=admin_menu())


@dp.callback_query(F.data == "info")
async def info(callback: CallbackQuery):
    await callback.message.edit_text(
        "КАК ЭТО РАБОТАЕТ\n"
        "\n"
        "Напишите сообщение через бота — оно будет передано администрации без отображения вашего профиля другим пользователям.\n"
        "\n"
        "Администрация может ответить на сообщение через бота.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Назад", callback_data="home")]
            ]
        ),
    )
    await callback.answer()


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "АНОНИМНАЯ ОБРАТНАЯ СВЯЗЬ\n"
        "\n"
        "Здесь вы можете оставить сообщение администрации флуда.\n"
        "\n"
        "Ваше сообщение будет передано без отображения профиля отправителя другим пользователям.\n"
        "\n"
        "Выберите действие:",
        reply_markup=main_menu(),
    )
    await callback.answer()


@dp.callback_query(F.data == "send")
async def send_start(callback: CallbackQuery, state: FSMContext):
    if is_blocked(callback.from_user.id):
        await callback.answer("Доступ ограничен.", show_alert=True)
        return

    await state.set_state(SendAnon.waiting)
    await callback.message.edit_text(
        "НОВОЕ СООБЩЕНИЕ\n"
        "\n"
        "Напишите текст следующим сообщением.\n"
        "\n"
        "Спасибо, что помогаете поддерживать комфорт и работу флуда.",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data == "cancel")
async def cancel_button(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "АНОНИМНАЯ ОБРАТНАЯ СВЯЗЬ\n"
        "\n"
        "Выберите действие.",
        reply_markup=main_menu(),
    )
    await callback.answer()


@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отправка отменена.", reply_markup=main_menu())


@dp.message(SendAnon.waiting, F.text)
async def receive_anon(message: Message, state: FSMContext):
    user = message.from_user
    register_user(user)

    if is_blocked(user.id):
        await state.clear()
        await message.answer("Доступ ограничен.")
        return

    text = message.text.strip()

    if not text:
        await message.answer("Сообщение пустое. Попробуйте ещё раз.", reply_markup=cancel_keyboard())
        return

    if len(text) > 4000:
        await message.answer(
            "Сообщение слишком длинное. Максимум — 4000 символов.",
            reply_markup=cancel_keyboard(),
        )
        return

    increment_messages(user.id)
    await state.clear()

    sender_name = " ".join(
        part for part in (user.first_name, user.last_name) if part
    ).strip() or "Без имени"
    username = f"@{user.username}" if user.username else "нет username"

    admin_text = (
        "📨 НОВОЕ СООБЩЕНИЕ\n"
        "\n"
        "────────────────────\n"
        "Текст:\n"
        f"{text}\n"
        "────────────────────\n"
        "\n"
        "Отправитель:\n"
        f"Имя: {sender_name}\n"
        f"Username: {username}\n"
        f"ID: {user.id}"
    )

    await bot.send_message(
        ADMIN_ID,
        admin_text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Ответить", callback_data=f"reply:{user.id}"
                    ),
                    InlineKeyboardButton(
                        text="🚫 Заблокировать", callback_data=f"block:{user.id}"
                    ),
                ]
            ]
        ),
    )

    await message.answer(
        "СООБЩЕНИЕ ОТПРАВЛЕНО\n"
        "\n"
        "Спасибо за обратную связь.\n"
        "\n"
        "Ваше сообщение передано администрации.\n"
        "\n"
        "Вы можете отправить ещё одно сообщение.",
        reply_markup=after_send_keyboard(),
    )


@dp.message(SendAnon.waiting)
async def non_text(message: Message):
    await message.answer(
        "Пожалуйста, отправьте сообщение текстом.",
        reply_markup=cancel_keyboard(),
    )


@dp.callback_query(F.data.startswith("reply:"))
async def reply_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])
    await state.update_data(reply_to=user_id)
    await state.set_state(AdminReply.waiting)

    await callback.message.answer(
        "💬 ОТВЕТ ПОЛЬЗОВАТЕЛЮ\n"
        "\n"
        "Напишите текст ответа.\n"
        "Пользователь получит его через бота.",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@dp.message(AdminReply.waiting, F.text)
async def admin_reply(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    user_id = data.get("reply_to")
    await state.clear()

    if not user_id:
        await message.answer("Не удалось определить получателя.")
        return

    try:
        await bot.send_message(
            user_id,
            "💬 ОТВЕТ АДМИНИСТРАЦИИ\n"
            "\n"
            f"{message.text}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Отправить сообщение", callback_data="send")]
                ]
            ),
        )
        await message.answer("Ответ отправлен пользователю.")
    except Exception:
        await message.answer(
            "Не удалось доставить ответ. Возможно, пользователь заблокировал бота."
        )


@dp.callback_query(F.data.startswith("block:"))
async def block_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])
    set_blocked(user_id, True)
    await callback.answer("Пользователь заблокирован.", show_alert=True)


@dp.callback_query(F.data.startswith("admin:"))
async def admin_callbacks(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]

    if action == "home":
        await state.clear()
        await callback.message.edit_text(
            format_admin_home(),
            reply_markup=admin_menu(),
        )

    elif action == "stats":
        await callback.message.edit_text(
            "СТАТИСТИКА\n"
            "\n"
            "────────────────────\n"
            f"Всего пользователей: {user_count()}\n"
            f"Заблокировано: {blocked_count()}\n"
            f"Сообщений получено: {message_count()}\n"
            "────────────────────",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Назад", callback_data="admin:home")]
                ]
            ),
        )

    elif action == "messages":
        await callback.message.edit_text(
            "СООБЩЕНИЯ\n"
            "\n"
            "────────────────────\n"
            f"Всего получено: {message_count()}\n"
            "────────────────────",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Назад", callback_data="admin:home")]
                ]
            ),
        )

    elif action == "blocked":
        conn = db()
        rows = conn.execute(
            """
            SELECT * FROM users
            WHERE blocked = 1
            ORDER BY last_seen DESC
            LIMIT 50
            """
        ).fetchall()
        conn.close()

        text = "ЗАБЛОКИРОВАННЫЕ\n\n"
        if not rows:
            text += "Список пуст."
        else:
            for row in rows:
                username = f"@{row['username']}" if row["username"] else "нет username"
                text += (
                    f"• {display_name(row)} — {username} — ID {row['user_id']}\n"
                )

        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Назад", callback_data="admin:home")]
                ]
            ),
        )

    elif action == "users":
        page = int(parts[2]) if len(parts) > 2 else 0
        rows = get_users(limit=10, offset=page * 10)

        text = (
            "ПОЛЬЗОВАТЕЛИ\n"
            "\n"
            f"Всего: {user_count()}\n"
            f"Страница: {page + 1}\n"
            "\n"
            "Выберите пользователя."
        )

        buttons = []
        for row in rows:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=display_name(row)[:35],
                        callback_data=f"admin:user:{row['user_id']}",
                    )
                ]
            )

        navigation = []
        if page > 0:
            navigation.append(
                InlineKeyboardButton(
                    text="‹",
                    callback_data=f"admin:users:{page - 1}",
                )
            )
        navigation.append(
            InlineKeyboardButton(text=f"{page + 1}", callback_data="noop")
        )
        if len(rows) == 10:
            navigation.append(
                InlineKeyboardButton(
                    text="›",
                    callback_data=f"admin:users:{page + 1}",
                )
            )

        if navigation:
            buttons.append(navigation)

        buttons.append(
            [InlineKeyboardButton(text="Поиск", callback_data="admin:search")]
        )
        buttons.append(
            [InlineKeyboardButton(text="Назад", callback_data="admin:home")]
        )

        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    elif action == "search":
        await state.set_state(AdminSearch.waiting)
        await callback.message.answer(
            "ПОИСК ПОЛЬЗОВАТЕЛЯ\n"
            "\n"
            "Введите имя, username или Telegram ID.",
            reply_markup=cancel_keyboard(),
        )

    elif action == "user":
        user_id = int(parts[2])
        row = get_user(user_id)

        if not row:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return

        username = f"@{row['username']}" if row["username"] else "нет username"

        await callback.message.edit_text(
            "ПОЛЬЗОВАТЕЛЬ\n"
            "\n"
            "────────────────────\n"
            f"Имя: {display_name(row)}\n"
            f"Username: {username}\n"
            f"ID: {row['user_id']}\n"
            f"Первый запуск: {row['first_seen'][:19].replace('T', ' ')} UTC\n"
            f"Последняя активность: {row['last_seen'][:19].replace('T', ' ')} UTC\n"
            f"Сообщений: {row['messages_count']}\n"
            f"Статус: {'заблокирован' if row['blocked'] else 'активен'}\n"
            "────────────────────",
            reply_markup=admin_user_keyboard(
                user_id,
                bool(row["blocked"]),
            ),
        )

    elif action == "toggle":
        user_id = int(parts[2])
        row = get_user(user_id)

        if row:
            set_blocked(user_id, not bool(row["blocked"]))
            row = get_user(user_id)

            await callback.message.edit_text(
                "ПОЛЬЗОВАТЕЛЬ\n"
                "\n"
                "────────────────────\n"
                f"Имя: {display_name(row)}\n"
                f"ID: {row['user_id']}\n"
                f"Статус: {'заблокирован' if row['blocked'] else 'активен'}\n"
                "────────────────────",
                reply_markup=admin_user_keyboard(
                    user_id,
                    bool(row["blocked"]),
                ),
            )

    await callback.answer()


@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


@dp.message(AdminSearch.waiting, F.text)
async def admin_search(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    query = message.text.strip()
    rows = find_users(query)
    await state.clear()

    if not rows:
        await message.answer(
            "Пользователи не найдены.",
            reply_markup=admin_menu(),
        )
        return

    text = "РЕЗУЛЬТАТЫ ПОИСКА\n\n"
    for row in rows:
        username = f"@{row['username']}" if row["username"] else "нет username"
        text += f"• {display_name(row)} — {username} — ID {row['user_id']}\n"

    await message.answer(text, reply_markup=admin_menu())


async def health(request):
    return web.Response(text="OK")


async def on_startup(app: web.Application):
    init_db()

    external_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL")
    if not external_url:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL is missing. Run this service on Render or set WEBHOOK_URL."
        )

    webhook_url = external_url.rstrip("/") + "/webhook"

    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
    )

    logging.info("Webhook set: %s", webhook_url)


async def on_shutdown(app: web.Application):
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.session.close()


def create_app():
    app = web.Application()

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )

    webhook_requests_handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)

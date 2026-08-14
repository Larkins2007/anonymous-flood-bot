
import os
import sqlite3
import logging
from datetime import datetime, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = 1682289834
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")
DB_PATH = os.getenv("DB_PATH", "users.db")

REPORT_COOLDOWN_SECONDS = 600  # 10 минут между жалобами с одного аккаунта

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ===== Aesthetic =====

def title(text: str) -> str:
    return f"୨୧ {text} ୨୧"

def section(text: str) -> str:
    return f"✦ {text} ✧"

def bullet(text: str) -> str:
    return f"୨ৎ {text}"


# ===== States =====

class FeedbackState(StatesGroup):
    waiting = State()

class ReportTargetState(StatesGroup):
    waiting = State()

class ReportReasonState(StatesGroup):
    waiting = State()

class ReportConfirmState(StatesGroup):
    waiting = State()

class AdminReplyState(StatesGroup):
    waiting = State()

class AdminSearchState(StatesGroup):
    waiting = State()

class BroadcastState(StatesGroup):
    waiting = State()


# ===== Database =====

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def now():
    return datetime.now(timezone.utc).isoformat()

def init_db():
    conn = db()
    conn.executescript("""
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
    """)
    conn.commit()
    conn.close()

def migrate_db():
    conn = db()
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "reports_count" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN reports_count INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()
    conn.close()

def register_user(user):
    stamp = now()
    conn = db()
    conn.execute("""
        INSERT INTO users (
            user_id, first_name, last_name, username, first_seen, last_seen
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            username = excluded.username,
            last_seen = excluded.last_seen
    """, (
        user.id,
        user.first_name or "",
        user.last_name or "",
        user.username or "",
        stamp,
        stamp,
    ))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row

def is_blocked(user_id):
    row = get_user(user_id)
    return bool(row and row["blocked"])

def set_blocked(user_id, value):
    conn = db()
    conn.execute(
        "UPDATE users SET blocked = ? WHERE user_id = ?",
        (1 if value else 0, user_id)
    )
    conn.commit()
    conn.close()

def increment_messages(user_id):
    conn = db()
    conn.execute(
        "UPDATE users SET messages_count = messages_count + 1 WHERE user_id = ?",
        (user_id,)
    )
    conn.execute(
        "INSERT INTO sent_messages (user_id, sent_at) VALUES (?, ?)",
        (user_id, now())
    )
    conn.commit()
    conn.close()

def last_report_time(user_id):
    conn = db()
    row = conn.execute(
        "SELECT created_at FROM reports WHERE reporter_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    conn.close()
    return row["created_at"] if row else None

def increment_reports(user_id):
    conn = db()
    conn.execute(
        "UPDATE users SET reports_count = reports_count + 1 WHERE user_id = ?",
        (user_id,)
    )
    conn.commit()
    conn.close()

def has_reported_target(reporter_id, target_user_id):
    if target_user_id is None:
        return False
    conn = db()
    row = conn.execute(
        "SELECT 1 FROM reports WHERE reporter_id = ? AND target_user_id = ? LIMIT 1",
        (reporter_id, target_user_id)
    ).fetchone()
    conn.close()
    return row is not None

def create_report(reporter_id, target_text, target_user_id, reason):
    conn = db()
    try:
        cur = conn.execute("""
            INSERT INTO reports (
                reporter_id, target_text, target_user_id, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (reporter_id, target_text, target_user_id, reason, now()))
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
        "SELECT * FROM reports WHERE id = ?", (report_id,)
    ).fetchone()
    conn.close()
    return row

def close_report(report_id):
    conn = db()
    conn.execute(
        "UPDATE reports SET status = 'closed' WHERE id = ?",
        (report_id,)
    )
    conn.commit()
    conn.close()

def user_count():
    conn = db()
    value = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return value

def blocked_count():
    conn = db()
    value = conn.execute(
        "SELECT COUNT(*) FROM users WHERE blocked = 1"
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
        "SELECT COUNT(*) FROM reports WHERE status = 'new'"
    ).fetchone()[0]
    conn.close()
    return value

def list_users(limit=10, offset=0):
    conn = db()
    rows = conn.execute("""
        SELECT * FROM users
        ORDER BY last_seen DESC
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()
    conn.close()
    return rows

def search_users(query, limit=15):
    conn = db()
    q = f"%{query}%"
    rows = conn.execute("""
        SELECT * FROM users
        WHERE CAST(user_id AS TEXT) LIKE ?
           OR username LIKE ?
           OR first_name LIKE ?
           OR last_name LIKE ?
        ORDER BY last_seen DESC
        LIMIT ?
    """, (q, q, q, q, limit)).fetchall()
    conn.close()
    return rows

def recent_reports(limit=20):
    conn = db()
    rows = conn.execute("""
        SELECT * FROM reports
        WHERE status = 'new'
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return rows

def display_name(row):
    if not row:
        return "Без имени"
    value = " ".join(
        x for x in (row["first_name"], row["last_name"]) if x
    ).strip()
    return value or "Без имени"


# ===== Keyboards =====

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨  Оставить сообщение", callback_data="u:send")],
        [InlineKeyboardButton(text="✦  Как это работает", callback_data="u:info")],
        [InlineKeyboardButton(text="⚠️  Пожаловаться", callback_data="u:report")],
    ])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="୨ৎ  Отмена", callback_data="u:cancel")]
    ])

def report_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️  Отправить жалобу", callback_data="u:report_confirm")],
        [InlineKeyboardButton(text="୨ৎ  Изменить причину", callback_data="u:report_edit")],
        [InlineKeyboardButton(text="୨ৎ  Отмена", callback_data="u:cancel")],
    ])

def after_send_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨  Ещё сообщение", callback_data="u:send")],
        [InlineKeyboardButton(text="୨୧  В меню", callback_data="u:home")],
    ])

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥  Пользователи", callback_data="a:users:0"),
            InlineKeyboardButton(text="📨  Сообщения", callback_data="a:messages"),
        ],
        [
            InlineKeyboardButton(text="⚠️  Жалобы", callback_data="a:reports"),
            InlineKeyboardButton(text="📊  Статистика", callback_data="a:stats"),
        ],
        [InlineKeyboardButton(text="🚫  Заблокированные", callback_data="a:blocked")],
        [InlineKeyboardButton(text="📣  Рассылка", callback_data="a:broadcast")],
    ])

def back_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="୨ৎ  В админку", callback_data="a:home")]
    ])

def user_admin_kb(user_id, blocked):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Разблокировать" if blocked else "🚫  Заблокировать",
            callback_data=f"a:toggle:{user_id}"
        )],
        [InlineKeyboardButton(
            text="💬  Ответить",
            callback_data=f"a:replyuser:{user_id}"
        )],
        [InlineKeyboardButton(text="୨ৎ  Назад", callback_data="a:users:0")],
    ])

def report_admin_kb(report_id, target_user_id=None):
    rows = []
    if target_user_id:
        rows.append([
            InlineKeyboardButton(
                text="🚫  Заблокировать пользователя",
                callback_data=f"a:block:{target_user_id}"
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="✓  Закрыть жалобу",
            callback_data=f"a:close_report:{report_id}"
        )
    ])
    rows.append([
        InlineKeyboardButton(
            text="୨ৎ  К жалобам",
            callback_data="a:reports"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ===== User interface =====

def home_text():
    return (
        f"{title('Анонимная обратная связь')}\n\n"
        "Здесь можно оставить сообщение администрации флуда.\n\n"
        f"{bullet('Сообщение передаётся без отображения вашего профиля другим пользователям.')}\n\n"
        f"{section('Выберите действие')}"
    )

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    register_user(message.from_user)
    if is_blocked(message.from_user.id):
        await message.answer(
            f"{title('Доступ ограничен')}\n\n"
            "Для вашего аккаунта отправка сообщений отключена."
        )
        return
    await message.answer(home_text(), reply_markup=main_kb())

@dp.callback_query(F.data == "u:home")
async def user_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(home_text(), reply_markup=main_kb())
    await callback.answer()

@dp.callback_query(F.data == "u:info")
async def user_info(callback: CallbackQuery):
    await callback.message.edit_text(
        f"{title('Как это работает')}\n\n"
        f"{bullet('Вы оставляете сообщение через этого бота.')}\n"
        f"{bullet('Другие участники не видят ваш профиль.')}\n"
        f"{bullet('Администрация получает сообщение и может ответить вам через бота.')}\n\n"
        "♡ Спасибо, что помогаете поддерживать комфорт и работу флуда.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📨  Обратная связь", callback_data="u:send")],
            [InlineKeyboardButton(text="⚠️  О жалобах", callback_data="u:report_info")],
            [InlineKeyboardButton(text="୨ৎ  Назад", callback_data="u:home")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "u:report_info")
async def report_info(callback: CallbackQuery):
    await callback.message.edit_text(
        f"⚠️ {title('О жалобах')}\n\n"
        f"{bullet('Жалоба отправляется администрации, а не другим участникам.')}"
        f"\n{bullet('Одна жалоба с аккаунта на одного пользователя.')}"
        f"\n{bullet('Между жалобами действует пауза 10 минут.')}"
        f"\n{bullet('Нельзя пожаловаться на самого себя.')}"
        f"\n{bullet('Администрация видит, кто отправил жалобу.')}"
        "\n\nПеред отправкой бот покажет вам итоговую карточку и попросит подтверждение.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚠️  Подать жалобу", callback_data="u:report")],
            [InlineKeyboardButton(text="୨ৎ  Назад", callback_data="u:info")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "u:send")
async def user_send(callback: CallbackQuery, state: FSMContext):
    if is_blocked(callback.from_user.id):
        await callback.answer("Доступ ограничен.", show_alert=True)
        return
    await state.set_state(FeedbackState.waiting)
    await callback.message.edit_text(
        f"{title('Новое сообщение')}\n\n"
        "Напишите текст следующим сообщением.\n\n"
        "♡ Спасибо, что помогаете поддерживать комфорт и работу флуда.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✦  Что будет с сообщением", callback_data="u:send_info")],
            [InlineKeyboardButton(text="୨ৎ  Отмена", callback_data="u:cancel")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "u:send_info")
async def send_info(callback: CallbackQuery):
    await callback.message.edit_text(
        f"✦ {title('Что будет с сообщением')}\n\n"
        f"{bullet('Сообщение получит администрация бота.')}"
        f"\n{bullet('Ваш профиль не показывается в обычном пользовательском интерфейсе.')}"
        f"\n{bullet('Администрация может ответить вам через бота.')}"
        "\n\n♡ Спасибо за обратную связь.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📨  Написать сообщение", callback_data="u:send")],
            [InlineKeyboardButton(text="୨ৎ  Назад", callback_data="u:home")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "u:cancel")
async def user_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(home_text(), reply_markup=main_kb())
    await callback.answer()

@dp.message(Command("cancel"))
async def command_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("୨ৎ Отправка отменена.", reply_markup=main_kb())

@dp.message(FeedbackState.waiting, F.text)
async def feedback(message: Message, state: FSMContext):
    register_user(message.from_user)
    if is_blocked(message.from_user.id):
        await state.clear()
        await message.answer("Доступ ограничен.")
        return

    text = message.text.strip()
    if not text:
        await message.answer("Сообщение пустое. Попробуйте ещё раз.", reply_markup=cancel_kb())
        return
    if len(text) > 4000:
        await message.answer("Сообщение слишком длинное. Максимум — 4000 символов.", reply_markup=cancel_kb())
        return

    increment_messages(message.from_user.id)
    await state.clear()

    sender_name = " ".join(
        x for x in (message.from_user.first_name, message.from_user.last_name) if x
    ).strip() or "Без имени"
    username = f"@{message.from_user.username}" if message.from_user.username else "нет username"

    admin_text = (
        f"📨 {title('Новое сообщение')}\n\n"
        f"{section('Текст')}\n{text}\n\n"
        f"{section('Отправитель')}\n"
        f"{bullet(f'Имя: {sender_name}')}\n"
        f"{bullet(f'Username: {username}')}\n"
        f"{bullet(f'ID: {message.from_user.id}')}"
    )

    await bot.send_message(
        ADMIN_ID,
        admin_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="💬  Ответить",
                callback_data=f"reply:{message.from_user.id}"
            ),
            InlineKeyboardButton(
                text="🚫  Заблокировать",
                callback_data=f"a:block:{message.from_user.id}"
            )
        ]])
    )

    await message.answer(
        f"{title('Сообщение отправлено')}\n\n"
        "Спасибо за обратную связь.\n\n"
        "Сообщение передано администрации.",
        reply_markup=after_send_kb()
    )

@dp.message(FeedbackState.waiting)
async def feedback_non_text(message: Message):
    await message.answer("Пожалуйста, отправьте сообщение текстом.", reply_markup=cancel_kb())


# ===== Reports =====

@dp.callback_query(F.data == "u:report")
async def report_start(callback: CallbackQuery, state: FSMContext):
    if is_blocked(callback.from_user.id):
        await callback.answer("Доступ ограничен.", show_alert=True)
        return

    last = last_report_time(callback.from_user.id)
    if last:
        try:
            from datetime import datetime, timezone
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
            if elapsed < REPORT_COOLDOWN_SECONDS:
                left = int(REPORT_COOLDOWN_SECONDS - elapsed)
                minutes = max(1, (left + 59) // 60)
                await callback.answer(
                    f"Следующую жалобу можно отправить примерно через {minutes} мин.",
                    show_alert=True
                )
                return
        except ValueError:
            pass

    await state.set_state(ReportTargetState.waiting)
    await callback.message.edit_text(
        f"⚠️ {title('Жалоба на пользователя')}\n\n"
        "Укажите username или Telegram ID пользователя.\n\n"
        "⚠️ При отправке жалобы администрация увидит, от кого она поступила. "
        "Эта жалоба не является анонимной.",
        reply_markup=cancel_kb()
    )
    await callback.answer()

@dp.message(ReportTargetState.waiting, F.text)
async def report_target(message: Message, state: FSMContext):
    value = message.text.strip()
    if len(value) < 2 or len(value) > 100:
        await message.answer("Укажите корректный username или Telegram ID.", reply_markup=cancel_kb())
        return

    cleaned = value.lstrip("@")
    target_user_id = int(cleaned) if cleaned.isdigit() else None

    if target_user_id is not None:
        if target_user_id == message.from_user.id:
            await state.clear()
            await message.answer(
                f"{title('Жалоба')}\\n\\n"
                "Нельзя пожаловаться на самого себя.",
                reply_markup=main_kb()
            )
            return

        if has_reported_target(message.from_user.id, target_user_id):
            await state.clear()
            await message.answer(
                f"{title('Жалоба уже отправлена')}\\n\\n"
                "Вы уже отправляли жалобу на этого пользователя.\\n"
                "Повторная жалоба с этого аккаунта на того же пользователя недоступна.",
                reply_markup=main_kb()
            )
            return

    await state.update_data(target_text=value, target_user_id=target_user_id)
    await state.set_state(ReportReasonState.waiting)

    await message.answer(
        f"{title('Причина жалобы')}\n\n"
        "Кратко опишите, что произошло.\n"
        "После отправки информация будет передана администрации.",
        reply_markup=cancel_kb()
    )

@dp.message(ReportReasonState.waiting, F.text)
async def report_reason(message: Message, state: FSMContext):
    reason = message.text.strip()
    if len(reason) < 3:
        await message.answer(
            "Опишите причину немного подробнее.",
            reply_markup=cancel_kb()
        )
        return
    if len(reason) > 2000:
        await message.answer(
            "Причина слишком длинная. Максимум — 2000 символов.",
            reply_markup=cancel_kb()
        )
        return

    data = await state.get_data()
    await state.update_data(reason=reason)
    await state.set_state(ReportConfirmState.waiting)

    await message.answer(
        f"⚠️ {title('Проверьте жалобу')}\n\n"
        f"{section('На пользователя')}\n"
        f"{bullet(data['target_text'])}\n\n"
        f"{section('Причина')}\n"
        f"{reason}\n\n"
        "Важно: жалоба не является анонимной. "
        "Администрация увидит ваш аккаунт как заявителя.\n\n"
        "Если всё верно, отправьте жалобу.",
        reply_markup=report_confirm_kb()
    )

@dp.callback_query(F.data == "u:report_edit")
async def report_edit(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("target_text"):
        await callback.answer("Сначала укажите пользователя.", show_alert=True)
        return
    await state.set_state(ReportReasonState.waiting)
    await callback.message.edit_text(
        f"{title('Причина жалобы')}\n\n"
        "Напишите причину заново.",
        reply_markup=cancel_kb()
    )
    await callback.answer()

@dp.callback_query(F.data == "u:report_confirm")
async def report_confirm(callback: CallbackQuery, state: FSMContext):
    if is_blocked(callback.from_user.id):
        await state.clear()
        await callback.answer("Доступ ограничен.", show_alert=True)
        return

    data = await state.get_data()
    if not data.get("target_text") or not data.get("reason"):
        await state.clear()
        await callback.answer("Данные жалобы устарели.", show_alert=True)
        return

    last = last_report_time(callback.from_user.id)
    if last:
        try:
            from datetime import datetime, timezone
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
            if elapsed < REPORT_COOLDOWN_SECONDS:
                left = int(REPORT_COOLDOWN_SECONDS - elapsed)
                minutes = max(1, (left + 59) // 60)
                await state.clear()
                await callback.message.edit_text(
                    f"{title('Слишком часто')}\n\n"
                    f"Следующую жалобу можно отправить примерно через {minutes} мин.",
                    reply_markup=main_kb()
                )
                await callback.answer()
                return
        except ValueError:
            pass

    target_id = data.get("target_user_id")
    if target_id is not None and has_reported_target(callback.from_user.id, target_id):
        await state.clear()
        await callback.message.edit_text(
            f"{title('Жалоба уже отправлена')}\n\n"
            "Вы уже отправляли жалобу на этого пользователя.",
            reply_markup=main_kb()
        )
        await callback.answer()
        return

    report_id = create_report(
        callback.from_user.id,
        data["target_text"],
        target_id,
        data["reason"]
    )

    if report_id is None:
        await state.clear()
        await callback.message.edit_text(
            f"{title('Жалоба уже отправлена')}\n\n"
            "Вы уже отправляли жалобу на этого пользователя.",
            reply_markup=main_kb()
        )
        await callback.answer()
        return

    increment_reports(callback.from_user.id)
    await state.clear()

    reporter = get_user(callback.from_user.id)
    reporter_name = display_name(reporter)
    reporter_username = (
        f"@{reporter['username']}" if reporter and reporter["username"]
        else "нет username"
    )
    target_id_text = str(target_id) if target_id else "не указан"

    admin_text = (
        f"⚠️ {title(f'Жалоба #{report_id}')}\n\n"
        f"{section('На пользователя')}\n"
        f"{bullet('Указано: ' + data['target_text'])}\n"
        f"{bullet('ID: ' + target_id_text)}\n\n"
        f"{section('Причина')}\n{data['reason']}\n\n"
        f"{section('Заявитель')}\n"
        f"{bullet('Имя: ' + reporter_name)}\n"
        f"{bullet('Username: ' + reporter_username)}\n"
        f"{bullet('ID: ' + str(callback.from_user.id))}"
    )

    await bot.send_message(
        ADMIN_ID,
        admin_text,
        reply_markup=report_admin_kb(report_id, target_id)
    )

    await callback.message.edit_text(
        f"{title('Жалоба отправлена')}\n\n"
        "Администрация получила обращение.\n\n"
        "⚠️ Напоминаем: заявитель виден администрации.\n\n"
        "Ограничение: следующую жалобу можно будет отправить не раньше чем через 10 минут.",
        reply_markup=main_kb()
    )
    await callback.answer("Жалоба отправлена.")

@dp.message(ReportTargetState.waiting)
@dp.message(ReportReasonState.waiting)
async def report_non_text(message: Message):
    await message.answer("Пожалуйста, отправьте ответ текстом.", reply_markup=cancel_kb())


# ===== Admin reply =====

@dp.callback_query(F.data.startswith("reply:"))
async def admin_reply_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    user_id = int(callback.data.split(":")[1])
    if not get_user(user_id):
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    await state.update_data(reply_to=user_id)
    await state.set_state(AdminReplyState.waiting)
    await callback.message.answer(
        f"💬 {title('Ответ пользователю')}\n\n"
        "Напишите текст ответа.",
        reply_markup=cancel_kb()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("a:replyuser:"))
async def admin_reply_user_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    user_id = int(callback.data.split(":")[2])
    if not get_user(user_id):
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    await state.update_data(reply_to=user_id)
    await state.set_state(AdminReplyState.waiting)
    await callback.message.answer(
        f"💬 {title('Ответ пользователю')}\n\n"
        "Напишите текст ответа.",
        reply_markup=cancel_kb()
    )
    await callback.answer()

@dp.message(AdminReplyState.waiting, F.text)
async def admin_reply(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    await state.clear()
    user_id = data.get("reply_to")

    if not user_id:
        await message.answer("Получатель не найден.")
        return

    try:
        await bot.send_message(
            user_id,
            f"💬 {title('Ответ администрации')}\n\n{message.text}\n\n୨ৎ Ответ отправлен через бота.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="📨  Ответить администрации",
                    callback_data="u:send"
                )]
            ])
        )
        await message.answer("💬 Ответ отправлен.")
    except Exception:
        await message.answer(
            "Не удалось доставить ответ. Возможно, пользователь заблокировал бота."
        )


# ===== Admin panel =====

def admin_home_text():
    return (
        f"{title('Панель администратора')}\n\n"
        f"{section('Сводка')}\n"
        f"{bullet(f'Пользователей: {user_count()}')}\n"
        f"{bullet(f'Сообщений: {message_count()}')}\n"
        f"{bullet(f'Заблокировано: {blocked_count()}')}\n"
        f"{bullet(f'Новых жалоб: {new_reports_count()}')}\n\n"
        "Выберите раздел."
    )

@dp.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer(admin_home_text(), reply_markup=admin_kb())


@dp.callback_query(F.data == "a:home")
async def admin_home(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(admin_home_text(), reply_markup=admin_kb())
    await callback.answer()

@dp.callback_query(F.data == "a:stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        f"{title('Статистика')}\n\n"
        f"{bullet(f'Пользователей: {user_count()}')}\n"
        f"{bullet(f'Получено сообщений: {message_count()}')}\n"
        f"{bullet(f'Заблокировано: {blocked_count()}')}\n"
        f"{bullet(f'Новых жалоб: {new_reports_count()}')}",
        reply_markup=back_admin()
    )
    await callback.answer()

@dp.callback_query(F.data == "a:messages")
async def admin_messages(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.message.edit_text(
        f"{title('Сообщения')}\n\n"
        f"{bullet(f'Всего получено: {message_count()}')}\n\n"
        "Новые сообщения приходят прямо в чат администрации.",
        reply_markup=back_admin()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("a:users:"))
async def admin_users(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    page = int(callback.data.split(":")[2])
    rows = list_users(10, page * 10)
    buttons = []

    for row in rows:
        label = display_name(row)
        if row["blocked"]:
            label = f"🚫  {label}"
        buttons.append([
            InlineKeyboardButton(
                text=label[:40],
                callback_data=f"a:user:{row['user_id']}"
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="‹",
            callback_data=f"a:users:{page - 1}"
        ))
    if len(rows) == 10:
        nav.append(InlineKeyboardButton(
            text="›",
            callback_data=f"a:users:{page + 1}"
        ))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(
        text="⌕  Поиск",
        callback_data="a:search"
    )])
    buttons.append([InlineKeyboardButton(
        text="୨ৎ  Назад",
        callback_data="a:home"
    )])

    await callback.message.edit_text(
        f"{title('Пользователи')}\n\n"
        f"{bullet(f'Всего: {user_count()}')}\n"
        f"{bullet(f'Страница: {page + 1}')}\n\n"
        "Выберите пользователя.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()

@dp.callback_query(F.data == "a:search")
async def admin_search_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminSearchState.waiting)
    await callback.message.answer(
        f"{title('Поиск пользователя')}\n\n"
        "Введите имя, username или Telegram ID.",
        reply_markup=cancel_kb()
    )
    await callback.answer()

@dp.message(AdminSearchState.waiting, F.text)
async def admin_search(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    rows = search_users(message.text.strip())
    await state.clear()

    if not rows:
        await message.answer("୨ৎ Пользователи не найдены.", reply_markup=admin_kb())
        return

    buttons = []
    for row in rows:
        buttons.append([
            InlineKeyboardButton(
                text=display_name(row)[:40],
                callback_data=f"a:user:{row['user_id']}"
            )
        ])

    await message.answer(
        f"{title('Результаты поиска')}\n\n"
        f"{bullet(f'Найдено: {len(rows)}')}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("a:user:"))
async def admin_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[2])
    row = get_user(user_id)
    if not row:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    username = f"@{row['username']}" if row["username"] else "нет username"
    status = "заблокирован" if row["blocked"] else "активен"

    await callback.message.edit_text(
        f"{title('Пользователь')}\n\n"
        f"{bullet('Имя: ' + display_name(row))}\n"
        f"{bullet('Username: ' + username)}\n"
        f"{bullet('Telegram ID: ' + str(row['user_id']))}\n"
        f"{bullet('Сообщений: ' + str(row['messages_count']))}\n"
        f"{bullet('Жалоб отправлено: ' + str(row['reports_count']))}\n"
        f"{bullet('Статус: ' + status)}\n\n"
        f"{section('Активность')}\n"
        f"{bullet('Первый запуск: ' + row['first_seen'][:19].replace('T', ' ') + ' UTC')}\n"
        f"{bullet('Последняя активность: ' + row['last_seen'][:19].replace('T', ' ') + ' UTC')}",
        reply_markup=user_admin_kb(user_id, bool(row["blocked"]))
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("a:toggle:"))
async def admin_toggle(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[2])
    row = get_user(user_id)
    if not row:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    new_value = not bool(row["blocked"])
    set_blocked(user_id, new_value)
    await callback.answer(
        "Пользователь заблокирован." if new_value else "Пользователь разблокирован.",
        show_alert=True
    )

    row = get_user(user_id)
    username = f"@{row['username']}" if row["username"] else "нет username"
    status = "заблокирован" if row["blocked"] else "активен"

    await callback.message.edit_text(
        f"{title('Пользователь')}\n\n"
        f"{bullet('Имя: ' + display_name(row))}\n"
        f"{bullet('Username: ' + username)}\n"
        f"{bullet('ID: ' + str(row['user_id']))}\n"
        f"{bullet('Статус: ' + status)}",
        reply_markup=user_admin_kb(user_id, bool(row["blocked"]))
    )

@dp.callback_query(F.data.startswith("a:block:"))
async def admin_block(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    user_id = int(callback.data.split(":")[2])
    if not get_user(user_id):
        await callback.answer("Пользователь не найден.", show_alert=True)
        return

    set_blocked(user_id, True)
    await callback.answer("Пользователь заблокирован.", show_alert=True)

@dp.callback_query(F.data == "a:blocked")
async def admin_blocked(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    conn = db()
    rows = conn.execute("""
        SELECT * FROM users
        WHERE blocked = 1
        ORDER BY last_seen DESC
        LIMIT 50
    """).fetchall()
    conn.close()

    text = f"{title('Заблокированные')}\n\n"
    if not rows:
        text += "♡ Список пуст."
    else:
        for row in rows:
            username = f"@{row['username']}" if row["username"] else "нет username"
            text += (
                f"🚫 {display_name(row)}\n"
                f"{bullet(username)}\n"
                f"{bullet(str(row['user_id']))}\n\n"
            )

    await callback.message.edit_text(text, reply_markup=back_admin())
    await callback.answer()

@dp.callback_query(F.data == "a:reports")
async def admin_reports(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    rows = recent_reports()
    if not rows:
        await callback.message.edit_text(
            f"{title('Жалобы')}\n\n♡ Новых жалоб нет.",
            reply_markup=back_admin()
        )
        await callback.answer()
        return

    buttons = []
    text = f"{title('Новые жалобы')}\n\n"

    for row in rows:
        text += (
            f"⚠️ #{row['id']}  {row['target_text']}\n"
            f"{bullet(row['reason'][:100])}\n\n"
        )
        buttons.append([
            InlineKeyboardButton(
                text=f"Открыть жалобу #{row['id']}",
                callback_data=f"a:report:{row['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            text="୨ৎ  Назад",
            callback_data="a:home"
        )
    ])

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("a:report:"))
async def admin_report(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    report_id = int(callback.data.split(":")[2])
    report = get_report(report_id)
    if not report:
        await callback.answer("Жалоба не найдена.", show_alert=True)
        return

    reporter = get_user(report["reporter_id"])
    reporter_username = (
        f"@{reporter['username']}"
        if reporter and reporter["username"]
        else "нет username"
    )
    target_id = str(report["target_user_id"]) if report["target_user_id"] else "не указан"

    await callback.message.edit_text(
        f"⚠️ {title(f'Жалоба #{report_id}')}\n\n"
        f"{section('Пользователь')}\n"
        f"{bullet(report['target_text'])}\n"
        f"{bullet('ID: ' + target_id)}\n\n"
        f"{section('Причина')}\n"
        f"{report['reason']}\n\n"
        f"{section('Заявитель')}\n"
        f"{bullet('Имя: ' + display_name(reporter))}\n"
        f"{bullet('Username: ' + reporter_username)}\n"
        f"{bullet('ID: ' + str(report['reporter_id']))}",
        reply_markup=report_admin_kb(report_id, report["target_user_id"])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("a:close_report:"))
async def admin_close_report(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    report_id = int(callback.data.split(":")[2])
    close_report(report_id)
    await callback.answer("Жалоба закрыта.", show_alert=True)
    await callback.message.edit_text(
        f"{title('Жалоба закрыта')}\n\n"
        f"{bullet('Номер: #' + str(report_id))}\n"
        "୨ৎ Обращение отмечено как рассмотренное.",
        reply_markup=back_admin()
    )

@dp.callback_query(F.data == "a:broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    await state.set_state(BroadcastState.waiting)
    await callback.message.answer(
        f"{title('Рассылка')}\n\n"
        "Напишите текст, который нужно отправить всем зарегистрированным пользователям.\n\n"
        "⚠️ Используйте рассылку только для важных объявлений.",
        reply_markup=cancel_kb()
    )
    await callback.answer()

@dp.message(BroadcastState.waiting, F.text)
async def admin_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    text = message.text.strip()
    await state.clear()

    conn = db()
    ids = [
        row["user_id"]
        for row in conn.execute(
            "SELECT user_id FROM users WHERE blocked = 0"
        ).fetchall()
    ]
    conn.close()

    sent = 0
    failed = 0

    for user_id in ids:
        try:
            await bot.send_message(
                user_id,
                f"📣 {title('Сообщение от администрации')}\n\n{text}"
            )
            sent += 1
        except Exception:
            failed += 1

    await message.answer(
        f"{title('Рассылка завершена')}\n\n"
        f"{bullet('Отправлено: ' + str(sent))}\n"
        f"{bullet('Не доставлено: ' + str(failed))}",
        reply_markup=admin_kb()
    )

@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


# ===== Webhook =====

async def health(request):
    return web.Response(text="OK")

async def startup(app):
    init_db()
    migrate_db()

    external_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL")
    if not external_url:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL is missing. Run this service on Render "
            "or set WEBHOOK_URL."
        )

    webhook_url = external_url.rstrip("/") + "/webhook"

    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
    )

    logging.info("Webhook set: %s", webhook_url)

async def shutdown(app):
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.session.close()

def create_app():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    return app

app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)

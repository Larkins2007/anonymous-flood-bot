import os
import asyncio
import logging
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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is not set")

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class SendAnon(StatesGroup):
    waiting = State()

class AdminReply(StatesGroup):
    waiting = State()

blocked_users = set()
last_message_at = {}

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕵️ Отправить анонимное сообщение", callback_data="send")]
    ])

def admin_message_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 Ответить", callback_data=f"reply:{user_id}"),
            InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block:{user_id}")
        ]
    ])

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id in blocked_users:
        await message.answer("Доступ к анонимке для вас ограничен.")
        return
    await message.answer(
        "🕵️ <b>Анонимка флуда</b>\n\n"
        "Нажми кнопку ниже и отправь сообщение. "
        "Твоё имя и username не будут показаны получателю.",
        reply_markup=main_menu()
    )

@dp.callback_query(F.data == "send")
async def send_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id in blocked_users:
        await callback.answer("Доступ ограничен.", show_alert=True)
        return
    await state.set_state(SendAnon.waiting)
    await callback.message.answer(
        "✍️ Напиши сообщение одним сообщением.\n\n"
        "Для отмены отправь /cancel"
    )
    await callback.answer()

@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id == ADMIN_ID:
        await message.answer("Отменено.")
    else:
        await message.answer("Отправка отменена.", reply_markup=main_menu())

@dp.message(SendAnon.waiting, F.text)
async def receive_anon(message: Message, state: FSMContext):
    uid = message.from_user.id
    if uid in blocked_users:
        await state.clear()
        await message.answer("Доступ ограничен.")
        return

    now = datetime.now(timezone.utc).timestamp()
    if now - last_message_at.get(uid, 0) < 15:
        await message.answer("⏳ Подожди 15 секунд перед следующей отправкой.")
        return

    text = message.text.strip()
    if not text:
        await message.answer("Сообщение пустое. Попробуй ещё раз.")
        return
    if len(text) > 4000:
        await message.answer("Сообщение слишком длинное. Максимум — 4000 символов.")
        return

    last_message_at[uid] = now
    await state.clear()

    admin_text = (
        "📨 <b>Новое анонимное сообщение</b>\n\n"
        f"{text}\n\n"
        "🕵️ Отправитель скрыт от отображения."
    )
    await bot.send_message(
        ADMIN_ID,
        admin_text,
        reply_markup=admin_message_kb(uid)
    )
    await message.answer(
        "✅ Сообщение анонимно отправлено.\n\n"
        "Можно отправить ещё одно:",
        reply_markup=main_menu()
    )

@dp.message(SendAnon.waiting)
async def non_text(message: Message):
    await message.answer("Пожалуйста, отправь сообщение именно текстом.")

@dp.callback_query(F.data.startswith("block:"))
async def block_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    uid = int(callback.data.split(":")[1])
    blocked_users.add(uid)
    await callback.answer("Пользователь заблокирован.", show_alert=True)

@dp.callback_query(F.data.startswith("reply:"))
async def reply_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    uid = int(callback.data.split(":")[1])
    await state.update_data(reply_to=uid)
    await state.set_state(AdminReply.waiting)
    await callback.message.answer(
        "💬 Напиши ответ пользователю.\n"
        "Ответ будет отправлен анонимно от имени бота.\n\n"
        "Для отмены: /cancel"
    )
    await callback.answer()

@dp.message(AdminReply.waiting, F.text)
async def admin_reply(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    uid = data.get("reply_to")
    if not uid:
        await state.clear()
        await message.answer("Не удалось определить получателя.")
        return
    try:
        await bot.send_message(
            uid,
            "💬 <b>Анонимный ответ от администрации:</b>\n\n" + message.text
        )
        await message.answer("✅ Ответ отправлен анонимно.")
    except Exception:
        await message.answer(
            "❌ Не удалось доставить ответ. Возможно, пользователь заблокировал бота."
        )
    await state.clear()

async def health(request):
    return web.Response(text="OK")

async def on_startup(app: web.Application):
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

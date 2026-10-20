"""Панель «⚙️ Настройки команд» в ЛС владельца.

/settings → категории → карточка команды → редактор параметра.
У каждой команды своя карточка: включение, доступ, место, пауза, автоудаление
и параметры, специфичные только для неё (см. cmdspecs.py).

Здесь же — «Свои команды»: создание, правка текста и удаление.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import cmdcfg
import ui
from cmdcfg import CATEGORIES, REGISTRY, Param, Spec, all_params, find_param, format_value

logger = logging.getLogger("settings_panel")

PAGE_SIZE_PARAMS = 8
PAGE_SIZE_COMMANDS = 10


class PanelState(StatesGroup):
    waiting_value = State()
    waiting_cc_name = State()
    waiting_cc_response = State()


class PanelCtx:
    bot: Any = None
    owner_id: int = 0
    refresh_menu: Optional[Callable[[], Awaitable[None]]] = None
    custom_list: Callable[[], list] = staticmethod(lambda: [])
    custom_save: Optional[Callable] = None
    custom_delete: Optional[Callable] = None
    custom_normalize: Optional[Callable] = None


CTX = PanelCtx()


def _b(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def _chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# --------------------------------------------------------------------------
# Экраны
# --------------------------------------------------------------------------

def home_view() -> tuple[str, InlineKeyboardMarkup]:
    total = len([s for s in REGISTRY.values() if not s.feature])
    features = len([s for s in REGISTRY.values() if s.feature])
    changed = sum(1 for k in REGISTRY if cmdcfg.changed_from_default(k))
    text = ui.card(
        "Настройки команд",
        ui.lines(
            ui.bullet(f"Команд: {total}, функций: {features}"),
            ui.bullet(f"Изменено вручную: {changed}"),
        ),
        ui.note("Выберите раздел. У каждой команды своя карточка настроек."),
    )
    buttons = []
    for cat, (emoji, label) in CATEGORIES.items():
        n = len(cmdcfg.specs_in(cat))
        if cat == "custom":
            n = len(cmdcfg.specs_in("custom"))
        buttons.append(_b(f"{emoji} {label} · {n}", f"cs:cat:{cat}:0"))
    rows = _chunk(buttons, 2)
    rows.append([_b("🔄 Обновить меню команд", "cs:menu")])
    return text, _kb(rows)


def cat_view(cat: str, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    emoji, label = CATEGORIES.get(cat, ("•", cat))
    specs = sorted(cmdcfg.specs_in(cat), key=lambda s: (s.feature, s.title))
    pages = max(1, -(-len(specs) // PAGE_SIZE_COMMANDS))
    page = max(0, min(page, pages - 1))
    chunk = specs[page * PAGE_SIZE_COMMANDS:(page + 1) * PAGE_SIZE_COMMANDS]
    rows = []
    for spec in chunk:
        mark = ui.onoff(cmdcfg.is_enabled(spec.key))
        star = " ✎" if cmdcfg.changed_from_default(spec.key) else ""
        rows.append([_b(f"{mark} {spec.emoji} {spec.title}{star}", f"cs:cmd:{spec.key}:0")])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_b("‹", f"cs:cat:{cat}:{page - 1}"))
        nav.append(_b(f"{page + 1}/{pages}", "cs:noop"))
        if page < pages - 1:
            nav.append(_b("›", f"cs:cat:{cat}:{page + 1}"))
        rows.append(nav)
    if cat == "custom":
        rows.append([_b("➕ Новая команда", "cs:ccnew")])
    rows.append([_b("‹ Разделы", "cs:home")])
    if specs:
        text = ui.card(f"{emoji} {label}", ui.note("🟢 включена · ⚫ выключена · ✎ есть свои настройки"))
    else:
        text = ui.card(f"{emoji} {label}", ui.note("Пока пусто."))
    return text, _kb(rows)


def _param_label(spec: Spec, p: Param) -> str:
    value = cmdcfg.get(spec.key, p.key)
    if p.kind == "bool":
        return f"{ui.onoff(bool(value))} {p.title}"
    return ui.short(f"{p.title}: {format_value(p, value)}", 46)


def cmd_view(key: str, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    spec = REGISTRY.get(key)
    if spec is None:
        return ui.card("Команда не найдена", ui.note("Возможно, она была удалена.")), _kb([[_b("‹ Разделы", "cs:home")]])
    params = list(all_params(spec))
    pages = max(1, -(-len(params) // PAGE_SIZE_PARAMS))
    page = max(0, min(page, pages - 1))
    chunk = params[page * PAGE_SIZE_PARAMS:(page + 1) * PAGE_SIZE_PARAMS]

    blocks = [spec.desc]
    if spec.triggers:
        blocks.append(ui.lines(ui.section("Как вызвать"), *[ui.bullet(t) for t in spec.triggers]))
    changed = cmdcfg.changed_from_default(key)
    state_line = f"{ui.onoff(cmdcfg.is_enabled(key))} {'включена' if cmdcfg.is_enabled(key) else 'выключена'}"
    blocks.append(ui.lines(ui.section("Состояние"), ui.bullet(state_line),
                           ui.bullet(f"Изменено параметров: {changed}") if changed else None))
    text = ui.card(f"{spec.emoji} {spec.title}", *blocks, footer=ui.note("Нажмите на параметр, чтобы изменить."))

    rows = []
    for p in chunk:
        data = f"cs:t:{key}:{p.key}" if p.kind == "bool" else f"cs:p:{key}:{p.key}"
        rows.append([_b(_param_label(spec, p), data)])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_b("‹", f"cs:cmd:{key}:{page - 1}"))
        nav.append(_b(f"{page + 1}/{pages}", "cs:noop"))
        if page < pages - 1:
            nav.append(_b("›", f"cs:cmd:{key}:{page + 1}"))
        rows.append(nav)
    tools = [_b("↺ Сбросить всё", f"cs:r:{key}")]
    if spec.custom:
        tools.append(_b("🗑 Удалить", f"cs:ccdel:{key}"))
    rows.append(tools)
    rows.append([_b("‹ К разделу", f"cs:cat:{spec.category}:0")])
    return text, _kb(rows)


def param_view(key: str, pkey: str) -> tuple[str, InlineKeyboardMarkup]:
    spec = REGISTRY.get(key)
    p = find_param(spec, pkey) if spec else None
    if spec is None or p is None:
        return cmd_view(key)
    value = cmdcfg.get(key, pkey)
    lines = [ui.kv("Сейчас", format_value(p, value)), ui.kv("По умолчанию", format_value(p, p.default))]
    if p.kind == "text":
        lines = [ui.kv("Сейчас", ui.short(str(value or "— пусто —"), 300))]
    blocks = [ui.lines(*lines)]
    if p.help:
        blocks.append(ui.note(p.help))
    text = ui.card(f"{spec.emoji} {p.title}", *blocks)

    rows: list[list] = []
    if p.kind == "int":
        rows.append([_b(f"−{p.step}", f"cs:i:{key}:{pkey}:-{p.step}"), _b(f"+{p.step}", f"cs:i:{key}:{pkey}:{p.step}")])
        if p.presets:
            rows.append([_b(f"{v}" + (f" {p.unit}" if p.unit else ""), f"cs:v:{key}:{pkey}:{v}") for v in p.presets][:5])
        rows.append([_b("✎ Ввести число", f"cs:e:{key}:{pkey}")])
    elif p.kind == "choice":
        for v, label in p.choices:
            mark = "✓ " if v == value else ""
            rows.append([_b(f"{mark}{label}", f"cs:v:{key}:{pkey}:{v}")])
    elif p.kind == "hour":
        hours = [6, 8, 9, 10, 12, 15, 18, 20, 21, 22]
        rows += _chunk([_b(f"{'✓ ' if h == value else ''}{h:02d}:00", f"cs:v:{key}:{pkey}:{h}") for h in hours], 5)
        rows.append([_b("✎ Ввести час (0–23)", f"cs:e:{key}:{pkey}")])
    elif p.kind == "text":
        rows.append([_b("✎ Изменить текст", f"cs:e:{key}:{pkey}")])
        if p.allow_empty:
            rows.append([_b("🧹 Очистить", f"cs:v:{key}:{pkey}:")])
    rows.append([_b("↺ По умолчанию", f"cs:d:{key}:{pkey}")])
    rows.append([_b("‹ Назад", f"cs:cmd:{key}:0")])
    return text, _kb(rows)


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------

async def _show(callback: CallbackQuery, view: tuple[str, InlineKeyboardMarkup]) -> None:
    text, markup = view
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass  # «message is not modified»


def _after_change(key: str, pkey: str) -> None:
    if pkey in ("enabled", "access", "where") and CTX.refresh_menu is not None:
        try:
            asyncio.get_running_loop().create_task(CTX.refresh_menu())
        except RuntimeError:
            pass


def _is_owner(user) -> bool:
    return bool(user and int(user.id) == CTX.owner_id)


# --------------------------------------------------------------------------
# Хендлеры
# --------------------------------------------------------------------------

async def cmd_settings(message: Message, state: FSMContext):
    if not await cmdcfg.gate(message, "settings"):
        return
    await state.clear()
    text, markup = home_view()
    await message.answer(text, reply_markup=markup)


async def cmd_open_custom(message: Message, state: FSMContext):
    """/commands и /manage_commands открывают раздел «Свои команды»."""
    if not await cmdcfg.gate(message, "manage_commands"):
        return
    await state.clear()
    text, markup = cat_view("custom", 0)
    await message.answer(text, reply_markup=markup)


async def on_callback(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user):
        await callback.answer("Только для владельца.", show_alert=True)
        return
    parts = (callback.data or "").split(":", 4)
    action = parts[1] if len(parts) > 1 else ""

    if action == "noop":
        await callback.answer()
        return
    if action == "home":
        await state.clear()
        await _show(callback, home_view())
        await callback.answer()
        return
    if action == "menu":
        if CTX.refresh_menu is not None:
            await CTX.refresh_menu()
        await callback.answer("Меню команд обновлено.")
        return
    if action == "cat" and len(parts) >= 3:
        page = int(parts[3]) if len(parts) > 3 and parts[3].lstrip("-").isdigit() else 0
        await state.clear()
        await _show(callback, cat_view(parts[2], page))
        await callback.answer()
        return
    if action == "cmd" and len(parts) >= 3:
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        await state.clear()
        await _show(callback, cmd_view(parts[2], page))
        await callback.answer()
        return

    key = parts[2] if len(parts) > 2 else ""
    pkey = parts[3] if len(parts) > 3 else ""
    extra = parts[4] if len(parts) > 4 else ""

    if action == "p":
        await state.clear()
        await _show(callback, param_view(key, pkey))
        await callback.answer()
        return
    if action == "t":                                   # переключатель
        spec = REGISTRY.get(key)
        param = find_param(spec, pkey) if spec else None
        if param is None:
            await callback.answer("Настройка не найдена.", show_alert=True)
            return
        cmdcfg.set_value(key, pkey, not cmdcfg.get_bool(key, pkey))
        _after_change(key, pkey)
        await _show(callback, cmd_view(key, _page_of(spec, pkey)))
        await callback.answer("Сохранено.")
        return
    if action == "v":                                   # готовое значение
        ok, result = cmdcfg.set_value(key, pkey, extra if extra != "" else "")
        if not ok:
            await callback.answer(str(result), show_alert=True)
            return
        _after_change(key, pkey)
        await _show(callback, param_view(key, pkey))
        await callback.answer("Сохранено.")
        return
    if action == "i":                                   # шаг ±
        try:
            delta = int(extra)
        except ValueError:
            await callback.answer()
            return
        current = cmdcfg.get_int(key, pkey)
        ok, result = cmdcfg.set_value(key, pkey, current + delta)
        if not ok:
            await callback.answer(str(result), show_alert=True)
            return
        await _show(callback, param_view(key, pkey))
        await callback.answer(f"{result}")
        return
    if action == "d":
        cmdcfg.reset(key, pkey)
        _after_change(key, pkey)
        await _show(callback, param_view(key, pkey))
        await callback.answer("Возвращено значение по умолчанию.")
        return
    if action == "e":                                   # ввод текстом
        spec = REGISTRY.get(key)
        param = find_param(spec, pkey) if spec else None
        if param is None:
            await callback.answer("Настройка не найдена.", show_alert=True)
            return
        await state.set_state(PanelState.waiting_value)
        await state.update_data(key=key, pkey=pkey, chat_id=callback.message.chat.id, msg_id=callback.message.message_id)
        hint = {"int": f"число от {param.min} до {param.max}", "hour": "час от 0 до 23",
                "text": f"текст до {param.max_len} символов"}.get(param.kind, "значение")
        await callback.message.answer(
            ui.card(f"✎ {param.title}", ui.bullet(f"Отправьте {hint} одним сообщением."), ui.note("«-» — отмена.")))
        await callback.answer()
        return
    if action == "r":
        text = ui.card("Сбросить настройки?", ui.note("Все параметры этой команды вернутся к значениям по умолчанию."))
        await callback.message.edit_text(text, reply_markup=_kb([
            [_b("↺ Да, сбросить", f"cs:rc:{key}"), _b("Отмена", f"cs:cmd:{key}:0")]]))
        await callback.answer()
        return
    if action == "rc":
        cmdcfg.reset(key)
        if CTX.refresh_menu is not None:
            await CTX.refresh_menu()
        await _show(callback, cmd_view(key))
        await callback.answer("Сброшено.")
        return
    if action == "ccnew":
        await state.set_state(PanelState.waiting_cc_name)
        await callback.message.answer(ui.card(
            "Новая команда",
            ui.bullet("Отправьте имя: латиница, цифры и «_», до 32 символов."),
            ui.bullet("Например: rules"), ui.note("«-» — отмена.")))
        await callback.answer()
        return
    if action == "ccdel":
        spec = REGISTRY.get(key)
        title = spec.title if spec else key
        await callback.message.edit_text(
            ui.card("Удалить команду?", ui.bullet(title), ui.note("Это действие нельзя отменить.")),
            reply_markup=_kb([[_b("🗑 Да, удалить", f"cs:ccdelc:{key}"), _b("Отмена", f"cs:cmd:{key}:0")]]))
        await callback.answer()
        return
    if action == "ccdelc":
        name = key[3:] if key.startswith("cc_") else key
        if CTX.custom_delete:
            CTX.custom_delete(name)
        cmdcfg.unregister(key)
        cmdcfg.STORE.drop(key)
        if CTX.refresh_menu is not None:
            await CTX.refresh_menu()
        await _show(callback, cat_view("custom", 0))
        await callback.answer("Удалено.")
        return
    await callback.answer()


def _page_of(spec: Spec, pkey: str) -> int:
    keys = [p.key for p in all_params(spec)]
    return keys.index(pkey) // PAGE_SIZE_PARAMS if pkey in keys else 0


async def on_value_text(message: Message, state: FSMContext):
    data = await state.get_data()
    key, pkey = data.get("key"), data.get("pkey")
    text = (message.text or "").strip()
    if text in ("-", "—", "/cancel"):
        await state.clear()
        await message.answer(ui.note("Отменено."))
        return
    ok, result = cmdcfg.set_value(key, pkey, text)
    if not ok:
        await message.answer(ui.fail(str(result)) + "\n" + ui.note("Попробуйте ещё раз или отправьте «-»."))
        return
    await state.clear()
    _after_change(key, pkey)
    view_text, markup = param_view(key, pkey)
    try:
        await CTX.bot.edit_message_text(view_text, chat_id=data.get("chat_id"), message_id=data.get("msg_id"), reply_markup=markup)
    except Exception:
        await message.answer(view_text, reply_markup=markup)
        return
    await message.answer(ui.ok("Сохранено."))


async def on_cc_name(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if text in ("-", "—", "/cancel"):
        await state.clear()
        await message.answer(ui.note("Отменено."))
        return
    try:
        name = CTX.custom_normalize(text)
    except ValueError:
        await message.answer(ui.fail("Имя: латиница, цифры, «_», начинается с буквы, до 32 символов."))
        return
    if name in REGISTRY or ("cc_" + name) in REGISTRY or name in {s.triggers[0].lstrip("/").split()[0] for s in REGISTRY.values() if s.triggers and s.triggers[0].startswith("/")}:
        await message.answer(ui.fail("Такая команда уже есть (встроенная или своя). Выберите другое имя."))
        return
    await state.update_data(cc_name=name)
    await state.set_state(PanelState.waiting_cc_response)
    await message.answer(ui.card(
        f"/{name}", ui.bullet("Теперь отправьте текст ответа."),
        ui.bullet("Можно использовать {user}, {username}, {user_id}, {chat}."), ui.note("«-» — отмена.")))


async def on_cc_response(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    data = await state.get_data()
    if text in ("-", "—", "/cancel"):
        await state.clear()
        await message.answer(ui.note("Отменено."))
        return
    name = data.get("cc_name")
    try:
        CTX.custom_save(name, "Своя команда", text, "all", int(message.from_user.id))
    except ValueError as exc:
        reason = {"RESPONSE_TOO_LONG": "Ответ слишком длинный.", "EMPTY_VALUE": "Ответ не может быть пустым."}.get(str(exc), "Не удалось сохранить.")
        await message.answer(ui.fail(reason))
        return
    await state.clear()
    if CTX.refresh_menu is not None:
        await CTX.refresh_menu()
    view_text, markup = cmd_view("cc_" + name)
    await message.answer(view_text, reply_markup=markup)


def register(dp, *, bot, owner_id: int, refresh_menu: Callable[[], Awaitable[None]], custom_list: Callable,
             custom_save: Callable, custom_delete: Callable, custom_normalize: Callable) -> None:
    CTX.bot, CTX.owner_id, CTX.refresh_menu = bot, int(owner_id), refresh_menu
    CTX.custom_list, CTX.custom_save, CTX.custom_delete, CTX.custom_normalize = custom_list, custom_save, custom_delete, custom_normalize

    private_owner = (F.chat.type == "private") & (F.from_user.id == int(owner_id))
    dp.message.register(cmd_settings, Command("settings"), private_owner)
    dp.message.register(cmd_open_custom, Command("commands", "manage_commands"), private_owner)
    dp.message.register(on_value_text, PanelState.waiting_value, private_owner, F.text)
    dp.message.register(on_cc_name, PanelState.waiting_cc_name, private_owner, F.text)
    dp.message.register(on_cc_response, PanelState.waiting_cc_response, private_owner, F.text)
    dp.callback_query.register(on_callback, F.data.startswith("cs:"))

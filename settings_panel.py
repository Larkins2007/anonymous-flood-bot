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
    waiting_user_access = State()
    waiting_search = State()
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
    disabled = sum(1 for k in REGISTRY if not cmdcfg.is_enabled(k))
    text = ui.card(
        "Настройки команд",
        ui.lines(
            ui.bullet(f"Команд: {total} · функций: {features}"),
            ui.bullet(f"Выключено: {disabled} · настроено вручную: {changed}"),
        ),
        ui.note("У каждой команды отдельная карточка. Изменения применяются сразу, без перезапуска бота."),
    )
    buttons = []
    for cat, (emoji, label) in CATEGORIES.items():
        n = len(cmdcfg.specs_in(cat))
        if cat == "custom":
            n = len(cmdcfg.specs_in("custom"))
        buttons.append(_b(f"{emoji} {label} · {n}", f"cs:cat:{cat}:0"))
    rows = _chunk(buttons, 2)
    rows.append([_b("🔎 Найти команду", "cs:search"), _b("ℹ️ Как устроен доступ", "cs:guide")])
    rows.append([_b("📜 История изменений", "cs:audit:0")])
    rows.append([_b("🔄 Обновить меню команд", "cs:menu")])
    return text, _kb(rows)


def access_guide_view() -> tuple[str, InlineKeyboardMarkup]:
    text = ui.card(
        "ℹ️ Уровни доступа",
        ui.section("Кто может вызывать команду"),
        ui.lines(
            ui.bullet("🌍 Все — любой пользователь. Для команд в группе Telegram всё равно должен дать человеку возможность написать сообщение."),
            ui.bullet("🌸 Участники — только участники того чата, где вызвана команда."),
            ui.bullet("🛡 Администраторы — любой администратор Telegram-чата."),
            ui.bullet("🎖 С 1-го, 2-го, 3-го, 4-го или 5-го ранга — выбранный внутренний ранг и выше."),
            ui.bullet("👑 5-й ранг — создатель чата / высший ранг в системе Justice Faite."),
            ui.bullet("👑 Владелец бота — только Telegram ID владельца из настроек запуска."),
            ui.bullet("🔒 Выключенная команда недоступна всем, включая владельца. Исключение — личная панель /settings, чтобы можно было вернуть доступ."),
        ),
        ui.section("Дополнительные ограничения"),
        ui.lines(
            ui.bullet("⏸ Выключить — команда полностью перестаёт отвечать. В группе не появляется сообщение о том, что она отключена."),
            ui.bullet("📍 Где работает — личка, группа или оба места, если там есть обработчик."),
            ui.bullet("🏠 В каких группах — основной чат или все группы, если выбранный модуль поддерживает мультичаты."),
            ui.bullet("⏱ Пауза — ограничивает частоту вызовов отдельно для каждого пользователя."),
            ui.bullet("🧹 Удаление команды — удаляет исходное сообщение с командой по таймеру, если у бота есть право удаления."),
        ),
        ui.section("Персональные исключения"),
        ui.lines(
            ui.bullet("✅ Разрешить — пропускает выбранного пользователя через ранговую проверку."),
            ui.bullet("🚫 Запретить — блокирует команду даже для модераторов."),
            ui.bullet("🧹 Сбросить — возвращает обычные правила доступа."),
        ),
        ui.note("Выключение и ограничения действуют до запуска обработчика. Защитные проверки владения данными и иерархии модерации сохраняются отдельно."),
    )
    return text, _kb([[_b("‹ К настройкам", "cs:home")]])


def search_results_view(query: str, specs: list[Spec]) -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    for spec in specs[:10]:
        rows.append([_b(f"{spec.emoji} {spec.title}", f"cs:cmd:{spec.key}:0")])
    rows.append([_b("🔎 Искать ещё", "cs:search"), _b("‹ Разделы", "cs:home")])
    body = ui.note(f"Запрос: {query}")
    if len(specs) > 10:
        body += "\n" + ui.note(f"Показаны первые 10 из {len(specs)} совпадений. Уточни запрос.")
    elif not specs:
        body += "\n" + ui.note("Совпадений не найдено. Попробуй часть названия или саму команду.")
    return ui.card("🔎 Результаты поиска", body), _kb(rows)


def search_specs(query: str) -> list[Spec]:
    needle = " ".join((query or "").casefold().split())
    if not needle:
        return []
    matches = []
    for spec in REGISTRY.values():
        haystack = " ".join((
            spec.key, spec.title, spec.desc, spec.category,
            CATEGORIES.get(spec.category, ("", ""))[1],
            " ".join(str(x) for x in spec.triggers),
            " ".join(str(x) for x in spec.aliases),
        )).casefold()
        if needle in haystack:
            matches.append(spec)
    return sorted(matches, key=lambda item: (item.category, item.title.casefold()))


def audit_view(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Последние изменения командных настроек, с ограниченной пагинацией."""
    rows = cmdcfg.STORE.audit_rows(limit=100)
    page_size = 10
    pages = max(1, -(-len(rows) // page_size))
    page = max(0, min(int(page), pages - 1))
    chunk = rows[page * page_size:(page + 1) * page_size]
    content = []
    for audit_id, command, pkey, old, new, actor, stamp in chunk:
        spec = REGISTRY.get(command)
        title = spec.title if spec else command
        old_text = ui.short(str(old if old is not None else "по умолчанию"), 45)
        new_text = ui.short(str(new if new is not None else "по умолчанию"), 45)
        content.append(ui.bullet(
            f"<b>#{audit_id}</b> · {ui.esc(title)}\n"
            f"Параметр: <code>{ui.esc(pkey)}</code>\n"
            f"Было: <code>{ui.esc(old_text)}</code> → стало: <code>{ui.esc(new_text)}</code>\n"
            f"Кто: <code>{ui.esc(actor)}</code> · {ui.esc(stamp or 'время не указано')}"
        ))
    text = ui.card(
        "📜 История изменений",
        ui.lines(*(content or [ui.bullet("Изменений пока нет.")])),
        ui.note("Хранятся последние 100 изменений настроек команд."),
    )
    nav = []
    if page > 0:
        nav.append(_b("‹", f"cs:audit:{page - 1}"))
    nav.append(_b(f"{page + 1}/{pages}", "cs:noop"))
    if page < pages - 1:
        nav.append(_b("›", f"cs:audit:{page + 1}"))
    return text, _kb([nav, [_b("‹ К настройкам", "cs:home")]])

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
        examples = list(spec.triggers[:6])
        blocks.append(ui.lines(ui.section("Как вызвать"), *[ui.bullet(t) for t in examples]))
    if spec.aliases:
        extras = [a for a in spec.aliases if a not in spec.triggers]
        if extras:
            blocks.append(ui.note("Другие варианты: " + ", ".join(extras[:8])))
    changed = cmdcfg.changed_from_default(key)
    state_line = f"{ui.onoff(cmdcfg.is_enabled(key))} {'включена' if cmdcfg.is_enabled(key) else 'выключена'}"
    access_value = cmdcfg.get_str(key, "access")
    where_value = cmdcfg.get_str(key, "where")
    policy_lines = [
        ui.bullet(f"Доступ: {cmdcfg.ACCESS_LABELS.get(access_value, access_value)}"),
        ui.bullet(f"Область: {cmdcfg.WHERE_LABELS.get(where_value, where_value)}"),
        ui.bullet(f"Пауза: {cmdcfg.get_int(key, 'cooldown')} с"),
        ui.bullet(f"Удаление команды: {cmdcfg.get_int(key, 'autodelete')} с"),
    ]
    chat_scope = cmdcfg.get_str(key, "chat_scope")
    if chat_scope:
        policy_lines.insert(2, ui.bullet("Группы: " + ("🏠 основной чат" if chat_scope == "primary" else "🌐 все доступные группы")))
    blocks.append(ui.lines(ui.section("Состояние"), ui.bullet(state_line),
                           *policy_lines,
                           ui.bullet(f"Изменено параметров: {changed}") if changed else None))
    text = ui.card(f"{spec.emoji} {spec.title}", *blocks, footer=ui.note("Нажмите на параметр, чтобы изменить."))

    rows = []
    rows.append([_b(
        "⏸ Выключить команду" if cmdcfg.is_enabled(key) else "▶️ Включить команду",
        f"cs:t:{key}:enabled",
    )])
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
    rows.append([_b("👥 Индивидуальный доступ", f"cs:users:{key}")])
    rows.append([_b("‹ К разделу", f"cs:cat:{spec.category}:0")])
    return text, _kb(rows)


def user_access_view(key: str, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    spec = REGISTRY.get(key)
    if spec is None:
        return cmd_view(key)
    entries = cmdcfg.STORE.list_user_access(key)
    page_size = 20
    pages = max(1, -(-len(entries) // page_size))
    page = max(0, min(int(page), pages - 1))
    chunk = entries[page * page_size:(page + 1) * page_size]
    lines = []
    for user_id, chat_id, mode, updated_by, updated_at in chunk:
        label = "✅ разрешено лично" if mode == "allow" else "🚫 запрещено лично"
        scope = "везде" if chat_id == 0 else f"чат {chat_id}"
        lines.append(ui.bullet(f"<code>{user_id}</code> · {label} · {scope}"))
    body = [
        ui.note("Персональное исключение действует только для этой команды. Оно не отменяет выключение команды и ограничение по месту использования."),
        ui.section(f"Сейчас настроено · страница {page + 1}/{pages}"),
        ui.lines(*(lines or [ui.bullet("Исключений пока нет.")]))
    ]
    text = ui.card(f"👥 Индивидуальный доступ · {spec.title}", *body,
                   footer=ui.note("Формат: USER_ID или USER_ID@CHAT_ID. Без CHAT_ID правило действует во всех чатах; с CHAT_ID — только в указанной группе."))
    rows = [
        [_b("✅ Разрешить пользователю", f"cs:ua:{key}:allow")],
        [_b("🚫 Запретить пользователю", f"cs:ua:{key}:deny")],
        [_b("🧹 Сбросить исключение", f"cs:ua:{key}:clear")],
    ]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(_b("‹", f"cs:users:{key}:{page - 1}"))
        nav.append(_b(f"{page + 1}/{pages}", "cs:noop"))
        if page < pages - 1:
            nav.append(_b("›", f"cs:users:{key}:{page + 1}"))
        rows.append(nav)
    rows.append([_b("‹ Назад к команде", f"cs:cmd:{key}:0")])
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
    # cmdcfg.set_value/reset is the single place that refreshes Telegram menus.
    # Keeping this hook as a no-op avoids duplicate get/setMyCommands requests.
    return


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
    if action == "search":
        await state.set_state(PanelState.waiting_search)
        await callback.message.answer(ui.card("🔎 Найти команду", ui.bullet("Отправь название команды, часть названия или саму команду, например: «бан», «+ссылки», «/spy»."), ui.note("Отмена: /cancel")))
        await callback.answer()
        return
    if action == "guide":
        await state.clear()
        await _show(callback, access_guide_view())
        await callback.answer()
        return
    if action == "audit":
        page = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else 0
        await state.clear()
        await _show(callback, audit_view(page))
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
    if action == "users" and len(parts) >= 3:
        page = int(parts[3]) if len(parts) > 3 and parts[3].lstrip("-").isdigit() else 0
        await state.clear()
        await _show(callback, user_access_view(parts[2], page))
        await callback.answer()
        return

    key = parts[2] if len(parts) > 2 else ""
    pkey = parts[3] if len(parts) > 3 else ""
    extra = parts[4] if len(parts) > 4 else ""

    if action == "ua":
        mode = pkey
        if key not in REGISTRY or mode not in ("allow", "deny", "clear"):
            await callback.answer("Настройка не найдена.", show_alert=True)
            return
        await state.set_state(PanelState.waiting_user_access)
        await state.update_data(command_key=key, override_mode=mode)
        prompt = {
            "allow": "Отправь USER_ID или USER_ID@CHAT_ID, чтобы разрешить команду.",
            "deny": "Отправь USER_ID или USER_ID@CHAT_ID, чтобы запретить команду.",
            "clear": "Отправь USER_ID или USER_ID@CHAT_ID, чтобы убрать исключение. USER_ID без CHAT_ID удалит все исключения этого человека для команды.",
        }[mode]
        await callback.message.answer(ui.card("👥 Индивидуальный доступ", ui.bullet(prompt), ui.note("Отмена: /cancel")))
        await callback.answer()
        return
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
        cmdcfg.set_value(key, pkey, not cmdcfg.get_bool(key, pkey), actor_id=callback.from_user.id)
        _after_change(key, pkey)
        await _show(callback, cmd_view(key, _page_of(spec, pkey)))
        await callback.answer("Сохранено.")
        return
    if action == "v":                                   # готовое значение
        ok, result = cmdcfg.set_value(key, pkey, extra if extra != "" else "", actor_id=callback.from_user.id)
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
        ok, result = cmdcfg.set_value(key, pkey, current + delta, actor_id=callback.from_user.id)
        if not ok:
            await callback.answer(str(result), show_alert=True)
            return
        await _show(callback, param_view(key, pkey))
        await callback.answer(f"{result}")
        return
    if action == "d":
        cmdcfg.reset(key, pkey, actor_id=callback.from_user.id)
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
        cmdcfg.reset(key, actor_id=callback.from_user.id)
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



async def on_search_text(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    if query.casefold() in {"/cancel", "-", "отмена"}:
        await state.clear()
        await message.answer(ui.note("Поиск отменён."))
        return
    if len(query) > 100:
        await message.answer(ui.fail("Запрос слишком длинный. Максимум 100 символов."))
        return
    results = search_specs(query)
    await state.clear()
    text, markup = search_results_view(query, results)
    await message.answer(text, reply_markup=markup)


async def on_user_access_text(message: Message, state: FSMContext):
    data = await state.get_data()
    raw = (message.text or "").strip()
    if raw.casefold() in {"/cancel", "-", "отмена"}:
        await state.clear()
        await message.answer(ui.note("Отменено."))
        return
    user_part, separator, chat_part = raw.partition("@")
    if not user_part.isdigit() or not (1 <= int(user_part) <= 2**63 - 1):
        await message.answer(ui.fail("Нужен Telegram ID пользователя: 123456789 или 123456789@-1001234567890."))
        return
    chat_id = 0
    if separator:
        if not chat_part.lstrip("-").isdigit() or int(chat_part) >= 0 or len(chat_part.lstrip("-")) < 5:
            await message.answer(ui.fail("CHAT_ID должен быть отрицательным ID группы. Пример: 123456789@-1001234567890."))
            return
        chat_id = int(chat_part)
    command_key = str(data.get("command_key") or "")
    mode = str(data.get("override_mode") or "")
    user_id = int(user_part)
    if command_key not in REGISTRY:
        await state.clear()
        await message.answer(ui.fail("Команда не найдена. Открой настройки заново."))
        return
    try:
        if mode == "clear":
            removed = cmdcfg.STORE.delete_user_access(
                command_key, user_id, chat_id=(chat_id if separator else None), actor_id=int(message.from_user.id)
            )
            result = "Исключение удалено." if removed else "Для этого пользователя исключений не было."
        else:
            cmdcfg.STORE.set_user_access(command_key, user_id, mode, chat_id=chat_id, actor_id=int(message.from_user.id))
            result = "Доступ разрешён." if mode == "allow" else "Доступ запрещён."
            if chat_id:
                result += f" Только в чате {chat_id}."
    except Exception:
        logger.exception("Could not update per-user command access")
        await message.answer(ui.fail("Не удалось сохранить исключение. Попробуй ещё раз."))
        return
    await state.clear()
    await message.answer(ui.ok(f"{result}\nПользователь: <code>{user_id}</code>"))
    text, markup = user_access_view(command_key)
    await message.answer(text, reply_markup=markup)


async def on_value_text(message: Message, state: FSMContext):
    data = await state.get_data()
    key, pkey = data.get("key"), data.get("pkey")
    text = (message.text or "").strip()
    if text in ("-", "—", "/cancel"):
        await state.clear()
        await message.answer(ui.note("Отменено."))
        return
    ok, result = cmdcfg.set_value(key, pkey, text, actor_id=message.from_user.id)
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
    cmdcfg.set_menu_refresher(refresh_menu)
    CTX.custom_list, CTX.custom_save, CTX.custom_delete, CTX.custom_normalize = custom_list, custom_save, custom_delete, custom_normalize

    private_owner = (F.chat.type == "private") & (F.from_user.id == int(owner_id))
    dp.message.register(cmd_settings, Command("settings"), private_owner)
    dp.message.register(cmd_open_custom, Command("commands", "manage_commands"), private_owner)
    dp.message.register(on_value_text, PanelState.waiting_value, private_owner, F.text)
    dp.message.register(on_user_access_text, PanelState.waiting_user_access, private_owner, F.text)
    dp.message.register(on_search_text, PanelState.waiting_search, private_owner, F.text)
    dp.message.register(on_cc_name, PanelState.waiting_cc_name, private_owner, F.text)
    dp.message.register(on_cc_response, PanelState.waiting_cc_response, private_owner, F.text)
    dp.callback_query.register(on_callback, F.data.startswith("cs:"))

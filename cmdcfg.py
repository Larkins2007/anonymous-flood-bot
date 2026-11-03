"""Ядро персональных настроек команд.

У каждой команды есть собственная карточка настроек (в ЛС владельца, /settings):
включена ли команда, кто может её вызывать, где она работает, пауза между
вызовами, автоудаление сообщений и набор параметров, специфичных именно для неё.

Модуль разделён на чистую логику (реестр, хранилище, проверка доступа —
тестируется без Telegram) и тонкие async-обёртки (`gate`, `respond`).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("cmdcfg")

ACCESS_ORDER = {"all": 0, "members": 1, "admins": 2, "owner": 3}
ACCESS_LABELS = {
    "owner": "👑 только владелец",
    "admins": "🛡 администраторы флуда",
    "members": "🌸 участники флуда",
    "all": "🌍 все",
}
WHERE_LABELS = {"private": "💬 только ЛС", "group": "👥 только флуд", "both": "↔️ ЛС и флуд"}


# --------------------------------------------------------------------------
# Описание команд
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Param:
    key: str
    title: str
    kind: str                       # bool | int | choice | text | hour
    default: Any
    help: str = ""
    min: int = 0
    max: int = 0
    step: int = 1
    unit: str = ""
    choices: tuple = ()             # ((value, label), ...)
    max_len: int = 400
    allow_empty: bool = False
    presets: tuple = ()             # быстрые значения для int


@dataclass(frozen=True)
class Spec:
    key: str
    title: str
    emoji: str
    category: str
    desc: str
    triggers: tuple = ()            # что вводить: ("/mafia",) или ("выдать рест @user 15 09",)
    access: str = "owner"
    access_options: tuple = ("owner", "admins", "members", "all")
    where: str = "both"
    where_options: tuple = ("private", "group", "both")
    params: tuple = ()
    cooldown: bool = True
    cooldown_default: int = 0
    autodelete: bool = True
    menu: tuple = ()                # ((slash_command, описание), ...) для меню Telegram
    custom: bool = False
    feature: bool = False           # не команда, а «функция» (приветствие, поздравления…)


CATEGORIES = {
    "community": ("🌸", "Сообщество"),
    "roles": ("🎭", "Роли и теги"),
    "moderation": ("🛡", "Модерация"),
    "awards": ("🏆", "Награды"),
    "games": ("🎮", "Игры"),
    "anon": ("🤫", "Связь с админами"),
    "custom": ("✨", "Свои команды"),
    "system": ("⚙️", "Система"),
}

REGISTRY: dict[str, Spec] = {}


def register(spec: Spec) -> Spec:
    REGISTRY[spec.key] = spec
    return spec


def unregister(key: str) -> None:
    REGISTRY.pop(key, None)


def common_params(spec: Spec) -> tuple:
    """Общие настройки, которые есть у каждой команды."""
    items = [
        Param("enabled", "Команда включена", "bool", True, "Выключенная команда молчит и пропадает из /help и меню."),
        Param(
            "access", "Кто может вызывать", "choice", spec.access,
            "Владелец может всё всегда.",
            choices=tuple((a, ACCESS_LABELS[a]) for a in spec.access_options),
        ),
    ]
    if len(spec.where_options) > 1:
        items.append(Param(
            "where", "Где работает", "choice", spec.where, "ЛС бота или основной флуд.",
            choices=tuple((w, WHERE_LABELS[w]) for w in spec.where_options),
        ))
    if spec.cooldown:
        items.append(Param(
            "cooldown", "Пауза между вызовами", "int", spec.cooldown_default, "Для каждого пользователя отдельно. Владельца не касается. 0 — без паузы.",
            min=0, max=3600, step=5, unit="с", presets=(0, 10, 30, 60, 300),
        ))
    if spec.autodelete:
        items.append(Param(
            "autodelete", "Автоудаление сообщений", "int", 0,
            "Через сколько секунд удалять команду и ответ бота во флуде (нужно право удаления). 0 — не удалять.",
            min=0, max=600, step=5, unit="с", presets=(0, 10, 30, 60),
        ))
    return tuple(items)


def all_params(spec: Spec) -> tuple:
    return common_params(spec) + tuple(spec.params)


def find_param(spec: Spec, key: str) -> Optional[Param]:
    for p in all_params(spec):
        if p.key == key:
            return p
    return None


# --------------------------------------------------------------------------
# Валидация значений
# --------------------------------------------------------------------------

def _to_str(value, kind: str) -> str:
    if kind == "bool":
        return "1" if value else "0"
    return str(value)


def _from_str(raw: str, param: Param):
    try:
        if param.kind == "bool":
            return raw in ("1", "true", "True", "yes")
        if param.kind in ("int", "hour"):
            return int(raw)
        if param.kind == "choice":
            allowed = [c[0] for c in param.choices]
            return raw if raw in allowed else param.default
        return raw
    except (TypeError, ValueError):
        return param.default


def coerce(param: Param, raw) -> tuple[bool, Any]:
    """Проверяет введённое значение. Возвращает (ok, значение | текст ошибки)."""
    kind = param.kind
    if kind == "bool":
        if isinstance(raw, bool):
            return True, raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "да", "вкл", "on"):
            return True, True
        if text in ("0", "false", "no", "нет", "выкл", "off"):
            return True, False
        return False, "Ожидаю «да» или «нет»."
    if kind in ("int", "hour"):
        try:
            number = int(str(raw).strip().replace(" ", ""))
        except ValueError:
            return False, "Нужно целое число."
        lo, hi = (0, 23) if kind == "hour" else (param.min, param.max)
        if number < lo or number > hi:
            return False, f"Допустимо от {lo} до {hi}."
        return True, number
    if kind == "choice":
        allowed = [c[0] for c in param.choices]
        if raw in allowed:
            return True, raw
        return False, "Такого варианта нет."
    text = str(raw or "").strip()
    if not text and not param.allow_empty:
        return False, "Значение не может быть пустым."
    if len(text) > param.max_len:
        return False, f"Слишком длинно: максимум {param.max_len} символов."
    return True, text


def format_value(param: Param, value) -> str:
    if param.kind == "bool":
        return "включено" if value else "выключено"
    if param.kind == "choice":
        for v, label in param.choices:
            if v == value:
                return label
        return str(value)
    if param.kind == "hour":
        return f"{int(value):02d}:00"
    if param.kind == "int":
        return f"{value} {param.unit}".strip()
    text = str(value or "")
    if not text:
        return "— пусто —"
    return text if len(text) <= 80 else text[:79] + "…"


# --------------------------------------------------------------------------
# Хранилище
# --------------------------------------------------------------------------

class Store:
    """Значения настроек в SQLite с кэшем в памяти."""

    def __init__(self):
        self._db_op: Optional[Callable] = None
        self._cache: dict[tuple[str, str], str] = {}
        self._loaded = False
        self._hooks: list[Callable[[str, str, Any], None]] = []

    def bind(self, db_op: Callable) -> None:
        self._db_op = db_op
        self._loaded = False
        self.ensure_schema()

    def ensure_schema(self) -> None:
        if not self._db_op:
            return

        def op(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cmd_settings ("
                "command TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
                "updated_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(command,key))"
            )
            conn.commit()

        self._db_op(op)

    def _load(self) -> None:
        if self._loaded or not self._db_op:
            return
        rows = self._db_op(lambda conn: conn.execute("SELECT command,key,value FROM cmd_settings").fetchall())
        self._cache = {(r[0], r[1]): r[2] for r in rows}
        self._loaded = True

    def raw(self, command: str, key: str) -> Optional[str]:
        self._load()
        return self._cache.get((command, key))

    def put(self, command: str, key: str, raw: str) -> None:
        self._load()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        if self._db_op:
            self._db_op(lambda conn: (conn.execute(
                "INSERT INTO cmd_settings(command,key,value,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(command,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (command, key, raw, stamp)), conn.commit()))
        self._cache[(command, key)] = raw

    def drop(self, command: str, key: Optional[str] = None) -> None:
        self._load()
        if self._db_op:
            if key is None:
                self._db_op(lambda conn: (conn.execute("DELETE FROM cmd_settings WHERE command=?", (command,)), conn.commit()))
            else:
                self._db_op(lambda conn: (conn.execute("DELETE FROM cmd_settings WHERE command=? AND key=?", (command, key)), conn.commit()))
        for k in [k for k in self._cache if k[0] == command and (key is None or k[1] == key)]:
            del self._cache[k]

    def on_change(self, hook: Callable[[str, str, Any], None]) -> None:
        self._hooks.append(hook)

    def fire(self, command: str, key: str, value) -> None:
        for hook in list(self._hooks):
            try:
                hook(command, key, value)
            except Exception:
                logger.exception("settings hook failed | %s.%s", command, key)


STORE = Store()


def bind_db(db_op: Callable) -> None:
    STORE.bind(db_op)


def get(command: str, key: str):
    """Типизированное значение настройки (с умолчанием из реестра)."""
    spec = REGISTRY.get(command)
    param = find_param(spec, key) if spec else None
    if param is None:
        return None
    raw = STORE.raw(command, key)
    if raw is None:
        return param.default
    value = _from_str(raw, param)
    if param.kind == "int":
        value = max(param.min, min(param.max, value))
    return value


def get_bool(command: str, key: str) -> bool:
    return bool(get(command, key))


def get_int(command: str, key: str) -> int:
    value = get(command, key)
    try:
        return int(value)
    except (TypeError, ValueError):
        spec = REGISTRY.get(command)
        param = find_param(spec, key) if spec else None
        return int(param.default) if param else 0


def get_str(command: str, key: str) -> str:
    value = get(command, key)
    return "" if value is None else str(value)


def settings_for(command: str) -> dict:
    spec = REGISTRY.get(command)
    if not spec:
        return {}
    return {p.key: get(command, p.key) for p in all_params(spec)}


def set_value(command: str, key: str, raw) -> tuple[bool, Any]:
    spec = REGISTRY.get(command)
    param = find_param(spec, key) if spec else None
    if param is None:
        return False, "Неизвестная настройка."
    ok, value = coerce(param, raw)
    if not ok:
        return False, value
    STORE.put(command, key, _to_str(value, param.kind))
    STORE.fire(command, key, value)
    return True, value


def reset(command: str, key: Optional[str] = None) -> None:
    STORE.drop(command, key)
    spec = REGISTRY.get(command)
    if spec:
        for p in all_params(spec):
            if key is None or p.key == key:
                STORE.fire(command, p.key, get(command, p.key))


def is_enabled(command: str) -> bool:
    return get_bool(command, "enabled")


def changed_from_default(command: str) -> int:
    spec = REGISTRY.get(command)
    if not spec:
        return 0
    return sum(1 for p in all_params(spec) if STORE.raw(command, p.key) is not None and get(command, p.key) != p.default)


# --------------------------------------------------------------------------
# Права, пауза, решение «можно / нельзя»
# --------------------------------------------------------------------------

class Runtime:
    owner_id: int = 0
    primary_chat_id: int = 0
    bot: Any = None


RT = Runtime()
_COOLDOWNS: dict[tuple[str, int], float] = {}
_MEMBER_CACHE: dict[int, tuple[float, str, bool]] = {}
MEMBER_TTL = 60.0


def configure(*, bot, owner_id: int, primary_chat_id: int) -> None:
    RT.bot, RT.owner_id, RT.primary_chat_id = bot, int(owner_id), int(primary_chat_id or 0)


def user_level(*, is_owner: bool, is_admin: bool, is_member: bool) -> str:
    if is_owner:
        return "owner"
    if is_admin:
        return "admins"
    if is_member:
        return "members"
    return "all"


def decide(command: str, *, chat_type: str, chat_id: int = 0, user_id: int, level: str,
           now: Optional[float] = None, commit: bool = True) -> tuple[bool, str, int]:
    """Чистое решение: (разрешено, причина, секунд до конца паузы).

    Причины: ok, disabled, where, access, cooldown.
    """
    spec = REGISTRY.get(command)
    if spec is None:
        return True, "ok", 0
    cfg = settings_for(command)
    is_owner = level == "owner"
    if not cfg.get("enabled", True) and command != "settings":
        return False, "disabled", 0
    where = cfg.get("where", spec.where)
    in_group = chat_type in ("group", "supergroup")
    if where == "private" and chat_type != "private":
        return False, "where", 0
    if where == "group" and not in_group:
        return False, "where", 0
    if in_group and RT.primary_chat_id and chat_id and int(chat_id) != RT.primary_chat_id:
        return False, "where", 0
    need = cfg.get("access", spec.access)
    if ACCESS_ORDER.get(level, 0) < ACCESS_ORDER.get(need, 3):
        return False, "access", 0
    wait = int(cfg.get("cooldown", 0) or 0)
    if wait and not is_owner:
        now = time.monotonic() if now is None else now
        last = _COOLDOWNS.get((command, user_id))
        if last is not None and now - last < wait:
            return False, "cooldown", int(wait - (now - last)) + 1
        if commit:
            _COOLDOWNS[(command, user_id)] = now
    return True, "ok", 0


def reset_runtime_state() -> None:
    _COOLDOWNS.clear()
    _MEMBER_CACHE.clear()


async def _member_state(user_id: int) -> tuple[str, bool]:
    """(status, is_member) участника основного чата, с кэшем."""
    if not RT.bot or not RT.primary_chat_id:
        return "left", False
    cached = _MEMBER_CACHE.get(user_id)
    now = time.monotonic()
    if cached and now - cached[0] < MEMBER_TTL:
        return cached[1], cached[2]
    status, is_member = "left", False
    try:
        member = await RT.bot.get_chat_member(RT.primary_chat_id, user_id)
        status = str(getattr(member, "status", "left"))
        is_member = status in ("member", "administrator", "creator") or (
            status == "restricted" and bool(getattr(member, "is_member", False)))
    except Exception as exc:  # сеть/права: считаем «не участник», но не падаем
        logger.debug("member lookup failed | user=%s | %s", user_id, exc)
    _MEMBER_CACHE[user_id] = (now, status, is_member)
    return status, is_member


async def level_of(user_id: int) -> str:
    if user_id == RT.owner_id:
        return "owner"
    status, is_member = await _member_state(user_id)
    return user_level(is_owner=False, is_admin=status in ("administrator", "creator"), is_member=is_member)


DENY_TEXT = {
    "disabled": "🔒 Эта команда сейчас выключена.",
    "where": "🔒 Эта команда здесь недоступна.",
    "access": "🔒 У вас нет доступа к этой команде.",
}


def schedule_delete(chat_id: int, message_id: int, delay: int) -> None:
    if not RT.bot or delay <= 0 or not message_id:
        return

    async def runner():
        await asyncio.sleep(delay)
        try:
            await RT.bot.delete_message(chat_id, message_id)
        except Exception:
            pass

    try:
        asyncio.get_running_loop().create_task(runner())
    except RuntimeError:
        pass


async def gate(message, command: str, *, notify: bool = True, commit: bool = True) -> bool:
    """Проверка перед выполнением команды. Возвращает True, если можно продолжать."""
    user = getattr(message, "from_user", None)
    if user is None:
        return False
    uid = int(user.id)
    chat = message.chat
    level = await level_of(uid) if command in REGISTRY else "owner"
    ok, reason, wait = decide(command, chat_type=chat.type, chat_id=int(chat.id), user_id=uid, level=level, commit=commit)
    if ok:
        delay = get_int(command, "autodelete") if command in REGISTRY and REGISTRY[command].autodelete else 0
        if commit and delay and chat.type in ("group", "supergroup"):
            schedule_delete(int(chat.id), int(message.message_id), delay)
        return True
    if notify and chat.type == "private":
        text = DENY_TEXT.get(reason) or f"⏳ Подождите {wait} с перед повтором."
        if reason == "cooldown":
            text = f"⏳ Подождите {wait} с перед повтором."
        try:
            await message.answer(text)
        except Exception:
            pass
    return False


async def gate_callback(callback, command: str, *, commit: bool = True) -> bool:
    user = callback.from_user
    chat = callback.message.chat if getattr(callback, "message", None) else None
    chat_type = chat.type if chat else "private"
    chat_id = int(chat.id) if chat else 0
    level = await level_of(int(user.id))
    ok, reason, wait = decide(command, chat_type=chat_type, chat_id=chat_id, user_id=int(user.id), level=level, commit=commit)
    if ok:
        return True
    text = f"⏳ Подождите {wait} с." if reason == "cooldown" else DENY_TEXT.get(reason, "🔒 Недоступно.")
    try:
        await callback.answer(text, show_alert=True)
    except Exception:
        pass
    return False


async def respond(message, command: str, text: str, **kwargs):
    """reply + автоудаление ответа по настройке команды."""
    sent = await message.reply(text, **kwargs)
    delay = get_int(command, "autodelete") if command in REGISTRY and REGISTRY[command].autodelete else 0
    if delay and message.chat.type in ("group", "supergroup"):
        schedule_delete(int(message.chat.id), int(getattr(sent, "message_id", 0) or 0), delay)
    return sent


# --------------------------------------------------------------------------
# Видимость для /help и меню Telegram
# --------------------------------------------------------------------------

def visible(command: str, *, chat_type: str, level: str) -> bool:
    """Показывать ли команду человеку с данным уровнем в данном типе чата."""
    spec = REGISTRY.get(command)
    if spec is None:
        return False
    cfg = settings_for(command)
    if not cfg.get("enabled", True):
        return False
    where = cfg.get("where", spec.where)
    in_group = chat_type in ("group", "supergroup")
    if where == "private" and in_group:
        return False
    if where == "group" and not in_group:
        return False
    return ACCESS_ORDER.get(level, 0) >= ACCESS_ORDER.get(cfg.get("access", spec.access), 3)


def specs_in(category: str) -> list[Spec]:
    return [s for s in REGISTRY.values() if s.category == category]

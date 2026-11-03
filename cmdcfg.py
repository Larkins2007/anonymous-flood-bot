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
    "owner": "👑 владелец бота",
    "admins": "🛡 администраторы Telegram-чата",
    "members": "🌸 участники этого чата",
    "all": "🌍 все пользователи",
    "rank_1": "🎖 с 1-го ранга",
    "rank_2": "🎖 со 2-го ранга",
    "rank_3": "🎖 с 3-го ранга",
    "rank_4": "🎖 с 4-го ранга",
    "rank_5": "👑 только 5-й ранг",
}
ALL_ACCESS_OPTIONS = ("all", "members", "admins", "rank_1", "rank_2", "rank_3", "rank_4", "rank_5", "owner")
ACCESS_MIN_RANK = {"rank_1": 1, "rank_2": 2, "rank_3": 3, "rank_4": 4, "rank_5": 5}

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
    aliases: tuple = ()             # нормализуемые текстовые алиасы для центральной проверки доступа


CATEGORIES = {
    "community": ("🌸", "Сообщество"),
    "chat": ("⚙️", "Настройки чата"),
    "roles": ("🎭", "Роли и теги"),
    "moderation": ("🛡️", "Модерация"),
    "profiles": ("👤", "Профили участников"),
    "statistics": ("📊", "Статистика"),
    "reputation": ("✨", "Репутация"),
    "automation": ("🧩", "Автоматизация и жалобы"),
    "fun": ("🎲", "Развлечения"),
    "network": ("🌐", "Сетка чатов"),
    "events": ("🎉", "События"),
    "cleanup": ("🧹", "Чистка чата"),
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
            "Уровень доступа для включённой команды. Выключение действует на всех; личная панель /settings владельца остаётся аварийным исключением.",
            choices=tuple((a, ACCESS_LABELS[a]) for a in ALL_ACCESS_OPTIONS),
        ),
    ]
    items.append(Param(
        "where", "Где работает", "choice", spec.where,
        "Выбираются только места, в которых зарегистрирован обработчик команды.",
        choices=tuple((w, WHERE_LABELS[w]) for w in (spec.where_options or (spec.where,))),
    ))
    if "group" in (spec.where_options or ()) or "both" in (spec.where_options or ()):
        items.append(Param(
            "chat_scope", "В каких группах", "choice", "primary",
            "По умолчанию — основной флуд. Все группы доступны только для модулей, которые поддерживают работу с несколькими чатами.",
            choices=(("primary", "🏠 только основной чат"), ("all_groups", "🌐 все доступные группы")),
        ))
    items.append(Param(
        "cooldown", "Пауза между вызовами", "int", spec.cooldown_default,
        "Для каждого пользователя отдельно. Владельца не касается. 0 — без паузы.",
        min=0, max=3600, step=5, unit="с", presets=(0, 10, 30, 60, 300),
    ))
    items.append(Param(
        "autodelete", "Удалять сообщение команды", "int", 0,
        "Через сколько секунд удалить сообщение, в котором вызвали команду (нужно право удаления). 0 — не удалять.",
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
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cmd_user_access ("
                "command TEXT NOT NULL, chat_id INTEGER NOT NULL DEFAULT 0, "
                "user_id INTEGER NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('allow','deny')), "
                "updated_by INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT '', "
                "PRIMARY KEY(command,chat_id,user_id))"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cmd_user_access_user ON cmd_user_access(user_id,chat_id)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cmd_settings_audit ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, command TEXT NOT NULL, key TEXT NOT NULL, "
                "old_value TEXT, new_value TEXT, actor_id INTEGER NOT NULL DEFAULT 0, "
                "updated_at TEXT NOT NULL DEFAULT '')"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cmd_settings_audit_time ON cmd_settings_audit(updated_at DESC,id DESC)")
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

    def put(self, command: str, key: str, raw: str, *, actor_id: int = 0) -> None:
        self._load()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        old_raw = self._cache.get((command, key))
        if self._db_op:
            def op(conn):
                conn.execute(
                    "INSERT INTO cmd_settings(command,key,value,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(command,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (command, key, raw, stamp),
                )
                if old_raw != raw:
                    conn.execute(
                        "INSERT INTO cmd_settings_audit(command,key,old_value,new_value,actor_id,updated_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (command, key, old_raw, raw, int(actor_id or RT.owner_id or 0), stamp),
                    )
                conn.commit()
            self._db_op(op)
        self._cache[(command, key)] = raw

    def drop(self, command: str, key: Optional[str] = None, *, actor_id: int = 0) -> None:
        self._load()
        if self._db_op:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            def op(conn):
                if key is None:
                    rows = conn.execute("SELECT key,value FROM cmd_settings WHERE command=?", (command,)).fetchall()
                    conn.execute("DELETE FROM cmd_settings WHERE command=?", (command,))
                else:
                    row = conn.execute("SELECT key,value FROM cmd_settings WHERE command=? AND key=?", (command, key)).fetchone()
                    rows = [row] if row else []
                    conn.execute("DELETE FROM cmd_settings WHERE command=? AND key=?", (command, key))
                for row in rows:
                    conn.execute(
                        "INSERT INTO cmd_settings_audit(command,key,old_value,new_value,actor_id,updated_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (command, str(row[0]), str(row[1]), "<default>", int(actor_id or RT.owner_id or 0), stamp),
                    )
                conn.commit()
            self._db_op(op)
        for k in [k for k in self._cache if k[0] == command and (key is None or k[1] == key)]:
            del self._cache[k]

    def audit_rows(self, limit: int = 25) -> list[tuple[int, str, str, Optional[str], Optional[str], int, str]]:
        if not self._db_op:
            return []
        limit = max(1, min(int(limit), 100))
        rows = self._db_op(lambda conn: conn.execute(
            "SELECT id,command,key,old_value,new_value,actor_id,updated_at "
            "FROM cmd_settings_audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall())
        return [(int(r[0]), str(r[1]), str(r[2]), r[3], r[4], int(r[5]), str(r[6])) for r in rows]

    def set_user_access(self, command: str, user_id: int, mode: str, *, chat_id: int = 0, actor_id: int = 0) -> None:
        if mode not in ("allow", "deny"):
            raise ValueError("mode must be allow or deny")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        if self._db_op:
            def op(conn):
                row = conn.execute(
                    "SELECT mode FROM cmd_user_access WHERE command=? AND chat_id=? AND user_id=?",
                    (command, int(chat_id), int(user_id)),
                ).fetchone()
                old_mode = str(row[0]) if row else None
                conn.execute(
                    "INSERT INTO cmd_user_access(command,chat_id,user_id,mode,updated_by,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(command,chat_id,user_id) DO UPDATE SET mode=excluded.mode,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                    (command, int(chat_id), int(user_id), mode, int(actor_id), stamp),
                )
                if old_mode != mode:
                    conn.execute(
                        "INSERT INTO cmd_settings_audit(command,key,old_value,new_value,actor_id,updated_at) VALUES(?,?,?,?,?,?)",
                        (command, f"user:{int(user_id)}@{int(chat_id)}", old_mode, mode, int(actor_id), stamp),
                    )
                conn.commit()
            self._db_op(op)

    def delete_user_access(self, command: str, user_id: int, *, chat_id: Optional[int] = None, actor_id: int = 0) -> int:
        if not self._db_op:
            return 0
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        def op(conn):
            if chat_id is None:
                rows = conn.execute("SELECT chat_id,mode FROM cmd_user_access WHERE command=? AND user_id=?", (command, int(user_id))).fetchall()
                cur = conn.execute("DELETE FROM cmd_user_access WHERE command=? AND user_id=?", (command, int(user_id)))
            else:
                rows = conn.execute("SELECT chat_id,mode FROM cmd_user_access WHERE command=? AND user_id=? AND chat_id=?", (command, int(user_id), int(chat_id))).fetchall()
                cur = conn.execute("DELETE FROM cmd_user_access WHERE command=? AND user_id=? AND chat_id=?", (command, int(user_id), int(chat_id)))
            for row in rows:
                conn.execute(
                    "INSERT INTO cmd_settings_audit(command,key,old_value,new_value,actor_id,updated_at) VALUES(?,?,?,?,?,?)",
                    (command, f"user:{int(user_id)}@{int(row[0])}", str(row[1]), "<default>", int(actor_id), stamp),
                )
            conn.commit()
            return int(cur.rowcount)
        return self._db_op(op)

    def user_access(self, command: str, user_id: int, *, chat_id: int = 0) -> Optional[str]:
        if not self._db_op:
            return None
        row = self._db_op(lambda conn: conn.execute(
            "SELECT mode FROM cmd_user_access WHERE command=? AND user_id=? AND chat_id IN (?,0) "
            "ORDER BY CASE WHEN chat_id=? THEN 0 ELSE 1 END LIMIT 1",
            (command, int(user_id), int(chat_id), int(chat_id)),
        ).fetchone())
        return str(row[0]) if row else None

    def list_user_access(self, command: str, *, chat_id: Optional[int] = None) -> list[tuple[int, int, str, int, str]]:
        if not self._db_op:
            return []
        def op(conn):
            if chat_id is None:
                rows = conn.execute("SELECT user_id,chat_id,mode,updated_by,updated_at FROM cmd_user_access WHERE command=? ORDER BY updated_at DESC,user_id", (command,)).fetchall()
            else:
                rows = conn.execute("SELECT user_id,chat_id,mode,updated_by,updated_at FROM cmd_user_access WHERE command=? AND chat_id IN (?,0) ORDER BY updated_at DESC,user_id", (command, int(chat_id))).fetchall()
            return [(int(r[0]), int(r[1]), str(r[2]), int(r[3]), str(r[4])) for r in rows]
        return self._db_op(op)

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


def set_value(command: str, key: str, raw, *, actor_id: Optional[int] = None) -> tuple[bool, Any]:
    spec = REGISTRY.get(command)
    param = find_param(spec, key) if spec else None
    if param is None:
        return False, "Неизвестная настройка."
    ok, value = coerce(param, raw)
    if not ok:
        return False, value
    STORE.put(command, key, _to_str(value, param.kind), actor_id=int(actor_id or RT.owner_id or 0))
    STORE.fire(command, key, value)
    if key in ("enabled", "access", "where", "chat_scope"):
        _schedule_menu_refresh()
    return True, value


def reset(command: str, key: Optional[str] = None, *, actor_id: Optional[int] = None) -> None:
    STORE.drop(command, key, actor_id=int(actor_id or RT.owner_id or 0))
    spec = REGISTRY.get(command)
    if spec:
        reset_policy = False
        for p in all_params(spec):
            if key is None or p.key == key:
                STORE.fire(command, p.key, get(command, p.key))
                if p.key in ("enabled", "access", "where", "chat_scope"):
                    reset_policy = True
        if reset_policy:
            _schedule_menu_refresh()


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
    manager: Any = None


RT = Runtime()
_MENU_REFRESHER: Optional[Callable[[], Any]] = None
_COOLDOWNS: dict[tuple[str, int, int], float] = {}
_MEMBER_CACHE: dict[tuple[int, int], tuple[float, str, bool]] = {}
_ACTIVE_ACCESS: dict[tuple[int, int, int], str] = {}
MEMBER_TTL = 60.0


def configure(*, bot, owner_id: int, primary_chat_id: int, manager=None) -> None:
    RT.bot, RT.owner_id, RT.primary_chat_id, RT.manager = bot, int(owner_id), int(primary_chat_id or 0), manager


def set_menu_refresher(callback: Optional[Callable[[], Any]]) -> None:
    global _MENU_REFRESHER
    _MENU_REFRESHER = callback


def _schedule_menu_refresh() -> None:
    if _MENU_REFRESHER is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Settings may also change during an offline migration or synchronous
        # test. A coroutine cannot run there; close it explicitly so Python
        # does not report an un-awaited coroutine during garbage collection.
        loop = None
    try:
        result = _MENU_REFRESHER()
        if asyncio.iscoroutine(result):
            if loop is None:
                result.close()
            else:
                loop.create_task(result)
    except Exception:
        logger.exception("could not refresh Telegram command menu")


def user_level(*, is_owner: bool, is_admin: bool, is_member: bool) -> str:
    if is_owner:
        return "owner"
    if is_admin:
        return "admins"
    if is_member:
        return "members"
    return "all"


def _legacy_rank(level: str) -> int:
    return {"owner": 5, "admins": 3, "members": 0, "all": -1}.get(level, -1)


def access_allowed(required: str, *, level: str, rank: Optional[int] = None, is_member: Optional[bool] = None) -> bool:
    """Единая проверка уровней: legacy-группы и внутренние ранги 1–5."""
    if required == "all":
        return True
    if required == "members":
        return bool(is_member if is_member is not None else level in ("owner", "admins", "members"))
    if required == "admins":
        # This option means Telegram chat administrators only. Internal
        # Justice Faite ranks are an independent policy and must be selected
        # explicitly with rank_1 ... rank_5.
        return level in ("owner", "admins")
    if required == "owner":
        return level == "owner"
    minimum = ACCESS_MIN_RANK.get(required)
    if minimum is not None:
        actual = _legacy_rank(level) if rank is None else int(rank)
        return actual >= minimum
    return False


def decide(command: str, *, chat_type: str, chat_id: int = 0, user_id: int, level: str,
           rank: Optional[int] = None, is_member: Optional[bool] = None,
           now: Optional[float] = None, commit: bool = True,
           respect_cooldown: bool = True) -> tuple[bool, str, int]:
    """Чистое решение: (разрешено, причина, секунд до конца паузы)."""
    spec = REGISTRY.get(command)
    if spec is None:
        return True, "ok", 0
    if command == "settings" and chat_type == "private" and int(user_id) == RT.owner_id:
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
    if (in_group and RT.primary_chat_id and chat_id and int(chat_id) != RT.primary_chat_id
            and cfg.get("chat_scope", "primary") != "all_groups"):
        return False, "where", 0

    override = STORE.user_access(command, user_id, chat_id=chat_id)
    if override == "deny":
        return False, "personal_deny", 0
    need = cfg.get("access", spec.access)
    if override != "allow" and not access_allowed(need, level=level, rank=rank, is_member=is_member):
        return False, "access", 0

    wait = int(cfg.get("cooldown", 0) or 0) if respect_cooldown else 0
    if wait and not is_owner:
        now = time.monotonic() if now is None else now
        cooldown_key = (command, int(chat_id or 0), int(user_id))
        last = _COOLDOWNS.get(cooldown_key)
        if last is not None and now - last < wait:
            return False, "cooldown", int(wait - (now - last)) + 1
        if commit:
            _COOLDOWNS[cooldown_key] = now
    return True, "ok", 0

def reset_runtime_state() -> None:
    _COOLDOWNS.clear()
    _MEMBER_CACHE.clear()
    _ACTIVE_ACCESS.clear()


async def _member_state(user_id: int, *, chat_id: int = 0,
                        observed_in_chat: bool = False) -> tuple[str, bool]:
    """(status, is_member) в целевом чате; входящее сообщение — доказательство членства."""
    target_chat = int(chat_id) if int(chat_id or 0) < 0 else int(RT.primary_chat_id or 0)
    if not target_chat:
        return ("member", True) if observed_in_chat else ("left", False)
    if not RT.bot:
        return ("member", True) if observed_in_chat else ("left", False)
    cache_key = (target_chat, int(user_id))
    cached = _MEMBER_CACHE.get(cache_key)
    now = time.monotonic()
    # A fresh incoming group message supersedes stale cached membership data.
    if cached and now - cached[0] < MEMBER_TTL and not observed_in_chat:
        return cached[1], cached[2]
    status, is_member = "left", False
    try:
        member = await RT.bot.get_chat_member(target_chat, user_id)
        status = str(getattr(member, "status", "left"))
        is_member = status in ("member", "administrator", "creator") or (
            status == "restricted" and bool(getattr(member, "is_member", False)))
    except Exception as exc:  # сеть/права: не теряем доказательство из входящего сообщения
        logger.debug("member lookup failed | chat=%s | user=%s | %s", target_chat, user_id, exc)
    if observed_in_chat and not is_member:
        # Telegram delivered a message from this user in this exact group.
        # This proves membership, but deliberately does not grant admin status.
        status, is_member = "member", True
    _MEMBER_CACHE[cache_key] = (now, status, is_member)
    return status, is_member


async def rank_of(user_id: int, *, chat_id: int = 0,
                  observed_in_chat: bool = False) -> tuple[int, bool, str]:
    """Возвращает (ранг 0–5 или -1, участник, legacy-уровень)."""
    if int(user_id) == RT.owner_id:
        return 5, True, "owner"
    target_chat = int(chat_id) if chat_id and chat_id < 0 else RT.primary_chat_id
    status, is_member = await _member_state(
        user_id, chat_id=target_chat, observed_in_chat=observed_in_chat
    )
    if not is_member:
        return -1, False, "all"
    rank = 0
    if RT.manager is not None and target_chat:
        try:
            # Read only the rank explicitly assigned in Justice Faite.
            # Manager._rank also raises ranks from Telegram permissions for
            # moderation hierarchy; that is useful inside the manager module,
            # but must not leak into the independent command-access policy.
            rank_reader = getattr(RT.manager, "assigned_rank", None)
            if callable(rank_reader):
                rank = int(rank_reader(target_chat, int(user_id)))
        except Exception:
            logger.exception("internal rank lookup failed | chat=%s | user=%s", target_chat, user_id)
            rank = 0
    legacy = "admins" if status in ("administrator", "creator") else "members"
    return max(0, min(5, rank)), True, legacy


async def level_of(user_id: int) -> str:
    _, is_member, level = await rank_of(user_id)
    return level if is_member or level == "owner" else "all"


async def _access_context(user_id: int, chat_id: int, *,
                          observed_in_chat: bool = False) -> tuple[str, int, bool]:
    rank, is_member, level = await rank_of(
        user_id, chat_id=chat_id, observed_in_chat=observed_in_chat
    )
    return level, rank, is_member

DENY_TEXT = {
    "disabled": "🔒 Эта команда сейчас выключена.",
    "where": "🔒 Эта команда здесь недоступна.",
    "access": "🔒 У вас нет доступа к этой команде.",
    "personal_deny": "🔒 Для вас эта команда закрыта.",
}


def _preflight_denial(command: str, *, chat_type: str, chat_id: int, user_id: int) -> Optional[str]:
    """Проверки, которым не нужны Telegram-запросы о ранге пользователя.

    Особенно важно проверять выключение до getChatMember: отключённая команда
    должна молча завершаться и не создавать лишний запрос к Telegram.
    """
    spec = REGISTRY.get(command)
    if spec is None:
        return "unknown"
    # The bot owner must always be able to reopen the access panel after a bad
    # configuration. This is the intentional recovery-only exception.
    if command == "settings" and chat_type == "private" and int(user_id) == RT.owner_id:
        return None
    if not get_bool(command, "enabled") and command != "settings":
        return "disabled"

    in_group = chat_type in ("group", "supergroup")
    where = get_str(command, "where") or spec.where
    if where == "private" and chat_type != "private":
        return "where"
    if where == "group" and not in_group:
        return "where"
    if (in_group and RT.primary_chat_id and chat_id and int(chat_id) != RT.primary_chat_id
            and get_str(command, "chat_scope") != "all_groups"):
        return "where"

    if STORE.user_access(command, user_id, chat_id=chat_id) == "deny":
        return "personal_deny"
    return None


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
    # Central middleware has already checked this exact message and command.
    # Avoid a second rank lookup and a second cooldown commit inside handlers.
    if access_was_checked(message) and checked_command(message) == command:
        return True

    user = getattr(message, "from_user", None)
    if user is None:
        return False
    uid = int(user.id)
    chat = message.chat
    if command not in REGISTRY:
        logger.warning("Unregistered command attempted to pass access gate: %s", command)
        return False

    preflight = _preflight_denial(command, chat_type=chat.type, chat_id=int(chat.id), user_id=uid)
    if preflight:
        if notify and chat.type == "private" and preflight not in ("disabled", "personal_deny", "unknown"):
            try:
                await message.answer(DENY_TEXT.get(preflight, "🔒 Команда недоступна здесь."))
            except Exception:
                pass
        return False

    level, rank, is_member = await _access_context(
        uid, int(chat.id), observed_in_chat=chat.type in ("group", "supergroup")
    )
    ok, reason, wait = decide(command, chat_type=chat.type, chat_id=int(chat.id), user_id=uid,
                              level=level, rank=rank, is_member=is_member, commit=commit)
    if ok:
        delay = get_int(command, "autodelete") if command in REGISTRY and REGISTRY[command].autodelete else 0
        if commit and delay and chat.type in ("group", "supergroup"):
            schedule_delete(int(chat.id), int(message.message_id), delay)
        return True
    if notify and chat.type == "private" and reason not in ("disabled", "personal_deny"):
        text = DENY_TEXT.get(reason) or f"⏳ Подождите {wait} с перед повтором."
        if reason == "cooldown":
            text = f"⏳ Подождите {wait} с перед повтором."
        try:
            await message.answer(text)
        except Exception:
            pass
    return False


async def gate_callback(callback, command: str, *, commit: bool = True,
                        chat_id_override: Optional[int] = None,
                        chat_type_override: Optional[str] = None,
                        respect_cooldown: bool = True) -> bool:
    user = callback.from_user
    chat = callback.message.chat if getattr(callback, "message", None) else None
    chat_type = chat_type_override or (chat.type if chat else "private")
    chat_id = int(chat_id_override if chat_id_override is not None else (chat.id if chat else 0))
    if command not in REGISTRY:
        logger.warning("Unregistered callback command attempted to pass access gate: %s", command)
        try:
            await callback.answer()
        except Exception:
            pass
        return False

    preflight = _preflight_denial(command, chat_type=chat_type, chat_id=chat_id, user_id=int(user.id))
    if preflight:
        # A disabled command is deliberately invisible in the chat: no service
        # message and no popup explaining that the command is switched off.
        try:
            await callback.answer("" if preflight in ("disabled", "personal_deny") else DENY_TEXT.get(preflight, "🔒 Недоступно."),
                                  show_alert=preflight not in ("disabled", "personal_deny"))
        except Exception:
            pass
        return False
    level, rank, is_member = await _access_context(int(user.id), chat_id)
    ok, reason, wait = decide(command, chat_type=chat_type, chat_id=chat_id, user_id=int(user.id),
                              level=level, rank=rank, is_member=is_member, commit=commit,
                              respect_cooldown=respect_cooldown)
    if ok:
        return True
    if reason in ("disabled", "personal_deny"):
        text = ""
    else:
        text = f"⏳ Подождите {wait} с." if reason == "cooldown" else DENY_TEXT.get(reason, "🔒 Недоступно.")
    try:
        await callback.answer(text, show_alert=bool(text))
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

def visible(command: str, *, chat_type: str, level: str, rank: Optional[int] = None,
            is_member: Optional[bool] = None, user_id: Optional[int] = None, chat_id: int = 0) -> bool:
    """Показывать ли команду с учётом ранга, личного исключения и типа чата."""
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
    if (in_group and RT.primary_chat_id and chat_id and int(chat_id) != RT.primary_chat_id
            and get_str(command, "chat_scope") != "all_groups"):
        return False
    if user_id is not None:
        override = STORE.user_access(command, int(user_id), chat_id=chat_id)
        if override == "deny":
            return False
        if override == "allow":
            return True
    return access_allowed(cfg.get("access", spec.access), level=level, rank=rank, is_member=is_member)


def resolve_command_key(value: str) -> Optional[str]:
    """Resolve a settings target by registry key, title, slash name or alias."""
    raw = " ".join(str(value or "").strip().casefold().split())
    if not raw:
        return None
    if raw in REGISTRY:
        return raw
    for spec in REGISTRY.values():
        if raw == spec.title.casefold().strip():
            return spec.key
        if any(raw == str(alias).casefold().strip() for alias in spec.aliases):
            return spec.key
    matched = match_command(raw)
    if matched:
        return matched
    if not raw.startswith("/"):
        return match_command("/" + raw)
    return None


def normalize_command_candidates(text: str) -> tuple[str, ...]:
    """Варианты первой строки для распознавания slash- и текстовых команд."""
    line = (text or "").splitlines()[0].strip().casefold()
    if not line:
        return ()
    candidates = [line]
    line_no_mention = line
    line_no_mention = __import__("re").sub(r"@[a-z0-9_]+(?=\s|$)", "", line_no_mention, count=1)
    if line_no_mention != line:
        candidates.append(line_no_mention.strip())
    for candidate in list(candidates):
        stripped = __import__("re").sub(r"^[!.]{1,4}\s*", "", candidate).strip()
        if stripped and stripped not in candidates:
            candidates.append(stripped)
        iris_stripped = __import__("re").sub(r"^(?:ирис(?:ка)?|iris)\s+", "", stripped, flags=__import__("re").I).strip()
        if iris_stripped and iris_stripped not in candidates:
            candidates.append(iris_stripped)
    return tuple(candidates)


def match_command(text: str) -> Optional[str]:
    """Ищет самый длинный совпавший алиас среди всех встроенных команд."""
    import re
    candidates = normalize_command_candidates(text)
    if not candidates:
        return None
    matches: list[tuple[int, str]] = []
    for spec in REGISTRY.values():
        for alias in spec.aliases:
            raw_alias = str(alias).casefold().strip()
            if not raw_alias:
                continue
            for candidate in candidates:
                if candidate == raw_alias or candidate.startswith(raw_alias + " "):
                    matches.append((len(raw_alias), spec.key))
                    break
        # Special command form: positive reputation by replying with + / +2 / +3.
        if spec.key == "reputation_vote" and any(re.fullmatch(r"\+[1-3]?", c) for c in candidates):
            matches.append((2, spec.key))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def _event_access_id(event) -> tuple[int, int, int]:
    chat = getattr(event, "chat", None)
    user = getattr(event, "from_user", None)
    return (int(getattr(chat, "id", 0) or 0),
            int(getattr(event, "message_id", 0) or 0),
            int(getattr(user, "id", 0) or 0))


def access_was_checked(event) -> bool:
    """True while the central middleware is executing this command handler."""
    return _event_access_id(event) in _ACTIVE_ACCESS


def checked_command(event) -> Optional[str]:
    return _ACTIVE_ACCESS.get(_event_access_id(event))


def _commit_cooldown(command: str, user_id: int, chat_id: int = 0) -> None:
    wait = get_int(command, "cooldown") if command in REGISTRY else 0
    if wait and int(user_id) != RT.owner_id:
        _COOLDOWNS[(command, int(chat_id or 0), int(user_id))] = time.monotonic()


async def command_access_middleware(handler, event, data):
    """Единая молчаливая проверка и для slash-, и для текстовых команд.

    Проверка выполняется до обработчика. После успешного исполнения фиксируется
    cooldown. Внутренние модули могут узнать, что права уже проверены, и не
    применять второй, конфликтующий набор рангов.
    """
    text = getattr(event, "text", None) or getattr(event, "caption", None) or ""
    key = match_command(text)
    if key is None:
        return await handler(event, data)
    chat = getattr(event, "chat", None)
    # In groups/private chats the middleware stays silent for a switched-off
    # command. In private chats other policy denials may still be explained.
    notify = bool(chat and getattr(chat, "type", None) == "private")
    if not await gate(event, key, notify=notify, commit=False):
        return None
    event_id = _event_access_id(event)
    _ACTIVE_ACCESS[event_id] = key
    try:
        result = await handler(event, data)
        user_id = int(getattr(getattr(event, "from_user", None), "id", 0) or 0)
        _commit_cooldown(key, user_id, int(getattr(chat, "id", 0) or 0))
        delay = get_int(key, "autodelete") if REGISTRY[key].autodelete else 0
        if delay and chat and getattr(chat, "type", None) in ("group", "supergroup"):
            schedule_delete(int(chat.id), int(getattr(event, "message_id", 0) or 0), delay)
        return result
    finally:
        _ACTIVE_ACCESS.pop(event_id, None)


def callback_command_for_data(data: str) -> Optional[str]:
    """Map interactive actions to the same separately configurable policies as commands."""
    value = str(data or "")
    if value in {"u:send", "u:report", "u:report_edit", "u:report_confirm"}:
        return "anon" if value == "u:send" else "report"
    if value.startswith("a:replyuser:") or value.startswith("reply:"):
        return "anon_admin"
    if value.startswith("a:"):
        return "admin_panel"
    if value.startswith("mf:"):
        parts = value.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        if action == "stop":
            return "mafia_admin_action"
        if action == "leave":
            return "mafia_leave"
        return "mafia"
    if value.startswith("gp:"):
        parts = value.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        if action == "v":
            return "game_poll_vote"
        if action == "i":
            return "game_poll_info"
        return "game_poll"
    if value.startswith("fm:role:"):
        return "call"
    if value.startswith("fm:confirm:"):
        return "member_confirm"
    if value.startswith("aw:"):
        return "awards"
    if value.startswith("jfcon:"):
        return "console_open"
    if value.startswith("cs:"):
        return "settings"
    return None


async def callback_access_middleware(handler, event, data):
    """Apply command settings to interactive buttons as well as typed commands."""
    key = callback_command_for_data(getattr(event, "data", "") or "")
    if key is not None and not await gate_callback(event, key, commit=False, respect_cooldown=False):
        return None
    return await handler(event, data)


def specs_in(category: str) -> list[Spec]:
    return [s for s in REGISTRY.values() if s.category == category]

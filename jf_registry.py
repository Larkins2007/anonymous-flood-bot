"""Runtime registry audit for Justice Faite Manager (0.16.0, read-only).

The command catalog (``cmdcfg`` / ``cmdspecs`` / ``cmdcatalog``) says what the
bot *should* answer to.  The aiogram ``Dispatcher`` knows what is *actually*
registered.  This module compares the two and reports every disagreement:

* catalog entry that no registered handler accepts (catalog without handler);
* handler registered twice with an identical filter signature;
* ``Command`` filter without a catalog policy (handler without policy);
* command text that is accepted by several handlers (shadowing) or routed to a
  module other than the one that owns the catalog category;
* alias collisions inside the catalog;
* callback handlers without any authorization evidence (callback without auth);
* duplicate / shadowed callback namespaces and callback policy targets that are
  missing from the catalog;
* inline-button ``callback_data`` produced by the source that no handler accepts
  (stale UI) or that exceeds Telegram's 64-byte limit;
* Telegram menu entries that are malformed, duplicated or unrouted.

Nothing here changes behaviour: no handler is added, removed or reordered, no
setting is written and no Telegram API call is made.  Routing is *probed* by
evaluating each handler's real filters against synthetic events, so the audit
follows the filters that are installed instead of a parallel hand-written list.

Limits (see REGISTRY_AUDIT_0.16.0.md): the probe cannot run FSM-state filters or
opaque Python-callable filters and reports them as such; authorization inside a
handler body is detected by a static source heuristic and is evidence for a human
reviewer, not a proof.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional

REPORT_SCHEMA = 1
CHAT_TYPES = ("private", "group", "supergroup", "channel")
OBSERVER_NAMES = (
    "message", "edited_message", "channel_post", "edited_channel_post", "callback_query",
    "inline_query", "chosen_inline_result", "chat_member", "my_chat_member",
    "chat_join_request", "poll_answer", "errors",
)
TEXT_FIELDS = ("text", "caption", "data")
SEVERITIES = ("error", "warning", "review", "info")
CALLBACK_LIMIT_BYTES = 64
NEUTRAL_TEXT = "qzxv ordinary sentence without any command word"
NEUTRAL_SLASH = "/qzxvnosuchcommand"
MENU_COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Category -> Python modules that own the handlers for the category.  The audit
# reports a command routed outside these modules (and outside bot.py, which owns
# core/anonymous/roles/games) as a mismatch.  The table is itself validated by
# tests: every catalog category must be mapped.
CATEGORY_MODULES: dict[str, tuple[str, ...]] = {
    "community": ("jf_community", "jf_pets", "jf_social", "justice_features", "bot"),
    "chat": ("jf_chatconfig",),
    "roles": ("bot", "justice_features"),
    "moderation": ("jf_manager", "justice_features", "jf_modvote"),
    "profiles": ("jf_profiles", "justice_features"),
    "statistics": ("jf_stats",),
    "reputation": ("jf_reputation",),
    "automation": ("jf_automod", "jf_reports", "jf_timers", "bot"),
    "fun": ("jf_fun",),
    "ai": ("jf_ai", "jf_ai_analytics"),
    "network": ("jf_network",),
    "events": ("jf_raffles", "justice_features"),
    "cleanup": ("jf_clean",),
    "awards": ("awards",),
    "games": ("bot",),
    "economy": ("jf_economy",),
    "anon": ("bot",),
    "social": ("jf_social",),
    "pets": ("jf_pets",),
    "system": ("jf_console", "settings_panel", "jf_registry", "jf_inline_guard", "bot"),
    "custom": ("bot",),
    "general": ("bot", "justice_features", "settings_panel"),
}

# Callbacks that carry no privileged action: they only navigate between static
# informational screens of the private anonymous-message bot.  They are listed
# explicitly so that a *new* ungated callback is flagged instead of silently
# accepted.  See cmdcfg.callback_command_for_data ("Navigation-only buttons are
# intentionally not gated.").
NAVIGATION_CALLBACKS: dict[str, str] = {
    "u:home": "navigation to the private user menu",
    "u:info": "static information screen",
    "u:send_info": "static information screen",
    "u:report_info": "static information screen",
    "u:cancel": "cancels the caller's own pending FSM state",
}

AUTH_MARKERS = re.compile(
    r"owner_id|ADMIN_ID|is_owner|_owner\b|cmdcfg\.(?:gate|gate_callback|decide|rank_of|level_of|access_allowed)"
    r"|\.gate\(|gate_callback|_access_context|rank_of\(|is_admin|get_chat_member|has_rank|_rank_of|"
    r"from_user\.id\s*[!=]=|\.is_manager|require_|_deny\(|\.deny\(",
)


# ---------------------------------------------------------------------------
# Probing helpers
# ---------------------------------------------------------------------------


class _Probe:
    """Synthetic Telegram event; records which text-like fields a filter reads."""

    def __init__(self, **fields: Any):
        object.__setattr__(self, "_fields", fields)
        object.__setattr__(self, "touched", set())

    def __getattr__(self, name: str):
        fields = object.__getattribute__(self, "_fields")
        if name in fields:
            if name in TEXT_FIELDS:
                object.__getattribute__(self, "touched").add(name)
            return fields[name]
        return None


def _user(user_id: int):
    return SimpleNamespace(id=int(user_id), is_bot=False, username="probe", first_name="Probe", last_name=None)


def message_probe(text: Optional[str], chat_type: str, user_id: int, chat_id: int = -100500) -> _Probe:
    chat = SimpleNamespace(id=int(chat_id) if chat_type != "private" else int(user_id), type=chat_type, title="Probe")
    return _Probe(text=text, caption=None, chat=chat, from_user=_user(user_id), message_id=1,
                  entities=None, forward_origin=None, reply_to_message=None, photo=None, document=None)


def callback_probe(data: str, chat_type: str, user_id: int) -> _Probe:
    chat = SimpleNamespace(id=-100500 if chat_type != "private" else int(user_id), type=chat_type, title="Probe")
    inner = SimpleNamespace(chat=chat, message_id=1, text="probe")
    return _Probe(data=data, from_user=_user(user_id), message=inner, id="probe")


def _filter_object(f: Any) -> Any:
    """Unwrap aiogram's FilterObject without confusing it with the stub Magic."""
    if type(f).__name__ == "FilterObject":
        return getattr(f, "callback", f)
    return f


def _is_magic(f: Any) -> bool:
    return type(f).__name__ in ("Magic", "MagicFilter")


def _is_state(f: Any) -> bool:
    return type(f).__name__ in ("State", "StateFilter")


def _is_command(f: Any) -> bool:
    return hasattr(f, "commands") and not _is_magic(f)


def _command_accepts(f: Any, text: Optional[str]) -> bool:
    if not isinstance(text, str):
        return False
    prefix = getattr(f, "prefix", "/")
    prefixes = tuple(prefix) if isinstance(prefix, (tuple, list, set, frozenset)) else tuple(str(prefix or "/"))
    match = re.match(r"^\s*(\S)([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s|$)", text)
    if not match or match.group(1) not in prefixes:
        return False
    names = []
    for item in getattr(f, "commands", ()) or ():
        if isinstance(item, str):
            names.append(item.lower())
        else:
            name = getattr(item, "command", None)
            if isinstance(name, str):
                names.append(name.lower())
    return match.group(2).lower() in names


def evaluate_filter(f: Any, event: _Probe) -> tuple[Optional[bool], str]:
    """Return (result, kind). result None means the filter cannot be probed."""
    f = _filter_object(f)
    if _is_magic(f):
        try:
            return bool(f.resolve(event)), "magic"
        except Exception:
            return None, "opaque"
    if _is_command(f):
        return _command_accepts(f, getattr(event, "text", None)), "command"
    if _is_state(f):
        return None, "state"
    return None, "opaque"


# ---------------------------------------------------------------------------
# Dispatcher introspection
# ---------------------------------------------------------------------------


def _iter_routers(dp: Any, seen: Optional[set] = None) -> Iterable[Any]:
    seen = seen if seen is not None else set()
    if id(dp) in seen:
        return
    seen.add(id(dp))
    yield dp
    for child in getattr(dp, "sub_routers", None) or ():
        yield from _iter_routers(child, seen)


def _observers(router: Any) -> Iterable[tuple[str, Any]]:
    mapping = getattr(router, "observers", None)
    if isinstance(mapping, dict):
        for name, observer in mapping.items():
            yield name, observer
        return
    for name in OBSERVER_NAMES:
        observer = router.__dict__.get(name) if hasattr(router, "__dict__") else None
        if observer is not None and hasattr(observer, "handlers"):
            yield name, observer


def _raw_handlers(observer: Any) -> list[tuple[Callable, list]]:
    out = []
    for item in getattr(observer, "handlers", None) or ():
        if isinstance(item, tuple):  # offline stub: (callback, filters)
            callback, filters = item[0], list(item[1])
        else:  # aiogram HandlerObject
            callback = getattr(item, "callback", None)
            filters = [_filter_object(x) for x in (getattr(item, "filters", None) or [])]
        out.append((callback, filters))
    return out


def _unwrap(callback: Callable) -> Callable:
    return getattr(callback, "__func__", callback)


def handler_identity(callback: Callable) -> tuple[str, str, int]:
    func = _unwrap(callback)
    module = getattr(func, "__module__", "?") or "?"
    qualname = getattr(func, "__qualname__", getattr(func, "__name__", repr(func)))
    try:
        line = inspect.getsourcelines(func)[1]
    except (OSError, TypeError):
        line = 0
    return module, qualname, line


def _magic_atoms(f: Any) -> list[tuple[str, str, Any]]:
    """Best-effort (path, op, value) extraction used only for display and samples."""
    atoms: list[tuple[str, str, Any]] = []

    def walk(ops, path=()):
        cur = list(path)
        for op in ops:
            kind = op[0]
            if kind == "attr":
                cur.append(op[1])
            elif kind in ("eq", "ne", "in", "startswith", "re", "is"):
                value = op[1].pattern if kind == "re" else op[1]
                atoms.append((".".join(cur), kind, sorted(map(str, value)) if kind == "in" else value))
            elif kind == "not":
                if atoms:
                    p, k, v = atoms[-1]
                    atoms[-1] = (p, "not " + k, v)
            elif kind in ("and", "or"):
                for sub in (op[1], op[2]):
                    walk(getattr(sub, "_ops", ()), ())
    try:
        ops = getattr(f, "_ops", None)
        if ops is not None:  # offline stub
            walk(ops)
        else:  # real MagicFilter: parse its repr, e.g. F.data.startswith('a:')
            text = repr(f)
            for m in re.finditer(r"startswith\(\s*['\"]([^'\"]+)['\"]", text):
                atoms.append(("data", "startswith", m.group(1)))
            for m in re.finditer(r"data\s*==\s*['\"]([^'\"]+)['\"]", text):
                atoms.append(("data", "eq", m.group(1)))
    except Exception:
        return []
    return atoms


def _filter_signature(f: Any) -> str:
    f = _filter_object(f)
    if _is_command(f):
        return "cmd:" + ",".join(sorted(str(c) for c in f.commands))
    if _is_magic(f):
        atoms = _magic_atoms(f)
        return "magic:" + json.dumps(atoms, sort_keys=True, default=str) if atoms else f"magic@{id(f)}"
    if _is_state(f):
        return f"state:{getattr(f, 'state', repr(f))}"
    return f"opaque@{getattr(f, '__qualname__', id(f))}"


@dataclass
class HandlerInfo:
    observer: str
    order: int
    module: str
    qualname: str
    line: int
    callback: Callable = field(repr=False, default=None)
    filters: list = field(repr=False, default_factory=list)
    commands: list = field(default_factory=list)
    callback_literals: list = field(default_factory=list)
    callback_prefixes: list = field(default_factory=list)
    text_patterns: list = field(default_factory=list)
    states: list = field(default_factory=list)
    chat_scope: list = field(default_factory=list)
    owner_guard: bool = False
    stateful: bool = False
    opaque: bool = False
    catch_all: bool = False
    signature: str = ""

    @property
    def hid(self) -> str:
        return f"{self.module}.{self.qualname}"

    def as_dict(self) -> dict:
        return {
            "id": self.hid, "observer": self.observer, "order": self.order, "module": self.module,
            "qualname": self.qualname, "line": self.line, "commands": self.commands,
            "callback_literals": self.callback_literals, "callback_prefixes": self.callback_prefixes,
            "text_patterns": [p[:120] for p in self.text_patterns], "states": self.states,
            "chat_scope": self.chat_scope, "owner_guard": self.owner_guard,
            "stateful": self.stateful, "opaque_filter": self.opaque, "catch_all": self.catch_all,
        }


def collect_handlers(dp: Any, owner_id: int) -> list[HandlerInfo]:
    """Flatten every registered handler of every observer in registration order."""
    out: list[HandlerInfo] = []
    other_user = int(owner_id) + 7919
    for router in _iter_routers(dp):
        for name, observer in _observers(router):
            for order, (callback, filters) in enumerate(_raw_handlers(observer)):
                if callback is None:
                    continue
                module, qualname, line = handler_identity(callback)
                info = HandlerInfo(name, order, module, qualname, line, callback, filters)
                scope = set(CHAT_TYPES)
                for flt in filters:
                    flt = _filter_object(flt)
                    if _is_command(flt):
                        info.commands.extend(str(c) for c in flt.commands)
                        continue
                    if _is_state(flt):
                        info.stateful = True
                        info.states.append(str(getattr(flt, "state", flt)))
                        continue
                    if not _is_magic(flt):
                        info.opaque = True
                        continue
                    for path, op, value in _magic_atoms(flt):
                        if path == "data" and op == "eq":
                            info.callback_literals.append(str(value))
                        elif path == "data" and op == "startswith":
                            info.callback_prefixes.append(str(value))
                        elif path == "text" and op == "re":
                            info.text_patterns.append(str(value))
                    # Scope / owner guard from filters that do not read text-like fields.
                    allowed = set()
                    guard_by_user = False
                    independent = True
                    for chat_type in CHAT_TYPES:
                        probe = message_probe("x", chat_type, owner_id) if name != "callback_query" \
                            else callback_probe("x", chat_type, owner_id)
                        try:
                            result = bool(flt.resolve(probe))
                        except Exception:
                            independent = False
                            break
                        if probe.touched:
                            independent = False
                            break
                        if result:
                            allowed.add(chat_type)
                    if independent:
                        scope &= allowed
                        probe_owner = message_probe("x", "private", owner_id)
                        probe_other = message_probe("x", "private", other_user)
                        try:
                            if flt.resolve(probe_owner) and not flt.resolve(probe_other):
                                guard_by_user = True
                        except Exception:
                            pass
                        if guard_by_user:
                            info.owner_guard = True
                info.chat_scope = sorted(scope, key=CHAT_TYPES.index)
                info.signature = "|".join(sorted(_filter_signature(f) for f in filters)) + f"|{name}|{module}.{qualname}"
                out.append(info)
    # A catch-all accepts ordinary chat text that is not a command at all (for
    # example passive member synchronisation or the custom-command dispatcher).
    for info in out:
        if info.observer == "message" and not info.stateful and not info.opaque:
            for chat_type in ("private", "supergroup"):
                for text in (NEUTRAL_TEXT, NEUTRAL_SLASH):
                    if route([info], "message", message_probe(text, chat_type, other_user)):
                        info.catch_all = True
    return out


def route(handlers: list[HandlerInfo], observer: str, event: _Probe) -> list[HandlerInfo]:
    """All stateless handlers (in order) whose probe-able filters accept the event."""
    accepted = []
    for info in handlers:
        if info.observer != observer or info.stateful or info.opaque:
            continue
        ok = True
        for flt in info.filters:
            result, _ = evaluate_filter(flt, event)
            if result is not True:
                ok = False
                break
        if ok:
            accepted.append(info)
    return accepted


# ---------------------------------------------------------------------------
# Static source scan
# ---------------------------------------------------------------------------


@dataclass
class CallbackSite:
    file: str
    line: int
    value: str
    partial: bool
    dynamic: bool = False


def _project_py_files(project_dir: str) -> list[str]:
    files = []
    for name in sorted(os.listdir(project_dir)):
        if name.endswith(".py"):
            files.append(os.path.join(project_dir, name))
    return files


def _string_prefix(node: ast.AST) -> tuple[Optional[str], bool]:
    """Return (constant prefix, is_partial) for a callback_data expression."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False
    if isinstance(node, ast.JoinedStr):
        prefix = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                prefix += part.value
            else:
                return prefix, True
        return prefix, False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, partial = _string_prefix(node.left)
        if left is None:
            return None, True
        if partial:
            return left, True
        right, right_partial = _string_prefix(node.right)
        if right is None:
            return left, True
        return left + right, right_partial
    if isinstance(node, ast.IfExp):
        a, pa = _string_prefix(node.body)
        b, pb = _string_prefix(node.orelse)
        if a is None or b is None:
            return None, True
        return os.path.commonprefix([a, b]), True
    return None, True


_STATIC_CACHE: dict[tuple, Any] = {}


def _source_signature(project_dir: str) -> tuple:
    sig = []
    for folder in (project_dir, os.path.join(project_dir, "tests")):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.endswith(".py"):
                try:
                    st = os.stat(os.path.join(folder, name))
                    sig.append((folder, name, st.st_mtime_ns, st.st_size))
                except OSError:
                    pass
    return tuple(sig)


def _cached(kind: str, project_dir: str, builder: Callable[[str], Any]) -> Any:
    key = (kind, os.path.abspath(project_dir))
    signature = _source_signature(project_dir)
    hit = _STATIC_CACHE.get(key)
    if hit and hit[0] == signature:
        return hit[1]
    value = builder(project_dir)
    _STATIC_CACHE[key] = (signature, value)
    return value


def scan_callback_sites(project_dir: str) -> list[CallbackSite]:
    return _cached("sites", project_dir, _scan_callback_sites)


def _scan_callback_sites(project_dir: str) -> list[CallbackSite]:
    """Find every callback_data produced by source, including one-hop helper wrappers."""
    sites: list[CallbackSite] = []
    trees: dict[str, ast.AST] = {}
    helpers: dict[str, set[int]] = {}
    for path in _project_py_files(project_dir):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
        except (OSError, SyntaxError):
            continue
        trees[path] = tree
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params = [a.arg for a in node.args.args]
                for sub in ast.walk(node):
                    if isinstance(sub, ast.keyword) and sub.arg == "callback_data" and isinstance(sub.value, ast.Name) \
                            and sub.value.id in params:
                        helpers.setdefault(node.name, set()).add(params.index(sub.value.id))
    for path, tree in trees.items():
        rel = os.path.basename(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "callback_data":
                value, partial = _string_prefix(node.value)
                if value is None:
                    sites.append(CallbackSite(rel, getattr(node.value, "lineno", 0), "", True, dynamic=True))
                elif not (isinstance(node.value, ast.Name)):
                    sites.append(CallbackSite(rel, getattr(node.value, "lineno", 0), value, partial))
            elif isinstance(node, ast.Call):
                fname = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
                if fname in helpers:
                    for index in helpers[fname]:
                        if index < len(node.args):
                            value, partial = _string_prefix(node.args[index])
                            sites.append(CallbackSite(rel, node.args[index].lineno, value or "", partial,
                                                      dynamic=value is None))
    return sites


def scan_module_facts(project_dir: str) -> dict[str, dict]:
    return _cached("facts", project_dir, _scan_module_facts)


def _scan_module_facts(project_dir: str) -> dict[str, dict]:
    """Static per-module facts: tables, env vars, background tasks, size."""
    facts: dict[str, dict] = {}
    table_re = re.compile(r"CREATE TABLE IF NOT EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
    use_re = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+([a-z_][a-z0-9_]*)\b", re.I)
    env_re = re.compile(r"(?:getenv|environ(?:\.get)?)\(?\[?\s*['\"]([A-Z][A-Z0-9_]+)['\"]")
    for path in _project_py_files(project_dir):
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, "r", encoding="utf-8") as handle:
                src = handle.read()
        except OSError:
            continue
        created = sorted(set(m.group(1) for m in table_re.finditer(src)))
        used = sorted({m.group(1) for m in use_re.finditer(src)
                       if m.group(1).lower() not in ("select", "where", "set", "values", "the", "a", "this", "each", "which")
                       and (m.group(1).startswith(("jf_", "group_", "spy_", "mafia_", "role_", "users", "messages", "reports",
                                                    "custom_", "anon", "blocked", "feedback")) or m.group(1) in created)})
        facts[name] = {
            "file": os.path.basename(path), "lines": src.count("\n") + 1,
            "tables_created": created, "tables_referenced": used,
            "env_vars": sorted(set(env_re.findall(src))),
            "background_tasks": len(re.findall(r"\b(?:create_task|ensure_future)\(", src)),
            "loops": len(re.findall(r"while\s+True\s*:", src)),
            "broad_except": len(re.findall(r"except\s+Exception\b", src)),
        }
    return facts


def scan_hardcoded_menu(project_dir: str) -> list[str]:
    """Names of BotCommand(command="...") literals in bot.py (the hand-written part of the Telegram menu)."""
    path = os.path.join(project_dir, "bot.py")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return sorted(set(re.findall(r"BotCommand\(\s*command\s*=\s*[\"']([A-Za-z0-9_]+)[\"']", handle.read())))
    except OSError:
        return []


def _tests_touching(project_dir: str) -> dict[str, list[str]]:
    return _cached("tests", project_dir, _scan_tests)


def _tests_text(project_dir: str) -> dict[str, str]:
    def build(root: str) -> dict[str, str]:
        out: dict[str, str] = {}
        tests_dir = os.path.join(root, "tests")
        if os.path.isdir(tests_dir):
            for name in sorted(os.listdir(tests_dir)):
                if name.startswith("test_") and name.endswith(".py"):
                    try:
                        with open(os.path.join(tests_dir, name), "r", encoding="utf-8") as handle:
                            out[name] = handle.read()
                    except OSError:
                        pass
        return out
    return _cached("tests_text", project_dir, build)


def _scan_tests(project_dir: str) -> dict[str, list[str]]:
    tests_dir = os.path.join(project_dir, "tests")
    out: dict[str, list[str]] = {}
    if not os.path.isdir(tests_dir):
        return out
    modules = [os.path.splitext(f)[0] for f in os.listdir(project_dir) if f.endswith(".py")]
    for name in sorted(os.listdir(tests_dir)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        try:
            with open(os.path.join(tests_dir, name), "r", encoding="utf-8") as handle:
                src = handle.read()
        except OSError:
            continue
        for module in modules:
            if re.search(r"(?<![A-Za-z0-9_])" + re.escape(module) + r"(?![A-Za-z0-9_])", src):
                out.setdefault(module, []).append(name)
    return out


def _is_inert(callback: Callable) -> bool:
    """True when the handler only answers the callback (no state, DB or Telegram side effects)."""
    func = _unwrap(callback)
    try:
        tree = ast.parse(inspect.getsource(func).lstrip() if False else __import__("textwrap").dedent(inspect.getsource(func)))
    except (OSError, TypeError, SyntaxError):
        return False
    node = tree.body[0] if tree.body else None
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for stmt in node.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        call = stmt.value.value if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Await) else None
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "answer"):
            return False
    return True


def _auth_evidence(callback: Callable) -> list[str]:
    func = _unwrap(callback)
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):
        return []
    # A bound method's class may delegate to helpers; scan the whole class body for
    # a guard method that the handler calls (e.g. self.is_owner_private).
    found = sorted(set(m.group(0).strip() for m in AUTH_MARKERS.finditer(source)))
    return found[:6]


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    code: str
    severity: str
    subject: str
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "subject": self.subject, "message": self.message}


FINDING_CODES = {
    "REG001": "catalog entry has no handler that accepts its trigger",
    "REG002": "handler registered more than once with an identical filter signature",
    "REG003": "Command handler has no catalog policy",
    "REG004": "callback handler without authorization evidence",
    "REG005": "callback namespace duplicated or shadowed by an earlier handler",
    "REG006": "inline button callback_data accepted by no handler (stale UI)",
    "REG007": "alias collision inside the catalog",
    "REG008": "Telegram menu entry malformed, duplicated or unrouted",
    "REG009": "callback policy target missing from the catalog",
    "REG010": "callback_data exceeds the Telegram 64-byte limit",
    "REG011": "command text accepted by several handlers or routed to an unexpected module",
    "REG012": "catalog policy inconsistent",
    "REG013": "callback_data built dynamically and not verifiable statically",
    "REG014": "handler with a state or opaque filter cannot be probed",
}


class RegistryAudit:
    def __init__(self, dp: Any, *, specs: dict, owner_id: int, project_dir: str,
                 match_command: Callable[[str], Optional[str]],
                 callback_policy: Callable[[str], Optional[str]],
                 categories: Optional[dict] = None, menu_commands: Iterable[str] = (),
                 version: str = "", runtime: Optional[dict] = None,
                 category_modules: Optional[dict] = None):
        self.dp = dp
        self.specs = specs
        self.owner_id = int(owner_id)
        self.project_dir = project_dir
        self.match_command = match_command
        self.callback_policy = callback_policy
        self.categories = categories or {}
        self.menu_commands = tuple(menu_commands) or tuple(scan_hardcoded_menu(project_dir))
        self.version = version
        # key -> {"state": cmdcfg.command_state(key), "settings": cmdcfg.settings_for(key)}; captured by the
        # caller so that run() is a pure function that can be executed off the event loop.
        self.runtime = runtime or {}
        self.category_modules = category_modules if category_modules is not None else CATEGORY_MODULES
        self.findings: list[Finding] = []
        self.handlers: list[HandlerInfo] = []

    # -- helpers -----------------------------------------------------------
    def add(self, code: str, severity: str, subject: str, message: str) -> None:
        self.findings.append(Finding(code, severity, subject, message))

    def _specs(self):
        return [s for s in self.specs.values() if not getattr(s, "feature", False)]

    # -- command side ------------------------------------------------------
    def _samples(self, spec) -> list[str]:
        samples: list[str] = []
        for text in tuple(getattr(spec, "aliases", ()) or ()) + tuple(getattr(spec, "triggers", ()) or ()):
            text = str(text)
            if not text:
                continue
            for variant in (text, text + " @user", text + " 1 тест"):
                if variant not in samples:
                    samples.append(variant)
        return samples

    def _tests_for(self, spec) -> list[str]:
        needles = {f'"{spec.key}"', f"'{spec.key}'"}
        found = []
        for name, text in _tests_text(self.project_dir).items():
            if any(n in text for n in needles):
                found.append(name)
        return found

    @staticmethod
    def _pattern_mentions(pattern: str, alias: str) -> bool:
        """True when a handler regex spells the alias, e.g. ``сетка привязать\\s+CODE`` for 'сетка привязать'."""
        norm = re.sub(r"\\s[+*]", " ", pattern)
        norm = re.sub(r"\\(.)", r"\1", norm).casefold()
        needle = re.sub(r"\s+", " ", str(alias).strip().casefold().lstrip("/!.")).strip()
        return bool(needle) and needle in re.sub(r"\s+", " ", norm)

    def audit_commands(self) -> list[dict]:
        rows = []
        callback_targets = self.callback_policy_targets()
        message_handlers = [h for h in self.handlers if h.observer == "message"]
        for spec in self._specs():
            where = getattr(spec, "where", "both")
            chat_types = {"private": ["private"], "group": ["supergroup"]}.get(where, ["private", "supergroup"])
            user_id = self.owner_id if getattr(spec, "access", "") == "owner" else self.owner_id + 7919
            runtime = self.runtime.get(spec.key, {})
            row = {"key": spec.key, "title": spec.title, "category": spec.category,
                   "default_access": spec.access, "default_where": where,
                   "aliases": list(spec.aliases), "routes": [], "status": "unrouted",
                   "state": runtime.get("state"), "settings": runtime.get("settings"),
                   "cooldown_default": getattr(spec, "cooldown_default", 0),
                   "menu": [c for c, _ in (getattr(spec, "menu", ()) or ())],
                   "tests": self._tests_for(spec)}
            if getattr(spec, "custom", False):
                row["status"] = "custom"
                rows.append(row)
                continue
            if not spec.aliases:
                # Policy-only entry reached through inline buttons (cmdcfg.callback_command_for_data).
                row["status"] = "callback_policy" if spec.key in callback_targets else "orphan"
                if row["status"] == "orphan":
                    self.add("REG012", "error", spec.key,
                             "entry has no alias/trigger and no callback maps to it; it gates nothing")
                rows.append(row)
                continue
            specific: dict[str, list[HandlerInfo]] = {}
            all_hits: dict[str, list[HandlerInfo]] = {}
            catch_all_only: set[str] = set()
            for chat_type in chat_types:
                for sample in self._samples(spec):
                    if self.match_command(sample) != spec.key:
                        continue
                    hits = route(self.handlers, "message", message_probe(sample, chat_type, user_id))
                    real = [h for h in hits if not h.catch_all]
                    if real:
                        specific[chat_type] = real
                        all_hits[chat_type] = hits
                        break
                    if hits:
                        catch_all_only.add(chat_type)
            for chat_type, hits in specific.items():
                row["routes"].append({"chat_type": chat_type, "winner": all_hits[chat_type][0].hid,
                                      "intended": hits[0].hid,
                                      "also_accepted_by": [h.hid for h in all_hits[chat_type][1:]]})
            if not specific:
                # Fall back to the handler regexes themselves: probing cannot invent every argument shape.
                covered = sorted({h.hid for h in message_handlers if not h.catch_all
                                  and any(self._pattern_mentions(p, a) for p in h.text_patterns for a in spec.aliases)})
                if covered:
                    row["status"] = "pattern_covered"
                    row["routes"].append({"chat_type": "*", "winner": covered[0], "also_accepted_by": covered[1:],
                                          "note": "alias is spelled in the handler regex; no probe argument shape matched"})
                    rows.append(row)
                    continue
                reason = ("only a catch-all handler accepts it" if catch_all_only
                          else "no handler accepts any probe")
                self.add("REG001", "error", spec.key,
                         f"{reason} for aliases {list(spec.aliases)[:3]} (where={where}); the catalog entry is not backed "
                         "by a specific handler")
                rows.append(row)
                continue
            row["status"] = "routed"
            expected = set(self.category_modules.get(spec.category, ()))
            for chat_type, hits in specific.items():
                first = all_hits[chat_type][0]          # aiogram runs only the first accepting handler
                intended = hits[0]
                if first.catch_all:
                    row["status"] = "swallowed"
                    self.add("REG011", "error", spec.key,
                             f"{chat_type}: catch-all {first.hid} is registered before {intended.hid} and accepts the same "
                             "text, so the specific handler is never reached (aiogram stops at the first match)")
                    continue
                if first is not intended:  # pragma: no cover - cannot happen: intended is the first non-catch-all
                    continue
                if expected and intended.module not in expected:
                    later_expected = [h.hid for h in hits[1:] if h.module in expected]
                    row["status"] = "swallowed" if later_expected else "routed_unexpected_module"
                    self.add("REG011", "error" if later_expected else "warning", spec.key,
                             f"{chat_type}: {intended.hid} runs first but category '{spec.category}' belongs to "
                             f"{sorted(expected)}" + (f"; the owning handler {later_expected[0]} is later and unreachable"
                                                      if later_expected else ""))
                    continue
                rivals = [h.hid for h in hits[1:] if h.module != intended.module]
                if rivals:
                    self.add("REG011", "info", spec.key,
                             f"{chat_type}: {intended.hid} wins; later non-catch-all handlers also accept the same text: {rivals[:4]}")
            rows.append(row)
        return rows

    def callback_policy_targets(self) -> set:
        values = set()
        for info in self.handlers:
            if info.observer == "callback_query":
                for sample in self._callback_samples(info):
                    key = self.callback_policy(sample)
                    if key:
                        values.add(key)
        for site in scan_callback_sites(self.project_dir):
            if site.value:
                key = self.callback_policy(site.value + ("0" if site.partial else ""))
                if key:
                    values.add(key)
        return values

    def audit_policy_consistency(self) -> None:
        # alias collisions: an alias must resolve to its own command
        owners: dict[str, str] = {}
        for spec in self.specs.values():
            for alias in tuple(spec.aliases):
                norm = str(alias).strip().casefold()
                resolved = self.match_command(str(alias))
                if resolved is not None and resolved != spec.key:
                    self.add("REG007", "error", spec.key,
                             f"alias {alias!r} resolves to {resolved!r}: it is shadowed by another command")
                if norm in owners and owners[norm] != spec.key:
                    self.add("REG007", "error", spec.key,
                             f"alias {alias!r} is declared by both {owners[norm]!r} and {spec.key!r}")
                owners.setdefault(norm, spec.key)
        valid_categories = set(self.categories) if self.categories else None
        for spec in self.specs.values():
            if valid_categories is not None and spec.category not in valid_categories:
                self.add("REG012", "error", spec.key, f"category {spec.category!r} is not defined in CATEGORIES")
            if spec.access not in getattr(spec, "access_options", (spec.access,)):
                self.add("REG012", "error", spec.key, f"default access {spec.access!r} is not among access_options")
            if spec.where not in getattr(spec, "where_options", (spec.where,)) and not getattr(spec, "feature", False):
                self.add("REG012", "error", spec.key, f"where={spec.where!r} is not among where_options")
            if spec.category not in self.category_modules:
                self.add("REG012", "warning", spec.key,
                         f"category {spec.category!r} has no owning module in jf_registry.CATEGORY_MODULES")

    def audit_handler_side(self) -> None:
        seen: dict[str, HandlerInfo] = {}
        for info in self.handlers:
            if info.signature in seen:
                self.add("REG002", "error", info.hid,
                         f"{info.observer} handler registered twice with an identical filter signature "
                         f"(orders {seen[info.signature].order} and {info.order})")
            seen.setdefault(info.signature, info)
            if info.observer == "message":
                for command in info.commands:
                    if self.match_command("/" + command) is None:
                        self.add("REG003", "warning", info.hid,
                                 f"Command('{command}') has no catalog entry, so cmdcfg cannot gate, disable or log it")
            if info.stateful or info.opaque:
                self.add("REG014", "info", info.hid,
                         "state/opaque filter: reachable only with runtime FSM state or custom predicate; "
                         "routing for this handler is not probed")

    # -- callback side -----------------------------------------------------
    def _callback_samples(self, info: HandlerInfo) -> list[str]:
        return list(info.callback_literals) + [p + "x" for p in info.callback_prefixes]

    def audit_callbacks(self) -> list[dict]:
        rows = []
        cb_handlers = [h for h in self.handlers if h.observer == "callback_query"]
        for info in cb_handlers:
            samples = self._callback_samples(info)
            policy = None
            for sample in samples:
                policy = self.callback_policy(sample)
                if policy:
                    break
            policy_known = policy in self.specs if policy else False
            evidence = _auth_evidence(info.callback)
            if policy and not policy_known:
                self.add("REG009", "error", info.hid,
                         f"callback_command_for_data maps {samples[:1]} to {policy!r}, which is not in the catalog")
            if policy_known:
                auth = "cmdcfg"
            elif info.owner_guard:
                auth = "filter:owner"
            elif evidence:
                auth = "handler-source (heuristic)"
            elif info.callback_literals and all(x in NAVIGATION_CALLBACKS for x in info.callback_literals) \
                    and not info.callback_prefixes:
                auth = "navigation-only (allowlisted)"
            elif _is_inert(info.callback):
                auth = "inert (only answers the callback)"
            else:
                auth = "none-found"
            row = {"handler": info.hid, "literals": info.callback_literals, "prefixes": info.callback_prefixes,
                   "policy": policy, "auth": auth, "auth_evidence": evidence, "chat_scope": info.chat_scope}
            rows.append(row)
            if auth == "none-found":
                self.add("REG004", "review", info.hid,
                         f"callbacks {samples[:3]} have no cmdcfg policy, no owner filter and no authorization marker "
                         "in the handler source; a human must confirm they cannot perform a privileged action")
            # shadowing: an earlier handler that accepts this handler's samples
            for sample in samples:
                accepted = route(cb_handlers, "callback_query", callback_probe(sample, "private", self.owner_id))
                if accepted and accepted[0] is not info:
                    self.add("REG005", "error", info.hid,
                             f"sample {sample!r} is accepted first by {accepted[0].hid}; this handler can be unreachable")
                    break
        # duplicate literal across handlers
        literal_owner: dict[str, str] = {}
        for info in cb_handlers:
            for literal in info.callback_literals:
                if literal in literal_owner and literal_owner[literal] != info.hid:
                    self.add("REG005", "error", info.hid, f"callback literal {literal!r} also handled by {literal_owner[literal]}")
                literal_owner.setdefault(literal, info.hid)
        return rows

    def audit_callback_sites(self) -> list[dict]:
        cb_handlers = [h for h in self.handlers if h.observer == "callback_query"]
        rows = []
        for site in scan_callback_sites(self.project_dir):
            row = {"file": site.file, "line": site.line, "value": site.value, "partial": site.partial,
                   "dynamic": site.dynamic, "bytes": len(site.value.encode("utf-8")), "accepted_by": None}
            if site.dynamic:
                self.add("REG013", "info", f"{site.file}:{site.line}", "callback_data is built dynamically; not statically verified")
                rows.append(row)
                continue
            size = row["bytes"]
            if size > CALLBACK_LIMIT_BYTES or (site.partial and size > CALLBACK_LIMIT_BYTES - 8):
                self.add("REG010", "error" if size > CALLBACK_LIMIT_BYTES else "warning", f"{site.file}:{site.line}",
                         f"callback_data prefix {site.value!r} is {size} bytes; Telegram allows {CALLBACK_LIMIT_BYTES}")
            probe_value = site.value + ("0" if site.partial else "")
            hits = route(cb_handlers, "callback_query", callback_probe(probe_value, "private", self.owner_id))
            if hits:
                row["accepted_by"] = hits[0].hid
            else:
                self.add("REG006", "warning" if site.partial else "error", f"{site.file}:{site.line}",
                         f"button callback_data {site.value!r}{'*' if site.partial else ''} is accepted by no callback handler "
                         "(dead button: Telegram shows a spinner until it times out)")
            rows.append(row)
        return rows

    # -- menu --------------------------------------------------------------
    def audit_menu(self) -> list[dict]:
        rows = []
        seen: dict[str, str] = {}
        for spec in self.specs.values():
            for command, description in getattr(spec, "menu", ()) or ():
                row = {"command": command, "spec": spec.key, "description": description, "routed_to": None}
                if not MENU_COMMAND_RE.match(command):
                    self.add("REG008", "error", f"{spec.key}:{command}", "menu command must match [a-z0-9_]{1,32}")
                if not (1 <= len(description or "") <= 256):
                    self.add("REG008", "error", f"{spec.key}:{command}", "menu description must be 1..256 characters")
                if command in seen and seen[command] != spec.key:
                    self.add("REG008", "warning", f"{spec.key}:{command}", f"menu command also declared by {seen[command]}")
                seen.setdefault(command, spec.key)
                chat_type = "private" if spec.where in ("private", "both") else "supergroup"
                user_id = self.owner_id if spec.access == "owner" else self.owner_id + 7919
                hits = route(self.handlers, "message", message_probe("/" + command, chat_type, user_id))
                if hits:
                    row["routed_to"] = hits[0].hid
                elif not getattr(spec, "custom", False):
                    self.add("REG008", "error", f"{spec.key}:{command}",
                             f"menu entry /{command} is not routed to any handler (menu advertises a dead command)")
                rows.append(row)
        for command in self.menu_commands:
            if self.match_command("/" + command) is None:
                self.add("REG008", "warning", f"setup_commands:{command}",
                         f"hard-coded menu command /{command} has no catalog policy")
        return rows

    # -- module cards ------------------------------------------------------
    def module_cards(self) -> list[dict]:
        facts = scan_module_facts(self.project_dir)
        tests = _tests_touching(self.project_dir)
        by_module: dict[str, dict[str, int]] = {}
        for info in self.handlers:
            by_module.setdefault(info.module, {}).setdefault(info.observer, 0)
            by_module[info.module][info.observer] += 1
        cards = []
        for name in sorted(set(facts) | set(by_module)):
            categories = sorted(c for c, mods in self.category_modules.items() if name in mods)
            specs = [s for s in self.specs.values() if s.category in categories and not getattr(s, "feature", False)]
            enabled = disabled = 0
            reasons: dict[str, int] = {}
            disabled_keys = []
            for spec in specs:
                state = (self.runtime.get(spec.key) or {}).get("state")
                if not state:
                    continue
                if state.get("enabled"):
                    enabled += 1
                else:
                    disabled += 1
                    reasons[state.get("reason", "?")] = reasons.get(state.get("reason", "?"), 0) + 1
                    disabled_keys.append(spec.key)
            states = {"known": bool(self.runtime), "enabled_commands": enabled, "disabled_commands": disabled,
                      "disabled_by": reasons, "disabled_keys": disabled_keys[:20]}
            fact = facts.get(name, {})
            cards.append({
                "module": name, "file": fact.get("file"), "lines": fact.get("lines"),
                "handlers": by_module.get(name, {}), "handler_total": sum(by_module.get(name, {}).values()),
                "catalog_categories": categories, "catalog_commands": len(specs),
                "tables_created": fact.get("tables_created", []),
                "tables_referenced": fact.get("tables_referenced", []),
                "env_vars": fact.get("env_vars", []),
                "background_tasks_static": fact.get("background_tasks", 0),
                "loops_static": fact.get("loops", 0),
                "broad_except_static": fact.get("broad_except", 0),
                "tests": tests.get(name, []), "state": states,
                "registered_in_dispatcher": bool(by_module.get(name)),
            })
        return cards

    # -- run ---------------------------------------------------------------
    def run(self) -> dict:
        started = time.time()
        self.findings = []
        self.handlers = collect_handlers(self.dp, self.owner_id)
        commands = self.audit_commands()
        self.audit_policy_consistency()
        self.audit_handler_side()
        callbacks = self.audit_callbacks()
        sites = self.audit_callback_sites()
        menu = self.audit_menu()
        modules = self.module_cards()
        order = {s: i for i, s in enumerate(SEVERITIES)}
        findings = sorted(self.findings, key=lambda f: (order.get(f.severity, 9), f.code, f.subject))
        counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
        by_observer: dict[str, int] = {}
        for info in self.handlers:
            by_observer[info.observer] = by_observer.get(info.observer, 0) + 1
        return {
            "schema": REPORT_SCHEMA,
            "version": self.version,
            "introspection": "aiogram" if hasattr(self.dp, "observers") else "offline-stub",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_ms": int((time.time() - started) * 1000),
            "summary": {
                "catalog_commands": len(self._specs()),
                "catalog_features": len(self.specs) - len(self._specs()),
                "handlers_by_observer": by_observer,
                "commands_routed": sum(1 for c in commands if c["status"] in ("routed", "pattern_covered", "routed_unexpected_module")),
                "commands_unrouted": sum(1 for c in commands if c["status"] == "unrouted"),
                "commands_swallowed": sum(1 for c in commands if c["status"] == "swallowed"),
                "commands_callback_policy": sum(1 for c in commands if c["status"] == "callback_policy"),
                "callback_handlers": len(callbacks),
                "callback_sites": len(sites),
                "menu_entries": len(menu),
                "findings": counts,
            },
            "finding_codes": FINDING_CODES,
            "findings": [f.as_dict() for f in findings],
            "handlers": [h.as_dict() for h in self.handlers],
            "commands": commands,
            "callbacks": callbacks,
            "callback_sites": sites,
            "menu": menu,
            "modules": modules,
            "not_verified": [
                "live Telegram delivery, Telegram permissions and privacy-mode behaviour",
                "FSM-state-only handlers and opaque Python predicates (listed as REG014)",
                "authorization inside handler bodies beyond the static marker heuristic",
            ],
        }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        f"# Justice Faite registry audit — {report.get('version', '')}",
        "",
        f"- generated: {report['generated_at']} (introspection: {report['introspection']})",
        f"- catalog commands: {s['catalog_commands']} (+{s['catalog_features']} features); routed {s['commands_routed']}, unrouted {s['commands_unrouted']}",
        f"- handlers: {json.dumps(s['handlers_by_observer'], ensure_ascii=False)}",
        f"- callback handlers: {s['callback_handlers']}; callback_data sites in source: {s['callback_sites']}; menu entries: {s['menu_entries']}",
        f"- findings: {json.dumps(s['findings'])}",
        "",
        "## Findings",
        "",
    ]
    if not report["findings"]:
        lines.append("_none_")
    for f in report["findings"]:
        lines.append(f"- **{f['severity']} {f['code']}** `{f['subject']}` — {f['message']}")
    lines += ["", "## Finding codes", ""]
    lines += [f"- {code}: {text}" for code, text in report["finding_codes"].items()]
    lines += ["", "## Callback handlers", "", "| handler | callbacks | policy | auth |", "|---|---|---|---|"]
    for c in report["callbacks"]:
        cb = ", ".join(c["literals"] + [p + "*" for p in c["prefixes"]])
        lines.append(f"| `{c['handler']}` | `{cb}` | {c['policy'] or '—'} | {c['auth']} |")
    lines += ["", "## Modules", "", "| module | lines | handlers | catalog cmds | tables created | tests |", "|---|---|---|---|---|---|"]
    for m in report["modules"]:
        lines.append(f"| {m['module']} | {m['lines']} | {m['handler_total']} | {m['catalog_commands']} | "
                     f"{', '.join(m['tables_created']) or '—'} | {', '.join(m['tests']) or '—'} |")
    lines += ["", "## Not verified", ""] + [f"- {x}" for x in report["not_verified"]]
    return "\n".join(lines) + "\n"


def render_summary(report: dict, limit: int = 12) -> str:
    """Plain-text summary for the owner's private chat (no markup, so it is safe to send as is)."""
    s = report["summary"]
    c = s["findings"]
    lines = [
        f"Реестр Justice Faite v{report.get('version', '')}",
        f"Команд в каталоге: {s['catalog_commands']} (+{s['catalog_features']} функций); с обработчиком: {s['commands_routed']}, без: {s['commands_unrouted']}",
        "Хендлеры: " + ", ".join(f"{k}={v}" for k, v in sorted(s["handlers_by_observer"].items())),
        f"Callback-хендлеров: {s['callback_handlers']}; callback_data в коде: {s['callback_sites']}; пунктов меню: {s['menu_entries']}",
        f"Находки: ошибок {c['error']}, предупреждений {c['warning']}, на проверку {c['review']}, справочно {c['info']}",
    ]
    shown = [f for f in report["findings"] if f["severity"] in ("error", "warning", "review")][:limit]
    if shown:
        lines.append("")
        lines.append("Главное:")
        lines += [f"• {f['severity']} {f['code']} {f['subject']}: {f['message'][:160]}" for f in shown]
    lines.append("")
    lines.append("Полный отчёт — в приложенном JSON. Режим только чтение: настройки и хендлеры не менялись.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Wiring into the live application
# ---------------------------------------------------------------------------


def capture_runtime() -> dict:
    """Snapshot effective cmdcfg state for every catalog entry (in-memory reads only)."""
    import cmdcfg
    snapshot = {}
    for key in list(cmdcfg.REGISTRY):
        try:
            snapshot[key] = {"state": cmdcfg.command_state(key), "settings": cmdcfg.settings_for(key)}
        except Exception as exc:  # DB not bound or settings unreadable: report, do not fail
            snapshot[key] = {"state": None, "settings": None, "error": f"{type(exc).__name__}: {exc}"[:120]}
    return snapshot


def build_audit(dp: Any, *, owner_id: int, project_dir: Optional[str] = None, version: str = "",
                runtime: Optional[dict] = None, menu_commands: Iterable[str] = ()) -> RegistryAudit:
    import cmdcfg
    return RegistryAudit(
        dp, specs=cmdcfg.REGISTRY, owner_id=owner_id,
        project_dir=project_dir or os.path.dirname(os.path.abspath(__file__)),
        match_command=cmdcfg.match_command, callback_policy=cmdcfg.callback_command_for_data,
        categories=cmdcfg.CATEGORIES, version=version,
        runtime=capture_runtime() if runtime is None else runtime, menu_commands=menu_commands,
    )


class RegistryModule:
    """Owner-only, private-chat, read-only ``/jf_registry`` command."""

    def __init__(self, dp, bot, F, owner_id: int, version: str, project_dir: Optional[str] = None,
                 menu_commands: Iterable[str] = ()):
        self.dp, self.bot, self.F = dp, bot, F
        self.owner_id = int(owner_id or 0)
        self.version = version
        self.project_dir = project_dir
        self.menu_commands = tuple(menu_commands)
        self.registered = False

    def register(self) -> None:
        if self.registered or not self.owner_id:
            return
        from aiogram.filters import Command
        private_owner = (self.F.chat.type == "private") & (self.F.from_user.id == self.owner_id)
        self.dp.message.register(self.handle, Command("jf_registry"), private_owner)
        self.registered = True

    def _is_owner_private(self, message) -> bool:
        user = getattr(message, "from_user", None)
        chat = getattr(message, "chat", None)
        return bool(self.owner_id and user and chat and int(getattr(user, "id", 0) or 0) == self.owner_id
                    and getattr(chat, "type", None) == "private")

    def build_report(self) -> dict:
        audit = build_audit(self.dp, owner_id=self.owner_id, project_dir=self.project_dir, version=self.version,
                            menu_commands=self.menu_commands)
        return audit.run()

    async def build_report_async(self) -> dict:
        """Capture cmdcfg state on the loop, then run the file/regex-heavy audit in a worker thread."""
        import asyncio
        audit = build_audit(self.dp, owner_id=self.owner_id, project_dir=self.project_dir, version=self.version,
                            menu_commands=self.menu_commands)
        return await asyncio.to_thread(audit.run)

    async def handle(self, message) -> None:
        if not self._is_owner_private(message):
            return
        try:
            report = await self.build_report_async()
        except Exception as exc:  # never crash the dispatcher; tell the owner what failed
            await message.answer(f"Не удалось построить реестр: {type(exc).__name__}: {str(exc)[:200]}", parse_mode=None)
            return
        await message.answer(render_summary(report), parse_mode=None)
        import tempfile
        from aiogram.types import FSInputFile
        tmpdir = tempfile.mkdtemp(prefix="jf_registry_")
        path = os.path.join(tmpdir, f"jf_registry_{self.version or 'report'}.json")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=1, default=str)
            await self.bot.send_document(chat_id=int(message.chat.id), document=FSInputFile(path),
                                         caption="Полный отчёт реестра (только чтение)", disable_notification=True)
        finally:
            try:
                os.remove(path)
                os.rmdir(tmpdir)
            except OSError:
                pass


def register(dp, bot, F, owner_id: int, version: str, project_dir: Optional[str] = None,
             menu_commands: Iterable[str] = ()) -> RegistryModule:
    module = RegistryModule(dp, bot, F, owner_id, version, project_dir, menu_commands)
    module.register()
    return module

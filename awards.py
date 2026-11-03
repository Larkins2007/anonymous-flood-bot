"""Награды за срок в чате без общения бота с Ирис.

Ирис не реагирует на сообщения других ботов, поэтому схема такая:

1. Владелец пишет «олды» Ирис во флуде и пересылает ответ боту в ЛС
   (после /jf_awards или просто так — ответ Ирис бот узнаёт сам).
2. Бот разбирает список, сопоставляет строки с участниками, считает пороги
   (30 / 60 / 120 дней по умолчанию) и присылает владельцу в ЛС
   **по одному сообщению на каждую награду** — это готовая команда для Ирис.
3. Владелец пересылает эти сообщения во флуд. Кнопки под сообщением
   («Выдано» / «Пропустить») ведут учёт, чтобы награда не выдавалась дважды.

Чистая логика (разбор, сопоставление, план) не зависит от aiogram и покрыта тестами.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import re
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import cmdcfg
import ui

logger = logging.getLogger("awards")

# --------------------------------------------------------------------------
# Разбор сроков
# --------------------------------------------------------------------------

_TOKEN = re.compile(r"(?P<n>\d+(?:[.,]\d+)?)\s*(?P<u>[A-Za-zА-Яа-яЁё]+)\.?")
_SEPARATORS = re.compile(r"^[\s,;и&]*$", re.IGNORECASE)

UNIT_DAYS = (
    ("год", 365.0), ("лет", 365.0), ("мес", 30.0), ("нед", 7.0), ("дн", 1.0),
    ("ден", 1.0), ("час", 1 / 24), ("мин", 1 / 1440), ("сек", 1 / 86400),
)


def _unit_days(word: str) -> Optional[float]:
    w = (word or "").casefold().replace("ё", "е")
    if w in ("г", "y", "yr", "yrs", "years", "year"):
        return 365.0
    if w == "д" or w == "d":
        return 1.0
    if w == "ч" or w == "h":
        return 1 / 24
    for prefix, value in UNIT_DAYS:
        if w.startswith(prefix):
            return value
    return None


def parse_duration(text: str) -> Optional[tuple[float, int]]:
    """Находит срок в КОНЦЕ строки: «1 месяц 12 дн» → (days, позиция начала).

    Возвращает None, если срока нет. Числа в начале строки (нумерация) и в
    именах не считаются: берётся только последняя связная группа «число+единица».
    """
    raw = (text or "").rstrip(" \t.,;:!)»”")
    tokens = []
    for m in _TOKEN.finditer(raw):
        factor = _unit_days(m.group("u"))
        if factor is None:
            continue
        tokens.append((m, factor))
    if not tokens:
        return None
    # хвост строки: последний токен должен стоять в самом конце
    last_m, _ = tokens[-1]
    if raw[last_m.end():].strip(" .)»”"):
        return None
    group = [tokens[-1]]
    for m, factor in reversed(tokens[:-1]):
        between = raw[m.end():group[0][0].start()]
        if _SEPARATORS.match(between):
            group.insert(0, (m, factor))
        else:
            break
    total = 0.0
    for m, factor in group:
        total += float(m.group("n").replace(",", ".")) * factor
    return total, group[0][0].start()


def days_of(text: str) -> Optional[float]:
    parsed = parse_duration(text)
    return parsed[0] if parsed else None


# --------------------------------------------------------------------------
# Разбор строк и сущностей (mention / text_mention / ссылки)
# --------------------------------------------------------------------------

@dataclass
class Identity:
    kind: str            # "id" | "username" | "link"
    value: Any           # int | str(lower) | исходный URL
    username: str = ""   # для kind == "id": если известен


@dataclass
class OldEntry:
    raw: str
    name: str
    age_text: str
    days: float
    identities: list = field(default_factory=list)


def _etype(entity) -> str:
    t = getattr(entity, "type", "")
    return str(getattr(t, "value", t)).lower()


def _u16_positions(text: str) -> list[int]:
    pos = [0]
    for ch in text:
        pos.append(pos[-1] + (2 if ord(ch) > 0xFFFF else 1))
    return pos


_TG_ID = re.compile(r"tg://(?:user\?id=|openmessage\?user_id=)(\d+)", re.IGNORECASE)
_TME = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{4,31})(?:[/?#].*)?$",
    re.IGNORECASE,
)
_TG_RESOLVE = re.compile(r"^tg://resolve\?[^#]*?domain=([A-Za-z][A-Za-z0-9_]{4,31})(?:&|$)", re.IGNORECASE)


def identities_in_range(text: str, entities, start: int, end: int, u16: Optional[list[int]] = None) -> list[Identity]:
    """Кликабельные упоминания внутри строки [start, end) (индексы Python)."""
    result: list[Identity] = []
    if not entities:
        return result
    u16 = u16 or _u16_positions(text)
    for ent in entities:
        e_start = bisect_left(u16, int(ent.offset))
        e_end = bisect_left(u16, int(ent.offset) + int(ent.length))
        # Проверяем пересечение с текущей строкой, а не только начало entity.
        if e_end <= start or e_start >= end:
            continue
        kind = _etype(ent)
        if kind == "text_mention" and getattr(ent, "user", None) is not None:
            u = ent.user
            result.append(Identity("id", int(u.id), (getattr(u, "username", "") or "").lower()))
        elif kind == "text_link":
            url = str(getattr(ent, "url", "") or "")
            m = _TG_ID.search(url)
            if m:
                result.append(Identity("id", int(m.group(1))))
                continue
            m = _TME.match(url)
            if m:
                result.append(Identity("username", m.group(1).lower()))
                continue
            m = _TG_RESOLVE.match(url)
            if m:
                result.append(Identity("username", m.group(1).lower()))
                continue
            # Имя является кликабельной ссылкой, но её нельзя надёжно
            # преобразовать в Telegram ID/username. Не подменяем личность
            # совпадением по видимому тексту — это может выдать награду не тому.
            result.append(Identity("link", url))
        elif kind == "mention":
            result.append(Identity("username", text[e_start:e_end].lstrip("@").lower()))
    return result


_LEAD_JUNK = re.compile(r"^[\s\W_]*?(?:\d{1,3}\s*[.)\]:\-–—]\s*)?", re.UNICODE)


def clean_name(fragment: str) -> str:
    """Убирает нумерацию, маркеры, статистику активности и разделители."""
    s = fragment or ""
    s = re.sub(r"^\s*(?:[#№]?\s*\d{1,3}\s*[.)\]:\-–—]+\s*)", "", s)
    s = s.strip(" \t—–-:|•·,;.")
    # В ответе Ирис после кликабельного имени идёт статистика в скобках:
    # «belochki_rulyat (36,5k)» или «The Empress (902)».
    # Это не часть имени Telegram и мешает текстовому сопоставлению.
    s = re.sub(r"\s*\(\s*\d+(?:[.,]\d+)?\s*[kкм]?\s*\)\s*$", "", s, flags=re.IGNORECASE)
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat.startswith("S") and ch not in "@_":
            continue          # эмодзи и символы-украшения
        out.append(ch)
    return " ".join("".join(out).split()).strip(" \t—–-:|•·,;.")


def key_of(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", (value or "").casefold().replace("ё", "е"))


def parse_olds(text: str, entities=None) -> list[OldEntry]:
    """Строки ответа Ирис на «олды» → записи со сроком ≥ 1 минуты."""
    text = text or ""
    u16 = _u16_positions(text)
    entries: list[OldEntry] = []
    cursor = 0
    for raw_line in text.splitlines(True):
        line = raw_line.rstrip("\r\n")
        start, end = cursor, cursor + len(line)
        cursor += len(raw_line)
        stripped = line.strip()
        if not stripped:
            continue
        parsed = parse_duration(stripped)
        if not parsed:
            continue
        days, pos = parsed
        head = stripped[:pos]
        # сам срок: от позиции pos до конца (без хвостовой пунктуации)
        age_text = stripped[pos:].strip(" .,;:)»”")
        name = clean_name(head)
        identities = identities_in_range(text, entities, start, end, u16)
        if not name and not identities and not re.search(r"@\w", head):
            continue
        entries.append(OldEntry(raw=stripped, name=name, age_text=age_text, days=days, identities=identities))
    return entries


# --------------------------------------------------------------------------
# Сопоставление с участниками
# --------------------------------------------------------------------------

@dataclass
class Match:
    entry: OldEntry
    member: Optional[dict]
    status: str                      # ok | no_username | ambiguous | not_found | lookup
    uid_hint: Optional[int] = None   # id из кликабельной ссылки, которого нет в базе


def _member_keys(m: dict) -> set[str]:
    first, last = (m.get("first_name") or ""), (m.get("last_name") or "")
    keys = {
        key_of(f"{first} {last}"), key_of(first), key_of(last),
        key_of(m.get("username") or ""), key_of(m.get("role_name") or ""),
    }
    tag = m.get("tag") or ""
    keys.add(key_of(tag))
    return {k for k in keys if len(k) >= 3}


def match_entry(entry: OldEntry, members: list[dict]) -> Match:
    by_id = {int(m["user_id"]): m for m in members}
    by_username = {}
    for m in members:
        u = (m.get("username") or "").lower()
        if u:
            by_username.setdefault(u, []).append(m)

    # Кликабельная ссылка — источник идентичности, а не декоративный текст.
    # Сначала используем ID: он надёжнее username и видимого имени.
    ids = [ident for ident in entry.identities if ident.kind == "id"]
    for ident in ids:
        hit = by_id.get(int(ident.value))
        if hit:
            return _finish(entry, hit)
    if ids:
        # Не пытаться сопоставлять по подписи ссылки: она может не совпадать
        # с именем/username в Telegram и случайно указывать на другого человека.
        return Match(entry, None, "lookup", int(ids[0].value))

    # Если ID нет, используем точный username из entity/ссылки.
    linked_usernames = [ident for ident in entry.identities if ident.kind == "username"]
    for ident in linked_usernames:
        hits = by_username.get(str(ident.value).lower(), [])
        if len(hits) == 1:
            return _finish(entry, hits[0])
        if len(hits) > 1:
            return Match(entry, None, "ambiguous")
    if entry.identities:
        # Есть кликабельная ссылка, но её владелец не распознан. Нельзя
        # откатываться к нечёткому совпадению видимого текста.
        return Match(entry, None, "not_found")

    inline_user = re.search(r"@([A-Za-z][A-Za-z0-9_]{3,31})", entry.raw)
    if inline_user:
        hits = by_username.get(inline_user.group(1).lower(), [])
        if len(hits) == 1:
            return _finish(entry, hits[0])

    line_key = key_of(entry.name or entry.raw)
    if line_key:
        scored = []
        for m in members:
            keys = _member_keys(m)
            best = max((len(k) for k in keys if k and k in line_key), default=0)
            if best:
                scored.append((best, m))
        if scored:
            scored.sort(key=lambda x: -x[0])
            top = [m for score, m in scored if score == scored[0][0]]
            if len(top) == 1:
                return _finish(entry, top[0])
            return Match(entry, None, "ambiguous")

        fuzzy = []
        for m in members:
            for k in _member_keys(m):
                fuzzy.append((difflib.SequenceMatcher(None, k, line_key).ratio(), m))
        fuzzy.sort(key=lambda x: -x[0])
        if fuzzy and fuzzy[0][0] >= 0.82:
            runner = next((r for r, m in fuzzy[1:] if m["user_id"] != fuzzy[0][1]["user_id"]), 0.0)
            if fuzzy[0][0] - runner >= 0.08:
                return _finish(entry, fuzzy[0][1])
            return Match(entry, None, "ambiguous")
    return Match(entry, None, "not_found")


def _finish(entry: OldEntry, member: dict) -> Match:
    if not (member.get("username") or "").strip():
        return Match(entry, member, "no_username")
    return Match(entry, member, "ok")


# --------------------------------------------------------------------------
# План наград
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Tier:
    days: int
    level: int
    title: str

    @property
    def code(self) -> str:
        return f"iris_old_{self.days}d"


def tiers_from_cfg(get_int: Callable[[str, str], int], get_str: Callable[[str, str], str]) -> list[Tier]:
    result = []
    for n in (1, 2, 3):
        result.append(Tier(get_int("awards", f"t{n}_days"), get_int("awards", f"t{n}_level"), get_str("awards", f"t{n}_title")))
    result.sort(key=lambda t: t.days)
    seen, unique = set(), []
    for t in result:
        if t.days in seen:
            continue
        seen.add(t.days)
        unique.append(t)
    return unique


def eligible_tiers(days: float, tiers: list[Tier], mode: str) -> list[Tier]:
    hit = [t for t in tiers if days >= t.days]
    if not hit:
        return []
    return [hit[-1]] if mode == "best" else hit


def build_command(prefix: str, word: str, level: int, username: str, description: str) -> str:
    head = f"{prefix}{word} {int(level)} @{username.lstrip('@')}"
    description = (description or "").strip()
    return f"{head}\n{description}" if description else head


def describe(template: str, *, title: str, age: str, days: int, name: str) -> str:
    return ui.render_template(template, title=title, age=age, days=days, name=name).strip()


# --------------------------------------------------------------------------
# Хранилище очереди
# --------------------------------------------------------------------------

class Queue:
    def __init__(self, db_op: Callable, now: Callable[[], str]):
        self.q, self.now = db_op, now
        self.ensure()

    def ensure(self) -> None:
        def op(conn):
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jf_award_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    display TEXT NOT NULL DEFAULT '',
                    code TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    days INTEGER NOT NULL DEFAULT 0,
                    command_text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready',
                    source TEXT NOT NULL DEFAULT 'olds',
                    msg_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_award_queue_active
                    ON jf_award_queue(chat_id,user_id,code) WHERE source='olds';
                CREATE INDEX IF NOT EXISTS idx_award_queue_status ON jf_award_queue(chat_id,status);
            """)
            conn.commit()
        self.q(op)

    # -- чтение ---------------------------------------------------------
    def legacy_issued(self, chat_id: int, user_id: int, code: str) -> bool:
        """Старый журнал jf_award_audit (прежняя автоотправка) тоже считается выдачей."""
        def op(conn):
            try:
                return conn.execute(
                    "SELECT 1 FROM jf_award_audit WHERE chat_id=? AND user_id=? AND kind=? AND status IN ('sent','manual') LIMIT 1",
                    (chat_id, user_id, code)).fetchone() is not None
            except Exception:
                return False
        return bool(self.q(op))

    def exists(self, chat_id: int, user_id: int, code: str) -> Optional[str]:
        row = self.q(lambda conn: conn.execute(
            "SELECT status FROM jf_award_queue WHERE chat_id=? AND user_id=? AND code=? AND source='olds'",
            (chat_id, user_id, code)).fetchone())
        if row:
            return row["status"]
        return "issued" if self.legacy_issued(chat_id, user_id, code) else None

    def get(self, item_id: int):
        return self.q(lambda conn: conn.execute("SELECT * FROM jf_award_queue WHERE id=?", (int(item_id),)).fetchone())

    def list(self, chat_id: int, status: str, limit: int = 200):
        return self.q(lambda conn: conn.execute(
            "SELECT * FROM jf_award_queue WHERE chat_id=? AND status=? ORDER BY id LIMIT ?",
            (chat_id, status, limit)).fetchall())

    def counts(self, chat_id: int) -> dict:
        rows = self.q(lambda conn: conn.execute(
            "SELECT status,COUNT(*) AS n FROM jf_award_queue WHERE chat_id=? GROUP BY status", (chat_id,)).fetchall())
        return {r["status"]: int(r["n"]) for r in rows}

    def unsent(self, chat_id: int):
        return self.q(lambda conn: conn.execute(
            "SELECT * FROM jf_award_queue WHERE chat_id=? AND status='ready' AND msg_id IS NULL ORDER BY id",
            (chat_id,)).fetchall())

    # -- запись ---------------------------------------------------------
    def add(self, *, chat_id, user_id, username, display, code, level, title, days, command_text, source="olds") -> Optional[int]:
        stamp = self.now()

        def op(conn):
            if source == "olds":
                cur = conn.execute(
                    "INSERT OR IGNORE INTO jf_award_queue(chat_id,user_id,username,display,code,level,title,days,command_text,status,source,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?,?)",
                    (chat_id, user_id, username, display, code, level, title, days, command_text, source, stamp, stamp))
                conn.commit()
                return cur.lastrowid if cur.rowcount else None
            cur = conn.execute(
                "INSERT INTO jf_award_queue(chat_id,user_id,username,display,code,level,title,days,command_text,status,source,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'ready',?,?,?)",
                (chat_id, user_id, username, display, code, level, title, days, command_text, source, stamp, stamp))
            conn.commit()
            return cur.lastrowid
        return self.q(op)

    def set_status(self, item_id: int, status: str) -> None:
        self.q(lambda conn: (conn.execute(
            "UPDATE jf_award_queue SET status=?,updated_at=? WHERE id=?", (status, self.now(), int(item_id))), conn.commit()))

    def set_msg(self, item_id: int, msg_id: Optional[int]) -> None:
        self.q(lambda conn: (conn.execute(
            "UPDATE jf_award_queue SET msg_id=?,updated_at=? WHERE id=?", (msg_id, self.now(), int(item_id))), conn.commit()))

    def mark_all_issued(self, chat_id: int) -> int:
        def op(conn):
            n = conn.execute("UPDATE jf_award_queue SET status='issued',updated_at=? WHERE chat_id=? AND status='ready'",
                             (self.now(), chat_id)).rowcount
            conn.commit()
            return n
        return int(self.q(op))

    def drop_skipped(self, chat_id: int) -> int:
        def op(conn):
            n = conn.execute("DELETE FROM jf_award_queue WHERE chat_id=? AND status='skipped'", (chat_id,)).rowcount
            conn.commit()
            return n
        return int(self.q(op))

    def audit(self, chat_id: int, user_id: int, code: str, status: str, reason: str) -> None:
        """Пишем в прежний журнал, чтобы старые проверки «уже выдано» продолжали работать."""
        def op(conn):
            try:
                conn.execute(
                    "INSERT INTO jf_award_audit(chat_id,user_id,kind,status,reason,created_at,external_ref) VALUES(?,?,?,?,?,?,?)",
                    (chat_id, user_id, code, status, reason[:1000], self.now(), "manual-forward"))
                conn.commit()
            except Exception:
                logger.debug("audit table missing", exc_info=True)
        self.q(op)


# --------------------------------------------------------------------------
# Планирование: что именно нужно выдать
# --------------------------------------------------------------------------

@dataclass
class PlanItem:
    member: dict
    tier: Tier
    entry: OldEntry
    command: str
    description: str


@dataclass
class Plan:
    items: list = field(default_factory=list)
    already: int = 0
    pending: int = 0
    no_username: list = field(default_factory=list)
    unknown: list = field(default_factory=list)
    ambiguous: list = field(default_factory=list)
    young: int = 0
    total_lines: int = 0


def make_plan(matches: list[Match], *, chat_id: int, tiers: list[Tier], mode: str, prefix: str, word: str,
              template: str, queue: Queue) -> Plan:
    plan = Plan(total_lines=len(matches))
    seen: set[tuple[int, str]] = set()
    for match in matches:
        entry = match.entry
        if match.status == "no_username":
            if eligible_tiers(entry.days, tiers, mode):
                plan.no_username.append(ui.person(match.member) + f" — {entry.age_text}")
            continue
        if match.status == "ambiguous":
            plan.ambiguous.append(entry.name or entry.raw)
            continue
        if match.status in ("not_found", "lookup") or not match.member:
            if eligible_tiers(entry.days, tiers, mode):
                plan.unknown.append(entry.name or entry.raw)
            continue
        hits = eligible_tiers(entry.days, tiers, mode)
        if not hits:
            plan.young += 1
            continue
        member = match.member
        uid = int(member["user_id"])
        for tier in hits:
            if (uid, tier.code) in seen:
                continue
            seen.add((uid, tier.code))
            state = queue.exists(chat_id, uid, tier.code)
            if state == "ready":
                plan.pending += 1
                continue
            if state:
                plan.already += 1
                continue
            description = describe(template, title=tier.title, age=entry.age_text, days=int(entry.days), name=ui.person(member))
            command = build_command(prefix, word, tier.level, member["username"], description)
            plan.items.append(PlanItem(member, tier, entry, command, description))
    return plan


# ==========================================================================
# Telegram-слой: команды, пересылка «олдов», кнопки
# ==========================================================================

from aiogram import F  # noqa: E402
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter  # noqa: E402
from aiogram.filters import Command  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.state import State, StatesGroup  # noqa: E402
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message  # noqa: E402

MAX_PER_RUN = 40
SEND_DELAY = 0.35


class AwardsState(StatesGroup):
    waiting_olds = State()


class Ctx:
    bot: Any = None
    owner_id: int = 0
    chat_id: int = 0
    queue: Optional[Queue] = None
    active_members: Callable[[], list] = staticmethod(lambda: [])
    lookup_member: Optional[Callable] = None
    issue_local_award: Optional[Callable] = None


CTX = Ctx()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def forward_username(message) -> str:
    origin = getattr(message, "forward_origin", None)
    sender = getattr(origin, "sender_user", None) if origin is not None else None
    if sender is None:
        sender = getattr(message, "forward_from", None)
    return (getattr(sender, "username", "") or "").lower() if sender is not None else ""


def is_iris_forward(message) -> bool:
    user = getattr(message, "from_user", None)
    if not user or int(user.id) != CTX.owner_id:
        return False
    iris = cmdcfg.get_str("awards", "iris_username").lstrip("@").lower()
    return bool(iris and forward_username(message) == iris)


def _queue_markup(item_id: int):
    if not cmdcfg.get_bool("awards", "marks"):
        return None
    return _kb([_btn("✅ Выдано", f"aw:done:{item_id}"), _btn("⏭ Пропустить", f"aw:skip:{item_id}")])


async def _send_plain(chat_id: int, text: str, markup=None):
    for attempt in (1, 2):
        try:
            return await CTX.bot.send_message(chat_id, text, reply_markup=markup)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(getattr(exc, "retry_after", 1) or 1) + 0.5)
        except Exception:
            logger.exception("award command send failed")
            return None
    return None


async def send_items(chat_id: int, rows, limit: int = MAX_PER_RUN) -> int:
    """Шлёт готовые команды по одному сообщению. Возвращает, сколько отправлено."""
    sent = 0
    for row in rows:
        if sent >= limit:
            break
        message = await _send_plain(chat_id, row["command_text"], _queue_markup(int(row["id"])))
        if message is not None:
            CTX.queue.set_msg(int(row["id"]), int(message.message_id))
            sent += 1
        await asyncio.sleep(SEND_DELAY)
    return sent


def _tiers_text() -> str:
    tiers = cfg_tiers()
    mode = cmdcfg.get_str("awards", "mode")
    parts = [f"{t.days} дн → степень {t.level}" for t in tiers]
    return " · ".join(parts) + (" (только высший)" if mode == "best" else "")


def cfg_tiers() -> list:
    return tiers_from_cfg(cmdcfg.get_int, cmdcfg.get_str)


def _intro_markup(counts: dict):
    ready = counts.get("ready", 0)
    return _kb(
        [_btn(f"📦 Очередь ({ready})", "aw:q")],
        [_btn("⚙️ Пороги и тексты", "cs:cmd:awards"), _btn("✔️ Готово", "aw:end")],
    )


def intro_text(counts: dict) -> str:
    return ui.card(
        "Награды за срок в чате",
        ui.lines(
            ui.section("Как выдать награды"),
            ui.bullet("1. Во флуде напишите Ирис «олды»."),
            ui.bullet("2. Перешлите её ответ сюда, в этот чат."),
            ui.bullet("3. Бот пришлёт готовые команды — по одной на награду."),
            ui.bullet("4. Пересылайте их во флуд и жмите «Выдано»."),
        ),
        ui.lines(ui.section("Сейчас настроено"), ui.bullet(_tiers_text())),
        ui.lines(ui.note(f"Ждём пересланное сообщение. В очереди: {counts.get('ready', 0)}, выдано: {counts.get('issued', 0)}.")),
    )


async def cmd_awards(message: Message, state: FSMContext):
    if not await cmdcfg.gate(message, "awards"):
        return
    await state.set_state(AwardsState.waiting_olds)
    counts = CTX.queue.counts(CTX.chat_id)
    await message.answer(intro_text(counts), reply_markup=_intro_markup(counts))


def _entities_of(message):
    return getattr(message, "entities", None) or getattr(message, "caption_entities", None) or []


async def process_olds(message: Message, state: FSMContext):
    text = message.text or getattr(message, "caption", None) or ""
    entries = parse_olds(text, _entities_of(message))
    if not entries:
        await message.answer(ui.card(
            "Не вижу списка",
            ui.lines(ui.bullet("В сообщении нет строк вида «имя — 1 месяц 12 дн»."),
                     ui.bullet("Перешлите именно ответ Ирис на «олды».")),
        ))
        return
    members = [dict(m) for m in CTX.active_members()]
    matches = [match_entry(e, members) for e in entries]
    for i, match in enumerate(matches):
        if match.status == "lookup" and CTX.lookup_member is not None:
            found = await CTX.lookup_member(match.uid_hint)
            if found:
                matches[i] = _finish(match.entry, dict(found))
    cfg = cmdcfg
    plan = make_plan(
        matches, chat_id=CTX.chat_id, tiers=cfg_tiers(), mode=cfg.get_str("awards", "mode"),
        prefix=cfg.get_str("awards", "prefix"), word=cfg.get_str("awards", "command_word"),
        template=cfg.get_str("awards", "desc_template"), queue=CTX.queue,
    )
    created = []
    for item in plan.items:
        member = item.member
        qid = CTX.queue.add(
            chat_id=CTX.chat_id, user_id=int(member["user_id"]), username=member["username"].lstrip("@"),
            display=ui.person(member), code=item.tier.code, level=item.tier.level, title=item.tier.title,
            days=int(item.entry.days), command_text=item.command,
        )
        if qid:
            created.append(qid)

    rows = [CTX.queue.get(q) for q in created]
    summary = [
        ui.section("Разбор списка"),
        ui.kv("Строк со сроком", plan.total_lines),
        ui.kv("Новых команд", len(created)),
    ]
    if plan.pending:
        summary.append(ui.kv("Уже ждут в очереди", plan.pending))
    if plan.already:
        summary.append(ui.kv("Уже выдано раньше", plan.already))
    if plan.young:
        summary.append(ui.kv("Ещё не дошли до порога", plan.young))
    extra = []
    if plan.no_username:
        extra.append(ui.lines(ui.section("Нет @username — выдайте вручную"), *[ui.bullet(x) for x in plan.no_username[:10]]))
    if plan.unknown or plan.ambiguous:
        names = [x for x in (plan.unknown + plan.ambiguous)[:10]]
        extra.append(ui.lines(ui.section("Не удалось определить участника"), *[ui.bullet(ui.short(x, 50)) for x in names]))
    footer = ui.note("Пересылайте сообщения ниже во флуд.") if created else None
    await message.answer(ui.card("Награды · результат", ui.lines(*summary), *extra, footer=footer))

    if rows:
        sent = await send_items(message.chat.id, rows)
        left = len(rows) - sent
        if left > 0:
            await message.answer(
                ui.card("Остальное — по кнопке", ui.bullet(f"Отправлено {sent}, осталось {left}.")),
                reply_markup=_kb([_btn(f"📤 Прислать остальные ({left})", "aw:rest")]),
            )


async def olds_in_state(message: Message, state: FSMContext):
    await process_olds(message, state)


async def olds_forward(message: Message, state: FSMContext):
    await process_olds(message, state)


# --------------------------------------------------------------------------
# Ручная награда
# --------------------------------------------------------------------------

_AWARD_RE = re.compile(r"^/jf_award(?:@\w+)?\s+(\S+)(?:\s+([1-8])(?=\s|$))?(?:\s+(.+))?$", re.DOTALL)


async def cmd_award(message: Message):
    if not await cmdcfg.gate(message, "award"):
        return
    m = _AWARD_RE.match((message.text or "").strip())
    if not m:
        await message.answer(ui.card(
            "Выдать награду вручную",
            ui.lines(ui.bullet("/jf_award @username 5 за активность"),
                     ui.bullet("Степень и описание необязательны."),
                     ui.bullet("Бот пришлёт готовую команду для Ирис — перешлите её во флуд.")),
        ))
        return
    target, level, description = m.group(1), m.group(2), (m.group(3) or "").strip()
    level = int(level) if level else cmdcfg.get_int("award", "default_level")
    members = [dict(x) for x in CTX.active_members()]
    member = None
    clean = ui.clean_username(target)
    if clean:
        member = next((x for x in members if (x.get("username") or "").lower() == clean.lower()), None)
    elif target.lstrip("-").isdigit():
        member = next((x for x in members if int(x["user_id"]) == int(target)), None)
    if not member:
        await message.answer(ui.fail("Участник не найден среди активных участников флуда."))
        return
    if not (member.get("username") or "").strip():
        await message.answer(ui.fail("У участника нет @username — Ирис не сможет принять команду."))
        return
    description = description or "🏵 Награда от Justice Faite"
    command = build_command(cmdcfg.get_str("awards", "prefix"), cmdcfg.get_str("awards", "command_word"),
                            level, member["username"], description)
    code = f"manual_{int(__import__('time').time())}"
    qid = CTX.queue.add(
        chat_id=CTX.chat_id, user_id=int(member["user_id"]), username=member["username"].lstrip("@"),
        display=ui.person(member), code=code, level=level, title=ui.short(description, 80), days=0,
        command_text=command, source="manual",
    )
    if CTX.issue_local_award:
        try:
            CTX.issue_local_award(CTX.chat_id, int(member["user_id"]), code, ui.short(description, 80), description, CTX.owner_id, "manual")
        except Exception:
            logger.exception("local award record failed")
    await message.answer(ui.card("Награда готова", ui.bullet(f"{ui.person(member)} · степень {level}"),
                                 footer=ui.note("Перешлите сообщение ниже во флуд.")))
    await send_items(message.chat.id, [CTX.queue.get(qid)])


# --------------------------------------------------------------------------
# Кнопки
# --------------------------------------------------------------------------

def queue_text(counts: dict, rows) -> str:
    body = [ui.kv("Ждут выдачи", counts.get("ready", 0)), ui.kv("Выдано", counts.get("issued", 0)),
            ui.kv("Пропущено", counts.get("skipped", 0))]
    listing = []
    for r in rows[:15]:
        listing.append(ui.bullet(f"{r['display']} — степень {r['level']} · {ui.short(r['title'], 40)}"))
    if len(rows) > 15:
        listing.append(ui.bullet(f"… и ещё {len(rows) - 15}"))
    return ui.card("Очередь наград", ui.lines(*body), ui.lines(*listing) if listing else ui.note("Очередь пуста."))


def queue_markup(counts: dict):
    rows = []
    if counts.get("ready"):
        rows.append([_btn("🔁 Прислать заново", "aw:resend"), _btn("✅ Всё выдано", "aw:alldone")])
    if counts.get("skipped"):
        rows.append([_btn("🧹 Сбросить пропущенные", "aw:clear")])
    rows.append([_btn("‹ Назад", "aw:home")])
    return _kb(*rows)


async def _safe_edit(callback: CallbackQuery, text: str, markup=None):
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


async def on_callback(callback: CallbackQuery, state: FSMContext):
    if int(callback.from_user.id) != CTX.owner_id:
        await callback.answer("Только для владельца.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    chat_id = CTX.chat_id
    q = CTX.queue

    if action in ("done", "skip") and len(parts) == 3 and parts[2].isdigit():
        item = q.get(int(parts[2]))
        if not item:
            await callback.answer("Запись не найдена.", show_alert=True)
            return
        if action == "done":
            q.set_status(item["id"], "issued")
            q.audit(chat_id, int(item["user_id"]), item["code"], "manual", item["title"])
            await callback.answer("Отмечено как выданное.")
            label = "✅ Выдано"
        else:
            q.set_status(item["id"], "skipped")
            await callback.answer("Пропущено.")
            label = "⏭ Пропущено"
        try:
            await callback.message.edit_reply_markup(reply_markup=_kb([_btn(label, f"aw:noop:{item['id']}")]))
        except TelegramBadRequest:
            pass
        return
    if action == "noop":
        await callback.answer()
        return
    if action == "q":
        counts = q.counts(chat_id)
        await _safe_edit(callback, queue_text(counts, q.list(chat_id, "ready")), queue_markup(counts))
        await callback.answer()
        return
    if action == "home":
        counts = q.counts(chat_id)
        await state.set_state(AwardsState.waiting_olds)
        await _safe_edit(callback, intro_text(counts), _intro_markup(counts))
        await callback.answer()
        return
    if action == "resend":
        rows = q.list(chat_id, "ready")
        await callback.answer(f"Присылаю {min(len(rows), MAX_PER_RUN)}…")
        await send_items(callback.message.chat.id, rows)
        return
    if action == "rest":
        rows = q.unsent(chat_id)
        await callback.answer(f"Присылаю {min(len(rows), MAX_PER_RUN)}…")
        await send_items(callback.message.chat.id, rows)
        return
    if action == "alldone":
        n = q.mark_all_issued(chat_id)
        await callback.answer(f"Отмечено выданными: {n}")
        counts = q.counts(chat_id)
        await _safe_edit(callback, queue_text(counts, q.list(chat_id, "ready")), queue_markup(counts))
        return
    if action == "clear":
        n = q.drop_skipped(chat_id)
        await callback.answer(f"Сброшено: {n}")
        counts = q.counts(chat_id)
        await _safe_edit(callback, queue_text(counts, q.list(chat_id, "ready")), queue_markup(counts))
        return
    if action == "end":
        await state.clear()
        await callback.answer("Готово.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        return
    await callback.answer()


def register(dp, *, bot, owner_id: int, chat_id: int, db_op: Callable, now: Callable[[], str],
             active_members: Callable[[], list], lookup_member: Optional[Callable] = None,
             issue_local_award: Optional[Callable] = None) -> None:
    CTX.bot, CTX.owner_id, CTX.chat_id = bot, int(owner_id), int(chat_id)
    CTX.queue = Queue(db_op, now)
    CTX.active_members = active_members
    CTX.lookup_member = lookup_member
    CTX.issue_local_award = issue_local_award

    private_owner = (F.chat.type == "private") & (F.from_user.id == int(owner_id))
    dp.message.register(cmd_awards, Command("jf_awards", "jf_iris_sync"), private_owner)
    dp.message.register(cmd_award, Command("jf_award"), private_owner)
    dp.message.register(olds_in_state, AwardsState.waiting_olds, private_owner, F.text, ~F.text.startswith("/"))
    dp.message.register(olds_forward, private_owner, F.text, ~F.text.startswith("/"), is_iris_forward)
    dp.callback_query.register(on_callback, F.data.startswith("aw:"))

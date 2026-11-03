"""Единый визуальный стиль бота и мелкие текстовые/временные утилиты.

Стиль: компактные карточки — заголовок с эмодзи, пустая строка между блоками,
короткие пункты с эмодзи. Декоративные горизонтальные линии отключены.
Только обычный Unicode — никакой разметки, поэтому пользовательский текст безопасен.

Модуль не зависит от aiogram, поэтому его можно тестировать офлайн.
"""
from __future__ import annotations

import html
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

# Deliberately empty: project-wide messages use spacing, not horizontal rules.
RULE = ""

ICON_ON = "🟢"
ICON_OFF = "⚫"
ICON_WARN = "⚠️"
ICON_OK = "✅"
ICON_FAIL = "❌"


# --------------------------------------------------------------------------
# Базовые «кирпичики» оформления
# --------------------------------------------------------------------------

def title(text: str) -> str:
    """Заголовок карточки без декоративной линии."""
    return f"✨ {text}"


def section(text: str) -> str:
    return f"🔹 {text}"


def bullet(text: str) -> str:
    return f"🌿 {text}"


def note(text: str) -> str:
    return f"💡 {text}"


def divider() -> str:
    """Совместимость со старым кодом: визуальные разделители отключены."""
    return ""


def warning(text: str) -> str:
    return f"{ICON_WARN} {text}"


def ok(text: str) -> str:
    return f"{ICON_OK} {text}"


def fail(text: str) -> str:
    return f"{ICON_FAIL} {text}"


def kv(label: str, value) -> str:
    return f"🔸 {label}: {value}"


def onoff(flag: bool) -> str:
    return ICON_ON if flag else ICON_OFF


def card(head: str, *blocks: Optional[str], footer: Optional[str] = None) -> str:
    """Собирает карточку: заголовок, пустая строка, блоки, подвал."""
    parts = [title(head)]
    for block in blocks:
        if block is None or block == "":
            continue
        parts.append(block if isinstance(block, str) else "\n".join(block))
    text = "\n\n".join(parts)
    if footer:
        text += f"\n\n{footer}"
    return text


def lines(*items: Optional[str]) -> str:
    return "\n".join(i for i in items if i)


# --------------------------------------------------------------------------
# Текст
# --------------------------------------------------------------------------

def short(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def esc(text) -> str:
    """HTML-экранирование (для редких сообщений с parse_mode=HTML)."""
    return html.escape(str(text if text is not None else ""), quote=False)


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def count_word(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {plural(n, one, few, many)}"


def days_word(n: int) -> str:
    return count_word(n, "день", "дня", "дней")


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render_template(template: str, **values) -> str:
    """Подставляет {name}-плейсхолдеры, неизвестные оставляет как есть."""
    try:
        return (template or "").format_map(_SafeDict({k: ("" if v is None else v) for k, v in values.items()}))
    except Exception:
        return template or ""


def split_chunks(text: str, limit: int = 3800) -> list[str]:
    """Режет длинный текст по строкам, чтобы влезть в лимит Telegram."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------
# Время
# --------------------------------------------------------------------------

def tz_offset_hours() -> int:
    try:
        return max(-12, min(14, int(os.getenv("DEFAULT_TIMEZONE_OFFSET_HOURS", "3"))))
    except ValueError:
        return 3


def local_tz() -> timezone:
    return timezone(timedelta(hours=tz_offset_hours()))


def local_now() -> datetime:
    return datetime.now(local_tz())


def parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_date(value) -> str:
    dt = parse_iso(value) if not isinstance(value, datetime) else value
    if not dt:
        return "—"
    return dt.astimezone(local_tz()).strftime("%d.%m.%Y")


def fmt_datetime(value) -> str:
    dt = parse_iso(value) if not isinstance(value, datetime) else value
    if not dt:
        return "—"
    return dt.astimezone(local_tz()).strftime("%d.%m.%Y %H:%M")


def left_text(until, now: Optional[datetime] = None) -> str:
    """«осталось 3 дня» / «осталось 5 ч» / «истёк»."""
    end = parse_iso(until) if not isinstance(until, datetime) else until
    if not end:
        return "—"
    now = now or datetime.now(timezone.utc)
    delta = end - now
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "истёк"
    days, rem = divmod(secs, 86400)
    hours = rem // 3600
    if days:
        return f"{days_word(days)}" + (f" {hours} ч" if hours and days < 3 else "")
    if hours:
        return f"{hours} ч"
    return f"{max(1, rem // 60)} мин"


# --------------------------------------------------------------------------
# Пользователи
# --------------------------------------------------------------------------

def person(row_or_user) -> str:
    """@username или имя; принимает dict/Row/объект пользователя."""
    def get(name, default=""):
        if isinstance(row_or_user, dict):
            return row_or_user.get(name, default)
        try:
            return row_or_user[name]
        except Exception:
            return getattr(row_or_user, name, default)

    username = (get("username") or "").strip()
    if username:
        return "@" + username.lstrip("@")
    first = (get("first_name") or "").strip()
    last = (get("last_name") or "").strip()
    name = (first + " " + last).strip()
    return name or str(get("user_id", "") or get("id", "") or "участник")


USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")


def clean_username(value: str) -> Optional[str]:
    m = USERNAME_RE.match((value or "").strip())
    return m.group(1) if m else None


def flatten(rows: Iterable[Iterable]) -> list:
    return [x for row in rows for x in row]

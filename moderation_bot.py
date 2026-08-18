import asyncio
import json
import logging
import os
import re
import sqlite3
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))
INFO_CHANNEL_ID = int(os.getenv("INFO_CHANNEL_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_MENTION = os.getenv("ADMIN_MENTION", "").strip()
def parse_message_ids(raw: str) -> list[int]:
    values = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            with suppress(ValueError):
                values.append(int(part))
    return values[:2]

INFO_MESSAGE_IDS = parse_message_ids(os.getenv("INFO_MESSAGE_IDS", "40,41"))
while len(INFO_MESSAGE_IDS) < 2:
    INFO_MESSAGE_IDS.append(0)
ROLE_ASSIGNMENT_TIMEOUT = int(os.getenv("ROLE_ASSIGNMENT_TIMEOUT", "120"))
DEFAULT_TAKEN_MARKER = os.getenv("DEFAULT_TAKEN_MARKER", "💛")
WELCOME_DELETE_AFTER = int(os.getenv("WELCOME_DELETE_AFTER", "0"))
DB_PATH = os.getenv("DB_PATH", "moderation.db").strip() or "moderation.db"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not GROUP_CHAT_ID:
    raise RuntimeError("GROUP_CHAT_ID is not set")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is not set")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("flood-moderator")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ---------------------------------------------------------------------------
# ROLE CATALOG
# ---------------------------------------------------------------------------
# The Russian label is what the admin types. english_name is the canonical
# English form used to build the Telegram member tag.
# source_note marks names that are lore/OC labels rather than standard playable
# character labels; the user's supplied list is preserved in full.

ROLE_ROWS = [
    ("Мондштадт", [
        ("Альбедо", "Albedo", "💛"), ("Барбара", "Barbara", "💛"), ("Беннет", "Bennett", ""),
        ("Варка", "Varka", "💛"), ("Венти", "Venti", "💛"), ("Далия", "Dahlia", ""),
        ("Джинн", "Jean", ""), ("Дилюк", "Diluc", "💛"), ("Диона", "Diona", "💛"),
        ("Дурин", "Durin", "💛"), ("Кли", "Klee", "💛"), ("Кэйя", "Kaeya", "💛"),
        ("Лиза", "Lisa", ""), ("Лоэн", "Lohen", "💛"), ("Мика", "Mika", ""),
        ("Мона", "Mona", ""), ("Ноэлль", "Noelle", "💛"), ("Прюн", "Prune", ""),
        ("Рейзор", "Razor", ""), ("Розария", "Rosaria", ""), ("Сахароза", "Sucrose", ""),
        ("Фишль", "Fischl", "💛"), ("Эмбер", "Amber", ""), ("Эола", "Eula", "🧡"),
    ]),
    ("Ли Юэ", [
        ("Бай Чжу", "Baizhu", "💛"), ("Бэй Доу", "Beidou", ""), ("Гань Юй", "Ganyu", ""),
        ("Е Лань", "Yelan", ""), ("Ка Мин", "Gaming", "💛"), ("Кэ Цин", "Keqing", ""),
        ("Лань Янь", "Lan Yan", ""), ("Нин Гуан", "Ningguang", ""), ("Син Цю", "Xingqiu", ""),
        ("Синь Янь", "Xinyan", ""), ("Сян Лин", "Xiangling", ""), ("Сянь Юнь", "Xianyun", ""),
        ("Сяо", "Xiao", "💛"), ("Ху Тао", "Hu Tao", "💛"), ("Цзы Бай", "Zibai", ""),
        ("Ци Ци", "Qiqi", "💛"), ("Чжун Ли", "Zhongli", "💛"), ("Чунь Юнь", "Chongyun", ""),
        ("Шэнь Хэ", "Shenhe", ""), ("Юнь Цзинь", "Yun Jin", ""), ("Янь Фэй", "Yanfei", ""),
        ("Яо Яо", "Yaoyao", ""),
    ]),
    ("Инадзума", [
        ("Аратаки Итто", "Arataki Itto", ""), ("Аяка", "Ayaka", "💛"), ("Аято", "Ayato", "💛"),
        ("Горо", "Gorou", ""), ("Ёимия", "Yoimiya", ""), ("Кадзуха", "Kazuha", ""),
        ("Кирара", "Kirara", ""), ("Кокоми", "Kokomi", ""), ("Мидзуки", "Mizuki", ""),
        ("Райдэн Эи", "Raiden", ""), ("Сара", "Sara", ""), ("Саю", "Sayu", ""),
        ("Синобу", "Shinobu", ""), ("Тома", "Thoma", "💛"), ("Хэйдзо", "Heizou", "💛"),
        ("Яэ Мико", "Yae Miko", ""),
    ]),
    ("Сумеру", [
        ("Аль-Хайтам", "Alhaitham", "💛"), ("Дори", "Dori", ""), ("Дэхья", "Dehya", ""),
        ("Кавех", "Kaveh", "💛"), ("Кандакия", "Candace", ""), ("Коллеи", "Collei", ""),
        ("Лайла", "Layla", ""), ("Нахида", "Nahida", ""), ("Нилу", "Nilou", ""),
        ("Сайно", "Cyno", "💛"), ("Сетос", "Sethos", ""), ("Странник", "Wanderer", "💛"),
        ("Тигнари", "Tighnari", ""), ("Фарузан", "Faruzan", ""),
    ]),
    ("Фонтейн", [
        ("Клоринда", "Clorinde", ""), ("Лини", "Lyney", "💛"), ("Линетт", "Lynette", ""),
        ("Навия", "Navia", "💛"), ("Нёвиллет", "Neuvillette", "💛"), ("Ризли", "Wriothesley", "💛"),
        ("Сиджвин", "Sigewinne", ""), ("Тиори", "Chiori", "💛"), ("Фремине", "Freminet", ""),
        ("Фурина", "Furina", "💚"), ("Шарлотта", "Charlotte", ""), ("Эмилия", "Emilie", ""),
        ("Эскофье", "Escoffier", ""),
    ]),
    ("Натлан", [
        ("Вареса", "Varesa", ""), ("Иансан", "Iansan", ""), ("Ифа", "Ifa", ""),
        ("Качина", "Kachina", ""), ("Кинич", "Kinich", ""), ("Мавуика", "Mavuika", ""),
        ("Муалани", "Mualani", ""), ("Оророн", "Ororon", ""), ("Ситлали", "Citlali", ""),
        ("Часка", "Chasca", ""), ("Шилонен", "Xilonen", ""),
    ]),
    ("Нод-Край", [
        ("Айно", "Aino", ""), ("Иллуги", "Illuga", "💛"), ("Инеффа", "Ineffa", ""),
        ("Коломбина", "Columbina", ""), ("Лаума", "Lauma", "💛"), ("Линнея", "Linnea", ""),
        ("Нефер", "Nefer", ""), ("Флинс", "Flins", "💛"), ("Ягода", "Yagoda", ""),
    ]),
    ("Снежная", [
        ("Алёша", "Alyosha", ""), ("Арлекино", "Arlecchino", ""), ("Валера", "Valera", ""),
        ("Весна", "Vesna", ""), ("Водяница", "Vodyanitsa", "💛"), ("Даника", "Danika", ""),
        ("Дотторе", "Dottore", "💛"), ("Капитано", "Capitano", "💛"), ("Митя", "Mitya", "💛"),
        ("Ной", "Noah", "💛"), ("Одетта", "Odette", ""), ("Панталоне", "Pantalone", ""),
        ("Пьеро", "Pierro", ""), ("Пульчинелла", "Pulcinella", ""), ("Сандроне", "Sandrone", ""),
        ("Синьора", "Signora", ""), ("Тарталья", "Tartaglia", "💛"), ("Царица", "Tsaritsa", ""),
    ]),
    ("Каэнри'ах", [
        ("Ведрфельнир", "Vedrfolnir", ""), ("Дайнслейф", "Dainsleif", "💛"), ("Рери", "Rerir", ""),
        ("Сурталоги", "Surtalogi", ""), ("Толиндис", "Tholindis", ""), ("Хальфдан", "Halfdan", ""),
        ("Хрофтатюр", "Hroptatyr", ""),
    ]),
    ("Шабаш", [
        ("Алиса", "Alice", ""), ("Андерсдоттер", "Andersdotter", ""), ("Барбелот", "Barbeloth", ""),
        ("Николь Рейн", "Nicole Reine", ""), ("Октавия", "Octavia", ""), ("Рэйндоттир", "Rhinedottir", "💛"),
    ]),
    ("Тени", [
        ("Астарот", "Istaroth", ""), ("Асмодей", "Asmoday", ""), ("Набериус", "Naberius", ""), ("Ронова", "Ronova", ""),
    ]),
    ("Другое", [
        ("Итэр", "Aether", "💛"), ("Люмин", "Lumine", "💛"), ("Паймон", "Paimon", ""), ("Скирк", "Skirk", ""),
    ]),
]


def normalize_role_name(value: str) -> str:
    value = value.strip().casefold()
    value = re.sub(r"\s+", " ", value)
    return value


def math_italic(text: str) -> str:
    upper = {
        "A":"𝑨","B":"𝑩","C":"𝑪","D":"𝑫","E":"𝑬","F":"𝑭","G":"𝑮","H":"𝑯","I":"𝑰","J":"𝑱","K":"𝑲","L":"𝑳","M":"𝑴","N":"𝑵","O":"𝑶","P":"𝑷","Q":"𝑸","R":"𝑹","S":"𝑺","T":"𝑻","U":"𝑼","V":"𝑽","W":"𝑾","X":"𝑿","Y":"𝒀","Z":"𝒁",
    }
    lower = {
        "a":"𝒂","b":"𝒃","c":"𝒄","d":"𝒅","e":"𝒆","f":"𝒇","g":"𝒈","h":"𝒉","i":"𝒊","j":"𝒋","k":"𝒌","l":"𝒍","m":"𝒎","n":"𝒏","o":"𝒐","p":"𝒑","q":"𝒒","r":"𝒓","s":"𝒔","t":"𝒕","u":"𝒖","v":"𝒗","w":"𝒘","x":"𝒙","y":"𝒚","z":"𝒛",
    }
    return "".join(upper.get(ch, lower.get(ch, ch)) for ch in text)


def make_tag(english_name: str) -> str:
    tag = f"❦{math_italic(english_name)}❦"
    if len(tag) > 16:
        compact = english_name.replace(" ", "")
        tag = f"❦{math_italic(compact)}❦"
    if len(tag) > 16:
        raise ValueError(f"Role tag exceeds Telegram 16-char limit: {english_name!r} -> {tag!r}")
    return tag

ROLES = {}
for region, items in ROLE_ROWS:
    for ru_name, en_name, initial_marker in items:
        key = normalize_role_name(ru_name)
        ROLES[key] = {
            "key": key,
            "ru_name": ru_name,
            "english_name": en_name,
            "tag": make_tag(en_name),
            "region": region,
            "initial_marker": initial_marker,
        }

# Useful aliases / transliterations that people commonly type.
ALIASES = {
    normalize_role_name("рейден") : "райдэн еи",
    normalize_role_name("райден") : "райдэн еи",
    normalize_role_name("альхайтам") : "аль-Хайтам",
    normalize_role_name("аль хайтам") : "аль-Хайтам",
    normalize_role_name("кадзуha") : "кадзуха",
    normalize_role_name("zhongli") : "чжун ли",
    normalize_role_name("navia") : "навия",
    normalize_role_name("alhaitham") : "аль-Хайтам",
}
for alias, target in list(ALIASES.items()):
    canonical = normalize_role_name(target)
    if canonical in ROLES:
        ALIASES[alias] = canonical

# Fail fast on role-tag constraints during startup.
for role in ROLES.values():
    if len(role["tag"]) > 16:
        raise RuntimeError(f"Invalid tag length for {role['ru_name']}")

REGION_ORDER = [region for region, _ in ROLE_ROWS]

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

DB_LOCK = asyncio.Lock()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    conn = db_connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                role_key TEXT NOT NULL DEFAULT '',
                role_name TEXT NOT NULL DEFAULT '',
                tag TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'left',
                joined_at TEXT,
                confirmed_at TEXT,
                left_at TEXT,
                welcome_message_id INTEGER,
                join_token TEXT,
                PRIMARY KEY(chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS member_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                role_key TEXT NOT NULL DEFAULT '',
                role_name TEXT NOT NULL DEFAULT '',
                tag TEXT NOT NULL DEFAULT '',
                occurred_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS roles (
                role_key TEXT PRIMARY KEY,
                ru_name TEXT NOT NULL,
                english_name TEXT NOT NULL,
                tag TEXT NOT NULL,
                region TEXT NOT NULL,
                initial_marker TEXT NOT NULL DEFAULT '',
                display_marker TEXT NOT NULL DEFAULT '',
                assigned_user_id INTEGER,
                assigned_chat_id INTEGER,
                managed_by_bot INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS list_templates (
                message_slot INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                template_text TEXT NOT NULL,
                template_entities TEXT NOT NULL DEFAULT '[]',
                captured_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_members_chat_state
            ON members(chat_id, state);

            CREATE INDEX IF NOT EXISTS idx_member_events_chat_time
            ON member_events(chat_id, occurred_at);
            """
        )
        for role in ROLES.values():
            conn.execute(
                """
                INSERT INTO roles (
                    role_key, ru_name, english_name, tag, region,
                    initial_marker, display_marker, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(role_key) DO UPDATE SET
                    ru_name=excluded.ru_name,
                    english_name=excluded.english_name,
                    tag=excluded.tag,
                    region=excluded.region
                """,
                (
                    role["key"], role["ru_name"], role["english_name"], role["tag"], role["region"],
                    role["initial_marker"], role["initial_marker"], now(),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_get_member(chat_id: int, user_id: int):
    conn = db_connect()
    try:
        return conn.execute(
            "SELECT * FROM members WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    finally:
        conn.close()


def db_set_member(chat_id: int, user, state: str, join_token: Optional[str] = None, welcome_message_id: Optional[int] = None):
    conn = db_connect()
    try:
        existing = conn.execute(
            "SELECT role_key, role_name, tag, joined_at, confirmed_at FROM members WHERE chat_id=? AND user_id=?",
            (chat_id, user.id),
        ).fetchone()
        joined_at = existing["joined_at"] if existing else now()
        role_key = existing["role_key"] if existing else ""
        role_name = existing["role_name"] if existing else ""
        tag = existing["tag"] if existing else ""
        confirmed_at = existing["confirmed_at"] if existing else None
        conn.execute(
            """
            INSERT INTO members (
                chat_id,user_id,username,first_name,last_name,role_key,role_name,tag,
                state,joined_at,confirmed_at,left_at,welcome_message_id,join_token
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                state=excluded.state,
                joined_at=excluded.joined_at,
                left_at=NULL,
                welcome_message_id=COALESCE(excluded.welcome_message_id,members.welcome_message_id),
                join_token=COALESCE(excluded.join_token,members.join_token)
            """,
            (
                chat_id, user.id, user.username or "", user.first_name or "", user.last_name or "",
                role_key, role_name, tag, state, joined_at, confirmed_at, None,
                welcome_message_id, join_token,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def db_assign_role(chat_id: int, user_id: int, role_key: str) -> bool:
    conn = db_connect()
    try:
        role = conn.execute("SELECT * FROM roles WHERE role_key=?", (role_key,)).fetchone()
        if not role:
            return False
        current_assignee = role["assigned_user_id"]
        if current_assignee is not None and current_assignee != user_id:
            return False
        member = conn.execute(
            "SELECT * FROM members WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        if not member:
            return False

        old_role_key = member["role_key"]
        if old_role_key and old_role_key != role_key:
            conn.execute(
                """
                UPDATE roles
                SET assigned_user_id=NULL, assigned_chat_id=NULL,
                    display_marker='', managed_by_bot=0, updated_at=?
                WHERE role_key=? AND assigned_user_id=? AND assigned_chat_id=?
                """,
                (now(), old_role_key, user_id, chat_id),
            )

        marker = role["initial_marker"] or DEFAULT_TAKEN_MARKER
        conn.execute(
            """
            UPDATE roles
            SET assigned_user_id=?, assigned_chat_id=?, managed_by_bot=1,
                display_marker=?, updated_at=?
            WHERE role_key=?
            """,
            (user_id, chat_id, marker, now(), role_key),
        )
        conn.execute(
            """
            UPDATE members
            SET role_key=?, role_name=?, tag=?
            WHERE chat_id=? AND user_id=?
            """,
            (role_key, role["ru_name"], role["tag"], chat_id, user_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def db_mark_confirmed(chat_id: int, user_id: int):
    conn = db_connect()
    try:
        conn.execute(
            "UPDATE members SET state='confirmed', confirmed_at=? WHERE chat_id=? AND user_id=?",
            (now(), chat_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def db_mark_left(chat_id: int, user_id: int):
    conn = db_connect()
    try:
        member = conn.execute(
            "SELECT * FROM members WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        if not member:
            return None
        role_key = member["role_key"]
        if role_key:
            conn.execute(
                """
                UPDATE roles
                SET assigned_user_id=NULL, assigned_chat_id=NULL,
                    display_marker=CASE WHEN managed_by_bot=1 THEN '' ELSE display_marker END,
                    managed_by_bot=CASE WHEN managed_by_bot=1 THEN 0 ELSE managed_by_bot END,
                    updated_at=?
                WHERE role_key=? AND assigned_user_id=? AND assigned_chat_id=?
                """,
                (now(), role_key, user_id, chat_id),
            )
        conn.execute(
            "UPDATE members SET state='left', left_at=? WHERE chat_id=? AND user_id=?",
            (now(), chat_id, user_id),
        )
        conn.commit()
        return member
    finally:
        conn.close()


def db_find_latest_pending(chat_id: int):
    conn = db_connect()
    try:
        return conn.execute(
            """
            SELECT * FROM members
            WHERE chat_id=? AND state='pending_role'
            ORDER BY joined_at DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()


def db_find_by_username(chat_id: int, username: str):
    normalized = username.lstrip("@").casefold()
    conn = db_connect()
    try:
        return conn.execute(
            """
            SELECT * FROM members
            WHERE chat_id=? AND lower(username)=?
            ORDER BY joined_at DESC LIMIT 1
            """,
            (chat_id, normalized),
        ).fetchone()
    finally:
        conn.close()


def db_get_role(role_key: str):
    conn = db_connect()
    try:
        return conn.execute("SELECT * FROM roles WHERE role_key=?", (role_key,)).fetchone()
    finally:
        conn.close()


def db_list_roles():
    conn = db_connect()
    try:
        return conn.execute("SELECT * FROM roles ORDER BY rowid").fetchall()
    finally:
        conn.close()


def db_record_event(chat_id: int, user_id: int, username: str, event_type: str, role_key: str, role_name: str, tag: str):
    conn = db_connect()
    try:
        conn.execute(
            """
            INSERT INTO member_events(chat_id,user_id,username,event_type,role_key,role_name,tag,occurred_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (chat_id, user_id, username or "", event_type, role_key, role_name, tag, now()),
        )
        conn.commit()
    finally:
        conn.close()


def db_save_template(message_slot: int, message_id: int, text: str, entities: list[dict]):
    conn = db_connect()
    try:
        conn.execute(
            """
            INSERT INTO list_templates(message_slot,message_id,template_text,template_entities,captured_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(message_slot) DO UPDATE SET
                message_id=excluded.message_id,
                template_text=excluded.template_text,
                template_entities=excluded.template_entities,
                captured_at=excluded.captured_at
            """,
            (message_slot, message_id, text, json.dumps(entities, ensure_ascii=False), now()),
        )
        conn.commit()
    finally:
        conn.close()


def db_get_template(slot: int):
    conn = db_connect()
    try:
        return conn.execute("SELECT * FROM list_templates WHERE message_slot=?", (slot,)).fetchone()
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# UI / ROLE HELPERS
# ---------------------------------------------------------------------------


def resolve_role(value: str):
    key = normalize_role_name(value)
    key = ALIASES.get(key, key)
    return ROLES.get(key)


def confirmation_kb(user_id: int, token: str):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ ПОДТВЕРДИТЬ", callback_data=f"confirm:{user_id}:{token}")
    ]])


def confirmed_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ ПОДТВЕРЖДЕНО", callback_data="noop")
    ]])


def role_mention(user) -> str:
    return f"@{user.username}" if user.username else user.first_name or str(user.id)


def welcome_text(user, role):
    role_line = role["tag"] if role else "Роль пока не назначена"
    return (
        f"👋 Добро пожаловать, {role_mention(user)}!\n\n"
        "Перед началом общения необходимо ознакомиться с правилами.\n\n"
        "Входя в чат, вы обязуетесь:\n"
        "• соблюдать правила сообщества;\n"
        "• уважать других участников;\n"
        "• не нарушать комфорт общения;\n"
        "• выполнять требования администрации.\n\n"
        f"Ваша роль: {role_line}\n\n"
        "Нажмите кнопку ниже, чтобы подтвердить согласие с правилами."
    )


def leave_notification(member_row):
    admin = ADMIN_MENTION or f"<a href=\"tg://user?id={ADMIN_ID}\">администратор</a>"
    username = f"@{member_row['username']}" if member_row["username"] else member_row["first_name"] or str(member_row["user_id"])
    role = member_row["tag"] or member_row["role_name"] or "роль не назначена"
    return (
        f"{admin}\n\n"
        "🚪 <b>Участник покинул чат</b>\n\n"
        f"{username}\n"
        f"ID: <code>{member_row['user_id']}</code>\n"
        f"Роль: {role}\n"
        f"Вышел: {member_row['left_at'] or now()}"
    )


def render_region_text(region: str, rows: list[sqlite3.Row]) -> str:
    header_map = {
        "Мондштадт": "✦ 🤍🤍𝑴𝒐𝒏𝒅𝒔𝒕𝒂𝒅𝒕🤍🤍✦",
        "Ли Юэ": "✦ 🤍🤍𝑳𝒊 𝒀𝒖𝒆🤍🤍✦",
        "Инадзума": "✦ 🤍🤍𝑰𝒏𝒂𝒛𝒖𝒎𝒂🤍🤍✦",
        "Сумеру": "✦ 🤍🤍𝑺𝒖𝒎𝒆𝒓𝒖🤍🤍✦",
        "Фонтейн": "✦ 🤍🤍𝑭𝒐𝒏𝒕𝒂𝒊𝒏𝒆🤍🤍✦",
        "Натлан": "✦ 🤍🤍𝑵𝒂𝒕𝒍𝒂𝒏🤍🤍✦",
        "Нод-Край": "✦ 🤍🤍𝑵𝒐𝒅-𝑲𝒓𝒂𝒊🤍🤍✦",
        "Снежная": "✦ 🤍🤍𝑺𝒏𝒆𝒛𝒉𝒏𝒂𝒚𝒂🤍🤍✦",
        "Каэнри'ах": "✦ 🤍🤍𝑲𝒉𝒂𝒆𝒏𝒓𝒊’𝒂𝒉🤍🤍✦",
        "Шабаш": "✦ 🤍🤍𝑺𝒉𝒂𝒃𝒂𝒔𝒉🤍🤍✦",
        "Тени": "✦ 🤍🤍𝑺𝒉𝒂𝒅𝒐𝒘𝒔🤍🤍✦",
        "Другое": "✦ 🤍🤍𝑨𝒏𝒐𝒕𝒉𝒆𝒓🤍🤍✦",
    }
    lines = [header_map[region]]
    for row in rows:
        marker = row["display_marker"] or ""
        suffix = f" - {marker}" if marker else " -"
        lines.append(f"{row['ru_name']}{suffix}")
    return "\n".join(lines)


def full_list_text(slots: list[str]) -> str:
    rows = db_list_roles()
    by_region = {region: [] for region in REGION_ORDER}
    for row in rows:
        by_region[row["region"]].append(row)
    return "\n".join(render_region_text(region, by_region[region]) for region in slots)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _utf16_offset(text: str, char_index: int) -> int:
    return _utf16_len(text[:char_index])


def _slice_utf16(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


def preserve_template_entities(template, new_text: str) -> list[MessageEntity]:
    """Preserve formatting and captured Telegram custom emoji IDs."""
    if not template:
        return []
    try:
        entity_data = json.loads(template["template_entities"] or "[]")
    except json.JSONDecodeError:
        return []

    old_text = template["template_text"] or ""
    result: list[MessageEntity] = []
    custom_by_char: dict[str, str] = {}

    # Static/header entities can retain their original offsets.
    first_role = ROLE_ROWS[0][1][0][0] if ROLE_ROWS and ROLE_ROWS[0][1] else ""
    split_at = new_text.find(first_role)
    if split_at < 0:
        split_at = len(new_text)
    split_units = _utf16_offset(new_text, split_at)

    for raw in entity_data:
        try:
            entity_type = raw.get("type")
            offset = int(raw.get("offset", 0))
            length = int(raw.get("length", 0))
            if entity_type == "custom_emoji":
                marker_text = _slice_utf16(old_text, offset, length)
                custom_id = raw.get("custom_emoji_id")
                if marker_text and custom_id:
                    custom_by_char.setdefault(marker_text, custom_id)
            elif offset + length <= split_units:
                result.append(MessageEntity(**raw))
        except Exception:
            logger.debug("Skipping malformed captured entity: %r", raw, exc_info=True)

    # Put the captured custom-emoji ID back onto every generated occupied
    # marker having the same visible character (e.g. 💛, 🧡, 💚).
    for row in db_list_roles():
        marker = row["display_marker"] or ""
        custom_id = custom_by_char.get(marker)
        if not custom_id:
            continue
        line_prefix = f"{row['ru_name']} - "
        line_start = new_text.find(line_prefix)
        if line_start < 0:
            continue
        marker_index = line_start + len(line_prefix)
        result.append(
            MessageEntity(
                type="custom_emoji",
                offset=_utf16_offset(new_text, marker_index),
                length=_utf16_len(marker),
                custom_emoji_id=custom_id,
            )
        )
    return result


async def safe_set_tag(chat_id: int, user_id: int, tag: str) -> bool:
    try:
        await bot.set_chat_member_tag(chat_id=chat_id, user_id=user_id, tag=tag)
        return True
    except TelegramBadRequest as exc:
        logger.error("Could not set tag | user_id=%s | tag=%s | %s", user_id, tag, exc)
        return False
    except TelegramForbiddenError:
        logger.exception("Bot lacks permission to manage member tags | user_id=%s", user_id)
        return False


async def restrict_user(chat_id: int, user_id: int, can_send: bool) -> bool:
    try:
        permissions = ChatPermissions(
            can_send_messages=can_send,
            can_send_audios=can_send,
            can_send_documents=can_send,
            can_send_photos=can_send,
            can_send_videos=can_send,
            can_send_video_notes=can_send,
            can_send_voice_notes=can_send,
            can_send_polls=can_send,
            can_send_other_messages=can_send,
            can_add_web_page_previews=can_send,
        )
        await bot.restrict_chat_member(chat_id, user_id, permissions=permissions)
        return True
    except Exception:
        logger.exception("Could not change restrictions | chat_id=%s | user_id=%s | can_send=%s", chat_id, user_id, can_send)
        return False


async def notify_admin(text: str):
    with suppress(Exception):
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")


async def sync_info_lists():
    if not INFO_CHANNEL_ID:
        return

    rows = db_list_roles()
    by_region = {region: [] for region in REGION_ORDER}
    for row in rows:
        by_region[row["region"]].append(row)

    first_regions = {"Мондштадт", "Ли Юэ", "Инадзума", "Сумеру", "Фонтейн"}
    second_regions = set(REGION_ORDER) - first_regions

    for slot_index, region_set in enumerate((first_regions, second_regions), start=1):
        template = db_get_template(slot_index)
        configured_id = INFO_MESSAGE_IDS[slot_index - 1]
        message_id = int(template["message_id"]) if template else configured_id
        if not message_id:
            logger.warning("No info-list message configured for slot %s.", slot_index)
            continue

        regions = [r for r in REGION_ORDER if r in region_set]
        text = "\n".join(render_region_text(region, by_region[region]) for region in regions)
        entities = preserve_template_entities(template, text)

        try:
            await bot.edit_message_text(
                chat_id=INFO_CHANNEL_ID,
                message_id=message_id,
                text=text,
                entities=entities or None,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.error("Could not edit info list message %s: %s", message_id, exc)
        except Exception:
            logger.exception("Could not update info list message %s", message_id)


async def resolve_target_for_role_command(message: Message, role_text: str):
    if message.reply_to_message:
        target = message.reply_to_message.from_user
        if target and not target.is_bot:
            return target

    # If the command is replying to one of our welcome messages, recover the
    # member linked to that welcome message.
    if message.reply_to_message:
        conn = db_connect()
        try:
            row = conn.execute(
                "SELECT user_id FROM members WHERE chat_id=? AND welcome_message_id=? LIMIT 1",
                (message.chat.id, message.reply_to_message.message_id),
            ).fetchone()
        finally:
            conn.close()
        if row:
            return await bot.get_chat_member(message.chat.id, row["user_id"])

    # Optional explicit username form: калл @username роль
    parts = message.text.strip().split()
    if len(parts) >= 3 and parts[1].startswith("@"):
        row = db_find_by_username(message.chat.id, parts[1])
        if row:
            member = await bot.get_chat_member(message.chat.id, row["user_id"])
            return member.user

    pending = db_find_latest_pending(message.chat.id)
    if pending:
        return await bot.get_chat_member(message.chat.id, pending["user_id"])
    return None


@router.message(F.chat.id == GROUP_CHAT_ID, F.text.regexp(r"^\s*калл\b"))
async def assign_role(message: Message):
    # Role assignment is admin-only.
    if message.from_user.id != ADMIN_ID:
        return

    raw = re.sub(r"^\s*калл\b", "", message.text, flags=re.IGNORECASE).strip()
    if not raw:
        await message.reply("Использование: <code>калл Чжун Ли</code> — в ответ на пользователя или сразу после его входа.", parse_mode="HTML")
        return

    parts = raw.split()
    target_user = None
    role_text = raw
    if parts and parts[0].startswith("@") and len(parts) > 1:
        target_row = db_find_by_username(message.chat.id, parts[0])
        if target_row:
            target_user = (await bot.get_chat_member(message.chat.id, target_row["user_id"])).user
            role_text = " ".join(parts[1:])
    else:
        target_user = await resolve_target_for_role_command(message, role_text)

    role = resolve_role(role_text)
    if not role:
        await message.reply("Роль не найдена. Проверьте написание или используйте одну из ролей из каталога.")
        return
    if not target_user:
        await message.reply("Не удалось определить участника. Ответьте этой командой на его сообщение или используйте <code>калл @username Роль</code>.", parse_mode="HTML")
        return

    member = await bot.get_chat_member(message.chat.id, target_user.id)
    if member.status not in {"member", "restricted"}:
        await message.reply("Для администраторов используется отдельная система титулов; обычный member-tag можно назначить только обычному участнику.")
        return

    if not db_assign_role(message.chat.id, target_user.id, role["key"]):
        await message.reply("Не удалось сохранить роль в базе данных.")
        return

    if not await safe_set_tag(message.chat.id, target_user.id, role["tag"]):
        await message.reply(
            f"Роль <b>{role['ru_name']}</b> сохранена, но Telegram не позволил поставить тег <code>{role['tag']}</code>. Проверьте право <b>Manage Tags</b>.",
            parse_mode="HTML",
        )
    else:
        await message.reply(f"✅ {target_user.first_name}: назначена роль <b>{role['ru_name']}</b>\nТег: <code>{role['tag']}</code>", parse_mode="HTML")

    member_row = db_get_member(message.chat.id, target_user.id)
    if member_row and member_row["state"] == "pending_role":
        token = member_row["join_token"] or ""
        welcome = await bot.send_message(
            message.chat.id,
            welcome_text(target_user, role),
            reply_markup=confirmation_kb(target_user.id, token),
        )
        db_set_member(message.chat.id, target_user, "awaiting_confirmation", join_token=token, welcome_message_id=welcome.message_id)
    elif member_row and member_row["state"] == "confirmed":
        db_record_event(message.chat.id, target_user.id, target_user.username or "", "role_changed", role["key"], role["ru_name"], role["tag"])

    await sync_info_lists()


@router.callback_query(F.data.startswith("confirm:"))
async def confirm_rules(callback: CallbackQuery):
    try:
        _, user_id_s, token = callback.data.split(":", 2)
        user_id = int(user_id_s)
    except ValueError:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return

    if callback.from_user.id != user_id:
        await callback.answer("Эта кнопка предназначена другому участнику.", show_alert=True)
        return

    member = db_get_member(GROUP_CHAT_ID, user_id)
    if not member or member["join_token"] != token:
        await callback.answer("Сессия подтверждения устарела.", show_alert=True)
        return

    if not await restrict_user(GROUP_CHAT_ID, user_id, True):
        await callback.answer("Не удалось снять ограничение. Сообщите администрации.", show_alert=True)
        return

    db_mark_confirmed(GROUP_CHAT_ID, user_id)
    role = db_get_role(member["role_key"]) if member["role_key"] else None
    db_record_event(
        GROUP_CHAT_ID,
        user_id,
        member["username"],
        "confirmed",
        member["role_key"],
        member["role_name"],
        member["tag"],
    )

    try:
        await callback.message.edit_text(
            welcome_text(callback.from_user, role) + "\n\n✅ <b>Подтверждено</b>",
            reply_markup=confirmed_kb(),
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass

    await callback.answer("Подтверждение принято!")
    await sync_info_lists()

    if WELCOME_DELETE_AFTER > 0:
        async def delete_later(chat_id: int, message_id: int):
            await asyncio.sleep(WELCOME_DELETE_AFTER)
            with suppress(Exception):
                await bot.delete_message(chat_id, message_id)
        asyncio.create_task(delete_later(callback.message.chat.id, callback.message.message_id))

# ---------------------------------------------------------------------------
# MEMBER JOIN / LEAVE
# ---------------------------------------------------------------------------

@router.chat_member(F.chat.id == GROUP_CHAT_ID)
async def member_status_update(update: ChatMemberUpdated):
    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status
    user = update.new_chat_member.user
    if user.is_bot:
        return

    joined = new_status in {"member", "restricted"} and old_status in {"left", "kicked"}
    left = new_status in {"left", "kicked"} and old_status in {"member", "restricted", "administrator"}

    if joined:
        previous = db_get_member(GROUP_CHAT_ID, user.id)
        token = os.urandom(6).hex()
        db_set_member(GROUP_CHAT_ID, user, "pending_role", join_token=token)
        db_record_event(GROUP_CHAT_ID, user.id, user.username or "", "join", "", "", "")

        # Immediately block messaging until confirmation.
        restricted = await restrict_user(GROUP_CHAT_ID, user.id, False)
        if not restricted:
            await notify_admin(f"⚠️ Не удалось ограничить нового участника @{user.username or user.first_name}. Проверьте право Restrict Members.")

        # If this member previously had a bot-managed role and the role is free,
        # restore the role automatically on re-entry. Otherwise wait for `калл <роль>`.
        if previous and previous["role_key"]:
            prior_role = db_get_role(previous["role_key"])
            if prior_role and prior_role["assigned_user_id"] is None:
                if db_assign_role(GROUP_CHAT_ID, user.id, prior_role["role_key"]):
                    tagged = await safe_set_tag(GROUP_CHAT_ID, user.id, prior_role["tag"])
                    if tagged:
                        welcome = await bot.send_message(
                            GROUP_CHAT_ID,
                            welcome_text(user, prior_role),
                            reply_markup=confirmation_kb(user.id, token),
                        )
                        db_set_member(
                            GROUP_CHAT_ID, user, "awaiting_confirmation",
                            join_token=token, welcome_message_id=welcome.message_id,
                        )
                        await sync_info_lists()
                        return

        role_timeout = ROLE_ASSIGNMENT_TIMEOUT

        async def role_waiter(user_id: int, join_token: str):
            await asyncio.sleep(role_timeout)
            row = db_get_member(GROUP_CHAT_ID, user_id)
            if not row or row["join_token"] != join_token or row["state"] != "pending_role":
                return
            try:
                member_obj = await bot.get_chat_member(GROUP_CHAT_ID, user_id)
                role = db_get_role(row["role_key"]) if row["role_key"] else None
                sent = await bot.send_message(
                    GROUP_CHAT_ID,
                    welcome_text(member_obj.user, role) + "\n\n⚠️ <b>Роль пока не назначена.</b>",
                    reply_markup=confirmation_kb(user_id, join_token),
                )
                db_set_member(GROUP_CHAT_ID, member_obj.user, "awaiting_confirmation", join_token=join_token, welcome_message_id=sent.message_id)
            except Exception:
                logger.exception("Could not send fallback welcome | user_id=%s", user_id)

        asyncio.create_task(role_waiter(user.id, token))

    if left:
        member_row = db_mark_left(GROUP_CHAT_ID, user.id)
        if member_row:
            await notify_admin(leave_notification(member_row))
            await sync_info_lists()

# ---------------------------------------------------------------------------
# ADMIN COMMANDS
# ---------------------------------------------------------------------------

@router.message(Command("id"))
async def show_ids(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    lines = [
        "🆔 <b>Telegram ID</b>",
        f"Чат: <code>{message.chat.id}</code>",
        f"Твоё ID: <code>{message.from_user.id}</code>",
        f"Это сообщение: <code>{message.message_id}</code>",
    ]
    if message.reply_to_message:
        lines.append(f"Сообщение, на которое отвечено: <code>{message.reply_to_message.message_id}</code>")
        if message.reply_to_message.from_user:
            lines.append(f"Автор сообщения: <code>{message.reply_to_message.from_user.id}</code>")
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("capture_list"))
async def capture_list(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.reply_to_message:
        await message.reply("Ответьте этой командой на сообщение списка: /capture_list 40 или /capture_list 41")
        return
    args = message.text.split(maxsplit=1)
    if len(args) != 2 or args[1] not in {"40", "41"}:
        await message.reply("Использование: /capture_list 40 или /capture_list 41")
        return

    slot = int(args[1])
    source = message.reply_to_message
    text = source.text or source.caption or ""
    entities = source.entities or source.caption_entities or []
    db_save_template(
        message_slot=slot,
        message_id=source.message_id,
        text=text,
        entities=[entity.model_dump(mode="json") for entity in entities],
    )
    await message.reply(
        f"✅ Сообщение #{source.message_id} сохранено как список {slot}.\n"
        "Оригинальные Telegram entities/custom emoji сохранены."
    )


@router.message(Command("sync_list"))
async def manual_sync_list(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await sync_info_lists()
    await message.reply("✅ Список синхронизирован с текущими ролями.")


@router.message(Command("role_list"))
async def role_list(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    lines = ["<b>Роли и теги:</b>"]
    for region in REGION_ORDER:
        lines.append(f"\n<b>{region}</b>")
        for row in db_list_roles():
            if row["region"] == region:
                lines.append(f"{row['ru_name']} — <code>{row['tag']}</code>")
    # Telegram message limit guard.
    text = "\n".join(lines)
    if len(text) > 4096:
        text = text[:4000] + "\n…"
    await message.reply(text, parse_mode="HTML")


@router.message(Command("release"))
async def release_role(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    raw = message.text.partition(" ")[2].strip()
    role = resolve_role(raw)
    if not role:
        await message.reply("Роль не найдена.")
        return
    conn = db_connect()
    try:
        conn.execute(
            """
            UPDATE roles
            SET assigned_user_id=NULL, assigned_chat_id=NULL,
                display_marker='', managed_by_bot=0, updated_at=?
            WHERE role_key=?
            """,
            (now(), role["key"]),
        )
        conn.commit()
    finally:
        conn.close()
    await sync_info_lists()
    await message.reply(f"✅ Роль {role['ru_name']} освобождена.")

# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------

async def startup_checks():
    me = await bot.get_me()
    logger.info("Connected as @%s (%s)", me.username, me.id)
    init_db()
    if not INFO_CHANNEL_ID:
        logger.warning("INFO_CHANNEL_ID is not set; list sync is disabled.")
    else:
        # Verify bot is present in target channel.
        try:
            member = await bot.get_chat_member(INFO_CHANNEL_ID, me.id)
            logger.info("Info channel bot status: %s", member.status)
        except Exception:
            logger.exception("Could not verify info channel access")
    try:
        member = await bot.get_chat_member(GROUP_CHAT_ID, me.id)
        logger.info("Group bot status: %s", member.status)
        if getattr(member, "status", "") != "administrator":
            logger.warning("Bot is not an administrator in GROUP_CHAT_ID.")
        else:
            logger.info(
                "Group rights: restrict=%s manage_tags=%s delete=%s invite=%s",
                getattr(member, "can_restrict_members", None),
                getattr(member, "can_manage_tags", None),
                getattr(member, "can_delete_messages", None),
                getattr(member, "can_invite_users", None),
            )
    except Exception:
        logger.exception("Could not verify group bot access")


async def main():
    await startup_checks()
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("Starting moderation polling...")
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            handle_signals=True,
            close_bot_session=False,
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

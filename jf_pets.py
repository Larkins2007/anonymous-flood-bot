"""Safe virtual companions and personal den for Justice Faite.

No biological/romantic roleplay, paid random drops, decay, illness or penalties.
All purchases use the shared economy ledger in the same SQLite transaction.
"""
from __future__ import annotations

import html
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timezone

GROUP_TYPES = {"group", "supergroup"}
PETS = {
    "кот": ("🐈", "кот"), "кошка": ("🐈", "кошка"),
    "пёс": ("🐕", "пёс"), "пес": ("🐕", "пёс"), "собака": ("🐕", "собака"),
    "лиса": ("🦊", "лиса"), "кролик": ("🐇", "кролик"),
    "сова": ("🦉", "сова"), "дракон": ("🐉", "дракон"),
}
DENS = {"уютный": "🕯️ Уютная комната", "сад": "🌿 Зелёный сад", "звёзды": "🌌 Звёздная комната"}
ROOMS = {
    "библиотека": (40, "📚 Библиотека"),
    "сад": (45, "🌿 Зимний сад"),
    "обсерватория": (60, "🔭 Обсерватория"),
    "мастерская": (50, "🛠️ Мастерская"),
}
COMMANDS = (
    "/jf_pets", "питомец", "питомцы", "мой питомец", "питомец выбрать", "питомец имя",
    "питомец погладить", "питомец поиграть", "питомец похвалить", "питомец покормить",
    "питомец история", "питомец достижения", "дом", "дом оформить", "дом комнаты", "дом действия",
    "дом открыть", "дом улучшить", "отн пт", "отн пт имя", "отн пт выбрать",
    "отн пт погладить", "отн пт поиграть", "отн пт похвалить", "отн пт покормить", "отн пт история",
    "отн пт достижения",
)
PATTERN = r"(?isu)^\s*(?:/jf_pets(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:питомец|питомцы|мой питомец|дом(?:\s+(?:оформить|комнаты|действия|открыть|улучшить))?|отн\s+пт(?:\s+(?:имя|выбрать|погладить|поиграть|покормить|похвалить|история|достижения))?)(?:\s|$).*)"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def esc(value):
    return html.escape(str(value or ""), quote=False)


class Pets:
    def __init__(self, dp, bot, F, path, lock=None, economy=None):
        self.dp, self.bot, self.F, self.path = dp, bot, F, str(path)
        self.lock = lock or threading.RLock()
        self.economy = economy
        self.registered = False

    def run(self, fn, immediate=False):
        with self.lock:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=30000")
            try:
                c.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                result = fn(c)
                c.commit()
                return result
            except Exception:
                with suppress(Exception): c.rollback()
                raise
            finally:
                c.close()

    def init(self):
        def op(c):
            c.execute("""CREATE TABLE IF NOT EXISTS jf_pets(
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                species TEXT NOT NULL, pet_name TEXT NOT NULL,
                den_style TEXT NOT NULL DEFAULT 'уютный', den_level INTEGER NOT NULL DEFAULT 1,
                adopted_at TEXT NOT NULL, last_action_at TEXT NOT NULL DEFAULT '',
                action_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id,user_id))""")
            columns = {r[1] for r in c.execute("PRAGMA table_info(jf_pets)").fetchall()}
            if "den_level" not in columns:
                c.execute("ALTER TABLE jf_pets ADD COLUMN den_level INTEGER NOT NULL DEFAULT 1")
            c.execute("""CREATE TABLE IF NOT EXISTS jf_pet_action_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS jf_pet_rooms(
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, room_key TEXT NOT NULL,
                opened_at TEXT NOT NULL, PRIMARY KEY(chat_id,user_id,room_key))""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_jf_pet_actions ON jf_pet_action_log(chat_id,user_id,id DESC)")
        self.run(op)

    def register(self):
        if not self.registered:
            self.dp.message.register(self.handle, self.F.chat.type.in_(GROUP_TYPES), self.F.text.regexp(PATTERN))
            self.registered = True

    async def reply(self, m, text):
        try: await m.reply(text, parse_mode="HTML", disable_web_page_preview=True)
        except TypeError: await m.reply(text)

    @staticmethod
    def help_text():
        return ("<b>Питомцы и личный дом Justice Faite</b>\n"
                "<code>Питомцы</code> — каталог. <code>Питомец выбрать кот</code> — выбрать компаньона бесплатно.\n"
                "<code>Мой питомец</code>, <code>Питомец имя Луна</code>, <code>Питомец погладить</code>, <code>Питомец поиграть</code>, <code>Питомец похвалить</code>, <code>Питомец покормить</code>.\n"
                "<code>Питомец история</code> · <code>Питомец достижения</code>.\n"
                "<code>Дом</code> · <code>Дом действия</code> · <code>Дом комнаты</code> · <code>Дом открыть библиотека</code> · <code>Дом улучшить</code> · <code>Дом оформить сад</code>.\n"
                "Команды доступны также через префикс <code>.отн пт</code>. Нет яиц с платным шансом, болезней, ухудшения состояния или потери прогресса за отсутствие.")

    def _pet(self, chat, uid):
        return self.run(lambda c: c.execute("SELECT * FROM jf_pets WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone())

    def _record_action(self, chat, uid, action):
        """Check cooldown and append action atomically to prevent concurrent spam."""
        stamp = now_iso()
        def op(c):
            row = c.execute("SELECT last_action_at FROM jf_pets WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone()
            if not row:
                raise ValueError("Сначала заведи питомца: <code>Питомец выбрать кот</code>.")
            last = row["last_action_at"]
            if last:
                try:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
                except ValueError:
                    elapsed = 30
                if elapsed < 30:
                    raise ValueError(f"🐾 Компаньон уже получил внимание. Попробуй снова через {max(1, int(30-elapsed))} сек.")
            c.execute("UPDATE jf_pets SET last_action_at=?,action_count=action_count+1 WHERE chat_id=? AND user_id=?", (stamp, chat, uid))
            c.execute("INSERT INTO jf_pet_action_log(chat_id,user_id,action,created_at) VALUES(?,?,?,?)", (chat, uid, action, stamp))
        return self.run(op, immediate=True)

    async def handle(self, m):
        user = getattr(m, "from_user", None)
        if not user or getattr(user, "is_bot", False): return
        raw = (m.text or "").strip()
        first = raw.splitlines()[0] if raw else ""
        first = re.sub(r"^\s*[!.\/]{0,4}", "", first).strip()
        first = re.sub(r"^(?:ирис(?:ка)?|iris)\s+", "", first, flags=re.I)
        low = first.casefold()
        chat, uid = int(m.chat.id), int(user.id)

        if low.startswith("/jf_pets") or low == "питомцы":
            return await self.reply(m, self.help_text() + "\n\nКаталог: " + ", ".join(PETS))

        row = self._pet(chat, uid)
        if low.startswith("питомец выбрать ") or low.startswith("отн пт выбрать "):
            species = first.split(maxsplit=2)[2].strip().casefold()
            if species not in PETS:
                return await self.reply(m, "Выбери питомца из каталога: " + ", ".join(PETS))
            if row:
                return await self.reply(m, "У тебя уже есть компаньон. Его можно переименовать; повторный выбор не сбрасывает прогресс.")
            emoji, label = PETS[species]
            name = label.capitalize()
            self.run(lambda c: c.execute("INSERT INTO jf_pets(chat_id,user_id,species,pet_name,adopted_at) VALUES(?,?,?,?,?)", (chat,uid,species,name,now_iso())), immediate=True)
            self.run(lambda c: c.execute("INSERT OR IGNORE INTO jf_pet_rooms(chat_id,user_id,room_key,opened_at) VALUES(?,?,?,?)", (chat,uid,"основная",now_iso())), immediate=True)
            return await self.reply(m, f"{emoji} Теперь у тебя есть компаньон — <b>{esc(name)}</b>. Основная комната уже открыта бесплатно.")

        if low in {"питомец", "мой питомец", "отн пт"}:
            if not row: return await self.reply(m, "У тебя пока нет компаньона. Напиши <code>Питомец выбрать кот</code> или открой <code>Питомцы</code>.")
            emoji = PETS.get(row["species"], ("🐾", row["species"]))[0]
            rooms = self.run(lambda c: c.execute("SELECT room_key FROM jf_pet_rooms WHERE chat_id=? AND user_id=? ORDER BY opened_at", (chat,uid)).fetchall())
            return await self.reply(m, f"{emoji} <b>{esc(row['pet_name'])}</b> · {esc(row['species'])}\n{esc(DENS.get(row['den_style'],DENS['уютный']))}\nУровень дома: {int(row['den_level'])}/5 · Комнат открыто: {len(rooms)}\nДружеских взаимодействий: {int(row['action_count'])}\nКомпаньон всегда в порядке — ежедневный уход не обязателен.")

        if low.startswith("питомец имя ") or low.startswith("отн пт имя "):
            if not row: return await self.reply(m, "Сначала заведи питомца: <code>Питомец выбрать кот</code>.")
            name = first.split(maxsplit=2)[2].strip()
            name = re.sub(r"[\r\n<>]", "", name)[:32].strip()
            if not name: return await self.reply(m, "Укажи имя длиной до 32 символов.")
            self.run(lambda c: c.execute("UPDATE jf_pets SET pet_name=? WHERE chat_id=? AND user_id=?", (name,chat,uid)), immediate=True)
            return await self.reply(m, f"🏷 Имя компаньона изменено: <b>{esc(name)}</b>.")

        pet_actions = {"питомец погладить":"погладить", "питомец поиграть":"поиграть", "питомец похвалить":"похвалить", "питомец покормить":"покормить", "отн пт погладить":"погладить", "отн пт поиграть":"поиграть", "отн пт похвалить":"похвалить", "отн пт покормить":"покормить"}
        if low in pet_actions:
            if not row: return await self.reply(m, "Сначала заведи питомца: <code>Питомец выбрать кот</code>.")
            action = pet_actions[low]
            try:
                self._record_action(chat, uid, action)
            except ValueError as exc:
                return await self.reply(m, esc(exc))
            reactions = {"погладить":"🐾 Компаньон доволен вниманием.", "поиграть":"🎾 Вы немного поиграли вместе.", "похвалить":"✨ Компаньон получил добрые слова.", "покормить":"🥣 Ты угостил(а) компаньона. Его состояние не ухудшается, если пропустить уход."}
            return await self.reply(m, reactions[action])

        if low in {"питомец история", "отн пт история"}:
            if not row: return await self.reply(m, "Сначала выбери питомца.")
            logs = self.run(lambda c: c.execute("SELECT action,created_at FROM jf_pet_action_log WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT 10", (chat,uid)).fetchall())
            return await self.reply(m, "<b>История компаньона</b>\n" + ("\n".join(f"• {esc(x['action'])} · {esc(x['created_at'][:16].replace('T',' '))} UTC" for x in logs) or "Пока нет действий."))

        if low in {"питомец достижения", "отн пт достижения"}:
            if not row: return await self.reply(m, "Сначала выбери питомца.")
            count = int(row["action_count"])
            milestones = [5,10,25,50,100]
            achieved = [x for x in milestones if count >= x]
            return await self.reply(m, f"🏅 <b>Достижения компаньона</b>\nВзаимодействий: {count}\nОткрыты рубежи: {', '.join(map(str, achieved)) if achieved else 'пока нет'}. Никаких штрафов за перерыв нет.")

        if low == "дом действия":
            return await self.reply(m, "Действия дома: <code>Дом</code>, <code>Дом комнаты</code>, <code>Дом оформить сад</code>, <code>Дом открыть библиотека</code>, <code>Дом улучшить</code>.")

        if low == "дом" or low == "дом комнаты" or low.startswith("дом оформить ") or low.startswith("дом открыть ") or low == "дом улучшить":
            if not row: return await self.reply(m, "Твой дом пока пуст. Сначала заведи компаньона: <code>Питомец выбрать кот</code>.")
            if low == "дом комнаты":
                opened = {r[0] for r in self.run(lambda c: c.execute("SELECT room_key FROM jf_pet_rooms WHERE chat_id=? AND user_id=?", (chat,uid)).fetchall())}
                lines = [f"{'✅' if key in opened else '🔒'} {title} · {'открыта' if key in opened else f'{price} жетонов'}" for key,(price,title) in ROOMS.items()]
                return await self.reply(m, "<b>Комнаты дома</b>\n" + "\n".join(lines) + "\nОткрыть: <code>Дом открыть библиотека</code>.")
            if low.startswith("дом оформить "):
                style = first.split(maxsplit=2)[2].strip().casefold()
                if style not in DENS: return await self.reply(m, "Оформления: " + ", ".join(DENS))
                self.run(lambda c: c.execute("UPDATE jf_pets SET den_style=? WHERE chat_id=? AND user_id=?", (style,chat,uid)), immediate=True)
                return await self.reply(m, f"🏡 Дом оформлен: <b>{esc(DENS[style])}</b>.")
            if low.startswith("дом открыть "):
                room = first.split(maxsplit=2)[2].strip().casefold()
                if room not in ROOMS: return await self.reply(m, "Комнаты: " + ", ".join(ROOMS))
                if not self.economy: return await self.reply(m, "Покупки комнат временно недоступны: экономика не подключена.")
                cost, title = ROOMS[room]
                def open_room(c):
                    if c.execute("SELECT 1 FROM jf_pet_rooms WHERE chat_id=? AND user_id=? AND room_key=?", (chat,uid,room)).fetchone():
                        raise ValueError("Эта комната уже открыта.")
                    self.economy._debit(c,chat,uid,cost,"pet_room",f"pet-room:{chat}:{uid}:{room}",uid)
                    c.execute("INSERT INTO jf_pet_rooms(chat_id,user_id,room_key,opened_at) VALUES(?,?,?,?)", (chat,uid,room,now_iso()))
                try: self.economy.store.run(open_room, immediate=True)
                except ValueError as e: return await self.reply(m, esc(e))
                return await self.reply(m, f"🏡 Открыта комната <b>{esc(title)}</b> за {cost} жетонов.")
            if low == "дом улучшить":
                if not self.economy: return await self.reply(m, "Улучшение временно недоступно: экономика не подключена.")
                level = int(row["den_level"])
                if level >= 5: return await self.reply(m, "Дом уже достиг максимального уровня 5.")
                cost = 50 * level
                def upgrade(c):
                    current = c.execute("SELECT den_level FROM jf_pets WHERE chat_id=? AND user_id=?", (chat,uid)).fetchone()
                    if not current: raise ValueError("Дом не найден.")
                    if int(current[0]) != level: raise ValueError("Уровень дома уже изменился. Открой карточку заново.")
                    self.economy._debit(c,chat,uid,cost,"pet_home_upgrade",f"pet-home-upgrade:{chat}:{uid}:{level}",uid)
                    c.execute("UPDATE jf_pets SET den_level=den_level+1 WHERE chat_id=? AND user_id=?", (chat,uid))
                try: self.economy.store.run(upgrade, immediate=True)
                except ValueError as e: return await self.reply(m, esc(e))
                return await self.reply(m, f"🏡 Дом улучшен до уровня {level+1}/5. Списано {cost} жетонов.")
            rooms = self.run(lambda c: c.execute("SELECT room_key FROM jf_pet_rooms WHERE chat_id=? AND user_id=? ORDER BY opened_at", (chat,uid)).fetchall())
            names = [ROOMS.get(x[0],(0,x[0]))[1] if x[0] in ROOMS else "🛋️ Основная комната" for x in rooms]
            return await self.reply(m, f"🏡 <b>Твой дом</b>\n{esc(DENS.get(row['den_style'],DENS['уютный']))}\nУровень: {int(row['den_level'])}/5\n" + ("\n".join(esc(x) for x in names) or "Основная комната") + "\nКомнаты: <code>Дом комнаты</code> · улучшение: <code>Дом улучшить</code>.")


def register(dp, bot, F, db_path, lock=None, economy=None):
    module = Pets(dp, bot, F, db_path, lock=lock, economy=economy)
    module.register()
    return module

"""Simple, persistent Justice Faite token economy.

The farm is a one-step four-hour claim inspired by Iris: a free 5–50 token reward,
without crops, seeds, betting, debt, interest, taxes or paid random rewards.
Legacy tables from earlier versions are retained for safe upgrades.
"""
from __future__ import annotations

import html
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timezone, timedelta
import secrets

import cmdcfg

GROUP_TYPES = {"group", "supergroup"}
CURRENCY = "жетонов"
STARTER_BONUS = 30
FARM_MIN_REWARD = 5
FARM_MAX_REWARD = 50
FARM_COOLDOWN_HOURS = 4
# Justice Faite's existing schedule is Moscow-based. Keep the economy day aligned
# with that schedule unless the owner explicitly changes the offset.
DAY_OFFSET_HOURS = int(__import__("os").getenv("JF_ECONOMY_UTC_OFFSET_HOURS", "3") or "3")
ECONOMY_CLOCK_LABEL = "MSK" if DAY_OFFSET_HOURS == 3 else f"UTC{DAY_OFFSET_HOURS:+d}"
SHOP = {
    # slot: which showcase slot the item can occupy when equipped
    "значок": {"price": 30, "desc": "Значок профиля — можно надеть в слот «значок»", "slot": "badge", "emoji": "🎖️"},
    "рамка": {"price": 60, "desc": "Рамка профиля — слот «рамка»", "slot": "frame", "emoji": "🖼️"},
    "плюш": {"price": 45, "desc": "Плюш для витрины — слот «витрина»", "slot": "showcase", "emoji": "🧸"},
    "фон": {"price": 80, "desc": "Фон карточки — слот «фон»", "slot": "background", "emoji": "🌄"},
    "открытка": {"price": 15, "desc": "Открытка в витрине — слот «витрина»", "slot": "showcase", "emoji": "💌"},
    "книга": {"price": 35, "desc": "Книга в витрине — слот «витрина»", "slot": "showcase", "emoji": "📖"},
    "брелок": {"price": 25, "desc": "Брелок — слот «аксессуар»", "slot": "accessory", "emoji": "🔑"},
    "чашка": {"price": 30, "desc": "Чашка Justice Faite — слот «витрина»", "slot": "showcase", "emoji": "☕"},
    "медаль": {"price": 70, "desc": "Медаль — слот «значок»", "slot": "badge", "emoji": "🏅"},
    "постер": {"price": 55, "desc": "Постер — слот «фон»", "slot": "background", "emoji": "🪧"},
    "шарф": {"price": 40, "desc": "Шарф — слот «аксессуар»", "slot": "accessory", "emoji": "🧣"},
    "значок-клуба": {"price": 50, "desc": "Значок клуба — слот «значок»", "slot": "badge", "emoji": "💠"},
}
SHOP_SLOTS = ("badge", "frame", "showcase", "background", "accessory")
SLOT_LABELS = {
    "badge": "Значок",
    "frame": "Рамка",
    "showcase": "Витрина",
    "background": "Фон",
    "accessory": "Аксессуар",
}
COMMANDS = (
    "/jf_economy", "баланс", "кошелёк", "кошелек", "топ богатых", "ферма", "фарм", "работа",
    "собрать", "посадить", "профессии", "профессия", "образование", "учиться", "магазин",
    "купить", "инвентарь", "надеть", "снять", "витрина", "подарить", "задания", "задание", "достижения", "перевести",
    "отн искры подарок", "отн подарок", "отн подарок витрина", "отн прем", "отн прем подарок", "отн задания", "отн история",
)
# `отн история связи` belongs to jf_social; prevent the broad economy-history
# prefix from capturing it. The remaining aliases retain their argument tails.
_economy_aliases = [x for x in COMMANDS[1:] if x != "отн история"]
_economy_aliases.append(r"отн\s+история(?!\s+связи\b)")
COMMAND_PATTERN = (r"(?isu)^\s*(?:/jf_economy(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:"
    + "|".join(x if x.startswith("отн\\s") else re.escape(x) for x in sorted(_economy_aliases, key=len, reverse=True))
    + r")(?:\s|$).*$)")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat()


def economy_day(now: datetime | None = None) -> str:
    return ((now or utcnow()).astimezone(timezone.utc) + timedelta(hours=DAY_OFFSET_HOURS)).date().isoformat()


def esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


class Store:
    def __init__(self, path: str, lock=None):
        self.path = str(path)
        self.lock = lock or threading.RLock()

    def run(self, fn, *, immediate: bool = False):
        with self.lock:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                result = fn(conn)
                conn.commit()
                return result
            except Exception:
                with suppress(Exception):
                    conn.rollback()
                raise
            finally:
                conn.close()

    def init(self):
        def op(c):
            c.executescript("""
            CREATE TABLE IF NOT EXISTS jf_wallets(
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0 CHECK(balance>=0),
                updated_at TEXT NOT NULL, PRIMARY KEY(chat_id,user_id));
            CREATE TABLE IF NOT EXISTS jf_economy_ledger(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL, delta INTEGER NOT NULL,
                balance_before INTEGER NOT NULL, balance_after INTEGER NOT NULL,
                reason TEXT NOT NULL, actor_id INTEGER NOT NULL DEFAULT 0,
                operation_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_jf_ledger_user ON jf_economy_ledger(chat_id,user_id,id DESC);
            CREATE TABLE IF NOT EXISTS jf_economy_profiles(
                chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,job TEXT NOT NULL DEFAULT '',
                education TEXT NOT NULL DEFAULT '',last_work_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(chat_id,user_id));
            CREATE TABLE IF NOT EXISTS jf_economy_inventory(
                chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,item TEXT NOT NULL,quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity>=0),
                PRIMARY KEY(chat_id,user_id,item));
            CREATE TABLE IF NOT EXISTS jf_economy_equipped(
                chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,slot TEXT NOT NULL,item TEXT NOT NULL,
                equipped_at TEXT NOT NULL, PRIMARY KEY(chat_id,user_id,slot));
            -- Retained only to preserve old farms from previous versions during upgrades.
            CREATE TABLE IF NOT EXISTS jf_economy_farms(
                chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,crop TEXT NOT NULL,planted_at TEXT NOT NULL,
                ready_at TEXT NOT NULL,PRIMARY KEY(chat_id,user_id));
            CREATE TABLE IF NOT EXISTS jf_economy_daily(
                chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,day TEXT NOT NULL,
                worked INTEGER NOT NULL DEFAULT 0,harvested INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id,user_id,day));
            CREATE TABLE IF NOT EXISTS jf_economy_farm_state(
                chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,last_claim_at TEXT NOT NULL,
                next_claim_at TEXT NOT NULL,claim_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(chat_id,user_id));
            CREATE TABLE IF NOT EXISTS jf_economy_achievements(
                chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,achievement TEXT NOT NULL,
                unlocked_at TEXT NOT NULL,PRIMARY KEY(chat_id,user_id,achievement));
            CREATE TABLE IF NOT EXISTS jf_economy_gifts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,from_id INTEGER NOT NULL,
                to_id INTEGER NOT NULL,item TEXT NOT NULL,quantity INTEGER NOT NULL,operation_key TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL);
            """)
            # The migration is intentionally additive only. Do not drop or reset any old balance data.
            c.execute("CREATE INDEX IF NOT EXISTS idx_jf_econ_ledger_chat_reason ON jf_economy_ledger(chat_id,reason,id DESC)")
            # Preserve the previous farm timestamp as a cooldown anchor. Users who
            # have already farmed in v0.11.1 do not get a free immediate second claim.
            c.execute("""INSERT OR IGNORE INTO jf_economy_farm_state(chat_id,user_id,last_claim_at,next_claim_at,claim_count)
                SELECT l.chat_id,l.user_id,l.created_at,
                       datetime(l.created_at, '+4 hours'),
                       (SELECT COUNT(*) FROM jf_economy_ledger x WHERE x.chat_id=l.chat_id AND x.user_id=l.user_id AND x.reason='farm_daily')
                FROM jf_economy_ledger l
                WHERE l.reason='farm_daily' AND l.id=(SELECT MAX(x.id) FROM jf_economy_ledger x
                    WHERE x.chat_id=l.chat_id AND x.user_id=l.user_id AND x.reason='farm_daily')""")
        self.run(op)


class Economy:
    def __init__(self, dp, bot, F, path, lock=None):
        self.dp, self.bot, self.F = dp, bot, F
        self.store = Store(path, lock)
        self.registered = False

    def init(self):
        self.store.init()

    def register(self):
        if not self.registered:
            self.dp.message.register(self.handle, self.F.chat.type.in_(GROUP_TYPES), self.F.text.regexp(COMMAND_PATTERN))
            self.registered = True

    async def reply(self, m, text):
        try:
            await m.reply(text, parse_mode="HTML", disable_web_page_preview=True)
        except TypeError:
            await m.reply(text)

    def _wallet(self, c, chat, uid):
        c.execute("INSERT OR IGNORE INTO jf_wallets(chat_id,user_id,balance,updated_at) VALUES(?,?,0,?)", (chat, uid, iso()))
        welcome_key = f"welcome:{chat}:{uid}"
        if not c.execute("SELECT 1 FROM jf_economy_ledger WHERE operation_key=?", (welcome_key,)).fetchone():
            before = int(c.execute("SELECT balance FROM jf_wallets WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone()[0])
            after = before + STARTER_BONUS
            c.execute("UPDATE jf_wallets SET balance=?,updated_at=? WHERE chat_id=? AND user_id=?", (after, iso(), chat, uid))
            c.execute("INSERT INTO jf_economy_ledger(chat_id,user_id,delta,balance_before,balance_after,reason,actor_id,operation_key,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (chat, uid, STARTER_BONUS, before, after, "starter_bonus", 0, welcome_key, iso()))
        return int(c.execute("SELECT balance FROM jf_wallets WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone()[0])

    def _credit(self, c, chat, uid, amount, reason, key, actor=0):
        if c.execute("SELECT 1 FROM jf_economy_ledger WHERE operation_key=?", (key,)).fetchone():
            return self._wallet(c, chat, uid)
        before = self._wallet(c, chat, uid)
        after = before + int(amount)
        if after < 0:
            raise ValueError("Недостаточно жетонов.")
        c.execute("UPDATE jf_wallets SET balance=?,updated_at=? WHERE chat_id=? AND user_id=?", (after, iso(), chat, uid))
        c.execute("INSERT INTO jf_economy_ledger(chat_id,user_id,delta,balance_before,balance_after,reason,actor_id,operation_key,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (chat, uid, int(amount), before, after, reason, actor, key, iso()))
        return after

    def _debit(self, c, chat, uid, amount, reason, key, actor=0):
        return self._credit(c, chat, uid, -int(amount), reason, key, actor)

    def _daily(self, c, chat, uid):
        day = economy_day()
        c.execute("INSERT OR IGNORE INTO jf_economy_daily(chat_id,user_id,day) VALUES(?,?,?)", (chat, uid, day))
        return c.execute("SELECT * FROM jf_economy_daily WHERE chat_id=? AND user_id=? AND day=?", (chat, uid, day)).fetchone()

    def _name(self, chat, uid):
        try:
            r = self.store.run(lambda c: c.execute("SELECT username,display_name FROM jf_manager_member_names WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone())
        except sqlite3.Error:
            r = None
        return "@" + r[0] if r and r[0] else (r[1] if r and r[1] else f"ID {uid}")

    def _unlock(self, c, chat, uid, key):
        c.execute("INSERT OR IGNORE INTO jf_economy_achievements(chat_id,user_id,achievement,unlocked_at) VALUES(?,?,?,?)", (chat, uid, key, iso()))

    def _check_achievements(self, c, chat, uid):
        balance = self._wallet(c, chat, uid)
        tx_count = int(c.execute("SELECT COUNT(*) FROM jf_economy_ledger WHERE chat_id=? AND user_id=? AND delta>0", (chat, uid)).fetchone()[0])
        farm_claims = int(c.execute("SELECT COUNT(*) FROM jf_economy_ledger WHERE chat_id=? AND user_id=? AND reason='farm_daily'", (chat, uid)).fetchone()[0])
        if balance >= 100: self._unlock(c, chat, uid, "first_hundred")
        if tx_count >= 10: self._unlock(c, chat, uid, "ten_earnings")
        if farm_claims >= 5: self._unlock(c, chat, uid, "five_farms")

    def _claim_farm(self, c, chat, uid, message_id=0, now=None):
        current = (now or utcnow()).astimezone(timezone.utc)
        stamp = iso(current)
        operation_key = f"farm:{chat}:{uid}:{int(message_id or 0)}"
        duplicate = c.execute("SELECT balance_after,delta,created_at FROM jf_economy_ledger WHERE operation_key=?", (operation_key,)).fetchone()
        state = c.execute("SELECT * FROM jf_economy_farm_state WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone()
        if duplicate:
            # A Telegram retry replays the original grant, but must not invent
            # a fresh cooldown relative to the retry time.
            try:
                replay_next = datetime.fromisoformat(state["next_claim_at"]) if state else datetime.fromisoformat(duplicate["created_at"]) + timedelta(hours=FARM_COOLDOWN_HOURS)
                if replay_next.tzinfo is None:
                    replay_next = replay_next.replace(tzinfo=timezone.utc)
                replay_next = replay_next.astimezone(timezone.utc)
            except (TypeError, ValueError):
                replay_next = current
            return int(duplicate["balance_after"]), int(duplicate["delta"]), replay_next

        if state:
            try:
                next_claim = datetime.fromisoformat(state["next_claim_at"])
                if next_claim.tzinfo is None:
                    next_claim = next_claim.replace(tzinfo=timezone.utc)
                next_claim = next_claim.astimezone(timezone.utc)
            except (TypeError, ValueError):
                next_claim = current
            if current < next_claim:
                remaining = int((next_claim - current).total_seconds())
                hours, rem = divmod(remaining, 3600)
                minutes = max(1, (rem + 59) // 60)
                wait = f"{hours} ч. {minutes} мин." if hours else f"{minutes} мин."
                raise ValueError(f"🌱 Следующий сбор будет доступен через {wait}. Фарм обновляется раз в {FARM_COOLDOWN_HOURS} часа.")

        reward = secrets.randbelow(FARM_MAX_REWARD - FARM_MIN_REWARD + 1) + FARM_MIN_REWARD
        balance = self._credit(c, chat, uid, reward, "farm_daily", operation_key)
        next_claim = current + timedelta(hours=FARM_COOLDOWN_HOURS)
        c.execute("""INSERT INTO jf_economy_farm_state(chat_id,user_id,last_claim_at,next_claim_at,claim_count)
            VALUES(?,?,?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET
            last_claim_at=excluded.last_claim_at,next_claim_at=excluded.next_claim_at,
            claim_count=jf_economy_farm_state.claim_count+1""", (chat, uid, stamp, iso(next_claim)))
        # Keep the legacy day table as a coarse history marker for old reports.
        day = economy_day(current)
        c.execute("INSERT OR IGNORE INTO jf_economy_daily(chat_id,user_id,day) VALUES(?,?,?)", (chat, uid, day))
        c.execute("UPDATE jf_economy_daily SET worked=1,harvested=harvested+1 WHERE chat_id=? AND user_id=? AND day=?", (chat, uid, day))
        self._check_achievements(c, chat, uid)
        return balance, reward, next_claim

    async def handle(self, m):
        if not getattr(m, "from_user", None) or getattr(m.from_user, "is_bot", False):
            return
        raw = (m.text or "").strip()
        lines = raw.splitlines()
        first = lines[0] if lines else ""
        first = re.sub(r"^\s*[!.\/]{0,4}", "", first).strip()
        first = re.sub(r"^(?:ирис(?:ка)?|iris)\s+", "", first, flags=re.I)
        low = first.casefold()
        chat, uid = int(m.chat.id), int(m.from_user.id)
        if low.startswith("jf_economy") or low.startswith("/jf_economy"):
            return await self.reply(m, self.help_text())
        if low in {"баланс", "кошелёк", "кошелек"}:
            bal = self.store.run(lambda c: self._wallet(c, chat, uid), immediate=True)
            return await self.reply(m, f"🪙 Твой баланс: <b>{bal} {CURRENCY}</b>.")
        if low.startswith("топ богатых"):
            limit = 10
            mt = re.search(r"\b(\d{1,2})\b", low)
            if mt: limit = max(1, min(20, int(mt.group(1))))
            rows = self.store.run(lambda c: c.execute("SELECT user_id,balance FROM jf_wallets WHERE chat_id=? ORDER BY balance DESC,user_id LIMIT ?", (chat, limit)).fetchall())
            body = "\n".join(f"{i}. {esc(self._name(chat, int(r['user_id'])))} — <b>{r['balance']} {CURRENCY}</b>" for i, r in enumerate(rows, 1))
            return await self.reply(m, "🏆 <b>Топ по жетонам</b>\n" + (body or "Пока нет участников в рейтинге."))
        if low in {"ферма", "фарм", "собрать", "работа"}:
            try:
                bal, reward, next_claim = self.store.run(lambda c: self._claim_farm(c, chat, uid, getattr(m, "message_id", 0)), immediate=True)
            except ValueError as e:
                return await self.reply(m, esc(e))
            return await self.reply(m, f"🌱 <b>Фарм завершён!</b>\nНачислено: <b>+{reward} {CURRENCY}</b> (от {FARM_MIN_REWARD} до {FARM_MAX_REWARD}).\nТвой баланс: <b>{bal} {CURRENCY}</b>.\nСледующий сбор: <b>{esc((next_claim.astimezone(timezone.utc) + timedelta(hours=DAY_OFFSET_HOURS)).strftime('%H:%M') + ' ' + ECONOMY_CLOCK_LABEL)}</b> — через {FARM_COOLDOWN_HOURS} часа после этого сбора.")
        if low in {"посадить", "учиться"} or low.startswith(("посадить ", "профессия", "профессии", "образование", "учиться ")):
            return await self.reply(m, "🌿 Мы упростили экономику: посадка растений, семена, курсы и профессии больше не нужны. Напиши <code>Ферма</code>, чтобы сразу получить жетоны; следующий сбор откроется через 4 часа.")
        if low == "магазин":
            items = "\n".join(
                f"• <b>{esc(k)}</b> — {v['price']} {CURRENCY}. {esc(v['desc'])}."
                for k, v in SHOP.items()
            )
            return await self.reply(
                m,
                "🛍️ <b>Магазин Justice Faite</b>\n"
                + items
                + "\nПокупка: <code>Купить название</code>."
                + "\nНадеть: <code>Надеть название</code> · Снять: <code>Снять слот</code>."
                + "\nСлоты: значок, рамка, витрина, фон, аксессуар. Витрина: <code>Витрина</code>.",
            )
        if low in {"отн подарок витрина", "отн подарок витрина общая"}:
            rows = self.store.run(lambda c: c.execute(
                "SELECT from_id,to_id,item,created_at FROM jf_economy_gifts WHERE chat_id=? ORDER BY id DESC LIMIT 10",
                (chat,)).fetchall())
            lines = [f"• {esc(self._name(chat, int(r['from_id'])))} → {esc(self._name(chat, int(r['to_id'])))}: <b>{esc(r['item'])}</b> · {esc(r['created_at'][:10])}" for r in rows]
            return await self.reply(m, "🎁 <b>Общая витрина подарков</b>\n" + ("\n".join(lines) if lines else "Подарков пока нет. Их можно отправить командой <code>Подарить название</code> ответом на сообщение."))
        if low.startswith("купить "):
            item = first[len("купить "):].strip().casefold()
            if item not in SHOP: return await self.reply(m, "Такого предмета нет. Напиши <code>Магазин</code>.")
            def buy(c):
                op_key = f"shop:{chat}:{uid}:{item}:{m.message_id}"
                if c.execute("SELECT 1 FROM jf_economy_ledger WHERE operation_key=?", (op_key,)).fetchone():
                    return self._wallet(c, chat, uid)
                price = int(SHOP[item]["price"]); self._debit(c, chat, uid, price, "shop", op_key)
                c.execute("INSERT INTO jf_economy_inventory(chat_id,user_id,item,quantity) VALUES(?,?,?,1) ON CONFLICT(chat_id,user_id,item) DO UPDATE SET quantity=quantity+1", (chat, uid, item))
                return self._wallet(c, chat, uid)
            try: bal = self.store.run(buy, immediate=True)
            except ValueError: return await self.reply(m, f"Недостаточно {CURRENCY}. Напиши <code>Ферма</code>, чтобы получить следующую награду через четыре часа.")
            return await self.reply(m, f"✅ Куплено: <b>{esc(item)}</b>. Остаток: <b>{bal} {CURRENCY}</b>.")
        if low.startswith("отн прем"):
            return await self.reply(m, "💎 В Justice Faite нет платного VIP и покупаемых привилегий. Декоративные предметы за игровые жетоны находятся в команде <code>Магазин</code>. История уже подаренных предметов: <code>.отн подарок витрина общая</code>. Подарить предмет можно ответом на сообщение: <code>.отн подарок значок</code>.")
        if low == "инвентарь":
            rows = self.store.run(lambda c: c.execute("SELECT item,quantity FROM jf_economy_inventory WHERE chat_id=? AND user_id=? AND quantity>0 ORDER BY item", (chat, uid)).fetchall())
            equipped = self.store.run(lambda c: c.execute("SELECT slot,item FROM jf_economy_equipped WHERE chat_id=? AND user_id=?", (chat, uid)).fetchall())
            eq_map = {str(r["slot"]): str(r["item"]) for r in equipped}
            body = "\n".join(f"• {esc(r['item'])} × {r['quantity']}" for r in rows)
            eq_lines = []
            for slot in SHOP_SLOTS:
                item = eq_map.get(slot)
                if item:
                    meta = SHOP.get(item, {})
                    eq_lines.append(f"• {SLOT_LABELS.get(slot, slot)}: {meta.get('emoji','')} {esc(item)}")
            eq_block = ("\n\n<b>Надето</b>\n" + "\n".join(eq_lines)) if eq_lines else "\n\nНадетых предметов нет. <code>Надеть название</code>"
            return await self.reply(m, "🎒 <b>Инвентарь</b>\n" + (body or "Пока пусто.") + eq_block)
        if low.startswith("надеть "):
            item = first[len("надеть "):].strip().casefold()
            if item not in SHOP:
                return await self.reply(m, "Такого предмета нет в магазине. Напиши <code>Магазин</code>.")
            slot = SHOP[item]["slot"]
            def equip(c):
                row = c.execute("SELECT quantity FROM jf_economy_inventory WHERE chat_id=? AND user_id=? AND item=?", (chat, uid, item)).fetchone()
                if not row or int(row[0]) < 1:
                    raise ValueError("Сначала купи этот предмет: <code>Купить " + item + "</code>.")
                c.execute(
                    "INSERT INTO jf_economy_equipped(chat_id,user_id,slot,item,equipped_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(chat_id,user_id,slot) DO UPDATE SET item=excluded.item,equipped_at=excluded.equipped_at",
                    (chat, uid, slot, item, iso()),
                )
            try:
                self.store.run(equip, immediate=True)
            except ValueError as e:
                return await self.reply(m, str(e))
            return await self.reply(m, f"✨ Надето: <b>{esc(item)}</b> → слот «{esc(SLOT_LABELS.get(slot, slot))}».")
        if low.startswith("снять "):
            token = first[len("снять "):].strip().casefold()
            # accept slot label or key
            slot = None
            for key, label in SLOT_LABELS.items():
                if token in {key, label.casefold()}:
                    slot = key
                    break
            if slot is None and token in SHOP:
                slot = SHOP[token]["slot"]
            if slot is None:
                return await self.reply(m, "Укажи слот: значок, рамка, витрина, фон, аксессуар.")
            def unequip(c):
                cur = c.execute("DELETE FROM jf_economy_equipped WHERE chat_id=? AND user_id=? AND slot=?", (chat, uid, slot))
                return cur.rowcount
            removed = self.store.run(unequip, immediate=True)
            if not removed:
                return await self.reply(m, f"В слоте «{esc(SLOT_LABELS.get(slot, slot))}» ничего не надето.")
            return await self.reply(m, f"Слот «{esc(SLOT_LABELS.get(slot, slot))}» освобождён.")
        if low == "витрина":
            equipped = self.store.run(lambda c: c.execute("SELECT slot,item FROM jf_economy_equipped WHERE chat_id=? AND user_id=? ORDER BY slot", (chat, uid)).fetchall())
            if not equipped:
                return await self.reply(m, "Витрина пуста. Купи предмет в магазине и надень: <code>Надеть постер</code>.")
            lines = []
            for r in equipped:
                item = str(r["item"])
                meta = SHOP.get(item, {})
                lines.append(f"{meta.get('emoji','•')} <b>{esc(SLOT_LABELS.get(str(r['slot']), r['slot']))}</b>: {esc(item)}")
            return await self.reply(m, "🪞 <b>Твоя витрина</b>\n" + "\n".join(lines))
        if low == "отн подарок витрина моя":
            rows = self.store.run(lambda c: c.execute(
                "SELECT from_id,item,created_at FROM jf_economy_gifts WHERE chat_id=? AND to_id=? ORDER BY id DESC LIMIT 15",
                (chat, uid)).fetchall())
            lines = [f"• От {esc(self._name(chat, int(r['from_id'])))}: <b>{esc(r['item'])}</b> · {esc(r['created_at'][:10])}" for r in rows]
            return await self.reply(m, "🎁 <b>Твоя витрина подарков</b>\n" + ("\n".join(lines) if lines else "Тебе пока не дарили предметы."))
        if low.startswith("подарить ") or (low.startswith("отн подарок ") and not low.startswith("отн подарок витрина")):
            target_msg = getattr(m, "reply_to_message", None)
            target_user = getattr(target_msg, "from_user", None) if target_msg else None
            if not target_user: return await self.reply(m, "Ответь командой на сообщение участника: <code>Подарить значок</code>.")
            target = int(target_user.id)
            item = (first[len("отн подарок "):] if low.startswith("отн подарок ") else first[len("подарить "):]).strip().casefold()
            if target == uid or getattr(target_user, "is_bot", False): return await self.reply(m, "Подарить можно другому участнику.")
            if item not in SHOP: return await self.reply(m, "Можно подарить предмет из магазина. Напиши <code>Магазин</code>.")
            def gift(c):
                op_key = f"gift:{chat}:{uid}:{target}:{item}:{m.message_id}"
                if c.execute("SELECT 1 FROM jf_economy_gifts WHERE operation_key=?", (op_key,)).fetchone():
                    raise ValueError("Этот подарок уже обработан.")
                row = c.execute("SELECT quantity FROM jf_economy_inventory WHERE chat_id=? AND user_id=? AND item=?", (chat, uid, item)).fetchone()
                if not row or int(row[0]) < 1: raise ValueError("У тебя нет такого предмета в инвентаре.")
                c.execute("UPDATE jf_economy_inventory SET quantity=quantity-1 WHERE chat_id=? AND user_id=? AND item=?", (chat, uid, item))
                c.execute("INSERT INTO jf_economy_inventory(chat_id,user_id,item,quantity) VALUES(?,?,?,1) ON CONFLICT(chat_id,user_id,item) DO UPDATE SET quantity=quantity+1", (chat, target, item))
                c.execute("INSERT INTO jf_economy_gifts(chat_id,from_id,to_id,item,quantity,operation_key,created_at) VALUES(?,?,?,?,1,?,?)", (chat, uid, target, item, op_key, iso()))
            try: self.store.run(gift, immediate=True)
            except ValueError as e: return await self.reply(m, esc(e))
            return await self.reply(m, f"🎁 Подарок отправлен: <b>{esc(item)}</b> → {esc(self._name(chat,target))}.")
        if low.startswith("отн история"):
            parts = low.split()
            mode = parts[2] if len(parts) > 2 else "всё"
            page = 1
            if len(parts) > 3:
                try: page = max(1, min(50, int(parts[3])))
                except ValueError: return await self.reply(m, "Формат: <code>.отн история [доход|расход] [страница]</code>.")
            if mode not in {"всё", "все", "доход", "расход", "связи"}:
                return await self.reply(m, "Формат: <code>.отн история</code>, <code>.отн история доход</code> или <code>.отн история расход 2</code>.")
            if mode in {"связи", "связей"}:
                rows = self.store.run(lambda c: c.execute("SELECT from_name,to_name,status,created_at FROM jf_social_invites WHERE chat_id=? AND (from_id=? OR to_id=?) ORDER BY id DESC LIMIT 10", (chat,uid,uid)).fetchall())
                lines = [f"• {esc(r['from_name'])} → {esc(r['to_name'])}: {esc(r['status'])} · {esc(r['created_at'][:10])}" for r in rows]
                return await self.reply(m, "<b>История приглашений</b>\n" + ("\n".join(lines) or "История пока пуста."))
            where = ""
            params = [chat,uid]
            if mode == "доход": where = " AND delta>0"
            elif mode == "расход": where = " AND delta<0"
            total = int(self.store.run(lambda c: c.execute("SELECT COUNT(*) FROM jf_economy_ledger WHERE chat_id=? AND user_id=?"+where, params).fetchone()[0]))
            rows = self.store.run(lambda c: c.execute("SELECT delta,reason,balance_after,created_at FROM jf_economy_ledger WHERE chat_id=? AND user_id=?"+where+" ORDER BY id DESC LIMIT 10 OFFSET ?", params+[ (page-1)*10 ]).fetchall())
            labels = {"starter_bonus":"Стартовый бонус", "farm_daily":"Ежедневный фарм", "shop":"Покупка в магазине", "transfer_out":"Перевод участнику", "transfer_in":"Получен перевод", "pet_room":"Открытие комнаты", "pet_home_upgrade":"Улучшение дома"}
            lines = [f"• {'+' if int(r['delta'])>0 else ''}{int(r['delta'])} {CURRENCY} · {esc(labels.get(r['reason'],r['reason']))} · баланс {int(r['balance_after'])} · {esc(r['created_at'][:10])}" for r in rows]
            pages = max(1,(total+9)//10)
            return await self.reply(m, f"<b>История жетонов · {esc(mode)}</b>\nСтраница {page}/{pages} · операций: {total}\n" + ("\n".join(lines) if lines else "Операций пока нет."))

        if low in {"задания", "задание", "отн задания"}:
            row = self.store.run(lambda c: self._daily(c, chat, uid))
            return await self.reply(m, "📜 <b>Ежедневная цель</b>\n" + ("✅" if row["worked"] else "▫️") + f" Получить фарм через <code>Ферма</code> (от {FARM_MIN_REWARD} до {FARM_MAX_REWARD} {CURRENCY} раз в 4 часа).\nНикаких дополнительных действий не требуется.")
        if low == "достижения":
            rows = self.store.run(lambda c: c.execute("SELECT achievement,unlocked_at FROM jf_economy_achievements WHERE chat_id=? AND user_id=? ORDER BY unlocked_at", (chat, uid)).fetchall())
            names = {"first_hundred": f"Первые 100 {CURRENCY}", "ten_earnings": "Десять начислений", "five_farms": "Пять сборов фермы", "green_thumb": "Старое достижение: садовод"}
            return await self.reply(m, "🏅 <b>Достижения</b>\n" + ("\n".join("• " + esc(names.get(r["achievement"],r["achievement"])) for r in rows) or "Пока нет достижений. Продолжай участвовать в жизни флуда."))
        if low.startswith("перевести ") or low.startswith("отн искры подарок "):
            target_msg = getattr(m, "reply_to_message", None); target_user = getattr(target_msg, "from_user", None) if target_msg else None
            if not target_user: return await self.reply(m, "Ответь на сообщение получателя: <code>Перевести 10</code> или <code>.отн искры подарок 10</code>.")
            target = int(target_user.id)
            try: amount = int(first.split()[-1])
            except (ValueError, IndexError): return await self.reply(m, "Укажи целое число жетонов.")
            if target == uid or getattr(target_user, "is_bot", False) or not 1 <= amount <= 500: return await self.reply(m, "Можно перевести от 1 до 500 жетонов другому участнику.")
            key = f"transfer:{chat}:{uid}:{target}:{m.message_id}"
            def transfer(c):
                # Daily transfer cap is 500 tokens total, not 500 per message.
                local_now = utcnow() + timedelta(hours=DAY_OFFSET_HOURS)
                local_midnight = datetime.combine(local_now.date(), datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=DAY_OFFSET_HOURS)
                sent_today = int(c.execute("SELECT COALESCE(-SUM(delta),0) FROM jf_economy_ledger WHERE chat_id=? AND user_id=? AND reason='transfer_out' AND created_at>=?", (chat, uid, iso(local_midnight))).fetchone()[0])
                if sent_today + amount > 500: raise ValueError("Дневной лимит переводов — 500 жетонов.")
                bal = self._debit(c, chat, uid, amount, "transfer_out", key, uid)
                self._credit(c, chat, target, amount, "transfer_in", key + ":in", uid)
                return bal
            try: bal = self.store.run(transfer, immediate=True)
            except ValueError as e: return await self.reply(m, esc(e))
            return await self.reply(m, f"💸 Переведено {amount} {CURRENCY}. Твой баланс: <b>{bal} {CURRENCY}</b>.")

    @staticmethod
    def help_text():
        return ("<b>Экономика Justice Faite</b>\n"
                f"Стартовый баланс: <b>{STARTER_BONUS} {CURRENCY}</b>.\n"
                f"<code>Баланс</code> / <code>Кошелёк</code> · <code>Ферма</code> ({FARM_MIN_REWARD}–{FARM_MAX_REWARD} жетонов раз в 4 часа) · <code>Топ богатых</code>\n"
                "<code>Магазин</code> · <code>Купить название</code> · <code>Инвентарь</code>\n"
                "<code>.отн подарок витрина общая</code> — последние подарки чата · <code>.отн подарок витрина моя</code> — твои полученные подарки\n"
                "<code>Подарить название</code> или <code>.отн подарок название</code> ответом на сообщение · <code>Перевести 10</code> / <code>.отн искры подарок 10</code> ответом на сообщение\n"
                "<code>Задания</code> / <code>.отн задания</code> · <code>Достижения</code> · <code>.отн история доход</code> / <code>.отн история расход 2</code>\n"
                "Ферма начисляет награду сразу раз в 4 часа. Все цены фиксированы; нет ставок, казино, кредитов, налогов, платного шанса или покупки привилегий.")


def register(dp, bot, F, db_path, lock=None):
    module = Economy(dp, bot, F, db_path, lock=lock)
    module.register()
    return module

def get_equipped_items(store, chat_id, user_id):
    """Return ordered equipped showcase for profile cards."""
    try:
        rows = store.run(lambda c: c.execute(
            "SELECT slot,item FROM jf_economy_equipped WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        ).fetchall())
    except Exception:
        return []
    out = []
    for r in rows:
        item = str(r["item"])
        meta = SHOP.get(item, {})
        out.append({
            "slot": str(r["slot"]),
            "label": SLOT_LABELS.get(str(r["slot"]), str(r["slot"])),
            "item": item,
            "emoji": meta.get("emoji", ""),
            "desc": meta.get("desc", ""),
        })
    return out


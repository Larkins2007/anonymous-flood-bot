"""Persistent, transaction-safe Justice Faite community economy.

The module deliberately has no betting, casino, paid random rewards, debt,
interest, taxes, or moderation immunity. All balances and item ownership are
stored in the shared SQLite database and every balance mutation is journaled.
"""
from __future__ import annotations

import html
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timedelta, timezone

import cmdcfg

GROUP_TYPES = {"group", "supergroup"}
CURRENCY = "Искры"
JOBS = {
    "садовник": {"course": "ботаника", "pay": 28},
    "редактор": {"course": "письмо", "pay": 32},
    "исследователь": {"course": "наука", "pay": 36},
    "дизайнер": {"course": "дизайн", "pay": 30},
}
COURSES = {"ботаника": "садовник", "письмо": "редактор", "наука": "исследователь", "дизайн": "дизайнер"}
CROPS = {
    "мята": {"seed": 5, "seconds": 3600, "yield": 13},
    "ромашка": {"seed": 8, "seconds": 7200, "yield": 20},
    "тыква": {"seed": 14, "seconds": 14400, "yield": 36},
}
SHOP = {
    "значок": {"price": 40, "desc": "Коллекционный значок профиля"},
    "рамка": {"price": 90, "desc": "Декоративная рамка профиля"},
    "плюш": {"price": 65, "desc": "Коллекционная мягкая игрушка"},
    "фон": {"price": 120, "desc": "Тематический фон профиля"},
}
COMMANDS = (
    "/jf_economy", "баланс", "кошелёк", "кошелек", "топ богатых", "работа", "профессии",
    "профессия", "образование", "учиться", "ферма", "посадить", "собрать", "магазин",
    "купить", "инвентарь", "подарить", "задания", "задание", "достижения", "перевести",
)
COMMAND_PATTERN = r"(?isu)^\s*(?:/jf_economy(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:" + "|".join(re.escape(x) for x in sorted(COMMANDS[1:], key=len, reverse=True)) + r")(?:\s|$).*$)"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat()


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
                if immediate:
                    conn.execute("BEGIN IMMEDIATE")
                else:
                    conn.execute("BEGIN")
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
            CREATE TABLE IF NOT EXISTS jf_economy_farms(
                chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,crop TEXT NOT NULL,planted_at TEXT NOT NULL,
                ready_at TEXT NOT NULL,PRIMARY KEY(chat_id,user_id));
            CREATE TABLE IF NOT EXISTS jf_economy_daily(
                chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,day TEXT NOT NULL,
                worked INTEGER NOT NULL DEFAULT 0,harvested INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id,user_id,day));
            CREATE TABLE IF NOT EXISTS jf_economy_achievements(
                chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,achievement TEXT NOT NULL,
                unlocked_at TEXT NOT NULL,PRIMARY KEY(chat_id,user_id,achievement));
            CREATE TABLE IF NOT EXISTS jf_economy_gifts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,from_id INTEGER NOT NULL,
                to_id INTEGER NOT NULL,item TEXT NOT NULL,quantity INTEGER NOT NULL,operation_key TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL);
            """)
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
        return int(c.execute("SELECT balance FROM jf_wallets WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone()[0])

    def _credit(self, c, chat, uid, amount, reason, key, actor=0):
        if not amount:
            return self._wallet(c, chat, uid)
        if c.execute("SELECT 1 FROM jf_economy_ledger WHERE operation_key=?", (key,)).fetchone():
            return self._wallet(c, chat, uid)
        before = self._wallet(c, chat, uid)
        after = before + int(amount)
        if after < 0:
            raise ValueError("Недостаточно средств.")
        c.execute("UPDATE jf_wallets SET balance=?,updated_at=? WHERE chat_id=? AND user_id=?", (after, iso(), chat, uid))
        c.execute("INSERT INTO jf_economy_ledger(chat_id,user_id,delta,balance_before,balance_after,reason,actor_id,operation_key,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (chat, uid, int(amount), before, after, reason, actor, key, iso()))
        return after

    def _debit(self, c, chat, uid, amount, reason, key, actor=0):
        return self._credit(c, chat, uid, -int(amount), reason, key, actor)

    def _daily(self, c, chat, uid):
        day = utcnow().date().isoformat()
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
        harvests = int(c.execute("SELECT COUNT(*) FROM jf_economy_ledger WHERE chat_id=? AND user_id=? AND reason='harvest'", (chat, uid)).fetchone()[0])
        if balance >= 100: self._unlock(c, chat, uid, "first_hundred")
        if tx_count >= 10: self._unlock(c, chat, uid, "ten_earnings")
        if harvests >= 5: self._unlock(c, chat, uid, "green_thumb")

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
            bal = self.store.run(lambda c: self._wallet(c, chat, uid))
            return await self.reply(m, f"💠 Твой баланс: <b>{bal} {CURRENCY}</b>.")
        if low.startswith("топ богатых"):
            limit = 10
            mt = re.search(r"\b(\d{1,2})\b", low)
            if mt: limit = max(1, min(20, int(mt.group(1))))
            rows = self.store.run(lambda c: c.execute("SELECT user_id,balance FROM jf_wallets WHERE chat_id=? ORDER BY balance DESC,user_id LIMIT ?", (chat, limit)).fetchall())
            body = "\n".join(f"{i}. {esc(self._name(chat, int(r['user_id'])))} — <b>{r['balance']}</b>" for i, r in enumerate(rows, 1))
            return await self.reply(m, "🏆 <b>Топ по Искрам</b>\n" + (body or "Пока нет балансов."))
        if low == "профессии":
            lines_out = [f"• {esc(k.title())} — {v['pay']} Искр/день · курс: {esc(v['course'])}" for k, v in JOBS.items()]
            return await self.reply(m, "🧰 <b>Профессии</b>\n" + "\n".join(lines_out) + "\n\nВыбор: <code>Профессия садовник</code>. Сначала можно пройти курс через <code>Учиться ботаника</code>.")
        if low.startswith("профессия"):
            choice = first[len("профессия"):].strip().casefold()
            if not choice:
                row = self.store.run(lambda c: c.execute("SELECT job FROM jf_economy_profiles WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone())
                return await self.reply(m, f"Твоя профессия: <b>{esc(row['job'] or 'не выбрана') if row else 'не выбрана'}</b>.")
            if choice not in JOBS:
                return await self.reply(m, "Такой профессии нет. Напиши <code>Профессии</code>.")
            def choose(c):
                c.execute("INSERT OR IGNORE INTO jf_economy_profiles(chat_id,user_id) VALUES(?,?)", (chat, uid))
                row = c.execute("SELECT education FROM jf_economy_profiles WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone()
                if row[0] != JOBS[choice]['course']:
                    raise ValueError(f"Для профессии нужен курс «{JOBS[choice]['course']}». Напиши Учиться {JOBS[choice]['course']}.")
                c.execute("UPDATE jf_economy_profiles SET job=? WHERE chat_id=? AND user_id=?", (choice, chat, uid))
            try: self.store.run(choose, immediate=True)
            except ValueError as e: return await self.reply(m, esc(e))
            return await self.reply(m, f"✅ Теперь твоя профессия — <b>{esc(choice)}</b>.")
        if low == "образование":
            row = self.store.run(lambda c: c.execute("SELECT education FROM jf_economy_profiles WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone())
            return await self.reply(m, "🎓 <b>Образование</b>\nДоступные курсы: " + ", ".join(COURSES) + f"\nТвой курс: <b>{esc(row['education'] or 'не пройден') if row else 'не пройден'}</b>.\nПройти: <code>Учиться ботаника</code>.")
        if low.startswith("учиться "):
            course = first[len("учиться "):].strip().casefold()
            if course not in COURSES:
                return await self.reply(m, "Неизвестный курс. Доступно: " + ", ".join(COURSES) + ".")
            def study(c):
                c.execute("INSERT OR IGNORE INTO jf_economy_profiles(chat_id,user_id) VALUES(?,?)", (chat, uid))
                c.execute("UPDATE jf_economy_profiles SET education=? WHERE chat_id=? AND user_id=?", (course, chat, uid))
            self.store.run(study, immediate=True)
            return await self.reply(m, f"🎓 Курс «{esc(course)}» завершён. Теперь доступна профессия «{esc(COURSES[course])}».")
        if low == "работа":
            def work(c):
                c.execute("INSERT OR IGNORE INTO jf_economy_profiles(chat_id,user_id) VALUES(?,?)", (chat, uid))
                profile = c.execute("SELECT job,last_work_at FROM jf_economy_profiles WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone()
                if not profile['job']:
                    raise ValueError("Сначала выбери профессию: Учиться ботаника → Профессия садовник.")
                day = self._daily(c, chat, uid)
                if int(day['worked']): raise ValueError("Сегодня ты уже получил оплату. Новая смена будет после 00:00 UTC.")
                pay = int(JOBS[profile['job']]['pay'])
                key = f"work:{chat}:{uid}:{utcnow().date().isoformat()}"
                bal = self._credit(c, chat, uid, pay, "work", key)
                c.execute("UPDATE jf_economy_daily SET worked=1 WHERE chat_id=? AND user_id=? AND day=?", (chat, uid, day['day']))
                c.execute("UPDATE jf_economy_profiles SET last_work_at=? WHERE chat_id=? AND user_id=?", (iso(), chat, uid))
                self._check_achievements(c, chat, uid)
                return pay, bal
            try: pay, bal = self.store.run(work, immediate=True)
            except ValueError as e: return await self.reply(m, esc(e))
            return await self.reply(m, f"🧰 Смена завершена! Начислено <b>{pay} {CURRENCY}</b>. Баланс: <b>{bal}</b>.")
        if low == "ферма":
            row = self.store.run(lambda c: c.execute("SELECT * FROM jf_economy_farms WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone())
            crops = "\n".join(f"• {k}: семена {v['seed']}, сбор через {v['seconds']//3600} ч., награда {v['yield']}" for k, v in CROPS.items())
            if row:
                ready = datetime.fromisoformat(row['ready_at']) <= utcnow()
                status = "🌾 Урожай готов — напиши <code>Собрать</code>." if ready else f"🌱 Растёт до {esc(row['ready_at'][:16].replace('T',' '))} UTC."
            else: status = "У тебя пока нет посаженной культуры."
            return await self.reply(m, f"🌿 <b>Твоя ферма</b>\n{status}\n\n<b>Культуры</b>\n{crops}\nПосадка: <code>Посадить мята</code>.")
        if low.startswith("посадить "):
            crop = first[len("посадить "):].strip().casefold()
            if crop not in CROPS: return await self.reply(m, "Неизвестная культура. Напиши <code>Ферма</code>.")
            def plant(c):
                if c.execute("SELECT 1 FROM jf_economy_farms WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone(): raise ValueError("Сначала собери текущий урожай.")
                cost = CROPS[crop]['seed']; self._debit(c, chat, uid, cost, "seed", f"seed:{chat}:{uid}:{iso()}")
                start = utcnow(); ready = start + timedelta(seconds=CROPS[crop]['seconds'])
                c.execute("INSERT INTO jf_economy_farms(chat_id,user_id,crop,planted_at,ready_at) VALUES(?,?,?,?,?)", (chat, uid, crop, iso(start), iso(ready)))
            try: self.store.run(plant, immediate=True)
            except ValueError as e: return await self.reply(m, esc(e))
            except sqlite3.IntegrityError: return await self.reply(m, "Не удалось посадить. Проверь баланс и статус фермы.")
            return await self.reply(m, f"🌱 Посажена культура «{esc(crop)}». Срок сбора: {CROPS[crop]['seconds']//3600} ч.")
        if low == "собрать":
            def harvest(c):
                row = c.execute("SELECT * FROM jf_economy_farms WHERE chat_id=? AND user_id=?", (chat, uid)).fetchone()
                if not row: raise ValueError("На ферме пока ничего не растёт.")
                if datetime.fromisoformat(row['ready_at']) > utcnow(): raise ValueError("Урожай ещё не созрел. Проверь командой «Ферма».")
                crop = row['crop']; amount = int(CROPS[crop]['yield'])
                key = f"harvest:{chat}:{uid}:{row['planted_at']}"
                bal = self._credit(c, chat, uid, amount, "harvest", key)
                c.execute("DELETE FROM jf_economy_farms WHERE chat_id=? AND user_id=?", (chat, uid))
                day = self._daily(c, chat, uid)
                c.execute("UPDATE jf_economy_daily SET harvested=1 WHERE chat_id=? AND user_id=? AND day=?", (chat, uid, day['day']))
                self._check_achievements(c, chat, uid)
                return crop, amount, bal
            try: crop, amount, bal = self.store.run(harvest, immediate=True)
            except ValueError as e: return await self.reply(m, esc(e))
            return await self.reply(m, f"🌾 Собран урожай: {esc(crop)}. Получено <b>{amount} {CURRENCY}</b>. Баланс: <b>{bal}</b>.")
        if low == "магазин":
            items = "\n".join(f"• <b>{esc(k)}</b> — {v['price']} {CURRENCY}. {esc(v['desc'])}." for k,v in SHOP.items())
            return await self.reply(m, "🛍️ <b>Магазин</b>\n" + items + "\nПокупка: <code>Купить значок</code>.")
        if low.startswith("купить "):
            item = first[len("купить "):].strip().casefold()
            if item not in SHOP: return await self.reply(m, "Такого предмета нет. Напиши <code>Магазин</code>.")
            def buy(c):
                op_key = f"shop:{chat}:{uid}:{item}:{m.message_id}"
                # A replayed Telegram update must not duplicate inventory after
                # the debit has already been committed.
                if c.execute("SELECT 1 FROM jf_economy_ledger WHERE operation_key=?", (op_key,)).fetchone():
                    return self._wallet(c, chat, uid)
                price = int(SHOP[item]['price']); self._debit(c, chat, uid, price, "shop", op_key)
                c.execute("INSERT INTO jf_economy_inventory(chat_id,user_id,item,quantity) VALUES(?,?,?,1) ON CONFLICT(chat_id,user_id,item) DO UPDATE SET quantity=quantity+1", (chat, uid, item))
                return self._wallet(c, chat, uid)
            try: bal = self.store.run(buy, immediate=True)
            except ValueError: return await self.reply(m, "Недостаточно Искр для покупки.")
            return await self.reply(m, f"✅ Куплено: <b>{esc(item)}</b>. Остаток: <b>{bal}</b> {CURRENCY}.")
        if low == "инвентарь":
            rows = self.store.run(lambda c: c.execute("SELECT item,quantity FROM jf_economy_inventory WHERE chat_id=? AND user_id=? AND quantity>0 ORDER BY item", (chat, uid)).fetchall())
            body = "\n".join(f"• {esc(r['item'])} × {r['quantity']}" for r in rows)
            return await self.reply(m, "🎒 <b>Инвентарь</b>\n" + (body or "Пока пусто."))
        if low.startswith("подарить "):
            target_msg = getattr(m, "reply_to_message", None)
            target_user = getattr(target_msg, "from_user", None) if target_msg else None
            if not target_user: return await self.reply(m, "Ответь командой на сообщение участника: <code>Подарить значок</code>.")
            target = int(target_user.id); item = first[len("подарить "):].strip().casefold()
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
        if low in {"задания", "задание"}:
            day = utcnow().date().isoformat()
            row = self.store.run(lambda c: self._daily(c, chat, uid))
            return await self.reply(m, "📜 <b>Ежедневные задания</b>\n" + ("✅" if row['worked'] else "▫️") + " Завершить рабочую смену и получить оплату\n" + ("✅" if row['harvested'] else "▫️") + " Собрать урожай\nПрогресс отмечается автоматически; отдельная выплата за повторные команды не начисляется.")
        if low == "достижения":
            rows = self.store.run(lambda c: c.execute("SELECT achievement,unlocked_at FROM jf_economy_achievements WHERE chat_id=? AND user_id=? ORDER BY unlocked_at", (chat, uid)).fetchall())
            names = {"first_hundred": "Первые 100 Искр", "ten_earnings": "Десять начислений", "green_thumb": "Зелёный палец — 5 сборов"}
            return await self.reply(m, "🏅 <b>Достижения</b>\n" + ("\n".join("• " + esc(names.get(r['achievement'],r['achievement'])) for r in rows) or "Пока нет достижений. Продолжай играть и выполнять задания."))
        if low.startswith("перевести "):
            target_msg = getattr(m, "reply_to_message", None); target_user = getattr(target_msg, "from_user", None) if target_msg else None
            if not target_user: return await self.reply(m, "Ответь на сообщение получателя: <code>Перевести 10</code>.")
            target = int(target_user.id)
            try: amount = int(first.split(maxsplit=1)[1])
            except (ValueError, IndexError): return await self.reply(m, "Укажи целое число Искр.")
            if target == uid or getattr(target_user, "is_bot", False) or not 1 <= amount <= 500: return await self.reply(m, "Можно перевести от 1 до 500 Искр другому участнику.")
            day = utcnow().date().isoformat(); key = f"transfer:{chat}:{uid}:{target}:{day}:{m.message_id}"
            def transfer(c):
                bal = self._debit(c, chat, uid, amount, "transfer_out", key, uid)
                self._credit(c, chat, target, amount, "transfer_in", key + ":in", uid)
                return bal
            try: bal = self.store.run(transfer, immediate=True)
            except ValueError: return await self.reply(m, "Недостаточно Искр.")
            return await self.reply(m, f"💸 Переведено {amount} {CURRENCY}. Твой баланс: <b>{bal}</b>.")

    @staticmethod
    def help_text():
        return ("<b>Экономика Justice Faite</b>\n"
                "<code>Баланс</code> · <code>Топ богатых</code> · <code>Профессии</code> · <code>Учиться ботаника</code> · <code>Профессия садовник</code> · <code>Работа</code>\n"
                "<code>Ферма</code> · <code>Посадить мята</code> · <code>Собрать</code> · <code>Магазин</code> · <code>Купить значок</code> · <code>Инвентарь</code>\n"
                "<code>Подарить значок</code> ответом на сообщение · <code>Задания</code> · <code>Достижения</code> · <code>Перевести 10</code> ответом на сообщение.\n"
                "Баланс хранится в SQLite и каждая операция записывается в журнал. Нет ставок, казино, кредитов, налогов или платного шанса.")


def register(dp, bot, F, db_path, lock=None):
    module = Economy(dp, bot, F, db_path, lock=lock)
    module.register()
    return module

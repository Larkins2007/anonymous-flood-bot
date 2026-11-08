"""Consent-based, non-romantic social links and friendly actions for Justice Faite."""
from __future__ import annotations

import html
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timezone

GROUP_TYPES = {"group", "supergroup"}

# 30 standard + 30 additional neutral actions. No romantic, sexual, violent,
# humiliating or coercive roleplay; all actions are opt-in and reply-based.
BASIC_ACTIONS = {
    "поддержать": "поддержал(а) участника и напомнил(а), что он не один в команде",
    "похвалить": "отметил(а), что у участника хорошо получается",
    "поблагодарить": "поблагодарил(а) за помощь",
    "поздравить": "поздравил(а) с достижением",
    "дать пять": "предложил(а) дружеский high-five",
    "помахать": "помахал(а) в знак приветствия",
    "аплодировать": "устроил(а) виртуальные аплодисменты",
    "пожелать удачи": "пожелал(а) удачи",
    "подбодрить": "подбодрил(а) перед новым делом",
    "помочь": "предложил(а) помощь",
    "выслушать": "предложил(а) спокойно выслушать",
    "поделиться идеей": "поделился(ась) новой идеей",
    "поделиться советом": "предложил(а) полезный совет",
    "отправить открытку": "отправил(а) виртуальную открытку",
    "подарить значок": "показал(а) коллекционный значок",
    "пригласить в игру": "пригласил(а) присоединиться к игре",
    "пригласить в команду": "пригласил(а) в дружескую команду",
    "пожелать хорошего дня": "пожелал(а) хорошего дня",
    "пожелать спокойного вечера": "пожелал(а) спокойного вечера",
    "отметить успех": "отметил(а) успех участника",
    "сказать спасибо": "сказал(а) большое спасибо",
    "поделиться мемом": "поделился(ась) добрым мемом",
    "поделиться книгой": "посоветовал(а) интересную книгу",
    "поделиться ресурсом": "поделился(ась) полезным ресурсом",
    "помочь выбрать": "предложил(а) помочь с выбором",
    "обменяться советами": "предложил(а) обменяться советами",
    "составить план": "предложил(а) составить общий план",
    "похлопать по плечу": "отправил(а) символический знак поддержки",
    "уважать": "выразил(а) уважение",
    "пожелать вдохновения": "пожелал(а) вдохновения",
}

EXTRA_ACTIONS = {
    "придумать девиз": "предложил(а) придумать общий девиз",
    "создать викторину": "предложил(а) устроить короткую викторину",
    "загадать загадку": "загадал(а) дружескую загадку",
    "обменяться загадками": "предложил(а) обменяться загадками",
    "мозговой штурм": "пригласил(а) на мозговой штурм",
    "сделать плакат": "предложил(а) придумать идею плаката",
    "придумать эмблему": "предложил(а) придумать эмблему команды",
    "назвать команду": "предложил(а) выбрать название команды",
    "написать историю": "предложил(а) сочинить короткую общую историю",
    "придумать персонажа": "предложил(а) придумать вымышленного персонажа",
    "собрать плейлист": "предложил(а) собрать общий плейлист",
    "устроить челлендж": "предложил(а) безопасный творческий челлендж",
    "нарисовать идею": "предложил(а) набросать идею рисунка",
    "придумать мем": "предложил(а) придумать новый мем",
    "обсудить игру": "предложил(а) обсудить любимую игру",
    "обсудить фильм": "предложил(а) обсудить фильм",
    "обсудить книгу": "предложил(а) обсудить книгу",
    "обменяться фактами": "предложил(а) обменяться интересными фактами",
    "устроить блиц": "предложил(а) короткий блиц-опрос",
    "придумать загадочный вопрос": "предложил(а) загадочный вопрос",
    "поделиться лайфхаком": "поделился(ась) полезным лайфхаком",
    "сделать доброе дело": "предложил(а) придумать доброе дело для чата",
    "собрать коллекцию": "предложил(а) начать общую коллекцию идей",
    "придумать маршрут": "предложил(а) придумать маршрут вымышленного путешествия",
    "сделать карточку": "предложил(а) создать карточку достижения",
    "придумать награду": "предложил(а) придумать шуточную награду",
    "устроить обмен": "предложил(а) обменяться рекомендациями",
    "создать традицию": "предложил(а) придумать традицию команды",
    "подвести итоги": "предложил(а) подвести итоги общего дела",
    "пожелать новых идей": "пожелал(а) новых интересных идей",
}

COMMANDS = (
    "/jf_social", "предложить напарничество", "пригласить в напарники", "принять напарничество",
    "отклонить напарничество", "мой напарник", "напарники", "отменить напарничество",
    "отн запрос", "отн да", "отн нет", "отн я", "отн список", "отн топ", "отн документ",
    "отн стата", "отн статистика", "отн история связи", "отн обида", "отн проверка",
    "отн расторгнуть", "отн контроль", "отн кт", "отн передать", "отн перенос",
    "отн действия", "отн поддержать", "отн поздравить", "отн похвалить", "отн дать пять", "действия", "действие",
)
PATTERN = r"(?isu)^\s*(?:/jf_social(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:" + "|".join(re.escape(x) for x in sorted(COMMANDS[1:], key=len, reverse=True)) + r")(?:\s|$).*)"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def esc(s):
    return html.escape(str(s or ""), quote=False)


def _age_days(iso_value: str) -> int:
    try:
        dt = datetime.fromisoformat(iso_value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days)
    except Exception:
        return 0


class Social:
    def __init__(self, dp, bot, F, path, lock=None, manager=None):
        self.dp, self.bot, self.F, self.path = dp, bot, F, str(path)
        self.manager = manager
        self.lock = lock or threading.RLock()
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
                with suppress(Exception):
                    c.rollback()
                raise
            finally:
                c.close()

    def init(self):
        def op(c):
            c.execute("""CREATE TABLE IF NOT EXISTS jf_social_invites(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                from_id INTEGER NOT NULL, to_id INTEGER NOT NULL,
                from_name TEXT NOT NULL DEFAULT '', to_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
                answered_at TEXT NOT NULL DEFAULT '', expires_at TEXT NOT NULL DEFAULT '')""")
            invite_cols = {r[1] for r in c.execute("PRAGMA table_info(jf_social_invites)").fetchall()}
            if "expires_at" not in invite_cols:
                c.execute("ALTER TABLE jf_social_invites ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''")
            c.execute("CREATE INDEX IF NOT EXISTS idx_jf_social_invites_inbox ON jf_social_invites(chat_id,to_id,status,id DESC)")
            c.execute("""CREATE TABLE IF NOT EXISTS jf_social_pairs(
                chat_id INTEGER NOT NULL, user_a INTEGER NOT NULL, user_b INTEGER NOT NULL,
                name_a TEXT NOT NULL DEFAULT '', name_b TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, PRIMARY KEY(chat_id,user_a),
                UNIQUE(chat_id,user_b), CHECK(user_a < user_b))""")
            c.execute("""CREATE TABLE IF NOT EXISTS jf_social_action_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
                action_key TEXT NOT NULL, created_at TEXT NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_jf_social_action_pair ON jf_social_action_log(chat_id,actor_id,target_id,id DESC)")
        self.run(op)

    def register(self):
        if not self.registered:
            self.dp.message.register(self.handle, self.F.chat.type.in_(GROUP_TYPES), self.F.text.regexp(PATTERN))
            self.registered = True

    async def reply(self, m, text):
        try:
            await m.reply(text, parse_mode="HTML", disable_web_page_preview=True)
        except TypeError:
            await m.reply(text)

    @staticmethod
    def display_name(user):
        return ("@" + user.username) if getattr(user, "username", None) else (getattr(user, "full_name", None) or getattr(user, "first_name", None) or "участник")

    def _pair(self, chat, uid):
        return self.run(lambda c: c.execute(
            "SELECT * FROM jf_social_pairs WHERE chat_id=? AND (user_a=? OR user_b=?)",
            (chat, uid, uid)).fetchone())

    async def _show_pair(self, m, chat, uid, *, certificate=False, stats=False):
        row = self._pair(chat, uid)
        if not row:
            return await self.reply(m, "🤝 У тебя пока нет активного напарничества. Ответь на сообщение участника командой <code>.отн запрос</code>.")
        partner_id = int(row["user_b"] if int(row["user_a"]) == uid else row["user_a"])
        partner_name = row["name_b"] if int(row["user_a"]) == uid else row["name_a"]
        days = _age_days(row["created_at"])
        if certificate:
            return await self.reply(m, f"📜 <b>Сертификат дружеской команды</b>\n\nУчастники: <a href=\"tg://user?id={int(row['user_a'])}\">{esc(row['name_a'])}</a> и <a href=\"tg://user?id={int(row['user_b'])}\">{esc(row['name_b'])}</a>.\nВместе с: {days} дн.\nСоздано: {esc(row['created_at'][:10])}.\n\nЭто добровольное дружеское напарничество, не романтический статус.")
        if stats:
            count = self.run(lambda c: c.execute(
                "SELECT COUNT(*) FROM jf_social_action_log WHERE chat_id=? AND ((actor_id=? AND target_id=?) OR (actor_id=? AND target_id=?))",
                (chat, uid, partner_id, partner_id, uid)).fetchone()[0])
            return await self.reply(m, f"📊 <b>Статистика напарничества</b>\nУчастник: <a href=\"tg://user?id={partner_id}\">{esc(partner_name)}</a>\nДней вместе: {days}\nДружеских действий: {count}.")
        return await self.reply(m, f"🤝 Твой напарник: <a href=\"tg://user?id={partner_id}\">{esc(partner_name or 'участник')}</a>.\nДней вместе: {days}.\nСвязь добровольная и может быть прекращена любой стороной командой <code>.отн расторгнуть</code>.")

    async def _invite(self, m, chat, uid, user):
        target_msg = getattr(m, "reply_to_message", None)
        target_user = getattr(target_msg, "from_user", None) if target_msg else None
        if not target_user:
            parts = (m.text or "").split(maxsplit=2)
            username = parts[2].strip().lstrip("@") if len(parts) > 2 else ""
            target_id = None
            if username and self.manager:
                with suppress(Exception):
                    target_id = self.manager.store.member_id(chat, username)
            if target_id:
                target_user = type("KnownUser", (), {"id": int(target_id), "is_bot": False, "username": username, "first_name": username})()
            else:
                return await self.reply(m, "Ответь на сообщение участника командой <code>.отн запрос</code> или укажи @username, которого бот уже знает. Неизвестные имена бот не угадывает.")
        target = int(target_user.id)
        if target == uid or getattr(target_user, "is_bot", False):
            return await self.reply(m, "Пригласить можно другого участника, не бота.")
        def create(c):
            pair = c.execute("SELECT 1 FROM jf_social_pairs WHERE chat_id=? AND (user_a=? OR user_b=?)", (chat, uid, uid)).fetchone()
            target_pair = c.execute("SELECT 1 FROM jf_social_pairs WHERE chat_id=? AND (user_a=? OR user_b=?)", (chat, target, target)).fetchone()
            if pair: raise ValueError("У тебя уже есть напарник. Сначала заверши текущее напарничество.")
            if target_pair: raise ValueError("У этого участника уже есть напарник.")
            pending = c.execute("SELECT 1 FROM jf_social_invites WHERE chat_id=? AND from_id=? AND to_id=? AND status='pending'", (chat, uid, target)).fetchone()
            if pending: raise ValueError("Приглашение этому участнику уже отправлено.")
            reverse = c.execute("SELECT 1 FROM jf_social_invites WHERE chat_id=? AND from_id=? AND to_id=? AND status='pending'", (chat, target, uid)).fetchone()
            if reverse: raise ValueError("У тебя уже есть встречное приглашение. Напиши <code>.отн да</code>.")
            stamp = datetime.now(timezone.utc)
            c.execute("INSERT INTO jf_social_invites(chat_id,from_id,to_id,from_name,to_name,status,created_at,expires_at) VALUES(?,?,?,?,?,'pending',?,?)",
                      (chat, uid, target, self.display_name(user), self.display_name(target_user), stamp.isoformat(), (stamp + __import__('datetime').timedelta(days=7)).isoformat()))
        try:
            self.run(create, True)
        except ValueError as e:
            return await self.reply(m, esc(e))
        return await self.reply(m, f"🤝 Приглашение отправлено участнику {esc(self.display_name(target_user))}. Он должен сам принять или отклонить его командами <code>.отн да</code> / <code>.отн нет</code>.")

    async def _answer_invite(self, m, chat, uid, user, accept):
        stamp = now_iso()
        self.run(lambda c: c.execute("UPDATE jf_social_invites SET status='expired',answered_at=? WHERE chat_id=? AND to_id=? AND status='pending' AND expires_at<>'' AND expires_at<?", (stamp,chat,uid,stamp)), True)
        def answer(c):
            invite = c.execute("SELECT * FROM jf_social_invites WHERE chat_id=? AND to_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (chat, uid)).fetchone()
            if not invite:
                raise ValueError("У тебя нет ожидающих приглашений в напарники.")
            a, b = sorted((int(invite["from_id"]), uid))
            if accept:
                occupied = c.execute("SELECT 1 FROM jf_social_pairs WHERE chat_id=? AND ((user_a=? OR user_b=?) OR (user_a=? OR user_b=?))", (chat, a, a, b, b)).fetchone()
                if occupied:
                    raise ValueError("Кто-то из участников уже состоит в напарничестве. Приглашение остаётся ожидающим.")
                name_a, name_b = (invite["from_name"], self.display_name(user)) if int(invite["from_id"]) == a else (self.display_name(user), invite["from_name"])
                c.execute("INSERT INTO jf_social_pairs(chat_id,user_a,user_b,name_a,name_b,created_at) VALUES(?,?,?,?,?,?)", (chat, a, b, name_a, name_b, now_iso()))
            c.execute("UPDATE jf_social_invites SET status=?,answered_at=? WHERE id=?", ("accepted" if accept else "declined", now_iso(), invite["id"]))
            return invite
        try:
            invite = self.run(answer, True)
        except ValueError as e:
            return await self.reply(m, esc(e))
        if accept:
            return await self.reply(m, f"🤝 Напарничество подтверждено! Теперь вы с {esc(invite['from_name'])} — дружеская команда. Любой участник может завершить связь.")
        return await self.reply(m, "Приглашение отклонено. Никаких изменений в профилях участников не сделано.")

    async def _social_action(self, m, chat, uid, action_key):
        target_msg = getattr(m, "reply_to_message", None)
        target_user = getattr(target_msg, "from_user", None) if target_msg else None
        if not target_user:
            return await self.reply(m, "Ответь на сообщение участника командой <code>Действие название</code>.")
        target = int(target_user.id)
        if target == uid or getattr(target_user, "is_bot", False):
            return await self.reply(m, "Выбери другого участника, не бота.")
        action = BASIC_ACTIONS.get(action_key) or EXTRA_ACTIONS.get(action_key)
        if not action:
            return await self.reply(m, "Неизвестное действие. Напиши <code>Действия</code>, чтобы увидеть список.")
        def record(c):
            last = c.execute("SELECT created_at FROM jf_social_action_log WHERE chat_id=? AND actor_id=? ORDER BY id DESC LIMIT 1", (chat, uid)).fetchone()
            if last:
                try:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last[0])).total_seconds()
                    if elapsed < 5: raise ValueError("Подожди несколько секунд перед следующим действием.")
                except ValueError as exc:
                    if "Подожди" in str(exc): raise
            c.execute("INSERT INTO jf_social_action_log(chat_id,actor_id,target_id,action_key,created_at) VALUES(?,?,?,?,?)", (chat, uid, target, action_key, now_iso()))
        try:
            self.run(record, True)
        except ValueError as e:
            return await self.reply(m, esc(e))
        return await self.reply(m, f"✨ {esc(self.display_name(m.from_user))} {esc(action)} для {esc(self.display_name(target_user))}.")

    async def handle(self, m):
        user = getattr(m, "from_user", None)
        if not user or getattr(user, "is_bot", False):
            return
        raw = (m.text or "").strip()
        first = raw.splitlines()[0] if raw else ""
        first = re.sub(r"^\s*[!.\/]{0,4}", "", first).strip()
        first = re.sub(r"^(?:ирис(?:ка)?|iris)\s+", "", first, flags=re.I)
        low = first.casefold()
        chat, uid = int(m.chat.id), int(user.id)

        if low.startswith("/jf_social"):
            return await self.reply(m, self.help_text())
        if low in {"действия", "действие"}:
            basic = ", ".join(BASIC_ACTIONS)
            extra = ", ".join(EXTRA_ACTIONS)
            return await self.reply(m, f"<b>Дружеские действия (30)</b>\n{esc(basic)}\n\n<b>Дополнительные действия (30)</b>\n{esc(extra)}\n\nИспользование: ответь на сообщение командой <code>Действие поддержать</code>. Все действия бесплатные и не создают романтический статус.")
        if low.startswith("действие "):
            return await self._social_action(m, chat, uid, first[len("действие "):].strip().casefold())

        if low.startswith("отн запрос") or low in {"предложить напарничество", "пригласить в напарники"}:
            return await self._invite(m, chat, uid, user)
        if low in {"отн да", "принять напарничество"}:
            return await self._answer_invite(m, chat, uid, user, True)
        if low in {"отн нет", "отклонить напарничество"}:
            return await self._answer_invite(m, chat, uid, user, False)
        if low in {"отн я", "отн проверка", "мой напарник", "напарники"}:
            return await self._show_pair(m, chat, uid)
        if low in {"отн документ"}:
            return await self._show_pair(m, chat, uid, certificate=True)
        if low in {"отн стата", "отн статистика"}:
            return await self._show_pair(m, chat, uid, stats=True)
        if low == "отн действия":
            basic = ", ".join(BASIC_ACTIONS)
            extra = ", ".join(EXTRA_ACTIONS)
            return await self.reply(m, f"<b>Дружеские действия (30)</b>\n{esc(basic)}\n\n<b>Дополнительные действия (30)</b>\n{esc(extra)}\n\nВызов ответом: <code>Действие поддержать</code>.")
        shorthand_actions = {"отн поддержать":"поддержать", "отн поздравить":"поздравить", "отн похвалить":"похвалить", "отн дать пять":"дать пять"}
        if low in shorthand_actions:
            return await self._social_action(m, chat, uid, shorthand_actions[low])

        if low == "отн список":
            rows = self.run(lambda c: c.execute("SELECT * FROM jf_social_pairs WHERE chat_id=? ORDER BY created_at DESC LIMIT 50", (chat,)).fetchall())
            lines = [f"• {esc(r['name_a'])} ↔ {esc(r['name_b'])} · {_age_days(r['created_at'])} дн." for r in rows]
            return await self.reply(m, "<b>Дружеские команды чата</b>\n" + ("\n".join(lines) or "Пока нет активных команд."))
        if low.startswith("отн топ"):
            mode_parts = low.split(maxsplit=2)
            mode = mode_parts[2].strip() if len(mode_parts) > 2 else "длительность"
            if mode == "искры":
                return await self.reply(m, "Жетоны — личная валюта участника, а не общий показатель отношений. Для рейтинга используй <code>Топ богатых</code>.")
            if mode == "активность":
                rows = self.run(lambda c: c.execute("SELECT p.*,COUNT(a.id) AS actions FROM jf_social_pairs p LEFT JOIN jf_social_action_log a ON a.chat_id=p.chat_id AND ((a.actor_id=p.user_a AND a.target_id=p.user_b) OR (a.actor_id=p.user_b AND a.target_id=p.user_a)) WHERE p.chat_id=? GROUP BY p.user_a,p.user_b ORDER BY actions DESC,p.created_at ASC LIMIT 10", (chat,)).fetchall())
                lines = [f"{i}. {esc(r['name_a'])} ↔ {esc(r['name_b'])} · {r['actions']} действий" for i,r in enumerate(rows,1)]
                return await self.reply(m, "<b>Топ дружеских команд по активности</b>\n" + ("\n".join(lines) or "Пока нет активных команд."))
            if mode not in {"длительность", "время"}:
                return await self.reply(m, "Доступно: <code>.отн топ длительность</code>, <code>.отн топ активность</code>. Жетоны не являются рейтингом отношений.")
            rows = self.run(lambda c: c.execute("SELECT * FROM jf_social_pairs WHERE chat_id=? ORDER BY created_at ASC LIMIT 10", (chat,)).fetchall())
            lines = [f"{i}. {esc(r['name_a'])} ↔ {esc(r['name_b'])} · {_age_days(r['created_at'])} дн." for i, r in enumerate(rows, 1)]
            return await self.reply(m, "<b>Топ дружеских команд по времени</b>\n" + ("\n".join(lines) or "Пока нет активных команд."))
        if low in {"отн история", "отн история связи"}:
            rows = self.run(lambda c: c.execute("SELECT from_name,to_name,status,created_at FROM jf_social_invites WHERE chat_id=? AND (from_id=? OR to_id=?) ORDER BY id DESC LIMIT 10", (chat, uid, uid)).fetchall())
            lines = [f"• {esc(r['from_name'])} → {esc(r['to_name'])}: {esc(r['status'])} · {esc(r['created_at'][:10])}" for r in rows]
            return await self.reply(m, "<b>История приглашений</b>\n" + ("\n".join(lines) or "История пока пуста."))
        if low == "отн обида":
            return await self.reply(m, "В Justice Faite нет шкалы обид и скрытых проверок. Если напарничество больше некомфортно, можно спокойно завершить его командой <code>.отн расторгнуть</code>.")
        if low in {"отн контроль", "отн кт"}:
            return await self.reply(m, "Социальная связь не даёт контроля над аккаунтом, перепиской или решениями другого участника. Каждый управляет только своими действиями и может завершить связь.")
        if low == "отн обида":
            return await self.reply(m, "В Justice Faite нет шкалы обид и скрытых проверок. Если напарничество больше некомфортно, его можно завершить командой <code>.отн расторгнуть</code>.")
        if low == "отн передать":
            return await self.reply(m, "Передача или назначение владельца другому участнику не поддерживаются. Напарничество создаётся только по взаимному согласию.")
        if low == "отн перенос":
            return await self.reply(m, "Перенос существующей связи на третьего участника не поддерживается. Чтобы создать новую связь, отправь отдельное приглашение и дождись согласия.")
        if low in {"отн расторгнуть", "отменить напарничество"}:
            def end(c):
                row = c.execute("SELECT * FROM jf_social_pairs WHERE chat_id=? AND (user_a=? OR user_b=?)", (chat, uid, uid)).fetchone()
                if not row:
                    return None
                c.execute("DELETE FROM jf_social_pairs WHERE chat_id=? AND user_a=?", (chat, row["user_a"]))
                return row
            row = self.run(end, True)
            if not row:
                return await self.reply(m, "У тебя нет активного напарничества.")
            return await self.reply(m, "Напарничество завершено. Статус удалён у обоих участников.")

    @staticmethod
    def help_text():
        return ("<b>Социальные функции Justice Faite</b>\n"
                "<code>.отн запрос</code> — ответом на сообщение участника; приглашение требует согласия.\n"
                "<code>.отн да</code> / <code>.отн нет</code> — принять или отклонить приглашение.\n"
                "<code>.отн я</code>, <code>.отн список</code>, <code>.отн топ</code>, <code>.отн документ</code>, <code>.отн стата</code>, <code>.отн история связи</code>, <code>.отн проверка</code>.\n"
                "<code>.отн расторгнуть</code> — завершить связь.\n"
                "<code>Действия</code> — 30 дружеских и 30 дополнительных действий; вызов ответом: <code>Действие поддержать</code>.\n"
                "Команды обиды, контроля и передачи отвечают безопасной справкой: нет слежки, принуждения или управления другим участником.\n"
                "Это добровольная дружеская команда, не романтический статус. Сексуальные и взрослые ролевые функции не используются.")


def register(dp, bot, F, db_path, lock=None, manager=None):
    module = Social(dp, bot, F, db_path, lock=lock, manager=manager)
    module.register()
    return module

"""Consent-based, non-romantic social links and friendly actions for Justice Faite."""
from __future__ import annotations

import html
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timezone

GROUP_TYPES = {"group", "supergroup"}
SOCIAL_UTC_OFFSET_HOURS = int(__import__("os").getenv("JF_ECONOMY_UTC_OFFSET_HOURS", "3") or "3")


def _local_day_start_utc(now=None):
    current = now or datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc)
    local_now = current + __import__("datetime").timedelta(hours=SOCIAL_UTC_OFFSET_HOURS)
    local_midnight = datetime.combine(local_now.date(), datetime.min.time(), tzinfo=timezone.utc)
    return (local_midnight - __import__("datetime").timedelta(hours=SOCIAL_UTC_OFFSET_HOURS)).isoformat()

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
    "отношения", "отн уровень", "отн профиль", "отн искры",
)
SOCIAL_ALIASES = [x for x in COMMANDS[1:] if x != "отн искры"]
SOCIAL_ALIASES.append(r"отн\s+искры(?!\s+подарок\b)")
PATTERN = (r"(?isu)^\s*(?:/(?:jf_social|jf_social_media_set|jf_social_media_clear|jf_social_media_list)(?:@[A-Za-z0-9_]+)?(?:\s|$)|"
    r"[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:"
    + "|".join(x if x.startswith("отн\\s") else re.escape(x) for x in sorted(SOCIAL_ALIASES, key=len, reverse=True))
    + r")(?:\s|$).*)")


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
    def __init__(self, dp, bot, F, path, lock=None, manager=None, owner_id=0):
        self.dp, self.bot, self.F, self.path = dp, bot, F, str(path)
        self.manager = manager
        self.owner_id = int(owner_id or 0)
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
            # Additive migration: existing links remain active and receive the
            # same initial shared balance as a newly accepted pair.
            pair_cols = {r[1] for r in c.execute("PRAGMA table_info(jf_social_pairs)").fetchall()}
            for name, declaration in (
                ("sparks", "INTEGER NOT NULL DEFAULT 100"),
                ("lifetime_xp", "INTEGER NOT NULL DEFAULT 0"),
                ("level", "INTEGER NOT NULL DEFAULT 1"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in pair_cols:
                    c.execute(f"ALTER TABLE jf_social_pairs ADD COLUMN {name} {declaration}")
            c.execute("""CREATE TABLE IF NOT EXISTS jf_social_action_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
                action_key TEXT NOT NULL, created_at TEXT NOT NULL)""")
            action_cols = {r[1] for r in c.execute("PRAGMA table_info(jf_social_action_log)").fetchall()}
            if "operation_key" not in action_cols:
                c.execute("ALTER TABLE jf_social_action_log ADD COLUMN operation_key TEXT NOT NULL DEFAULT ''")
            if "source_message_id" not in action_cols:
                c.execute("ALTER TABLE jf_social_action_log ADD COLUMN source_message_id INTEGER NOT NULL DEFAULT 0")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jf_social_action_operation ON jf_social_action_log(operation_key) WHERE operation_key<>''")
            c.execute("CREATE INDEX IF NOT EXISTS idx_jf_social_action_pair ON jf_social_action_log(chat_id,actor_id,target_id,id DESC)")
            c.execute("""CREATE TABLE IF NOT EXISTS jf_social_spark_ledger(
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                user_a INTEGER NOT NULL, user_b INTEGER NOT NULL, delta INTEGER NOT NULL,
                balance_before INTEGER NOT NULL, balance_after INTEGER NOT NULL,
                lifetime_xp_after INTEGER NOT NULL, reason TEXT NOT NULL,
                actor_id INTEGER NOT NULL DEFAULT 0, operation_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_jf_social_spark_pair ON jf_social_spark_ledger(chat_id,user_a,user_b,id DESC)")
            # Backfill a visible opening-balance ledger row for pairs migrated
            # from v0.11.1, where the new shared-spark columns did not exist.
            legacy_pairs = c.execute("SELECT chat_id,user_a,user_b,sparks,lifetime_xp,created_at FROM jf_social_pairs").fetchall()
            for pair in legacy_pairs:
                op_key = f"pair-created:{int(pair['chat_id'])}:{int(pair['user_a'])}:{int(pair['user_b'])}:{pair['created_at']}"
                c.execute("""INSERT OR IGNORE INTO jf_social_spark_ledger(
                    chat_id,user_a,user_b,delta,balance_before,balance_after,
                    lifetime_xp_after,reason,actor_id,operation_key,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                    int(pair['chat_id']), int(pair['user_a']), int(pair['user_b']),
                    int(pair['sparks']), 0, int(pair['sparks']), int(pair['lifetime_xp']),
                    "pair_created", 0, op_key, now_iso()))
            c.execute("""CREATE TABLE IF NOT EXISTS jf_social_media(
                action_key TEXT PRIMARY KEY, file_id TEXT NOT NULL,
                updated_by INTEGER NOT NULL, updated_at TEXT NOT NULL)""")
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
        level = max(1, min(30, int(row["level"])))
        xp = int(row["lifetime_xp"])
        progress = 100 if level >= 30 else xp % 100
        level_line = f"Уровень: <b>{level}/30</b> · прогресс: <b>{progress}/100 XP</b>\nОбщий баланс: <b>{int(row['sparks'])} искр</b>\n"
        if certificate:
            return await self.reply(m, f"📜 <b>Сертификат дружеской команды</b>\n\nУчастники: <a href=\"tg://user?id={int(row['user_a'])}\">{esc(row['name_a'])}</a> и <a href=\"tg://user?id={int(row['user_b'])}\">{esc(row['name_b'])}</a>.\nВместе с: {days} дн.\n{level_line}Создано: {esc(row['created_at'][:10])}.\n\nЭто добровольная связь; её развитие не зависит от ежедневного присутствия.")
        if stats:
            count = self.run(lambda c: c.execute(
                "SELECT COUNT(*) FROM jf_social_action_log WHERE chat_id=? AND ((actor_id=? AND target_id=?) OR (actor_id=? AND target_id=?))",
                (chat, uid, partner_id, partner_id, uid)).fetchone()[0])
            earned = self.run(lambda c: c.execute(
                "SELECT COALESCE(SUM(delta),0) FROM jf_social_spark_ledger WHERE chat_id=? AND user_a=? AND user_b=?",
                (chat, int(row['user_a']), int(row['user_b']))).fetchone()[0])
            return await self.reply(m, f"📊 <b>Статистика напарничества</b>\nУчастник: <a href=\"tg://user?id={partner_id}\">{esc(partner_name)}</a>\nДней вместе: {days}\nДружеских действий: {count}\nВсего получено искр: {earned}\n{level_line}")
        return await self.reply(m, f"🤝 <b>Твоя дружеская команда</b>\nНапарник: <a href=\"tg://user?id={partner_id}\">{esc(partner_name or 'участник')}</a>\nДней вместе: {days}.\n{level_line}Уровень повышается за действия друг для друга. Каждый получает искры за первые 10 парных действий в сутки. Неактивность не уменьшает прогресс.")

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
                created_at = now_iso()
                c.execute("INSERT INTO jf_social_pairs(chat_id,user_a,user_b,name_a,name_b,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (chat, a, b, name_a, name_b, created_at, created_at))
                # Opening shared balance is a real ledger movement, not an
                # invisible default. It is idempotent and shares the pair transaction.
                c.execute("""INSERT OR IGNORE INTO jf_social_spark_ledger(
                    chat_id,user_a,user_b,delta,balance_before,balance_after,
                    lifetime_xp_after,reason,actor_id,operation_key,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                    chat, a, b, 100, 0, 100, 0, "pair_created", uid,
                    f"pair-created:{chat}:{a}:{b}:{created_at}", created_at))
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
        source_message_id = int(getattr(m, "message_id", 0) or 0)
        operation_key = f"social-action:{chat}:{uid}:{source_message_id}"
        def record(c):
            existing = c.execute("SELECT id FROM jf_social_action_log WHERE operation_key=?", (operation_key,)).fetchone()
            if existing:
                return {"duplicate": True, "sparks": 0, "level": None, "balance": None, "capped": False}
            last = c.execute("SELECT created_at FROM jf_social_action_log WHERE chat_id=? AND actor_id=? ORDER BY id DESC LIMIT 1", (chat, uid)).fetchone()
            if last:
                try:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last[0])).total_seconds()
                    if elapsed < 5:
                        raise ValueError("Подожди несколько секунд перед следующим действием.")
                except ValueError as exc:
                    if "Подожди" in str(exc):
                        raise
            stamp = now_iso()
            c.execute("INSERT INTO jf_social_action_log(chat_id,actor_id,target_id,action_key,created_at,operation_key,source_message_id) VALUES(?,?,?,?,?,?,?)", (chat, uid, target, action_key, stamp, operation_key, source_message_id))
            pair = c.execute("SELECT * FROM jf_social_pairs WHERE chat_id=? AND ((user_a=? AND user_b=?) OR (user_a=? AND user_b=?))", (chat, uid, target, target, uid)).fetchone()
            if not pair:
                return {"duplicate": False, "sparks": 0, "level": None, "balance": None, "capped": False}
            day_start = _local_day_start_utc()
            daily_count = int(c.execute("SELECT COUNT(*) FROM jf_social_spark_ledger WHERE chat_id=? AND user_a=? AND user_b=? AND actor_id=? AND reason='paired_action' AND created_at>=?", (chat, int(pair['user_a']), int(pair['user_b']), uid, day_start)).fetchone()[0])
            if daily_count >= 10:
                return {"duplicate": False, "sparks": 0, "level": int(pair['level']), "balance": int(pair['sparks']), "capped": True}
            before = int(pair['sparks'])
            after = before + 5
            xp_after = int(pair['lifetime_xp']) + 10
            level_after = min(30, 1 + xp_after // 100)
            ledger_key = f"pair-action:{chat}:{int(pair['user_a'])}:{int(pair['user_b'])}:{uid}:{source_message_id}"
            c.execute("INSERT INTO jf_social_spark_ledger(chat_id,user_a,user_b,delta,balance_before,balance_after,lifetime_xp_after,reason,actor_id,operation_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (chat, int(pair['user_a']), int(pair['user_b']), 5, before, after, xp_after, "paired_action", uid, ledger_key, stamp))
            c.execute("UPDATE jf_social_pairs SET sparks=?,lifetime_xp=?,level=?,updated_at=? WHERE chat_id=? AND user_a=?", (after, xp_after, level_after, stamp, chat, int(pair['user_a'])))
            return {"duplicate": False, "sparks": 5, "level": level_after, "balance": after, "capped": False}
        try:
            result = self.run(record, True)
        except ValueError as e:
            return await self.reply(m, esc(e))
        caption = f"✨ {esc(self.display_name(m.from_user))} {esc(action)} для {esc(self.display_name(target_user))}."
        if result["sparks"]:
            caption += f"\n💚 Общий баланс: +5 искр (теперь {result['balance']}). Уровень пары: {result['level']}/30."
        elif result["capped"]:
            caption += "\nДневной лимит начисления искр за действия исчерпан; действие сохранено в истории."
        media = self.run(lambda c: c.execute("SELECT file_id FROM jf_social_media WHERE action_key=?", (action_key,)).fetchone())
        if media and media["file_id"] and hasattr(m, "reply_photo"):
            try:
                await m.reply_photo(media["file_id"], caption=caption, parse_mode="HTML")
                return
            except Exception:
                # The text response is the reliable fallback if Telegram rejects
                # a stale/deleted file_id or media delivery is temporarily unavailable.
                pass
        return await self.reply(m, caption)

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

        if low.startswith("jf_social_media_set"):
            if not self.owner_id or uid != self.owner_id:
                return await self.reply(m, "⛔ Настраивать изображения может только владелец бота.")
            parts = first.split(maxsplit=1)
            action_key = parts[1].strip().casefold() if len(parts) > 1 else ""
            if action_key not in BASIC_ACTIONS and action_key not in EXTRA_ACTIONS:
                return await self.reply(m, "Укажи точное название действия из списка «Действия» и ответь командой на фотографию.")
            target_msg = getattr(m, "reply_to_message", None)
            photos = getattr(target_msg, "photo", None) if target_msg else None
            if not photos:
                return await self.reply(m, "Команда должна быть ответом на фотографию. Пример: <code>/jf_social_media_set поддержать</code>.")
            file_id = str(getattr(photos[-1], "file_id", "") or "")
            if not file_id:
                return await self.reply(m, "Не удалось получить Telegram file_id этой фотографии.")
            self.run(lambda c: c.execute("INSERT INTO jf_social_media(action_key,file_id,updated_by,updated_at) VALUES(?,?,?,?) ON CONFLICT(action_key) DO UPDATE SET file_id=excluded.file_id,updated_by=excluded.updated_by,updated_at=excluded.updated_at", (action_key, file_id, uid, now_iso())), True)
            return await self.reply(m, f"🖼️ Изображение для действия <b>{esc(action_key)}</b> сохранено.")
        if low.startswith("jf_social_media_clear"):
            if not self.owner_id or uid != self.owner_id:
                return await self.reply(m, "⛔ Настраивать изображения может только владелец бота.")
            parts = first.split(maxsplit=1)
            action_key = parts[1].strip().casefold() if len(parts) > 1 else ""
            if action_key not in BASIC_ACTIONS and action_key not in EXTRA_ACTIONS:
                return await self.reply(m, "Укажи точное название действия, например <code>/jf_social_media_clear поддержать</code>.")
            removed = self.run(lambda c: c.execute("DELETE FROM jf_social_media WHERE action_key=?", (action_key,)).rowcount, True)
            return await self.reply(m, "Изображение удалено." if removed else "Для этого действия изображение не было задано.")
        if low.startswith("jf_social_media_list"):
            if not self.owner_id or uid != self.owner_id:
                return await self.reply(m, "⛔ Смотреть настройки изображений может только владелец бота.")
            rows = self.run(lambda c: c.execute("SELECT action_key,updated_at FROM jf_social_media ORDER BY action_key").fetchall())
            lines = [f"• {esc(r['action_key'])} · {esc(r['updated_at'][:10])}" for r in rows]
            return await self.reply(m, "<b>Изображения действий</b>\n" + ("\n".join(lines) or "Пока не настроено ни одного изображения."))

        if low.startswith("jf_social"):
            return await self.reply(m, self.help_text())
        if low in {"отношения", "отн уровень", "отн профиль", "мой напарник", "напарники", "отн я", "отн проверка"}:
            return await self._show_pair(m, chat, uid)
        if low == "отн искры":
            row = self._pair(chat, uid)
            if not row:
                return await self.reply(m, "У тебя пока нет активной пары. Искры отношений появятся после взаимного принятия приглашения.")
            return await self.reply(m, f"✨ Общий баланс вашей пары: <b>{int(row['sparks'])} искр</b>. Уровень: <b>{int(row['level'])}/30</b>. Личные жетоны смотри командой <code>Баланс</code>.")
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
                rows = self.run(lambda c: c.execute("SELECT * FROM jf_social_pairs WHERE chat_id=? ORDER BY sparks DESC,level DESC,created_at ASC LIMIT 10", (chat,)).fetchall())
                lines = [f"{i}. {esc(r['name_a'])} ↔ {esc(r['name_b'])} · {int(r['sparks'])} искр" for i, r in enumerate(rows, 1)]
                return await self.reply(m, "<b>Топ пар по общим искрам</b>\n" + ("\n".join(lines) or "Пока нет активных пар."))
            if mode == "уровень":
                rows = self.run(lambda c: c.execute("SELECT * FROM jf_social_pairs WHERE chat_id=? ORDER BY level DESC,lifetime_xp DESC,created_at ASC LIMIT 10", (chat,)).fetchall())
                lines = [f"{i}. {esc(r['name_a'])} ↔ {esc(r['name_b'])} · уровень {int(r['level'])}/30" for i, r in enumerate(rows, 1)]
                return await self.reply(m, "<b>Топ пар по уровню</b>\n" + ("\n".join(lines) or "Пока нет активных пар."))
            if mode == "активность":
                rows = self.run(lambda c: c.execute("SELECT p.*,COUNT(a.id) AS actions FROM jf_social_pairs p LEFT JOIN jf_social_action_log a ON a.chat_id=p.chat_id AND ((a.actor_id=p.user_a AND a.target_id=p.user_b) OR (a.actor_id=p.user_b AND a.target_id=p.user_a)) WHERE p.chat_id=? GROUP BY p.user_a,p.user_b ORDER BY actions DESC,p.created_at ASC LIMIT 10", (chat,)).fetchall())
                lines = [f"{i}. {esc(r['name_a'])} ↔ {esc(r['name_b'])} · {r['actions']} действий" for i,r in enumerate(rows,1)]
                return await self.reply(m, "<b>Топ дружеских команд по активности</b>\n" + ("\n".join(lines) or "Пока нет активных команд."))
            if mode not in {"длительность", "время"}:
                return await self.reply(m, "Доступно: <code>.отн топ длительность</code>, <code>.отн топ активность</code>, <code>.отн топ уровень</code> и <code>.отн топ искры</code>.")
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
                "<code>Отношения</code> / <code>.отн уровень</code> — уровень 1–30 и прогресс. <code>.отн искры</code> — общий баланс пары.\n"
                "<code>.отн топ уровень</code> / <code>.отн топ искры</code> — рейтинги пар.\n"
                "<code>Действия</code> — 30 дружеских и 30 дополнительных действий; первые 10 парных действий за сутки дают награду.\n"
                "Владелец может настроить картинки: ответить командой <code>/jf_social_media_set поддержать</code> на фото.\n"
                "Команды обиды, контроля и передачи отвечают безопасной справкой: нет слежки, принуждения или управления другим участником.\n"
                "Это добровольная дружеская команда, не романтический статус. Сексуальные и взрослые ролевые функции не используются.")


def register(dp, bot, F, db_path, lock=None, manager=None, owner_id=0):
    module = Social(dp, bot, F, db_path, lock=lock, manager=manager, owner_id=owner_id)
    module.register()
    return module

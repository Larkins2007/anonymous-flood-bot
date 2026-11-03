"""Chat-scoped community tools for Justice Faite (clans, circles, notes, bookmarks)."""
from __future__ import annotations
import html
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional

GROUP_TYPES = {"group", "supergroup"}
MAX_TITLE, MAX_BODY = 40, 3500
COMMANDS = (
    "создать клан", "клан вступить", "выйти из клана", "клан изменить", "мой клан", "удалить клан", "кланы", "участники клана",
    "создать кружок", "кружок вступить", "выйти из кружка", "кружок основа", "кружок изменить",
    "участники кружка", "мои кружки", "удалить кружок", "кружки", "+клан", "-клан", "+кружок", "-кружок",
    "+заметка", "~заметка", "~название заметки", "-заметка", "текст заметки", "заметки", "заметка",
    "+закладка", "мои закладки", "закладки", "чатбук", "закладка", "удалить закладку", "исключить закладку", "убрать закладку", "-закладка",
)
COMMAND_PATTERN = r"(?ius)^\s*(?:/jf_community(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:" + "|".join(re.escape(x) for x in sorted(COMMANDS, key=len, reverse=True)) + r")\b.*$)"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def parse(raw: str):
    raw = (raw or "").strip()
    lines = raw.splitlines()
    first = lines[0] if lines else ""
    body = "\n".join(lines[1:]).strip()[:MAX_BODY]
    if first.strip().lower().split()[0:1] == ["/jf_community"]:
        return "/jf_community", "", body
    first = re.sub(r"^\s*[!.\/]{0,4}", "", first).strip()
    first = re.sub(r"^(?:ирис(?:ка)?|iris)\s+", "", first, flags=re.I)
    first = re.sub(r"^/jf_community\s*", "", first, flags=re.I)
    low = first.casefold()
    for cmd in sorted(COMMANDS, key=len, reverse=True):
        if low == cmd or low.startswith(cmd + " "):
            return cmd, first[len(cmd):].strip(), body
    if low.startswith("/jf_community"):
        return "/jf_community", first[len("/jf_community"):].strip(), body
    return "", "", body


class Store:
    def __init__(self, path: str, lock=None):
        self.path = str(path)
        self.lock = lock or threading.RLock()

    def run(self, fn):
        with self.lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                result = fn(conn)
                if conn.in_transaction:
                    conn.commit()
                return result
            except Exception:
                with suppress(Exception): conn.rollback()
                raise
            finally:
                conn.close()

    def init(self):
        def op(c):
            c.executescript("""
            CREATE TABLE IF NOT EXISTS jf_community_people(chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,username TEXT NOT NULL DEFAULT '',display_name TEXT NOT NULL DEFAULT '',seen_at TEXT NOT NULL,PRIMARY KEY(chat_id,user_id));
            CREATE TABLE IF NOT EXISTS jf_community_clans(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,owner_id INTEGER NOT NULL,name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_jf_clans_chat ON jf_community_clans(chat_id,id);
            CREATE TABLE IF NOT EXISTS jf_community_clan_members(chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,clan_id INTEGER NOT NULL,joined_at TEXT NOT NULL,PRIMARY KEY(chat_id,user_id),FOREIGN KEY(clan_id) REFERENCES jf_community_clans(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS jf_community_circles(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,owner_id INTEGER NOT NULL,name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jf_community_circle_members(chat_id INTEGER NOT NULL,circle_id INTEGER NOT NULL,user_id INTEGER NOT NULL,joined_at TEXT NOT NULL,is_main INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(chat_id,circle_id,user_id),FOREIGN KEY(circle_id) REFERENCES jf_community_circles(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS jf_community_notes(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,owner_id INTEGER NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_jf_notes_chat ON jf_community_notes(chat_id,id);
            CREATE TABLE IF NOT EXISTS jf_community_bookmarks(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,owner_id INTEGER NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL DEFAULT '',source_message_id INTEGER,source_url TEXT NOT NULL DEFAULT '',visible INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_jf_bookmarks_chat ON jf_community_bookmarks(chat_id,visible,id);
            """)
        self.run(op)

    def remember(self, chat_id, user):
        un = (getattr(user, "username", "") or "").strip()
        dn = " ".join(x for x in (getattr(user, "first_name", ""), getattr(user, "last_name", "")) if x).strip()
        self.run(lambda c: c.execute("""INSERT INTO jf_community_people VALUES(?,?,?,?,?)
          ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,seen_at=excluded.seen_at""", (int(chat_id), int(user.id), un, dn, now())))

    def rank(self, chat_id, user_id):
        try:
            row = self.run(lambda c: c.execute("SELECT rank FROM jf_manager_moderators WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id))).fetchone())
            return int(row[0]) if row else 0
        except sqlite3.OperationalError:
            return 0


class Community:
    def __init__(self, dp, bot, F, db_path, lock=None, owner_id=0):
        self.dp, self.bot, self.F = dp, bot, F
        self.store = Store(db_path, lock)
        self.owner_id = int(owner_id or 0)
        self.registered = False

    def init(self): self.store.init()

    def register(self):
        if not self.registered:
            self.dp.message.register(self.handle, self.F.chat.type.in_(GROUP_TYPES), self.F.text.regexp(COMMAND_PATTERN))
            self.registered = True

    async def rank(self, chat_id, user_id):
        if user_id == self.owner_id and self.owner_id: return 5
        rank = self.store.rank(chat_id, user_id)
        try:
            cm = await self.bot.get_chat_member(chat_id, user_id)
            if getattr(cm, "status", "") == "creator": return 5
            if getattr(cm, "status", "") == "administrator": return max(rank, 3)
        except Exception: pass
        return rank

    async def moderate(self, chat_id, user_id): return await self.rank(chat_id, user_id) >= 3

    async def reply(self, m, text):
        try: await m.reply(text, parse_mode="HTML", disable_web_page_preview=True)
        except TypeError: await m.reply(text)

    def msg_url(self, message, message_id):
        if not message_id: return ""
        username = getattr(message.chat, "username", None)
        if username: return f"https://t.me/{username}/{message_id}"
        raw = str(int(message.chat.id))
        return f"https://t.me/c/{raw[4:]}/{message_id}" if raw.startswith("-100") else ""

    async def handle(self, m):
        if not getattr(m, "from_user", None) or getattr(m.from_user, "is_bot", False): return
        if getattr(m.chat, "type", "") not in GROUP_TYPES: return
        chat_id, user_id = int(m.chat.id), int(m.from_user.id)
        self.store.remember(chat_id, m.from_user)
        cmd, args, body = parse(getattr(m, "text", ""))
        try:
            await self.dispatch(m, chat_id, user_id, cmd, args, body)
        except sqlite3.Error:
            await self.reply(m, "⚠️ Ошибка базы данных. Данные не были изменены.")
        except Exception:
            await self.reply(m, "⚠️ Не удалось выполнить команду. Проверь формат и права.")

    async def dispatch(self, m, chat_id, uid, cmd, args, body):
        # Clans: one clan membership per person in each chat.
        if cmd == "создать клан":
            name = args[:MAX_TITLE].strip()
            if not name: return await self.reply(m, "Формат: <code>Создать клан Название</code>\nОписание — следующей строкой.")
            count = self.store.run(lambda c: c.execute("SELECT COUNT(*) FROM jf_community_clans WHERE chat_id=?", (chat_id,)).fetchone()[0])
            if count >= 100: return await self.reply(m, "Достигнут лимит в 100 кланов на чат.")
            cid = self.store.run(lambda c: c.execute("INSERT INTO jf_community_clans(chat_id,owner_id,name,description,created_at) VALUES(?,?,?,?,?)", (chat_id,uid,name,body,now())).lastrowid)
            self.store.run(lambda c: c.execute("INSERT OR REPLACE INTO jf_community_clan_members VALUES(?,?,?,?)", (chat_id,uid,cid,now())))
            return await self.reply(m, f"✅ Клан <b>{esc(name)}</b> создан · №<code>{cid}</code>.")
        if cmd in {"клан вступить", "+клан"}:
            try: cid = int(args.split()[0])
            except Exception: return await self.reply(m, "Формат: <code>Клан вступить 12</code>")
            row = self.store.run(lambda c: c.execute("SELECT name FROM jf_community_clans WHERE chat_id=? AND id=?", (chat_id,cid)).fetchone())
            if not row: return await self.reply(m, "Клан не найден.")
            self.store.run(lambda c: c.execute("INSERT INTO jf_community_clan_members VALUES(?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET clan_id=excluded.clan_id,joined_at=excluded.joined_at", (chat_id,uid,cid,now())))
            return await self.reply(m, f"✅ Ты вступил в клан <b>{esc(row['name'])}</b>.")
        if cmd in {"выйти из клана", "-клан"}:
            n = self.store.run(lambda c: c.execute("DELETE FROM jf_community_clan_members WHERE chat_id=? AND user_id=?", (chat_id,uid)).rowcount)
            return await self.reply(m, "Ты вышел из клана." if n else "Ты не состоишь в клане.")
        if cmd == "мой клан":
            r = self.store.run(lambda c: c.execute("""SELECT cl.*,COUNT(allm.user_id) AS n FROM jf_community_clan_members mine JOIN jf_community_clans cl ON cl.id=mine.clan_id LEFT JOIN jf_community_clan_members allm ON allm.clan_id=cl.id AND allm.chat_id=cl.chat_id WHERE mine.chat_id=? AND mine.user_id=? GROUP BY cl.id""", (chat_id,uid)).fetchone())
            if not r: return await self.reply(m, "Ты пока не состоишь в клане.")
            return await self.reply(m, f"<b>{esc(r['name'])}</b> · №{r['id']}\nУчастников: {r['n']}\nСоздатель: <code>{r['owner_id']}</code>\n{esc(r['description'])}")
        if cmd == "кланы":
            rows = self.store.run(lambda c: c.execute("""SELECT cl.id,cl.name,COUNT(mem.user_id) AS n FROM jf_community_clans cl LEFT JOIN jf_community_clan_members mem ON mem.chat_id=cl.chat_id AND mem.clan_id=cl.id WHERE cl.chat_id=? GROUP BY cl.id ORDER BY cl.id DESC LIMIT 50""", (chat_id,)).fetchall())
            return await self.reply(m, "<b>Кланы</b>\n" + ("\n".join(f"<code>{r['id']}</code> · <b>{esc(r['name'])}</b> · {r['n']} уч." for r in rows) or "Кланов пока нет."))
        if cmd == "участники клана":
            try: cid=int(args.split()[0])
            except Exception: return await self.reply(m,"Формат: <code>Участники клана 12</code>")
            rows=self.store.run(lambda c:c.execute("SELECT p.user_id,p.username,p.display_name FROM jf_community_clan_members cm LEFT JOIN jf_community_people p ON p.chat_id=cm.chat_id AND p.user_id=cm.user_id WHERE cm.chat_id=? AND cm.clan_id=? ORDER BY cm.joined_at LIMIT 100",(chat_id,cid)).fetchall())
            return await self.reply(m,"<b>Участники клана</b>\n"+("\n".join("@"+esc(r['username']) if r['username'] else f"{esc(r['display_name'] or 'Участник')} · <code>{r['user_id']}</code>" for r in rows) or "Клан пуст или не найден."))
        if cmd == "клан изменить":
            p = args.split(maxsplit=1)
            if len(p) != 2 or not p[0].isdigit(): return await self.reply(m, "Формат: <code>Клан изменить 12 Название</code>")
            n = self.store.run(lambda c: c.execute("UPDATE jf_community_clans SET name=?,description=? WHERE chat_id=? AND id=? AND owner_id=?", (p[1][:MAX_TITLE],body,chat_id,int(p[0]),uid)).rowcount)
            return await self.reply(m, "✅ Клан обновлён." if n else "Клан не найден или изменить его может только создатель.")
        if cmd == "удалить клан":
            try: cid = int(args.split()[0])
            except Exception:
                r = self.store.run(lambda c: c.execute("SELECT id FROM jf_community_clans WHERE chat_id=? AND owner_id=? ORDER BY id DESC LIMIT 1", (chat_id,uid)).fetchone()); cid = int(r[0]) if r else 0
            r = self.store.run(lambda c: c.execute("SELECT owner_id FROM jf_community_clans WHERE chat_id=? AND id=?", (chat_id,cid)).fetchone())
            if not r: return await self.reply(m, "Клан не найден.")
            if int(r[0]) != uid and not await self.moderate(chat_id,uid): return await self.reply(m, "Удалить клан может создатель или администратор.")
            self.store.run(lambda c: c.execute("DELETE FROM jf_community_clans WHERE chat_id=? AND id=?", (chat_id,cid)))
            return await self.reply(m, "🗑 Клан удалён.")

        # Circles: multiple memberships are allowed.
        if cmd == "создать кружок":
            name = args[:MAX_TITLE].strip()
            if not name: return await self.reply(m, "Формат: <code>Создать кружок Название</code>")
            count = self.store.run(lambda c: c.execute("SELECT COUNT(*) FROM jf_community_circles WHERE chat_id=? AND owner_id=?", (chat_id,uid)).fetchone()[0])
            if count >= 10: return await self.reply(m, "Можно создать максимум 10 кружков.")
            cid = self.store.run(lambda c: c.execute("INSERT INTO jf_community_circles(chat_id,owner_id,name,description,created_at) VALUES(?,?,?,?,?)", (chat_id,uid,name,body,now())).lastrowid)
            self.store.run(lambda c: c.execute("INSERT INTO jf_community_circle_members VALUES(?,?,?,?,1)", (chat_id,cid,uid,now())))
            return await self.reply(m, f"✅ Кружок <b>{esc(name)}</b> создан · №<code>{cid}</code>.")
        if cmd in {"кружок вступить", "+кружок"}:
            try: cid = int(args.split()[0])
            except Exception: return await self.reply(m, "Формат: <code>Кружок вступить 7</code>")
            r = self.store.run(lambda c: c.execute("SELECT name FROM jf_community_circles WHERE chat_id=? AND id=?", (chat_id,cid)).fetchone())
            if not r: return await self.reply(m, "Кружок не найден.")
            self.store.run(lambda c: c.execute("INSERT OR IGNORE INTO jf_community_circle_members(chat_id,circle_id,user_id,joined_at) VALUES(?,?,?,?)", (chat_id,cid,uid,now())))
            return await self.reply(m, f"✅ Ты вступил в кружок <b>{esc(r['name'])}</b>.")
        if cmd in {"выйти из кружка", "-кружок"}:
            try: cid = int(args.split()[0])
            except Exception: return await self.reply(m, "Формат: <code>Выйти из кружка 7</code>")
            n = self.store.run(lambda c: c.execute("DELETE FROM jf_community_circle_members WHERE chat_id=? AND circle_id=? AND user_id=?", (chat_id,cid,uid)).rowcount)
            return await self.reply(m, "Ты вышел из кружка." if n else "Ты не состоишь в этом кружке.")
        if cmd in {"кружки", "мои кружки"}:
            if cmd == "кружки":
                rows = self.store.run(lambda c: c.execute("SELECT cr.id,cr.name,COUNT(m.user_id) n FROM jf_community_circles cr LEFT JOIN jf_community_circle_members m ON m.chat_id=cr.chat_id AND m.circle_id=cr.id WHERE cr.chat_id=? GROUP BY cr.id ORDER BY cr.id DESC LIMIT 50", (chat_id,)).fetchall())
            else:
                rows = self.store.run(lambda c: c.execute("SELECT cr.id,cr.name,cm.is_main,1 AS n FROM jf_community_circle_members cm JOIN jf_community_circles cr ON cr.id=cm.circle_id WHERE cm.chat_id=? AND cm.user_id=? ORDER BY cm.is_main DESC,cr.id", (chat_id,uid)).fetchall())
            return await self.reply(m, "<b>Кружки</b>\n" + ("\n".join(f"{'⭐ ' if r['is_main'] else ''}<code>{r['id']}</code> · <b>{esc(r['name'])}</b> · {r['n']} уч." for r in rows) or "Список пуст."))
        if cmd == "кружок основа":
            try: cid = int(args.split()[0])
            except Exception: return await self.reply(m, "Формат: <code>Кружок основа 7</code>")
            exists = self.store.run(lambda c: c.execute("SELECT 1 FROM jf_community_circle_members WHERE chat_id=? AND circle_id=? AND user_id=?", (chat_id,cid,uid)).fetchone())
            if not exists: return await self.reply(m, "Сначала вступи в этот кружок.")
            def set_main(c):
                c.execute("UPDATE jf_community_circle_members SET is_main=0 WHERE chat_id=? AND user_id=?", (chat_id,uid))
                c.execute("UPDATE jf_community_circle_members SET is_main=1 WHERE chat_id=? AND circle_id=? AND user_id=?", (chat_id,cid,uid))
            self.store.run(set_main)
            return await self.reply(m, "⭐ Основной кружок обновлён.")
        if cmd == "участники кружка":
            try: cid = int(args.split()[0])
            except Exception: return await self.reply(m, "Формат: <code>Участники кружка 7</code>")
            rows = self.store.run(lambda c: c.execute("SELECT p.user_id,p.username,p.display_name FROM jf_community_circle_members cm LEFT JOIN jf_community_people p ON p.chat_id=cm.chat_id AND p.user_id=cm.user_id WHERE cm.chat_id=? AND cm.circle_id=? ORDER BY cm.joined_at LIMIT 100", (chat_id,cid)).fetchall())
            return await self.reply(m, "<b>Участники кружка</b>\n" + ("\n".join("@"+esc(r['username']) if r['username'] else f"{esc(r['display_name'] or 'Участник')} · <code>{r['user_id']}</code>" for r in rows) or "Кружок пуст или не найден."))
        if cmd == "кружок изменить":
            p = args.split(maxsplit=1)
            if len(p) != 2 or not p[0].isdigit(): return await self.reply(m, "Формат: <code>Кружок изменить 7 Название</code>")
            n = self.store.run(lambda c: c.execute("UPDATE jf_community_circles SET name=?,description=? WHERE chat_id=? AND id=? AND owner_id=?", (p[1][:MAX_TITLE],body,chat_id,int(p[0]),uid)).rowcount)
            return await self.reply(m, "✅ Кружок обновлён." if n else "Кружок не найден или изменить его может только создатель.")
        if cmd == "удалить кружок":
            try: cid = int(args.split()[0])
            except Exception: return await self.reply(m, "Формат: <code>Удалить кружок 7</code>")
            r = self.store.run(lambda c: c.execute("SELECT owner_id FROM jf_community_circles WHERE chat_id=? AND id=?", (chat_id,cid)).fetchone())
            if not r: return await self.reply(m, "Кружок не найден.")
            if int(r[0]) != uid and not await self.moderate(chat_id,uid): return await self.reply(m, "Удалить кружок может создатель или администратор.")
            self.store.run(lambda c: c.execute("DELETE FROM jf_community_circles WHERE chat_id=? AND id=?", (chat_id,cid)))
            return await self.reply(m, "🗑 Кружок удалён.")

        # Notes are chat-wide; the author or rank >= 3 can edit/delete.
        if cmd == "+заметка":
            title = args[:MAX_TITLE].strip()
            if not title or not body: return await self.reply(m, "Формат:\n<code>+Заметка Название\nТекст</code>")
            count = self.store.run(lambda c: c.execute("SELECT COUNT(*) FROM jf_community_notes WHERE chat_id=?", (chat_id,)).fetchone()[0])
            if count >= 100: return await self.reply(m, "Лимит заметок — 100 на чат.")
            nid = self.store.run(lambda c: c.execute("INSERT INTO jf_community_notes(chat_id,owner_id,title,body,created_at,updated_at) VALUES(?,?,?,?,?,?)", (chat_id,uid,title,body,now(),now())).lastrowid)
            return await self.reply(m, f"📝 Заметка создана · №<code>{nid}</code>.")
        if cmd == "~название заметки":
            p=args.split(maxsplit=1)
            if not p or not p[0].isdigit() or not body: return await self.reply(m,"Формат: <code>~Название заметки 5\nНовое название</code>")
            new_title=body[:MAX_TITLE].strip()
            n=self.store.run(lambda c:c.execute("UPDATE jf_community_notes SET title=?,updated_at=? WHERE chat_id=? AND id=? AND (owner_id=? OR ? >= 3)",(new_title,now(),chat_id,int(p[0]),uid,self.store.rank(chat_id,uid))).rowcount)
            return await self.reply(m,"✅ Название заметки изменено." if n else "Заметка не найдена или нет прав.")
        if cmd in {"~заметка", "текст заметки"}:
            p = args.split(maxsplit=1)
            if not p or not body: return await self.reply(m, "Формат: <code>~Заметка 5\nНовый текст</code>")
            ident = p[0]
            n = self.store.run(lambda c: c.execute("UPDATE jf_community_notes SET body=?,updated_at=? WHERE chat_id=? AND (CAST(id AS TEXT)=? OR lower(title)=lower(?)) AND (owner_id=? OR ? >= 3)", (body,now(),chat_id,ident,ident,uid,self.store.rank(chat_id,uid))).rowcount)
            return await self.reply(m, "✅ Заметка обновлена." if n else "Заметка не найдена или нет прав.")
        if cmd == "-заметка":
            ident = args.strip()
            n = self.store.run(lambda c: c.execute("DELETE FROM jf_community_notes WHERE chat_id=? AND (CAST(id AS TEXT)=? OR lower(title)=lower(?)) AND (owner_id=? OR ? >= 3)", (chat_id,ident,ident,uid,self.store.rank(chat_id,uid))).rowcount)
            return await self.reply(m, "🗑 Заметка удалена." if n else "Заметка не найдена или нет прав.")
        if cmd == "заметки":
            rows = self.store.run(lambda c: c.execute("SELECT id,title,owner_id FROM jf_community_notes WHERE chat_id=? ORDER BY id DESC LIMIT 50", (chat_id,)).fetchall())
            lines = [f"<code>{r['id']}</code> · <b>{esc(r['title'])}</b> · автор <code>{r['owner_id']}</code>" for r in rows]
            return await self.reply(m, "<b>Заметки</b>\n" + ("\n".join(lines) or "Заметок пока нет."))
        if cmd == "заметка":
            ident = args.strip()
            r = self.store.run(lambda c: c.execute("SELECT * FROM jf_community_notes WHERE chat_id=? AND (CAST(id AS TEXT)=? OR lower(title)=lower(?)) ORDER BY id LIMIT 1", (chat_id,ident,ident)).fetchone())
            if not r: return await self.reply(m, "Заметка не найдена.")
            return await self.reply(m, f"<b>{esc(r['title'])}</b> · #{r['id']}\nАвтор: <code>{r['owner_id']}</code>\n\n{esc(r['body'])}")

        # Bookmarks point to existing Telegram messages; no guessed usernames.
        if cmd == "+закладка":
            title = args[:80].strip()
            target = getattr(m, "reply_to_message", None)
            if not title or target is None: return await self.reply(m, "Формат: ответь на сообщение командой <code>+Закладка Название</code>.")
            mid = int(getattr(target, "message_id", 0) or 0)
            url = self.msg_url(m, mid)
            snippet = (getattr(target, "text", None) or getattr(target, "caption", None) or "")[:500]
            bid = self.store.run(lambda c: c.execute("INSERT INTO jf_community_bookmarks(chat_id,owner_id,title,body,source_message_id,source_url,created_at) VALUES(?,?,?,?,?,?,?)", (chat_id,uid,title,body or snippet,mid,url,now())).lastrowid)
            return await self.reply(m, f"🔖 Закладка сохранена · №<code>{bid}</code>.")
        if cmd in {"мои закладки", "закладки"}:
            rows = self.store.run(lambda c: c.execute("SELECT id,title,visible,source_url FROM jf_community_bookmarks WHERE chat_id=? AND owner_id=? ORDER BY id DESC LIMIT 50", (chat_id,uid)).fetchall())
            return await self.reply(m, "<b>Мои закладки</b>\n" + ("\n".join(f"<code>{r['id']}</code> · {_safe_title(r['title'])}" for r in rows) or "Закладок пока нет."))
        if cmd == "чатбук":
            rows = self.store.run(lambda c: c.execute("SELECT id,title,owner_id,source_url FROM jf_community_bookmarks WHERE chat_id=? AND visible=1 ORDER BY id DESC LIMIT 30", (chat_id,)).fetchall())
            return await self.reply(m, "<b>Чатбук</b>\n" + ("\n".join(f"<code>{r['id']}</code> · <b>{esc(r['title'])}</b> · <code>{r['owner_id']}</code>" + (f" · <a href=\"{esc(r['source_url'])}\">открыть</a>" if r['source_url'] else "") for r in rows) or "Опубликованных закладок нет."))
        if cmd == "закладка":
            try: bid = int(args.split()[0])
            except Exception: return await self.reply(m, "Формат: <code>Закладка 12</code>")
            r = self.store.run(lambda c: c.execute("SELECT * FROM jf_community_bookmarks WHERE chat_id=? AND id=? AND (visible=1 OR owner_id=?)", (chat_id,bid,uid)).fetchone())
            if not r: return await self.reply(m, "Закладка не найдена.")
            link = f"\n<a href=\"{esc(r['source_url'])}\">Перейти к сообщению</a>" if r['source_url'] else ""
            return await self.reply(m, f"🔖 <b>{esc(r['title'])}</b>\n{esc(r['body'])}{link}")
        if cmd in {"удалить закладку", "исключить закладку", "убрать закладку", "-закладка"}:
            try: bid = int(args.split()[0])
            except Exception: return await self.reply(m, "Укажи номер закладки.")
            r = self.store.run(lambda c: c.execute("SELECT owner_id FROM jf_community_bookmarks WHERE chat_id=? AND id=?", (chat_id,bid)).fetchone())
            if not r: return await self.reply(m, "Закладка не найдена.")
            if int(r[0]) != uid and not await self.moderate(chat_id,uid): return await self.reply(m, "Нет прав на это действие.")
            if cmd in {"исключить закладку", "убрать закладку"}:
                self.store.run(lambda c: c.execute("UPDATE jf_community_bookmarks SET visible=0 WHERE chat_id=? AND id=?", (chat_id,bid)))
                return await self.reply(m, "Закладка скрыта из чатбука.")
            self.store.run(lambda c: c.execute("DELETE FROM jf_community_bookmarks WHERE chat_id=? AND id=?", (chat_id,bid)))
            return await self.reply(m, "🗑 Закладка удалена.")
        if cmd == "/jf_community":
            return await self.reply(m, "<b>Justice Faite Community</b>\nКланы: <code>Создать клан</code>, <code>Кланы</code>, <code>Клан вступить №</code>, <code>Мой клан</code>.\nКружки: <code>Создать кружок</code>, <code>Кружки</code>, <code>Мои кружки</code>.\nЗаметки: <code>+Заметка Название</code> + текст, <code>Заметки</code>.\nЗакладки: ответ на сообщение <code>+Закладка Название</code>, <code>Чатбук</code>.")


def _safe_title(value):
    return esc(value)


def register(dp, bot, F, db_path, lock=None, owner_id=0):
    module = Community(dp, bot, F, db_path, lock=lock, owner_id=owner_id)
    module.register()
    return module

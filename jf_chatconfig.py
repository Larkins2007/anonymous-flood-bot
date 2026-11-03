"""Telegram chat configuration and conservative link filtering for Justice Faite."""
from __future__ import annotations
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timezone

import cmdcfg

GROUP_TYPES = {"group", "supergroup"}
COMMANDS = (
    "+правила", "-правила", "правила", "+приветствие", "-приветствие", "приветствие", "+входы", "-входы", "+выходы", "-выходы", "+входы-выходы", "-входы-выходы", "+описание чата", "-описание чата", "!название", "название чата",
    "!закреп", "!открепить", "+чат ссылка", "-чат ссылка", "чат-ссылка", "чат ссылка",
    "+ссылки", "-ссылки", "+чаты", "-чаты", "+сайты", "-сайты", "+теги", "-теги", "+боты", "-боты",
    "!разрешить ссылку", "!удалить ссылку", "jf_chatconfig",
)
COMMAND_PATTERN = r"(?ius)^\s*(?:/jf_chatconfig(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:" + "|".join(re.escape(x) for x in sorted(COMMANDS, key=len, reverse=True)) + r")\b.*$)"
URL_EXTRACT_PATTERN = r"(?iu)(?:https?://|www\.)[^\s<>]+|(?:t\.me|telegram\.me)/[^\s<>]+"
URL_PATTERN = r"(?isu)^.*(?:https?://|www\.)[^\s<>]+|^.*(?:t\.me|telegram\.me)/[^\s<>]+"


def _now(): return datetime.now(timezone.utc).isoformat()


def _url(value):
    value = (value or "").strip().lower().rstrip(".,!?;:)]}")
    if value.startswith("www."): value = "https://" + value
    if not value.startswith(("http://", "https://")): value = "https://" + value
    return value


class Store:
    def __init__(self, path, lock=None): self.path, self.lock = str(path), lock or threading.RLock()
    def run(self, fn):
        with self.lock:
            c = sqlite3.connect(self.path, timeout=30); c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=30000")
            try:
                r = fn(c)
                if c.in_transaction: c.commit()
                return r
            except Exception:
                with suppress(Exception): c.rollback()
                raise
            finally: c.close()
    def init(self):
        self.run(lambda c: c.executescript("""
          CREATE TABLE IF NOT EXISTS jf_chatconfig_settings(chat_id INTEGER NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,updated_by INTEGER NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(chat_id,key));
          CREATE TABLE IF NOT EXISTS jf_chatconfig_invites(chat_id INTEGER PRIMARY KEY,invite_link TEXT NOT NULL,created_by INTEGER NOT NULL,created_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS jf_chatconfig_whitelist(chat_id INTEGER NOT NULL,url TEXT NOT NULL,created_by INTEGER NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(chat_id,url));
          CREATE TABLE IF NOT EXISTS jf_chatconfig_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,actor_id INTEGER NOT NULL,action TEXT NOT NULL,details TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
        """))
    def get(self, chat_id, key, default="0"):
        r = self.run(lambda c: c.execute("SELECT value FROM jf_chatconfig_settings WHERE chat_id=? AND key=?", (chat_id,key)).fetchone())
        return str(r[0]) if r else default
    def set(self, chat_id, key, value, actor):
        self.run(lambda c: c.execute("INSERT INTO jf_chatconfig_settings VALUES(?,?,?,?,?) ON CONFLICT(chat_id,key) DO UPDATE SET value=excluded.value,updated_by=excluded.updated_by,updated_at=excluded.updated_at", (chat_id,key,str(value),actor,_now())))
        self.run(lambda c: c.execute("INSERT INTO jf_chatconfig_audit(chat_id,actor_id,action,details,created_at) VALUES(?,?,?,?,?)", (chat_id,actor,"setting",f"{key}={value}",_now())))
    def allowed(self, chat_id, url):
        u = _url(url)
        return bool(self.run(lambda c: c.execute("SELECT 1 FROM jf_chatconfig_whitelist WHERE chat_id=? AND ? LIKE url || '%' LIMIT 1", (chat_id,u)).fetchone()))


class ChatConfig:
    def __init__(self, dp, bot, F, db_path, lock=None, owner_id=0):
        self.dp, self.bot, self.F = dp, bot, F
        self.store = Store(db_path, lock)
        self.owner_id = int(owner_id or 0)
        self.registered = False
    def init(self): self.store.init()
    def register(self):
        if self.registered: return
        self.dp.message.register(self.handle_command, self.F.chat.type.in_(GROUP_TYPES), self.F.text.regexp(COMMAND_PATTERN))
        self.dp.message.register(self.filter_links, self.F.chat.type.in_(GROUP_TYPES), self._link_filter_active, self._has_link)
        self.registered = True
    async def rank(self, chat_id, user_id):
        if user_id == self.owner_id and self.owner_id: return 5
        try:
            row = self.store.run(lambda c: c.execute("SELECT rank FROM jf_manager_moderators WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone())
            rank = int(row[0]) if row else 0
        except sqlite3.Error: rank = 0
        try:
            cm = await self.bot.get_chat_member(chat_id,user_id)
            if getattr(cm,"status","") == "creator": return 5
            if getattr(cm,"status","") == "administrator": return max(rank,3)
        except Exception: pass
        return rank
    def _link_filter_active(self, message):
        chat_id = int(message.chat.id)
        try:
            return any(self.store.get(chat_id,k,"0") == "1" for k in ("all_links","chats","sites","tags","bots"))
        except sqlite3.OperationalError:
            # The application initializes schemas before polling; this also keeps
            # offline routing probes and partial startup states harmless.
            return False
    @staticmethod
    def _has_link(message):
        if re.search(URL_EXTRACT_PATTERN, getattr(message, "text", "") or ""):
            return True
        return any(getattr(e, "type", None) == "text_link" and getattr(e, "url", None) for e in (getattr(message, "entities", None) or []))
    async def reply(self,m,text):
        try: await m.reply(text,parse_mode="HTML",disable_web_page_preview=True)
        except TypeError: await m.reply(text)
    async def admin_only(self,m):
        if cmdcfg.access_was_checked(m):
            return True
        rank = await self.rank(int(m.chat.id),int(m.from_user.id))
        if rank < 3:
            await self.reply(m,"⛔ Настраивать чат могут администраторы (ранг 3) и выше.")
            return False
        return True
    async def handle_command(self,m):
        if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False): return
        raw = (m.text or "").strip()
        lines = raw.splitlines(); first = lines[0] if lines else ""; body = "\n".join(lines[1:]).strip()
        is_help = first.lower().startswith("/jf_chatconfig")
        first = re.sub(r"^\s*[!.\/]{0,4}","",first).strip()
        first = re.sub(r"^(?:ирис(?:ка)?|iris)\s+","",first,flags=re.I)
        if is_help:
            return await self.reply(m,"<b>Настройки чата</b>\n<code>+Правила</code> + текст · <code>Правила</code> · <code>+Приветствие</code> + текст · <code>+Входы</code> / <code>-Входы</code> · <code>+Выходы</code> / <code>-Выходы</code> · <code>!Название текст</code> · <code>+Описание чата</code> + текст · <code>!Закреп</code> ответом · <code>!Открепить</code> · <code>+Чат ссылка</code> · фильтры <code>+Ссылки</code>, <code>+Сайты</code>, <code>+Чаты</code>, <code>+Теги</code>, <code>+Боты</code>." )
        low = first.casefold()
        chat_id, actor = int(m.chat.id), int(m.from_user.id)
        if low not in {"правила", "чат-ссылка", "чат ссылка"} and not await self.admin_only(m):
            return
        try:
            if low == "+приветствие":
                if not body: return await self.reply(m,"Добавь текст приветствия на следующей строке. Переменные: <code>{имя}</code>, <code>{username}</code>.")
                self.store.set(chat_id,"greeting",body[:1000],actor); return await self.reply(m,"✅ Приветствие сохранено.")
            if low == "-приветствие":
                self.store.set(chat_id,"greeting","",actor); return await self.reply(m,"Приветствие отключено.")
            if low == "приветствие":
                greeting=self.store.get(chat_id,"greeting","")
                return await self.reply(m,"<b>Приветствие</b>\n"+(re.sub(r"[&<>]",lambda x:{"&":"&amp;","<":"&lt;",">":"&gt;"}[x.group()],greeting) if greeting else "Не установлено."))
            event_toggles={"+входы":"join_notices","-входы":"join_notices","+выходы":"leave_notices","-выходы":"leave_notices","+входы-выходы":"both_notices","-входы-выходы":"both_notices"}
            if low in event_toggles:
                key=event_toggles[low];enabled=low.startswith("+")
                keys=("join_notices","leave_notices") if key=="both_notices" else (key,)
                for setting in keys:self.store.set(chat_id,setting,"1" if enabled else "0",actor)
                return await self.reply(m,f"{'✅ Уведомления включены.' if enabled else 'Уведомления выключены.'}")
            if low == "+правила":
                if not body: return await self.reply(m,"Добавь текст после команды: <code>+Правила\\nТекст правил</code>")
                self.store.set(chat_id,"rules",body[:3500],actor); return await self.reply(m,"✅ Правила чата сохранены.")
            if low == "-правила":
                self.store.set(chat_id,"rules","",actor); return await self.reply(m,"Правила удалены.")
            if low == "правила":
                rules=self.store.get(chat_id,"rules","")
                return await self.reply(m,"<b>Правила чата</b>\n"+(re.sub(r"[&<>]",lambda x:{"&":"&amp;","<":"&lt;",">":"&gt;"}[x.group()],rules) if rules else "Правила пока не установлены."))
            if low == "+описание чата":
                if not body: return await self.reply(m,"Добавь описание на следующей строке.")
                await self.bot.set_chat_description(chat_id,body[:255]); return await self.reply(m,"✅ Описание чата изменено.")
            if low == "-описание чата":
                await self.bot.set_chat_description(chat_id,""); return await self.reply(m,"Описание чата удалено.")
            if low.startswith("название ") or low.startswith("название чата "):
                title = first.split(" ",1)[1].strip()
                if not title or len(title)>128: return await self.reply(m,"Название должно содержать от 1 до 128 символов.")
                await self.bot.set_chat_title(chat_id,title); return await self.reply(m,"✅ Название чата изменено.")
            if low == "закреп":
                target=getattr(m,"reply_to_message",None)
                if not target: return await self.reply(m,"Ответь на сообщение командой <code>!Закреп</code>.")
                await self.bot.pin_chat_message(chat_id,int(target.message_id),disable_notification=True); return await self.reply(m,"📌 Сообщение закреплено.")
            if low == "открепить":
                await self.bot.unpin_chat_message(chat_id); return await self.reply(m,"Закреплённое сообщение откреплено.")
            if low == "+чат ссылка":
                r=await self.bot.create_chat_invite_link(chat_id,name="Justice Faite")
                link=getattr(r,"invite_link",None)
                if not link: return await self.reply(m,"Telegram не вернул ссылку. Проверь право бота приглашать участников.")
                self.store.run(lambda c:c.execute("INSERT INTO jf_chatconfig_invites VALUES(?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET invite_link=excluded.invite_link,created_by=excluded.created_by,created_at=excluded.created_at",(chat_id,link,actor,_now())))
                return await self.reply(m,f"🔗 Ссылка создана: {link}")
            if low == "-чат ссылка":
                r=self.store.run(lambda c:c.execute("SELECT invite_link FROM jf_chatconfig_invites WHERE chat_id=?",(chat_id,)).fetchone())
                if not r: return await self.reply(m,"Созданная ботом ссылка не найдена.")
                await self.bot.revoke_chat_invite_link(chat_id,r[0])
                self.store.run(lambda c:c.execute("DELETE FROM jf_chatconfig_invites WHERE chat_id=?",(chat_id,)))
                return await self.reply(m,"Ссылка, созданная ботом, отозвана.")
            if low in {"чат-ссылка","чат ссылка"}:
                r=self.store.run(lambda c:c.execute("SELECT invite_link FROM jf_chatconfig_invites WHERE chat_id=?",(chat_id,)).fetchone())
                return await self.reply(m,("🔗 "+r[0]) if r else "Ссылка ещё не создана командой +Чат ссылка.")
            filter_map={"+ссылки":"all_links","-ссылки":"all_links","+чаты":"chats","-чаты":"chats","+сайты":"sites","-сайты":"sites","+теги":"tags","-теги":"tags","+боты":"bots","-боты":"bots"}
            if low in filter_map:
                enabled = low.startswith("+")
                key=filter_map[low]; self.store.set(chat_id,key,"1" if enabled else "0",actor)
                return await self.reply(m,f"{'✅ Фильтр включён' if enabled else 'Фильтр выключен'}: <b>{key}</b>.")
            if low.startswith("разрешить ссылку "):
                url=_url(first.split(" ",2)[2])
                self.store.run(lambda c:c.execute("INSERT OR IGNORE INTO jf_chatconfig_whitelist VALUES(?,?,?,?)",(chat_id,url,actor,_now())))
                return await self.reply(m,"✅ Ссылка добавлена в исключения.")
            if low.startswith("удалить ссылку "):
                url=_url(first.split(" ",2)[2])
                n=self.store.run(lambda c:c.execute("DELETE FROM jf_chatconfig_whitelist WHERE chat_id=? AND url=?",(chat_id,url)).rowcount)
                return await self.reply(m,"Исключение удалено." if n else "Такой ссылки нет в исключениях.")
        except Exception:
            return await self.reply(m,"⚠️ Telegram не выполнил действие. Проверь права бота и попробуй снова.")

    async def on_member_update(self,event):
        """Send an optional plain-text welcome when a new user joins."""
        try:
            old=getattr(event,"old_chat_member",None); new=getattr(event,"new_chat_member",None)
            user=getattr(new,"user",None)
            if not user or getattr(user,"is_bot",False): return
            old_status=getattr(old,"status","")
            new_status=getattr(new,"status","")
            joined=(new_status in {"member","administrator","creator"} and old_status in {"left","kicked",""})
            chat_id=int(event.chat.id); greeting=self.store.get(chat_id,"greeting","").strip()
            name=" ".join(x for x in (getattr(user,"first_name","") or "",getattr(user,"last_name","") or "") if x).strip() or "участник"
            safe_name=re.sub(r"[\r\n]"," ",name)[:100]
            username=("@"+user.username) if getattr(user,"username",None) else safe_name
            safe_username=re.sub(r"[\r\n]"," ",username)[:100]
            if joined:
                if greeting:
                    output=greeting.replace("{имя}",safe_name).replace("{username}",safe_username).replace("{id}",str(int(user.id)))
                    await self.bot.send_message(chat_id,output[:1000],disable_web_page_preview=True)
                elif self.store.get(chat_id,"join_notices","0")=="1":
                    await self.bot.send_message(chat_id,f"➕ {safe_name} присоединился к чату.")
            elif old_status in {"member","administrator","creator"} and new_status in {"left","kicked"} and self.store.get(chat_id,"leave_notices","0")=="1":
                await self.bot.send_message(chat_id,f"➖ {safe_name} покинул чат.")
        except Exception:
            return

    async def filter_links(self,m):
        if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False): return
        chat_id, user_id = int(m.chat.id), int(m.from_user.id)
        if await self.rank(chat_id,user_id) >= 2: return
        text = m.text or ""
        urls = re.findall(URL_EXTRACT_PATTERN,text)
        for entity in (getattr(m,"entities",None) or []):
            if getattr(entity,"type",None)=="text_link":
                url=getattr(entity,"url",None)
                if url: urls.append(url)
        toggles={k:self.store.get(chat_id,k,"0")=="1" for k in ("all_links","chats","sites","tags","bots")}
        for raw in urls:
            url=_url(raw)
            if self.store.allowed(chat_id,url): continue
            host_path=url.removeprefix("https://").removeprefix("http://")
            is_telegram=host_path.startswith(("t.me/","telegram.me/"))
            is_chat=is_telegram and any(x in host_path for x in ("+","joinchat","/c/"))
            is_bot=is_telegram and ("bot" in host_path.split("/")[1].casefold() or "?start=" in host_path)
            is_tag=is_telegram and not is_chat
            is_site=not is_telegram
            blocked=toggles["all_links"] or (is_chat and toggles["chats"]) or (is_bot and toggles["bots"]) or (is_tag and toggles["tags"]) or (is_site and toggles["sites"])
            if not blocked: continue
            try:
                await self.bot.delete_message(chat_id,int(m.message_id))
                if toggles["all_links"]: self.store.run(lambda c:c.execute("INSERT INTO jf_chatconfig_audit(chat_id,actor_id,action,details,created_at) VALUES(?,?,?,?,?)",(chat_id,user_id,"blocked_link",url,_now())))
            except Exception: pass
            return


def register(dp, bot, F, db_path, lock=None, owner_id=0):
    module=ChatConfig(dp,bot,F,db_path,lock=lock,owner_id=owner_id)
    module.register()
    return module

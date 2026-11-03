"""Chat-scoped user profiles for Justice Faite Manager."""
from __future__ import annotations
import html
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timezone
import cmdcfg

GROUP_TYPES={"group","supergroup"}
COMMANDS=(
    "моя анкета","анкета","+анкета","-анкета","кто я","кто ты",
    "+ник","ник удалить","ник","-ник","+звание","звание удалить","звание","-звание",
    "о себе","описание","-о себе","-описание","+девиз","-девиз","!девиз","девиз",
    "назначить ник","удалить ник","назначить звание","удалить звание",
)
COMMAND_PATTERN=r"(?ius)^\s*(?:/jf_profile(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:"+"|".join(re.escape(x) for x in sorted(COMMANDS,key=len,reverse=True))+r")\b.*$)"

def _now(): return datetime.now(timezone.utc).isoformat()
def _esc(v): return html.escape(str(v or ""),quote=False)

class Store:
    def __init__(self,path,lock=None): self.path=str(path); self.lock=lock or threading.RLock()
    def run(self,fn):
        with self.lock:
            c=sqlite3.connect(self.path,timeout=30);c.row_factory=sqlite3.Row;c.execute("PRAGMA busy_timeout=30000")
            try:
                out=fn(c)
                if c.in_transaction:c.commit()
                return out
            except Exception:
                with suppress(Exception):c.rollback()
                raise
            finally:c.close()
    def init(self):
        self.run(lambda c:c.executescript("""
          CREATE TABLE IF NOT EXISTS jf_profiles(chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,nickname TEXT NOT NULL DEFAULT '',title TEXT NOT NULL DEFAULT '',about TEXT NOT NULL DEFAULT '',motto TEXT NOT NULL DEFAULT '',visible INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL,updated_by INTEGER NOT NULL,PRIMARY KEY(chat_id,user_id));
          CREATE INDEX IF NOT EXISTS idx_jf_profiles_chat_nick ON jf_profiles(chat_id,nickname);
        """))
    def get(self,chat_id,user_id):
        return self.run(lambda c:c.execute("SELECT * FROM jf_profiles WHERE chat_id=? AND user_id=?",(chat_id,user_id)).fetchone())
    def update(self,chat_id,user_id,key,value,actor):
        self.run(lambda c:c.execute(f"INSERT INTO jf_profiles(chat_id,user_id,{key},updated_at,updated_by) VALUES(?,?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET {key}=excluded.{key},updated_at=excluded.updated_at,updated_by=excluded.updated_by",(chat_id,user_id,value,_now(),actor)))

class Profiles:
    def __init__(self,dp,bot,F,db_path,lock=None,owner_id=0):
        self.dp,self.bot,self.F=dp,bot,F;self.store=Store(db_path,lock);self.owner_id=int(owner_id or 0);self.registered=False
    def init(self):self.store.init()
    def register(self):
        if not self.registered:
            self.dp.message.register(self.handle,self.F.chat.type.in_(GROUP_TYPES),self.F.text.regexp(COMMAND_PATTERN));self.registered=True
    async def rank(self,chat_id,uid):
        if self.owner_id and uid==self.owner_id:return 5
        try:
            r=self.store.run(lambda c:c.execute("SELECT rank FROM jf_manager_moderators WHERE chat_id=? AND user_id=?",(chat_id,uid)).fetchone());rank=int(r[0]) if r else 0
        except sqlite3.Error:rank=0
        try:
            cm=await self.bot.get_chat_member(chat_id,uid)
            if getattr(cm,"status","")=="creator":return 5
            if getattr(cm,"status","")=="administrator":return max(rank,3)
        except Exception:pass
        return rank
    async def reply(self,m,text):
        try:await m.reply(text,parse_mode="HTML",disable_web_page_preview=True)
        except TypeError:await m.reply(text)
    def _resolve(self,m,args):
        reply=getattr(m,"reply_to_message",None)
        if reply and getattr(reply,"from_user",None):return int(reply.from_user.id),args.strip()
        p=(args or "").split()
        if p and p[-1].startswith("@"):
            try:
                r=self.store.run(lambda c:c.execute("SELECT user_id FROM jf_manager_member_names WHERE chat_id=? AND lower(username)=? ORDER BY last_seen DESC LIMIT 1",(int(m.chat.id),p[-1][1:].casefold())).fetchone())
                if r:return int(r[0])," ".join(p[:-1]).strip()
            except sqlite3.Error:pass
        if p and p[-1].isdigit():return int(p[-1])," ".join(p[:-1]).strip()
        return None,args.strip()
    def _display_name(self,chat_id,uid):
        try:r=self.store.run(lambda c:c.execute("SELECT username,display_name FROM jf_manager_member_names WHERE chat_id=? AND user_id=?",(chat_id,uid)).fetchone())
        except sqlite3.Error:r=None
        if r and r[0]:return "@"+r[0]
        if r and r[1]:return r[1]
        return f"ID {uid}"
    async def _card(self,m,chat_id,viewer_id,target_id):
        row=self.store.get(chat_id,target_id)
        if not row:row={"nickname":"","title":"","about":"","motto":"","visible":1}
        if target_id!=viewer_id and not int(row["visible"]) and await self.rank(chat_id,viewer_id)<3:return await self.reply(m,"🔒 Участник скрыл свою анкету.")
        label=self._display_name(chat_id,target_id)
        lines=[f"<b>Анкета · {_esc(label)}</b>"]
        if row["nickname"]:lines.append(f"Ник: <b>{_esc(row['nickname'])}</b>")
        if row["title"]:lines.append(f"Звание: <b>{_esc(row['title'])}</b>")
        if row["motto"]:lines.append(f"Девиз: <i>{_esc(row['motto'])}</i>")
        if row["about"]:lines.extend(["","<b>О себе</b>",_esc(row["about"])])
        if len(lines)==1:lines.append("Пользователь пока не заполнил анкету.")
        return await self.reply(m,"\n".join(lines))
    async def handle(self,m):
        if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False):return
        raw=(m.text or "").strip();lines=raw.splitlines();first=lines[0] if lines else "";body="\n".join(lines[1:]).strip()
        is_help=first.lower().startswith("/jf_profile")
        first=re.sub(r"^\s*[!.\/]{0,4}","",first).strip();first=re.sub(r"^(?:ирис(?:ка)?|iris)\s+","",first,flags=re.I)
        if is_help:return await self.reply(m,"<b>Анкеты Justice Faite</b>\n<code>Моя анкета</code>, <code>Анкета @user</code>, <code>+Ник текст</code>, <code>+Звание текст</code>, <code>О себе</code> + текст, <code>+Девиз текст</code>, <code>-Анкета</code> / <code>+Анкета</code>.")
        cmdline=first.casefold();chat_id,uid=int(m.chat.id),int(m.from_user.id)
        for cmd in sorted(COMMANDS,key=len,reverse=True):
            if cmdline==cmd or cmdline.startswith(cmd+" "):
                args=first[len(cmd):].strip();command=cmd;break
        else:return
        if command in {"моя анкета","кто я"}:return await self._card(m,chat_id,uid,uid)
        if command in {"анкета","кто ты"}:
            target,rest=self._resolve(m,args)
            return await self._card(m,chat_id,uid,target or uid)
        if command=="+анкета":
            self.store.update(chat_id,uid,"visible",1,uid);return await self.reply(m,"✅ Анкета открыта для участников чата.")
        if command=="-анкета":
            self.store.update(chat_id,uid,"visible",0,uid);return await self.reply(m,"🔒 Анкета скрыта от других участников.")
        if command in {"+ник","-ник","ник","ник удалить"}:
            if command=="ник":
                target,rest=self._resolve(m,args);target=target or uid
                row=self.store.get(chat_id,target);nick=(row["nickname"] if row else "")
                return await self.reply(m,f"Ник: <b>{_esc(nick or 'не установлен')}</b>")
            if command in {"-ник","ник удалить"}:
                target,rest=self._resolve(m,args)
                if target is None:target=uid
                if target!=uid and await self.rank(chat_id,uid)<3 and not cmdcfg.access_was_checked(m):return await self.reply(m,"⛔ Установить или удалить ник другого участника может администратор.")
                self.store.update(chat_id,target,"nickname","",uid);return await self.reply(m,"Ник удалён.")
            value=args[:30].strip()
            if not value:return await self.reply(m,"Формат: <code>+Ник Новый ник</code>")
            self.store.update(chat_id,uid,"nickname",value,uid);return await self.reply(m,"✅ Ник сохранён для этого чата.")
        if command in {"+звание","-звание","звание","звание удалить"}:
            if command=="звание":
                target,rest=self._resolve(m,args);target=target or uid;row=self.store.get(chat_id,target);value=row["title"] if row else ""
                return await self.reply(m,f"Звание: <b>{_esc(value or 'не установлено')}</b>")
            if command in {"-звание","звание удалить"}:
                target,rest=self._resolve(m,args)
                if target is None:target=uid
                if target!=uid and await self.rank(chat_id,uid)<3 and not cmdcfg.access_was_checked(m):return await self.reply(m,"⛔ Удалить чужое звание может администратор.")
                self.store.update(chat_id,target,"title","",uid);return await self.reply(m,"Звание удалено.")
            value=args[:30].strip()
            if not value:return await self.reply(m,"Формат: <code>+Звание Участник месяца</code>")
            self.store.update(chat_id,uid,"title",value,uid);return await self.reply(m,"✅ Звание сохранено.")
        if command in {"о себе","описание","-о себе","-описание"}:
            if command in {"-о себе","-описание"}:
                self.store.update(chat_id,uid,"about","",uid);return await self.reply(m,"Описание удалено.")
            if body:
                self.store.update(chat_id,uid,"about",body[:3800],uid);return await self.reply(m,"✅ Описание сохранено.")
            target,rest=self._resolve(m,args);target=target or uid;row=self.store.get(chat_id,target);about=row["about"] if row else ""
            return await self.reply(m,f"<b>О себе · {_esc(self._display_name(chat_id,target))}</b>\n{_esc(about or 'Описание не заполнено.')}")
        if command in {"+девиз","-девиз","!девиз","девиз"}:
            if command=="-девиз":
                self.store.update(chat_id,uid,"motto","",uid);return await self.reply(m,"Девиз удалён.")
            if command=="+девиз":
                value=args[:100].strip()
                if not value:return await self.reply(m,"Формат: <code>+Девиз Текст девиза</code>")
                self.store.update(chat_id,uid,"motto",value,uid);return await self.reply(m,"✅ Девиз сохранён.")
            row=self.store.get(chat_id,uid);return await self.reply(m,f"Твой девиз: <i>{_esc(row['motto'] if row else '') or 'не установлен'}</i>")
        if command in {"назначить ник","удалить ник","назначить звание","удалить звание"}:
            if await self.rank(chat_id,uid)<3 and not cmdcfg.access_was_checked(m):return await self.reply(m,"⛔ Команда доступна администраторам (ранг 3+) или ответом на сообщение участника.")
            target,value=self._resolve(m,args)
            if target is None:return await self.reply(m,"Укажи @username/ID в конце команды или ответь на сообщение участника.")
            key="nickname" if "ник" in command else "title"
            if command.startswith("удалить"):
                self.store.update(chat_id,target,key,"",uid);return await self.reply(m,"Поле удалено.")
            if not value:return await self.reply(m,"Укажи текст поля перед @username/ID.")
            self.store.update(chat_id,target,key,value[:30],uid);return await self.reply(m,"✅ Поле участника обновлено.")


def register(dp,bot,F,db_path,lock=None,owner_id=0):
    module=Profiles(dp,bot,F,db_path,lock=lock,owner_id=owner_id);module.register();return module

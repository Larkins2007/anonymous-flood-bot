"""Positive, rate-limited reputation for Justice Faite Manager.

The module deliberately supports appreciation only: no negative votes, paid boosts,
coin transfers, wagers, or trading.
"""
from __future__ import annotations
import html
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timezone

GROUP_TYPES={"group","supergroup"}
COMMAND_PATTERN=(r"(?iu)^\s*(?:/jf_reputation(?:@[A-Za-z0-9_]+)?(?:\s|$)|"
    r"[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:рейтинг|топ рейтинга|моя репутация|"
    r"моя звёздность|моя звездность|звёздность(?:\s+.*)?|звездность(?:\s+.*)?|"
    r"\+[1-3]?))\s*$")

def _now(): return datetime.now(timezone.utc)
def _esc(v): return html.escape(str(v or ""), quote=False)

class Store:
    def __init__(self,path,lock=None): self.path=str(path); self.lock=lock or threading.RLock()
    def run(self,fn):
        with self.lock:
            c=sqlite3.connect(self.path,timeout=30); c.row_factory=sqlite3.Row; c.execute("PRAGMA busy_timeout=30000")
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
          CREATE TABLE IF NOT EXISTS jf_reputation(chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,points INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,PRIMARY KEY(chat_id,user_id));
          CREATE TABLE IF NOT EXISTS jf_reputation_votes(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,voter_id INTEGER NOT NULL,target_id INTEGER NOT NULL,day TEXT NOT NULL,points INTEGER NOT NULL,source_message_id INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(chat_id,voter_id,target_id,day));
          CREATE INDEX IF NOT EXISTS idx_jf_reputation_chat_points ON jf_reputation(chat_id,points DESC);
        """))
    def add_vote(self,chat_id,voter,target,points,message_id):
        day=_now().date().isoformat();stamp=_now().isoformat()
        def op(c):
            used=c.execute("SELECT COALESCE(SUM(points),0) FROM jf_reputation_votes WHERE chat_id=? AND voter_id=? AND day=?",(chat_id,voter,day)).fetchone()[0]
            if int(used)+points>5:return "limit"
            try:c.execute("INSERT INTO jf_reputation_votes(chat_id,voter_id,target_id,day,points,source_message_id,created_at) VALUES(?,?,?,?,?,?,?)",(chat_id,voter,target,day,points,message_id,stamp))
            except sqlite3.IntegrityError:return "duplicate"
            c.execute("INSERT INTO jf_reputation(chat_id,user_id,points,updated_at) VALUES(?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET points=points+excluded.points,updated_at=excluded.updated_at",(chat_id,target,points,stamp))
            return "ok"
        return self.run(op)

class Reputation:
    def __init__(self,dp,bot,F,db_path,lock=None):self.dp,self.bot,self.F=dp,bot,F;self.store=Store(db_path,lock);self.registered=False
    def init(self):self.store.init()
    def register(self):
        if not self.registered:
            self.dp.message.register(self.handle,self.F.chat.type.in_(GROUP_TYPES),self.F.text.regexp(COMMAND_PATTERN));self.registered=True
    async def reply(self,m,text):
        try:await m.reply(text,parse_mode="HTML",disable_web_page_preview=True)
        except TypeError:await m.reply(text)
    def name(self,chat_id,uid):
        try:r=self.store.run(lambda c:c.execute("SELECT username,display_name FROM jf_manager_member_names WHERE chat_id=? AND user_id=?",(chat_id,uid)).fetchone())
        except sqlite3.Error:r=None
        return ("@"+r[0]) if r and r[0] else (r[1] if r and r[1] else f"ID {uid}")
    async def handle(self,m):
        if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False):return
        raw=(m.text or "").strip();first=raw.splitlines()[0] if raw else ""
        first=first.strip() if re.fullmatch(r"\+[1-3]?",first.strip()) else re.sub(r"^\s*[!.\/]{0,4}","",first).strip();first=re.sub(r"^(?:ирис(?:ка)?|iris)\s+","",first,flags=re.I)
        low=first.casefold();chat_id=int(m.chat.id);uid=int(m.from_user.id)
        if low.startswith("/jf_reputation"):
            return await self.reply(m,"<b>Репутация Justice Faite</b>\nОтветь командой <code>+</code> или <code>+3</code> на сообщение участника. Один участник может поддержать другого один раз в день, максимум 5 очков поддержки в сутки.")
        if low in {"рейтинг","топ рейтинга"}:
            rows=self.store.run(lambda c:c.execute("SELECT user_id,points FROM jf_reputation WHERE chat_id=? ORDER BY points DESC,user_id LIMIT 20",(chat_id,)).fetchall())
            lines=[f"{i}. {_esc(self.name(chat_id,int(r['user_id'])))} — <b>{r['points']}</b>" for i,r in enumerate(rows,1)]
            return await self.reply(m,"<b>Рейтинг участников</b>\n"+("\n".join(lines) or "Пока нет оценок."))
        if low in {"моя репутация","моя звёздность","моя звездность"} or low.startswith("звёздность ") or low.startswith("звездность "):
            target=uid
            if low.startswith(("звёздность ","звездность ")):
                arg=first.split(maxsplit=1)[1].strip()
                if m.reply_to_message and m.reply_to_message.from_user:target=int(m.reply_to_message.from_user.id)
                elif arg.isdigit():target=int(arg)
                elif arg.startswith("@"):
                    try:
                        r=self.store.run(lambda c:c.execute("SELECT user_id FROM jf_manager_member_names WHERE chat_id=? AND lower(username)=? ORDER BY last_seen DESC LIMIT 1",(chat_id,arg[1:].casefold())).fetchone());target=int(r[0]) if r else uid
                    except sqlite3.Error:target=uid
            r=self.store.run(lambda c:c.execute("SELECT points FROM jf_reputation WHERE chat_id=? AND user_id=?",(chat_id,target)).fetchone())
            points=int(r[0]) if r else 0
            return await self.reply(m,f"<b>Репутация · {_esc(self.name(chat_id,target))}</b>\nОчки поддержки: {points}\nУровень: {points//10}")
        if low in {"+","+1","+2","+3"}:
            target_user=getattr(getattr(m,"reply_to_message",None),"from_user",None)
            if not target_user:return await self.reply(m,"Ответь этой командой на сообщение участника.")
            target=int(target_user.id)
            if target==uid:return await self.reply(m,"Нельзя начислять очки самому себе.")
            if getattr(target_user,"is_bot",False):return await self.reply(m,"Ботам нельзя начислять очки репутации.")
            points=1 if low=="+" else int(low[1:])
            result=self.store.add_vote(chat_id,uid,target,points,int(m.reply_to_message.message_id))
            if result=="limit":return await self.reply(m,"На сегодня достигнут лимит поддержки: 5 очков.")
            if result=="duplicate":return await self.reply(m,"Ты уже поддержал этого участника сегодня.")
            return await self.reply(m,f"✨ {_esc(self.name(chat_id,target))} получил {points} оч. поддержки.")


def register(dp,bot,F,db_path,lock=None):
    module=Reputation(dp,bot,F,db_path,lock=lock);module.register();return module

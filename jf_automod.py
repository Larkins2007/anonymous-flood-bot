"""Keyword-based, conservative automod triggers for Justice Faite Manager.

Rules use literal substring matching (no user-supplied regex), are limited per
chat, skip moderators, and only support delete/warn actions.
"""
from __future__ import annotations
import html,re,sqlite3,threading
from contextlib import suppress
from datetime import datetime,timedelta,timezone
GROUP_TYPES={"group","supergroup"}
COMMAND_PATTERN=(r"(?isu)^\s*(?:/jf_automod(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:\+триггер\s+.+|триггеры|-триггер\s+\d+))\s*$")
def now():return datetime.now(timezone.utc)
def esc(x):return html.escape(str(x or ""),quote=False)
class Store:
 def __init__(self,path,lock=None):self.path=str(path);self.lock=lock or threading.RLock()
 def run(self,fn):
  with self.lock:
   c=sqlite3.connect(self.path,timeout=30);c.row_factory=sqlite3.Row;c.execute("PRAGMA busy_timeout=30000")
   try:o=fn(c);c.commit();return o
   except Exception:
    with suppress(Exception):c.rollback()
    raise
   finally:c.close()
 def init(self):self.run(lambda c:c.executescript("""
  CREATE TABLE IF NOT EXISTS jf_automod_triggers(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,keyword TEXT NOT NULL,action TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created_by INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(chat_id,keyword));
  CREATE INDEX IF NOT EXISTS idx_jf_automod_chat ON jf_automod_triggers(chat_id,enabled);
  CREATE TABLE IF NOT EXISTS jf_automod_hits(chat_id INTEGER NOT NULL,trigger_id INTEGER NOT NULL,user_id INTEGER NOT NULL,last_hit TEXT NOT NULL,PRIMARY KEY(chat_id,trigger_id,user_id));
 """))
class Automod:
 def __init__(self,dp,bot,F,path,manager,lock=None,owner_id=0):self.dp,self.bot,self.F,self.manager=dp,bot,F,manager;self.store=Store(path,lock);self.owner_id=int(owner_id or 0);self.registered=False
 def init(self):self.store.init()
 def register(self):
  if not self.registered:
   self.dp.message.register(self.handle_command,self.F.chat.type.in_(GROUP_TYPES),self.F.text.regexp(COMMAND_PATTERN))
   self.registered=True
 async def rank(self,chat,uid):
  if self.owner_id and uid==self.owner_id:return 5
  try:return await self.manager._rank(chat,uid)
  except Exception:return self.manager.store.rank(chat,uid)
 async def reply(self,m,t):
  try:await m.reply(t,parse_mode="HTML",disable_web_page_preview=True)
  except TypeError:await m.reply(t)
 async def handle_command(self,m):
  if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False):return
  raw=(m.text or "").strip();first=raw.splitlines()[0] if raw else ""
  first=re.sub(r"^\s*[!.\/]{0,4}","",first).strip();first=re.sub(r"^(?:ирис(?:ка)?|iris)\s+","",first,flags=re.I);low=first.casefold();chat,uid=int(m.chat.id),int(m.from_user.id)
  if low.startswith("/jf_automod"):return await self.reply(m,"<b>Автоматические триггеры</b>\n<code>+Триггер слово | удалить</code> или <code>+Триггер фраза | варн</code>. Список: <code>Триггеры</code>. Удалить: <code>-Триггер ID</code>. Правила создают администраторы от 3 ранга.")
  if low=="триггеры":
   rows=self.store.run(lambda c:c.execute("SELECT id,keyword,action,enabled FROM jf_automod_triggers WHERE chat_id=? ORDER BY id",(chat,)).fetchall())
   lines=[f"#{r['id']} · <b>{esc(r['keyword'])}</b> → {r['action']} · {'вкл' if r['enabled'] else 'выкл'}" for r in rows]
   return await self.reply(m,"<b>Триггеры чата</b>\n"+("\n".join(lines) or "Нет созданных триггеров."))
  if low.startswith("-триггер "):
   if await self.rank(chat,uid)<3:return await self.reply(m,"⛔ Управлять триггерами могут администраторы от 3 ранга.")
   try:tid=int(low.split()[-1])
   except ValueError:return await self.reply(m,"Укажи номер триггера.")
   n=self.store.run(lambda c:c.execute("DELETE FROM jf_automod_triggers WHERE chat_id=? AND id=?",(chat,tid)).rowcount)
   return await self.reply(m,"Триггер удалён." if n else "Триггер не найден.")
  if low.startswith("+триггер "):
   if await self.rank(chat,uid)<3:return await self.reply(m,"⛔ Управлять триггерами могут администраторы от 3 ранга.")
   payload=first[len("+Триггер "):].strip()
   if "|" not in payload:return await self.reply(m,"Формат: <code>+Триггер запрещённая фраза | удалить</code> или <code>| варн</code>.")
   keyword,action=[x.strip() for x in payload.rsplit("|",1)];action=action.casefold()
   if action not in {"удалить","варн","предупреждение"}:return await self.reply(m,"Действие должно быть <code>удалить</code> или <code>варн</code>.")
   keyword=re.sub(r"\s+"," ",keyword).strip()
   if len(keyword)<3 or len(keyword)>80:return await self.reply(m,"Фраза должна содержать от 3 до 80 символов.")
   count=self.store.run(lambda c:c.execute("SELECT COUNT(*) FROM jf_automod_triggers WHERE chat_id=?",(chat,)).fetchone()[0])
   if count>=20:return await self.reply(m,"В одном чате можно создать не более 20 триггеров.")
   try:
    tid=self.store.run(lambda c:c.execute("INSERT INTO jf_automod_triggers(chat_id,keyword,action,created_by,created_at) VALUES(?,?,?,?,?)",(chat,keyword,action,uid,now().isoformat())).lastrowid)
   except sqlite3.IntegrityError:return await self.reply(m,"Такой триггер уже существует.")
   return await self.reply(m,f"✅ Триггер №{tid} создан: <b>{esc(keyword)}</b> → {action}.")
 async def handle_message(self,m):
  if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False):return
  text=(getattr(m,"text",None) or "").strip()
  if not text or text.startswith("/"):return
  chat,uid=int(m.chat.id),int(m.from_user.id)
  if await self.rank(chat,uid)>=3:return False
  rows=self.store.run(lambda c:c.execute("SELECT * FROM jf_automod_triggers WHERE chat_id=? AND enabled=1 ORDER BY id",(chat,)).fetchall())
  folded=text.casefold()
  match=next((r for r in rows if str(r["keyword"]).casefold() in folded),None)
  if not match:return False
  trigger_id=int(match["id"]);stamp=now();cutoff=(stamp-timedelta(seconds=60)).isoformat()
  should_notify=self.store.run(lambda c:self._mark_hit(c,chat,trigger_id,uid,stamp.isoformat(),cutoff))
  if match["action"]=="удалить":
   try:await self.bot.delete_message(chat,int(m.message_id))
   except Exception:return False
   if should_notify:
    with suppress(Exception):await self.bot.send_message(chat_id,"⚠️ Сообщение удалено автоматически: сработало правило чата.")
   return True
  else:
   try:self.manager.store.add_warning(chat,uid,0,"Автоматический триггер: "+str(match["keyword"]),30*86400)
   except Exception:return False
   if should_notify:
    with suppress(Exception):await self.bot.send_message(chat_id,"⚠️ Выдано автоматическое предупреждение за нарушение правила чата.")
   return True
 @staticmethod
 def _mark_hit(c,chat,trigger_id,uid,stamp,cutoff):
  row=c.execute("SELECT last_hit FROM jf_automod_hits WHERE chat_id=? AND trigger_id=? AND user_id=?",(chat,trigger_id,uid)).fetchone()
  notify=not row or row["last_hit"]<cutoff
  c.execute("INSERT INTO jf_automod_hits(chat_id,trigger_id,user_id,last_hit) VALUES(?,?,?,?) ON CONFLICT(chat_id,trigger_id,user_id) DO UPDATE SET last_hit=excluded.last_hit",(chat,trigger_id,uid,stamp))
  return notify
def register(dp,bot,F,db_path,manager,lock=None,owner_id=0):
 m=Automod(dp,bot,F,db_path,manager,lock=lock,owner_id=owner_id);m.register();return m

"""Moderation report queue for Justice Faite Manager."""
from __future__ import annotations
import html,re,sqlite3,threading
from contextlib import suppress
from datetime import datetime,timezone,timedelta
import cmdcfg
GROUP_TYPES={"group","supergroup"}
COMMAND_PATTERN=(r"(?isu)^\s*(?:/jf_reports(?:@[A-Za-z0-9_]+)?(?:\s|$)|"
 r"[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:!?репорт|репортлист|репорт закрыть\s+\d+|!?сброс репортов))\b.*$")
def now():return datetime.now(timezone.utc).isoformat()
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
  CREATE TABLE IF NOT EXISTS jf_reports(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,reporter_id INTEGER NOT NULL,target_id INTEGER NOT NULL,message_id INTEGER NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'open',created_at TEXT NOT NULL,closed_by INTEGER,closed_at TEXT);
  CREATE INDEX IF NOT EXISTS idx_jf_reports_open ON jf_reports(chat_id,status,id);
 """))
 def add(self,chat,reporter,target,msg,reason):
  def op(c):
   cutoff=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
   dup=c.execute("SELECT id FROM jf_reports WHERE chat_id=? AND reporter_id=? AND target_id=? AND status='open' AND created_at>=? LIMIT 1",(chat,reporter,target,cutoff)).fetchone()
   if dup:return None
   cur=c.execute("INSERT INTO jf_reports(chat_id,reporter_id,target_id,message_id,reason,created_at) VALUES(?,?,?,?,?,?)",(chat,reporter,target,msg,reason,now()));return int(cur.lastrowid)
  return self.run(op)
class Reports:
 def __init__(self,dp,bot,F,path,manager,lock=None,owner_id=0):self.dp,self.bot,self.F,self.manager=dp,bot,F,manager;self.store=Store(path,lock);self.owner_id=int(owner_id or 0);self.registered=False
 def init(self):self.store.init()
 def register(self):
  if not self.registered:self.dp.message.register(self.handle,self.F.chat.type.in_(GROUP_TYPES),self.F.text.regexp(COMMAND_PATTERN));self.registered=True
 async def rank(self,chat,uid):
  if self.owner_id and uid==self.owner_id:return 5
  try:return await self.manager._rank(chat,uid)
  except Exception:return self.manager.store.rank(chat,uid)
 async def reply(self,m,t):
  try:await m.reply(t,parse_mode="HTML",disable_web_page_preview=True)
  except TypeError:await m.reply(t)
 async def handle(self,m):
  if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False):return
  raw=(m.text or "").strip();first=raw.splitlines()[0] if raw else "";reason="\n".join(raw.splitlines()[1:]).strip()
  first=re.sub(r"^\s*[!.\/]{0,4}","",first).strip();first=re.sub(r"^(?:ирис(?:ка)?|iris)\s+","",first,flags=re.I);low=first.casefold()
  chat,uid=int(m.chat.id),int(m.from_user.id)
  if low.startswith("/jf_reports"):return await self.reply(m,"<b>Жалобы Justice Faite</b>\nОтветь командой <code>!Репорт</code> на сообщение и укажи причину следующей строкой. Администраторы смотрят очередь: <code>Репортлист</code>.")
  if low in {"репорт","!репорт"}:
   target=getattr(getattr(m,"reply_to_message",None),"from_user",None)
   if not target:return await self.reply(m,"Для жалобы ответь на сообщение участника.")
   if target.id==uid:return await self.reply(m,"Нельзя отправить жалобу на собственное сообщение.")
   if getattr(target,"is_bot",False):return await self.reply(m,"Жалобы на ботов через этот модуль не принимаются.")
   if not reason:return await self.reply(m,"Добавь причину жалобы на следующей строке.")
   if len(reason)>1000:return await self.reply(m,"Причина слишком длинная: максимум 1000 символов.")
   mid=int(m.reply_to_message.message_id);rid=self.store.add(chat,uid,int(target.id),mid,reason)
   if rid is None:return await self.reply(m,"Похожая жалоба уже находится в очереди. Подожди решения администрации.")
   return await self.reply(m,f"✅ Жалоба №{rid} передана администрации.")
  if low=="репортлист":
   if not cmdcfg.access_was_checked(m) and await self.rank(chat,uid)<1:return await self.reply(m,"⛔ Очередь жалоб доступна модераторам от 1 ранга.")
   rows=self.store.run(lambda c:c.execute("SELECT * FROM jf_reports WHERE chat_id=? AND status='open' ORDER BY id DESC LIMIT 30",(chat,)).fetchall())
   lines=[f"<b>#{r['id']}</b> · от ID {r['reporter_id']} на ID {r['target_id']} · сообщение {r['message_id']}\n{esc(r['reason'])}" for r in rows]
   return await self.reply(m,"<b>Открытые жалобы</b>\n"+("\n\n".join(lines) or "Очередь пуста.")+"\n\nЗакрыть: <code>Репорт закрыть ID</code>")
  if low.startswith("репорт закрыть "):
   if not cmdcfg.access_was_checked(m) and await self.rank(chat,uid)<1:return await self.reply(m,"⛔ Закрывать жалобы могут модераторы от 1 ранга.")
   try:rid=int(low.split()[-1])
   except ValueError:return await self.reply(m,"Укажи номер жалобы.")
   changed=self.store.run(lambda c:c.execute("UPDATE jf_reports SET status='closed',closed_by=?,closed_at=? WHERE chat_id=? AND id=? AND status='open'",(uid,now(),chat,rid)).rowcount)
   return await self.reply(m,"✅ Жалоба закрыта." if changed else "Открытая жалоба с таким номером не найдена.")
  if low in {"сброс репортов","!сброс репортов"}:
   if not cmdcfg.access_was_checked(m) and await self.rank(chat,uid)<5:return await self.reply(m,"⛔ Полный сброс доступен только создателю чата.")
   count=self.store.run(lambda c:c.execute("DELETE FROM jf_reports WHERE chat_id=?",(chat,)).rowcount)
   return await self.reply(m,f"Удалено жалоб: {count}.")
def register(dp,bot,F,db_path,manager,lock=None,owner_id=0):
 m=Reports(dp,bot,F,db_path,manager,lock=lock,owner_id=owner_id);m.register();return m

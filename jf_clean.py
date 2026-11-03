"""Bounded message cleanup using only message IDs observed since installation."""
from __future__ import annotations
import re,sqlite3,threading
from contextlib import suppress
from datetime import datetime,timedelta,timezone
GROUP_TYPES={"group","supergroup"}
COMMAND_PATTERN=(r"(?isu)^\s*(?:/jf_clean(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:-смс(?:\s+\d+)?|!пург(?:\s+\d+)?(?:\s+тихо)?))\s*$")
def now():return datetime.now(timezone.utc)
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
  CREATE TABLE IF NOT EXISTS jf_clean_messages(chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,user_id INTEGER NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(chat_id,message_id));
  CREATE INDEX IF NOT EXISTS idx_jf_clean_recent ON jf_clean_messages(chat_id,message_id DESC);
 """))
class Cleaner:
 def __init__(self,dp,bot,F,path,manager,lock=None):self.dp,self.bot,self.F,self.manager=dp,bot,F,manager;self.store=Store(path,lock);self.registered=False
 def init(self):self.store.init()
 def register(self):
  if not self.registered:self.dp.message.register(self.handle,self.F.chat.type.in_(GROUP_TYPES),self.F.text.regexp(COMMAND_PATTERN));self.registered=True
 def track_message(self,m):
  if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False) or not getattr(m,"message_id",None):return
  try:
   stamp=now();cutoff=(stamp-timedelta(hours=48)).isoformat()
   self.store.run(lambda c:(c.execute("INSERT OR REPLACE INTO jf_clean_messages(chat_id,message_id,user_id,created_at) VALUES(?,?,?,?)",(int(m.chat.id),int(m.message_id),int(m.from_user.id),stamp.isoformat())),c.execute("DELETE FROM jf_clean_messages WHERE chat_id=? AND created_at<?",(int(m.chat.id),cutoff))))
  except sqlite3.Error:pass
 async def reply(self,m,t):
  try:await m.reply(t,disable_web_page_preview=True)
  except TypeError:await m.reply(t)
 async def handle(self,m):
  if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False):return
  raw=(m.text or "").strip();first=raw.splitlines()[0] if raw else ""
  first=re.sub(r"^\s*[!.\/]{0,4}","",first).strip();first=re.sub(r"^(?:ирис(?:ка)?|iris)\s+","",first,flags=re.I);low=first.casefold()
  if low.startswith("/jf_clean"):return await self.reply(m,"<b>Чистка Justice Faite</b>\n<code>-Смс</code> — удалить сообщение, на которое дан ответ. <code>-Смс 20</code> — удалить до 20 последних сообщений из сохранённой истории. <code>!Пург 20</code> — удалить до 20 сообщений от выбранного сообщения и ниже. Бот может очистить только известные ему ID и сообщения младше 48 часов.")
  chat,uid=int(m.chat.id),int(m.from_user.id)
  try:rank=await self.manager._rank(chat,uid)
  except Exception:rank=self.manager.store.rank(chat,uid)
  if rank<2:return await self.reply(m,"⛔ Чистка доступна модераторам от 2 ранга.")
  reply=getattr(m,"reply_to_message",None)
  command_id=int(getattr(m,"message_id",0) or 0)
  if low.startswith("-смс"):
   parts=low.split();count=1
   if len(parts)>1:
    try:count=int(parts[1])
    except ValueError:count=0
   if not 1<=count<=100:return await self.reply(m,"Укажи число от 1 до 100: <code>-Смс 20</code>.")
   if len(parts)==1 and reply:
    target_ids=[int(reply.message_id)]
   elif len(parts)==1:
    return await self.reply(m,"Ответь на сообщение командой <code>-Смс</code> или укажи число.")
   else:
    rows=self.store.run(lambda c:c.execute("SELECT message_id FROM jf_clean_messages WHERE chat_id=? ORDER BY message_id DESC LIMIT ?",(chat,count)).fetchall())
    target_ids=[int(r[0]) for r in rows]
   return await self._delete(m,target_ids+[command_id],quiet=False)
  if low.startswith("пург"):
   if not reply:return await self.reply(m,"Ответь на исходное сообщение командой <code>!Пург 20</code>.")
   parts=low.split();quiet="тихо" in parts
   nums=[x for x in parts[1:] if x.isdigit()];count=int(nums[0]) if nums else 100
   if not 1<=count<=100:return await self.reply(m,"Лимит пург-команды — от 1 до 100 сообщений.")
   rows=self.store.run(lambda c:c.execute("SELECT message_id FROM jf_clean_messages WHERE chat_id=? AND message_id>=? ORDER BY message_id LIMIT ?",(chat,int(reply.message_id),count)).fetchall())
   ids=[int(r[0]) for r in rows]
   return await self._delete(m,ids+[command_id],quiet=quiet)
 async def _delete(self,m,ids,quiet=False):
  chat=int(m.chat.id);unique=list(dict.fromkeys(i for i in ids if i>0));deleted=0;failed=[]
  for mid in unique:
   try:
    await self.bot.delete_message(chat,mid);deleted+=1
    self.store.run(lambda c:c.execute("DELETE FROM jf_clean_messages WHERE chat_id=? AND message_id=?",(chat,mid)))
   except Exception:failed.append(mid)
  if not quiet:await self.reply(m,f"🧹 Удалено сообщений: {deleted}."+(f" Не удалось удалить: {len(failed)}." if failed else ""))
  return deleted
def register(dp,bot,F,db_path,manager,lock=None):
 m=Cleaner(dp,bot,F,db_path,manager,lock=lock);m.register();return m

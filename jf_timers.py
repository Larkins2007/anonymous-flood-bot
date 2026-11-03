"""Persistent reminder timers. Timer text is delivered as plain text, never executed as a command."""
from __future__ import annotations
import asyncio,html,re,sqlite3,threading
from contextlib import suppress
from datetime import datetime,timedelta,timezone
GROUP_TYPES={"group","supergroup"}
COMMAND_PATTERN=(r"(?isu)^\s*(?:/jf_timers(?:@[A-Za-z0-9_]+)?(?:\s|$)|"
 r"[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:таймер через\s+\d+\s*(?:минут\w*|мин|м|час\w*|ч|дн\w*|д|недел\w*)|таймеры|-таймер\s+\d+|сбросить таймеры))\b.*$")
def utcnow():return datetime.now(timezone.utc)
def esc(x):return html.escape(str(x or ""),quote=False)
def duration_minutes(s):
 m=re.fullmatch(r"\s*(\d{1,5})\s*(минут\w*|мин|м|час\w*|ч|дн\w*|д|недел\w*)\s*",s,re.I)
 if not m:return None
 n=int(m.group(1));u=m.group(2).casefold()
 factor=1 if u.startswith("мин") or u=="м" else 60 if u.startswith("час") or u=="ч" else 1440 if u.startswith("д") else 10080
 minutes=n*factor
 return minutes if 1<=minutes<=525600 else None
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
  CREATE TABLE IF NOT EXISTS jf_timers(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,owner_id INTEGER NOT NULL,due_at TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
  CREATE INDEX IF NOT EXISTS idx_jf_timers_due ON jf_timers(status,due_at);
 """))
class Timers:
 def __init__(self,dp,bot,F,path,manager,lock=None):self.dp,self.bot,self.F,self.manager=dp,bot,F,manager;self.store=Store(path,lock);self.registered=False;self._runner=None
 def init(self):self.store.init()
 def register(self):
  if not self.registered:self.dp.message.register(self.handle,self.F.chat.type.in_(GROUP_TYPES),self.F.text.regexp(COMMAND_PATTERN));self.registered=True
 async def reply(self,m,t):
  try:await m.reply(t,parse_mode="HTML",disable_web_page_preview=True)
  except TypeError:await m.reply(t)
 async def rank(self,chat,uid):
  try:return await self.manager._rank(chat,uid)
  except Exception:return self.manager.store.rank(chat,uid)
 async def handle(self,m):
  if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False):return
  raw=(m.text or "").strip();lines=raw.splitlines();first=lines[0] if lines else "";body="\n".join(lines[1:]).strip()
  first=re.sub(r"^\s*[!.\/]{0,4}","",first).strip();first=re.sub(r"^(?:ирис(?:ка)?|iris)\s+","",first,flags=re.I);low=first.casefold()
  chat,uid=int(m.chat.id),int(m.from_user.id)
  if low.startswith("/jf_timers"):return await self.reply(m,"<b>Таймеры Justice Faite</b>\n<code>Таймер через 30 минут</code>, затем текст напоминания на новой строке. <code>Таймеры</code> — список; <code>-Таймер ID</code> — отмена.")
  if low.startswith("таймер через "):
   if await self.rank(chat,uid)<1:return await self.reply(m,"⛔ Таймеры доступны модераторам от 1 ранга.")
   if not body:return await self.reply(m,"Добавь текст напоминания на следующей строке.")
   mins=duration_minutes(first[len("таймер через "):])
   if mins is None:return await self.reply(m,"Период должен быть от 1 минуты до 1 года. Например: <code>Таймер через 30 минут</code>.")
   if len(body)>1000:return await self.reply(m,"Текст напоминания слишком длинный: максимум 1000 символов.")
   count=self.store.run(lambda c:c.execute("SELECT COUNT(*) FROM jf_timers WHERE chat_id=? AND owner_id=? AND status='pending'",(chat,uid)).fetchone()[0])
   if count>=5:return await self.reply(m,"У тебя уже 5 активных таймеров в этом чате.")
   due=(utcnow()+timedelta(minutes=mins)).isoformat()
   tid=self.store.run(lambda c:c.execute("INSERT INTO jf_timers(chat_id,owner_id,due_at,body,created_at) VALUES(?,?,?,?,?)",(chat,uid,due,body,utcnow().isoformat())).lastrowid)
   return await self.reply(m,f"⏰ Таймер №{tid} создан. Напоминание придёт через {mins} мин.")
  if low=="таймеры":
   rows=self.store.run(lambda c:c.execute("SELECT id,due_at,body FROM jf_timers WHERE chat_id=? AND owner_id=? AND status='pending' ORDER BY due_at LIMIT 10",(chat,uid)).fetchall())
   lines=[f"№{r['id']} · {r['due_at'][:16].replace('T',' ')} UTC\n{esc(r['body'][:120])}" for r in rows]
   return await self.reply(m,"<b>Твои таймеры</b>\n"+("\n\n".join(lines) or "Активных таймеров нет."))
  if low.startswith("-таймер "):
   try:tid=int(low.split()[-1])
   except ValueError:return await self.reply(m,"Укажи номер таймера.")
   changed=self.store.run(lambda c:c.execute("DELETE FROM jf_timers WHERE chat_id=? AND owner_id=? AND id=? AND status='pending'",(chat,uid,tid)).rowcount)
   return await self.reply(m,"Таймер отменён." if changed else "Таймер не найден.")
  if low=="сбросить таймеры":
   count=self.store.run(lambda c:c.execute("DELETE FROM jf_timers WHERE chat_id=? AND owner_id=? AND status='pending'",(chat,uid)).rowcount)
   return await self.reply(m,f"Удалено твоих таймеров: {count}.")
 async def run_due_once(self):
  cutoff=utcnow().isoformat()
  rows=self.store.run(lambda c:c.execute("SELECT * FROM jf_timers WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT 20",(cutoff,)).fetchall())
  sent=0
  for r in rows:
   tid=int(r['id']);claimed=self.store.run(lambda c:c.execute("UPDATE jf_timers SET status='sending' WHERE id=? AND status='pending'",(tid,)).rowcount)
   if not claimed:continue
   try:
    await self.bot.send_message(int(r['chat_id']),"⏰ Напоминание\n"+str(r['body']))
    self.store.run(lambda c:c.execute("DELETE FROM jf_timers WHERE id=?",(tid,)))
    sent+=1
   except Exception:
    attempts=int(r['attempts'])+1
    if attempts>=3:self.store.run(lambda c:c.execute("UPDATE jf_timers SET status='failed',attempts=? WHERE id=?",(attempts,tid)))
    else:self.store.run(lambda c:c.execute("UPDATE jf_timers SET status='pending',attempts=?,due_at=? WHERE id=?",(attempts,(utcnow()+timedelta(minutes=5)).isoformat(),tid)))
  return sent
 async def run_forever(self):
  while True:
   try:await self.run_due_once()
   except Exception:pass
   await asyncio.sleep(15)
def register(dp,bot,F,db_path,manager,lock=None):
 m=Timers(dp,bot,F,db_path,manager,lock=lock);m.register();return m

"""Free, no-purchase community draws for symbolic chat rewards."""
from __future__ import annotations
import asyncio,html,re,secrets,sqlite3,threading
from contextlib import suppress
from datetime import datetime,timezone
import cmdcfg
GROUP_TYPES={"group","supergroup"}
COMMAND_PATTERN=(r"(?isu)^\s*(?:/jf_raffles(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:\+розыгрыш\s+\d+|розыгрыш статус|участвую|выйти из розыгрыша|завершить розыгрыш|победители розыгрыша))\s*$")
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
  CREATE TABLE IF NOT EXISTS jf_raffles(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,creator_id INTEGER NOT NULL,winner_count INTEGER NOT NULL,prize TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',created_at TEXT NOT NULL,finished_at TEXT);
  CREATE UNIQUE INDEX IF NOT EXISTS idx_jf_raffles_one_active ON jf_raffles(chat_id) WHERE status='active';
  CREATE TABLE IF NOT EXISTS jf_raffle_entries(raffle_id INTEGER NOT NULL,user_id INTEGER NOT NULL,joined_at TEXT NOT NULL,PRIMARY KEY(raffle_id,user_id));
  CREATE TABLE IF NOT EXISTS jf_raffle_winners(raffle_id INTEGER NOT NULL,position INTEGER NOT NULL,user_id INTEGER NOT NULL,PRIMARY KEY(raffle_id,position),UNIQUE(raffle_id,user_id));
  CREATE TABLE IF NOT EXISTS jf_raffle_publications(
   raffle_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, body TEXT NOT NULL,
   status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
   next_attempt_at TEXT, last_error TEXT, telegram_message_id INTEGER,
   created_at TEXT NOT NULL, updated_at TEXT NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_jf_raffle_publications_due ON jf_raffle_publications(status,next_attempt_at);
  UPDATE jf_raffle_publications SET status='uncertain',last_error=COALESCE(last_error,'Process restarted during publication; delivery outcome must be reconciled'),updated_at=CURRENT_TIMESTAMP WHERE status='sending';
 """))
class Raffles:
 def __init__(self,dp,bot,F,path,manager,lock=None,owner_id=0):self.dp,self.bot,self.F,self.manager=dp,bot,F,manager;self.store=Store(path,lock);self.owner_id=int(owner_id or 0);self.registered=False
 def init(self):self.store.init()
 def register(self):
  if not self.registered:self.dp.message.register(self.handle,self.F.chat.type.in_(GROUP_TYPES),self.F.text.regexp(COMMAND_PATTERN));self.registered=True
 def _publication_retry_delay(self, attempts):
  return (1, 5, 15, 60, 180)[min(max(0, int(attempts)-1), 4)]
 def enqueue_publication(self, raffle_id, chat_id, body):
  stamp=now()
  return self.store.run(lambda c:c.execute(
   "INSERT OR IGNORE INTO jf_raffle_publications(raffle_id,chat_id,body,status,attempts,next_attempt_at,created_at,updated_at) VALUES(?,?,?,'pending',0,?,?,?)",
   (int(raffle_id),int(chat_id),str(body),stamp,stamp,stamp)).rowcount)
 async def publish_pending_once(self, limit=10):
  """Deliver queued results. Ambiguous transport failures become uncertain, never auto-duplicated."""
  due=now()
  rows=self.store.run(lambda c:c.execute(
   "SELECT * FROM jf_raffle_publications WHERE status='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY created_at LIMIT ?",
   (due,max(1,min(int(limit),50)))).fetchall())
  delivered=0
  for row in rows:
   rid=int(row['raffle_id'])
   claimed=self.store.run(lambda c:c.execute(
    "UPDATE jf_raffle_publications SET status='sending',updated_at=? WHERE raffle_id=? AND status='pending'",
    (now(),rid)).rowcount)
   if not claimed: continue
   try:
    sent=await self.bot.send_message(int(row['chat_id']),str(row['body']),parse_mode='HTML')
    mid=getattr(sent,'message_id',None)
    self.store.run(lambda c:c.execute(
     "UPDATE jf_raffle_publications SET status='sent',telegram_message_id=?,updated_at=?,last_error=NULL WHERE raffle_id=? AND status='sending'",
     (int(mid) if mid is not None else None,now(),rid)))
    delivered+=1
   except Exception as exc:
    # Telegram's explicit rejection means no message was accepted; retry is safe.
    cls=type(exc).__name__
    safe_reject=any(token in cls for token in ('TelegramRetryAfter','TelegramBadRequest','TelegramForbiddenError','TelegramNotFound'))
    attempts=int(row['attempts'])+1
    if safe_reject and attempts<6:
     delay=self._publication_retry_delay(attempts)
     from datetime import timedelta
     next_at=(datetime.now(timezone.utc)+timedelta(minutes=delay)).isoformat()
     status='pending'
    else:
     # Network/timeouts may happen after Telegram accepted the message. Do not
     # blindly resend; require owner reconciliation to preserve no-duplicates.
     next_at=None; status='uncertain'
    self.store.run(lambda c:c.execute(
     "UPDATE jf_raffle_publications SET status=?,attempts=?,next_attempt_at=?,last_error=?,updated_at=? WHERE raffle_id=?",
     (status,attempts,next_at,f"{cls}: {str(exc)[:400]}",now(),rid)))
  return delivered
 async def publication_worker(self):
  while True:
   await self.publish_pending_once()
   await asyncio.sleep(30)
 async def rank(self,chat,uid):
  try:return await self.manager._rank(chat,uid)
  except Exception:return self.manager.store.rank(chat,uid)
 async def reply(self,m,t):
  try:await m.reply(t,parse_mode="HTML",disable_web_page_preview=True)
  except TypeError:await m.reply(t)
 def name(self,chat,uid):
  try:r=self.store.run(lambda c:c.execute("SELECT username,display_name FROM jf_manager_member_names WHERE chat_id=? AND user_id=?",(chat,uid)).fetchone())
  except sqlite3.Error:r=None
  return ("@"+r[0]) if r and r[0] else (r[1] if r and r[1] else f"ID {uid}")
 async def handle(self,m):
  if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False):return
  raw=(m.text or "").strip();lines=raw.splitlines();first=lines[0] if lines else "";prize="\n".join(lines[1:]).strip()
  first=re.sub(r"^\s*[!.\/]{0,4}","",first).strip();first=re.sub(r"^(?:ирис(?:ка)?|iris)\s+","",first,flags=re.I);low=first.casefold()
  chat,uid=int(m.chat.id),int(m.from_user.id)
  if low.startswith("/jf_raffles"):return await self.reply(m,"<b>Бесплатный розыгрыш Justice Faite</b>\nСоздать: <code>+Розыгрыш 2</code> и описание символической награды на следующей строке. Войти: <code>Участвую</code>. Выйти: <code>Выйти из розыгрыша</code>. Завершить: <code>Завершить розыгрыш</code>. Без покупок, ставок и денежной награды.")
  if low.startswith("+розыгрыш "):
   if not cmdcfg.access_was_checked(m) and await self.rank(chat,uid)<3:return await self.reply(m,"⛔ Создавать розыгрыши могут администраторы от 3 ранга.")
   try:n=int(first.split()[-1])
   except ValueError:return await self.reply(m,"Укажи число победителей от 1 до 10.")
   if not 1<=n<=10:return await self.reply(m,"Число победителей должно быть от 1 до 10.")
   if not prize:return await self.reply(m,"Добавь описание символической награды на следующей строке.")
   if len(prize)>200:return await self.reply(m,"Описание слишком длинное: максимум 200 символов.")
   if re.search(r"(?:₽|руб(?:л|лей|ля)?|евро|usd|eur|доллар|покупк|билет за|ставк|денежн)",prize,re.I):return await self.reply(m,"Этот модуль предназначен только для бесплатных розыгрышей символических наград сообщества, без денег, ставок и платных билетов.")
   try:rid=self.store.run(lambda c:c.execute("INSERT INTO jf_raffles(chat_id,creator_id,winner_count,prize,created_at) VALUES(?,?,?,?,?)",(chat,uid,n,prize,now())).lastrowid)
   except sqlite3.IntegrityError:return await self.reply(m,"В этом чате уже есть активный розыгрыш. Сначала заверши его.")
   return await self.reply(m,f"🎉 <b>Начался розыгрыш №{rid}!</b>\nНаграда: {esc(prize)}\nПобедителей: {n}\nДля бесплатного участия напиши <code>Участвую</code>.")
  active=self.store.run(lambda c:c.execute("SELECT * FROM jf_raffles WHERE chat_id=? AND status='active' ORDER BY id DESC LIMIT 1",(chat,)).fetchone())
  if low=="розыгрыш статус":
   if not active:return await self.reply(m,"Активного розыгрыша нет.")
   n=self.store.run(lambda c:c.execute("SELECT COUNT(*) FROM jf_raffle_entries WHERE raffle_id=?",(active["id"],)).fetchone()[0])
   return await self.reply(m,f"<b>Розыгрыш №{active['id']}</b>\nНаграда: {esc(active['prize'])}\nПобедителей: {active['winner_count']}\nУчастников: {n}")
  if low=="участвую":
   if not active:return await self.reply(m,"Сейчас нет активного розыгрыша.")
   try:self.store.run(lambda c:c.execute("INSERT INTO jf_raffle_entries(raffle_id,user_id,joined_at) VALUES(?,?,?)",(active["id"],uid,now())))
   except sqlite3.IntegrityError:return await self.reply(m,"Ты уже участвуешь в этом розыгрыше.")
   return await self.reply(m,"✅ Ты добавлен в список участников бесплатно.")
  if low=="выйти из розыгрыша":
   if not active:return await self.reply(m,"Активного розыгрыша нет.")
   n=self.store.run(lambda c:c.execute("DELETE FROM jf_raffle_entries WHERE raffle_id=? AND user_id=?",(active["id"],uid)).rowcount)
   return await self.reply(m,"Ты вышел из розыгрыша." if n else "Ты не участвуешь в этом розыгрыше.")
  if low=="победители розыгрыша":
   done=self.store.run(lambda c:c.execute("SELECT * FROM jf_raffles WHERE chat_id=? AND status='done' ORDER BY id DESC LIMIT 1",(chat,)).fetchone())
   if not done:return await self.reply(m,"Завершённых розыгрышей ещё нет.")
   rows=self.store.run(lambda c:c.execute("SELECT user_id FROM jf_raffle_winners WHERE raffle_id=? ORDER BY position",(done["id"],)).fetchall())
   return await self.reply(m,f"<b>Победители розыгрыша №{done['id']}</b>\n"+("\n".join(f"{i}. {esc(self.name(chat,int(r[0])))}" for i,r in enumerate(rows,1)) or "Подходящих участников не было."))
  if low=="завершить розыгрыш":
   if not cmdcfg.access_was_checked(m) and await self.rank(chat,uid)<3:return await self.reply(m,"⛔ Завершить розыгрыш могут администраторы от 3 ранга.")
   if not active:return await self.reply(m,"Активного розыгрыша нет.")
   entries=self.store.run(lambda c:c.execute("SELECT user_id FROM jf_raffle_entries WHERE raffle_id=? ORDER BY joined_at",(active["id"],)).fetchall())
   pool=[int(r[0]) for r in entries];count=min(int(active["winner_count"]),len(pool));winners=secrets.SystemRandom().sample(pool,count) if count else []
   def finish(c):
    changed=c.execute("UPDATE jf_raffles SET status='done',finished_at=? WHERE id=? AND status='active'",(now(),active["id"])).rowcount
    if changed:
     c.executemany("INSERT INTO jf_raffle_winners(raffle_id,position,user_id) VALUES(?,?,?)",[(active["id"],i,u) for i,u in enumerate(winners,1)])
    return changed
   if not self.store.run(finish):return await self.reply(m,"Этот розыгрыш уже завершён другим администратором.")
   names="\n".join(f"{i}. {esc(self.name(chat,u))}" for i,u in enumerate(winners,1)) or "Участников не было — победителей нет."
   body = f"🏆 <b>Розыгрыш №{active['id']} завершён!</b>\nНаграда: {esc(active['prize'])}\n{names}"
   self.enqueue_publication(int(active['id']),chat,body)
   await self.publish_pending_once(limit=1)
   publication=self.store.run(lambda c:c.execute("SELECT status,last_error FROM jf_raffle_publications WHERE raffle_id=?",(int(active['id']),)).fetchone())
   if publication and publication['status']=='sent':
    return await self.reply(m,"Розыгрыш завершён. Результат опубликован.")
   if publication and publication['status']=='pending':
    return await self.reply(m,"Розыгрыш завершён. Telegram отклонил публикацию; она сохранена в очереди и будет повторена автоматически.")
   return await self.reply(m,"Розыгрыш завершён, но подтверждение публикации не получено. Победители сохранены; повторная отправка при неопределённом результате остановлена, чтобы не опубликовать дубликат.")
def register(dp,bot,F,db_path,manager,lock=None,owner_id=0):
 m=Raffles(dp,bot,F,db_path,manager,lock=lock,owner_id=owner_id);m.register();return m

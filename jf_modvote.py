"""Advisory moderation polls. Results never issue an automatic punishment."""
from __future__ import annotations
import html,re,sqlite3,threading
from contextlib import suppress
from datetime import datetime,timezone
import cmdcfg

GROUP_TYPES={"group","supergroup"}
COMMANDS=("/jf_modvote","голосование бан","голос за","голос против","голосование статус","закрыть голосование")
COMMAND_PATTERN=r"(?isu)^\s*(?:/jf_modvote(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:голосование бан|голосование статус|голос за|голос против|закрыть голосование)(?:\s|$).*$)"
def now():return datetime.now(timezone.utc).isoformat()
def esc(v):return html.escape(str(v or ""),quote=False)

class Store:
 def __init__(self,path,lock=None):self.path=str(path);self.lock=lock or threading.RLock()
 def run(self,fn,immediate=False):
  with self.lock:
   c=sqlite3.connect(self.path,timeout=30,isolation_level=None);c.row_factory=sqlite3.Row;c.execute("PRAGMA busy_timeout=30000")
   try:
    c.execute("BEGIN IMMEDIATE" if immediate else "BEGIN");out=fn(c);c.commit();return out
   except Exception:
    with suppress(Exception):c.rollback()
    raise
   finally:c.close()
 def init(self):
  self.run(lambda c:c.executescript("""
   CREATE TABLE IF NOT EXISTS jf_mod_votes(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id INTEGER NOT NULL,target_id INTEGER NOT NULL,creator_id INTEGER NOT NULL,reason TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'active',created_at TEXT NOT NULL,closed_at TEXT);
   CREATE UNIQUE INDEX IF NOT EXISTS idx_jf_mod_votes_active ON jf_mod_votes(chat_id) WHERE status='active';
   CREATE TABLE IF NOT EXISTS jf_mod_vote_ballots(poll_id INTEGER NOT NULL,user_id INTEGER NOT NULL,vote TEXT NOT NULL CHECK(vote IN ('for','against')),updated_at TEXT NOT NULL,PRIMARY KEY(poll_id,user_id),FOREIGN KEY(poll_id) REFERENCES jf_mod_votes(id) ON DELETE CASCADE);
  """))

class ModVotes:
 def __init__(self,dp,bot,F,path,manager,lock=None):self.dp,self.bot,self.F,self.manager=dp,bot,F,manager;self.store=Store(path,lock);self.registered=False
 def init(self):self.store.init()
 def register(self):
  if not self.registered:self.dp.message.register(self.handle,self.F.chat.type.in_(GROUP_TYPES),self.F.text.regexp(COMMAND_PATTERN));self.registered=True
 async def reply(self,m,t):
  try:await m.reply(t,parse_mode="HTML",disable_web_page_preview=True)
  except TypeError:await m.reply(t)
 async def rank(self,chat,uid):
  try:return await self.manager._rank(chat,uid)
  except Exception:return self.manager.store.rank(chat,uid)
 def counts(self,pid):
  return self.store.run(lambda c:c.execute("SELECT SUM(vote='for'),SUM(vote='against'),COUNT(*) FROM jf_mod_vote_ballots WHERE poll_id=?",(pid,)).fetchone())
 async def handle(self,m):
  if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False):return
  raw=(m.text or "").strip();first=(raw.splitlines() or [""])[0]
  first=re.sub(r"^\s*[!.\/]{0,4}","",first).strip();first=re.sub(r"^(?:ирис(?:ка)?|iris)\s+","",first,flags=re.I);low=first.casefold()
  chat,uid=int(m.chat.id),int(m.from_user.id)
  if low in {"jf_modvote","/jf_modvote"}:
   return await self.reply(m,"<b>Голосования модерации</b>\nОтветь на сообщение участника: <code>Голосование бан</code> + причина следующей строкой. Голос: <code>Голос за</code> / <code>Голос против</code>. Итог: <code>Голосование статус</code>. Закрыть: <code>Закрыть голосование</code>. Результат только рекомендация: бан автоматически не выдаётся.")
  if low=="голосование бан":
   if not cmdcfg.access_was_checked(m) and await self.rank(chat,uid)<3:return await self.reply(m,"⛔ Создать голосование может администратор от 3 ранга.")
   target_msg=getattr(m,"reply_to_message",None);target_user=getattr(target_msg,"from_user",None) if target_msg else None
   if not target_user:return await self.reply(m,"Ответь командой на сообщение участника, по которому предлагается голосование.")
   target=int(target_user.id)
   if target==uid or getattr(target_user,"is_bot",False):return await self.reply(m,"Нельзя запускать голосование по себе или по боту.")
   reason="\n".join(raw.splitlines()[1:]).strip()[:500]
   try:pid=self.store.run(lambda c:c.execute("INSERT INTO jf_mod_votes(chat_id,target_id,creator_id,reason,created_at) VALUES(?,?,?,?,?)",(chat,target,uid,reason,now())).lastrowid,immediate=True)
   except sqlite3.IntegrityError:return await self.reply(m,"В этом чате уже идёт голосование. Сначала закрой его.")
   return await self.reply(m,f"🗳 <b>Открыто голосование №{pid}</b>\nЦель: <code>{target}</code>\nПричина: {esc(reason or 'не указана')}\n\nУчастники могут выбрать: <code>Голос за</code> или <code>Голос против</code>. Голос можно изменить до закрытия. Итог не применяет наказание автоматически.")
  poll=self.store.run(lambda c:c.execute("SELECT * FROM jf_mod_votes WHERE chat_id=? AND status='active' ORDER BY id DESC LIMIT 1",(chat,)).fetchone())
  if low in {"голос за","голос против"}:
   if not poll:return await self.reply(m,"Активного голосования нет.")
   if uid==int(poll['target_id']):return await self.reply(m,"Участник, по которому идёт голосование, не может голосовать в нём.")
   vote="for" if low=="голос за" else "against"
   self.store.run(lambda c:c.execute("INSERT INTO jf_mod_vote_ballots(poll_id,user_id,vote,updated_at) VALUES(?,?,?,?) ON CONFLICT(poll_id,user_id) DO UPDATE SET vote=excluded.vote,updated_at=excluded.updated_at",(poll['id'],uid,vote,now())),immediate=True)
   counts=self.counts(int(poll['id']))
   return await self.reply(m,f"✅ Голос учтён. За: {int(counts[0] or 0)} · Против: {int(counts[1] or 0)}.")
  if low=="голосование статус":
   if not poll:
    done=self.store.run(lambda c:c.execute("SELECT * FROM jf_mod_votes WHERE chat_id=? ORDER BY id DESC LIMIT 1",(chat,)).fetchone())
    if not done:return await self.reply(m,"Голосований ещё не было.")
    poll=done
   counts=self.counts(int(poll['id']))
   status="открыто" if poll['status']=="active" else "закрыто"
   return await self.reply(m,f"🗳 <b>Голосование №{poll['id']} · {status}</b>\nЗа: <b>{int(counts[0] or 0)}</b>\nПротив: <b>{int(counts[1] or 0)}</b>\nВсего: {int(counts[2] or 0)}\nРезультат носит рекомендательный характер.")
  if low=="закрыть голосование":
   if not cmdcfg.access_was_checked(m) and await self.rank(chat,uid)<3:return await self.reply(m,"⛔ Закрыть голосование может администратор от 3 ранга.")
   if not poll:return await self.reply(m,"Активного голосования нет.")
   changed=self.store.run(lambda c:c.execute("UPDATE jf_mod_votes SET status='closed',closed_at=? WHERE id=? AND status='active'",(now(),poll['id'])).rowcount,immediate=True)
   if not changed:return await self.reply(m,"Голосование уже закрыто.")
   counts=self.counts(int(poll['id']));a=int(counts[0] or 0);b=int(counts[1] or 0)
   recommendation="большинство за предложение" if a>b else "большинство против предложения" if b>a else "равное число голосов"
   return await self.reply(m,f"🔒 Голосование №{poll['id']} закрыто.\nЗа: <b>{a}</b> · Против: <b>{b}</b>.\nВывод: <b>{recommendation}</b>.\nЭто рекомендация; окончательное решение принимает администрация отдельно.")

def register(dp,bot,F,db_path,manager,lock=None):
 m=ModVotes(dp,bot,F,db_path,manager,lock);m.register();return m

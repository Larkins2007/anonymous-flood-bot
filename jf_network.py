"""Owner-controlled network of chats (Iris-like grid) for Justice Faite Manager."""
from __future__ import annotations
import html,re,secrets,sqlite3,threading
from contextlib import suppress
from datetime import datetime,timezone
import cmdcfg
GROUP_TYPES={"group","supergroup"}
COMMAND_PATTERN=(r"(?isu)^\s*(?:/jf_network(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:сетка создать|сетка привязать\s+[A-Z0-9]{6,12}|сетка отвязать|сетка чаты|сетка \+модер\s+\d\s+.+|сетка -модер\s+.+|сетка модер|сетка баны|глобан\s+.+|глоразбан\s+.+))\s*$")
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
  CREATE TABLE IF NOT EXISTS jf_networks(id INTEGER PRIMARY KEY AUTOINCREMENT,code TEXT NOT NULL UNIQUE,owner_id INTEGER NOT NULL,created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS jf_network_chats(network_id INTEGER NOT NULL,chat_id INTEGER NOT NULL UNIQUE,chat_title TEXT NOT NULL DEFAULT '',joined_at TEXT NOT NULL,PRIMARY KEY(network_id,chat_id));
  CREATE TABLE IF NOT EXISTS jf_network_bans(network_id INTEGER NOT NULL,user_id INTEGER NOT NULL,reason TEXT NOT NULL DEFAULT '',created_by INTEGER NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(network_id,user_id));
 """))
 def network(self,chat):return self.run(lambda c:c.execute("SELECT n.* FROM jf_networks n JOIN jf_network_chats nc ON nc.network_id=n.id WHERE nc.chat_id=?",(chat,)).fetchone())
class Network:
 def __init__(self,dp,bot,F,path,manager,lock=None,owner_id=0):self.dp,self.bot,self.F,self.manager=dp,bot,F,manager;self.store=Store(path,lock);self.owner_id=int(owner_id or 0);self.registered=False
 def init(self):self.store.init()
 def register(self):
  if not self.registered:self.dp.message.register(self.handle,self.F.chat.type.in_(GROUP_TYPES),self.F.text.regexp(COMMAND_PATTERN));self.registered=True
 async def reply(self,m,t):
  try:await m.reply(t,parse_mode="HTML",disable_web_page_preview=True)
  except TypeError:await m.reply(t)
 async def rank(self,chat,uid):
  try:return await self.manager._rank(chat,uid)
  except Exception:return self.manager.store.rank(chat,uid)
 async def owner_only(self,m):
  uid=int(m.from_user.id)
  if cmdcfg.access_was_checked(m):
   return True
  if await self.rank(int(m.chat.id),uid)<5:
   await self.reply(m,"⛔ Управлять сеткой может только создатель чата (ранг 5).")
   return False
  return True
 async def network_owner_only(self,m,network):
  if int(network["owner_id"])!=int(m.from_user.id):
   await self.reply(m,"⛔ Это действие доступно только владельцу данной сети.")
   return False
  return await self.owner_only(m)
 async def apply_global_ban_on_join(self,chat_id,user_id):
  """Enforce a stored network ban when a member is added to another linked chat."""
  try:
   network=self.store.network(int(chat_id))
   if not network:return False
   row=self.store.run(lambda c:c.execute("SELECT reason FROM jf_network_bans WHERE network_id=? AND user_id=?",(int(network["id"]),int(user_id))).fetchone())
   if not row:return False
   await self.bot.ban_chat_member(int(chat_id),int(user_id))
   return True
  except Exception:
   return False
 async def resolve(self,m,text):
  args=text.split()
  target,rest,_=await self.manager._target(m,args)
  return target,rest
 async def handle(self,m):
  if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False):return
  raw=(m.text or "").strip();first=raw.splitlines()[0] if raw else "";first=re.sub(r"^\s*[!.\/]{0,4}","",first).strip();first=re.sub(r"^(?:ирис(?:ка)?|iris)\s+","",first,flags=re.I)
  low=first.casefold();chat,uid=int(m.chat.id),int(m.from_user.id)
  if low.startswith("/jf_network"):return await self.reply(m,"<b>Сетка Justice Faite</b>\n<code>Сетка создать</code> в первой группе; в остальных — <code>Сетка привязать CODE</code>. Создатель сети может назначать <code>Сетка +модер 2 @user</code>, выполнять <code>Глобан @user причина</code> и смотреть <code>Сетка чаты</code>.")
  if low=="сетка создать":
   if not await self.owner_only(m):return
   existing=self.store.network(chat)
   if existing:return await self.reply(m,f"Этот чат уже входит в сеть №{existing['id']}. Код: <code>{existing['code']}</code>")
   code=secrets.token_hex(4).upper()
   def op(c):
    cur=c.execute("INSERT INTO jf_networks(code,owner_id,created_at) VALUES(?,?,?)",(code,uid,now()));nid=int(cur.lastrowid)
    c.execute("INSERT INTO jf_network_chats(network_id,chat_id,chat_title,joined_at) VALUES(?,?,?,?)",(nid,chat,(getattr(m.chat,"title","") or "Чат")[:128],now()));return nid
   nid=self.store.run(op)
   return await self.reply(m,f"✅ Сетка №{nid} создана. Код для привязки остальных групп: <code>{code}</code>. Используй этот код только в своих чатах.")
  if low.startswith("сетка привязать "):
   if not await self.owner_only(m):return
   code=first.split(maxsplit=2)[2].strip().upper()
   network=self.store.run(lambda c:c.execute("SELECT * FROM jf_networks WHERE code=?",(code,)).fetchone())
   if not network:return await self.reply(m,"Сетка с таким кодом не найдена.")
   if int(network["owner_id"])!=uid:return await self.reply(m,"⛔ Привязать группу может только создатель этой сети.")
   if self.store.network(chat):return await self.reply(m,"Этот чат уже состоит в другой или этой сети.")
   self.store.run(lambda c:c.execute("INSERT INTO jf_network_chats(network_id,chat_id,chat_title,joined_at) VALUES(?,?,?,?)",(network["id"],chat,(getattr(m.chat,"title","") or "Чат")[:128],now())))
   return await self.reply(m,f"✅ Чат «{esc(getattr(m.chat,'title','Чат'))}» добавлен в сеть №{network['id']}.")
  if low=="сетка отвязать":
   if not await self.owner_only(m):return
   network=self.store.network(chat)
   if not network or int(network["owner_id"])!=uid:return await self.reply(m,"Этот чат не состоит в твоей сети.")
   self.store.run(lambda c:c.execute("DELETE FROM jf_network_chats WHERE chat_id=?",(chat,)))
   remaining=self.store.run(lambda c:c.execute("SELECT COUNT(*) FROM jf_network_chats WHERE network_id=?",(network["id"],)).fetchone()[0])
   if not remaining:self.store.run(lambda c:c.execute("DELETE FROM jf_networks WHERE id=?",(network["id"],)))
   return await self.reply(m,"Чат отвязан от сети." if remaining else "Последний чат отвязан; пустая сеть удалена.")
  network=self.store.network(chat)
  if not network:return await self.reply(m,"Этот чат ещё не входит в сетку. Создай её командой <code>Сетка создать</code>.")
  nid=int(network["id"])
  if low=="сетка чаты":
   rows=self.store.run(lambda c:c.execute("SELECT chat_id,chat_title FROM jf_network_chats WHERE network_id=? ORDER BY joined_at",(nid,)).fetchall())
   lines=[f"{i}. <b>{esc(r['chat_title'])}</b> · <code>{r['chat_id']}</code>" for i,r in enumerate(rows,1)]
   return await self.reply(m,f"<b>Чаты сети №{nid}</b>\n"+"\n".join(lines))
  if low=="сетка модер":
   rows=self.store.run(lambda c:c.execute("SELECT mm.user_id,MAX(mm.rank) rank FROM jf_manager_moderators mm JOIN jf_network_chats nc ON nc.chat_id=mm.chat_id WHERE nc.network_id=? GROUP BY mm.user_id ORDER BY rank DESC,mm.user_id",(nid,)).fetchall())
   return await self.reply(m,"<b>Модераторы сети</b>\n"+("\n".join(f"ID {r['user_id']} · ранг {r['rank']}" for r in rows) or "Отдельных назначений пока нет."))
  if low=="сетка баны":
   rows=self.store.run(lambda c:c.execute("SELECT user_id,reason,created_at FROM jf_network_bans WHERE network_id=? ORDER BY created_at DESC LIMIT 50",(nid,)).fetchall())
   return await self.reply(m,"<b>Глобальные блокировки</b>\n"+("\n".join(f"ID {r['user_id']} · {esc(r['reason'])}" for r in rows) or "Список пуст."))
  if low.startswith("сетка +модер "):
   if not await self.network_owner_only(m,network):return
   pieces=first.split(maxsplit=3)
   if len(pieces)<4 or not pieces[2].isdigit() or not 1<=int(pieces[2])<=4:return await self.reply(m,"Формат: <code>Сетка +модер 2 @username</code> (ранги 1–4).")
   target,rest=await self.resolve(m,pieces[3])
   if target is None:return await self.reply(m,"Пользователь не найден. Ответь на его сообщение или используй известный @username/ID.")
   rank=int(pieces[2]);rows=self.store.run(lambda c:c.execute("SELECT chat_id FROM jf_network_chats WHERE network_id=?",(nid,)).fetchall())
   done=0
   for row in rows:
    try:self.manager.store.change_rank(int(row[0]),target,rank,uid);done+=1
    except Exception:pass
   return await self.reply(m,f"✅ Ранг {rank} назначен ID {target}: {done}/{len(rows)} чатов.")
  if low.startswith("сетка -модер "):
   if not await self.network_owner_only(m,network):return
   target,rest=await self.resolve(m,first[len("Сетка -модер "):])
   if target is None:return await self.reply(m,"Не удалось определить участника.")
   rows=self.store.run(lambda c:c.execute("SELECT chat_id FROM jf_network_chats WHERE network_id=?",(nid,)).fetchall());done=0
   for row in rows:
    try:self.manager.store.change_rank(int(row[0]),target,0,uid);done+=1
    except Exception:pass
   return await self.reply(m,f"Статус снят с ID {target}: {done}/{len(rows)} чатов.")
  if low.startswith("глобан "):
   if not await self.network_owner_only(m,network):return
   target,rest=await self.resolve(m,first[len("Глобан "):])
   if target is None:return await self.reply(m,"Укажи пользователя ответом на сообщение или через @username/ID.")
   if target==uid:return await self.reply(m,"Нельзя заблокировать самого себя.")
   reason=" ".join(rest).strip() or "Глобальная блокировка сети"
   rows=self.store.run(lambda c:c.execute("SELECT chat_id FROM jf_network_chats WHERE network_id=?",(nid,)).fetchall());done=[];failed=[]
   for row in rows:
    cid=int(row[0])
    try:await self.bot.ban_chat_member(cid,target);done.append(cid)
    except Exception:failed.append(cid)
   self.store.run(lambda c:c.execute("INSERT INTO jf_network_bans(network_id,user_id,reason,created_by,created_at) VALUES(?,?,?,?,?) ON CONFLICT(network_id,user_id) DO UPDATE SET reason=excluded.reason,created_by=excluded.created_by,created_at=excluded.created_at",(nid,target,reason,uid,now())))
   return await self.reply(m,f"Глобальная блокировка ID {target}: успешно {len(done)}, ошибки {len(failed)}."+("\nПроверь права бота в чатах: "+", ".join(map(str,failed)) if failed else ""))
  if low.startswith("глоразбан "):
   if not await self.network_owner_only(m,network):return
   target,rest=await self.resolve(m,first[len("Глоразбан "):])
   if target is None:return await self.reply(m,"Не удалось определить участника.")
   rows=self.store.run(lambda c:c.execute("SELECT chat_id FROM jf_network_chats WHERE network_id=?",(nid,)).fetchall());done=[];failed=[]
   for row in rows:
    cid=int(row[0])
    try:await self.bot.unban_chat_member(cid,target,only_if_banned=True);done.append(cid)
    except Exception:failed.append(cid)
   self.store.run(lambda c:c.execute("DELETE FROM jf_network_bans WHERE network_id=? AND user_id=?",(nid,target)))
   return await self.reply(m,f"Глобальный разбан ID {target}: успешно {len(done)}, ошибки {len(failed)}.")
def register(dp,bot,F,db_path,manager,lock=None,owner_id=0):
 m=Network(dp,bot,F,db_path,manager,lock=lock,owner_id=owner_id);m.register();return m

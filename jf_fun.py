"""Safe chat fun commands for Justice Faite Manager (no wagers or punishments)."""
from __future__ import annotations
import html,re,secrets,sqlite3,threading
from contextlib import suppress
GROUP_TYPES={"group","supergroup"}
COMMAND_PATTERN=(r"(?isu)^\s*(?:/jf_fun(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:рандом(?:\s+-?\d+(?:\s+-?\d+)?)?|!данет(?:\s+.+)?|!выбери\s+.+|!инфа\s+.+|!кубик|кубик|пинг|кинг|пиу|бот))\s*$")
class Fun:
 def __init__(self,dp,bot,F):self.dp,self.bot,self.F=dp,bot,F;self.registered=False
 def register(self):
  if not self.registered:self.dp.message.register(self.handle,self.F.chat.type.in_(GROUP_TYPES),self.F.text.regexp(COMMAND_PATTERN));self.registered=True
 async def reply(self,m,t):
  try:await m.reply(t,disable_web_page_preview=True)
  except TypeError:await m.reply(t)
 async def handle(self,m):
  if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False):return
  raw=(m.text or "").strip();first=raw.splitlines()[0] if raw else ""
  clean=re.sub(r"^\s*[!.\/]{0,4}","",first).strip();clean=re.sub(r"^(?:ирис(?:ка)?|iris)\s+","",clean,flags=re.I);low=clean.casefold()
  if low.startswith("/jf_fun"):return await self.reply(m,"<b>Развлечения Justice Faite</b>\n<code>Рандом 1 100</code>, <code>!Данет вопрос</code>, <code>!Выбери чай или кофе</code>, <code>!Инфа текст</code>, <code>!Кубик</code>, <code>Пинг</code>.")
  if low.startswith("рандом"):
   parts=low.split()
   try:
    if len(parts)==2:a,b=0,int(parts[1])
    elif len(parts)==3:a,b=int(parts[1]),int(parts[2])
    else:raise ValueError
    if abs(a)>1_000_000_000 or abs(b)>1_000_000_000:raise ValueError
    lo,hi=sorted((a,b));return await self.reply(m,f"🎲 Выпало число: <b>{secrets.randbelow(hi-lo+1)+lo}</b>")
   except ValueError:return await self.reply(m,"Формат: <code>Рандом 1 100</code>. Диапазон — не более миллиарда в каждую сторону.")
  if low.startswith("данет"):
   if not clean.split(maxsplit=1)[1:]:return await self.reply(m,"Добавь вопрос после команды.")
   return await self.reply(m,"🔮 "+secrets.choice(("Да.","Нет.","Неопределённо.")))
  if low.startswith("выбери "):
   payload=clean[len("Выбери "):].strip();parts=re.split(r"\s+или\s+",payload,maxsplit=1,flags=re.I)
   if len(parts)!=2 or not all(parts) or len(payload)>300:return await self.reply(m,"Формат: <code>!Выбери чай или кофе</code> (два варианта до 300 символов).")
   return await self.reply(m,"🧭 Выбор: <b>"+html.escape(secrets.choice(parts),quote=False)+"</b>")
  if low.startswith("инфа "):
   payload=clean[len("Инфа "):].strip()
   if not payload or len(payload)>250:return await self.reply(m,"Добавь короткий текст после команды (до 250 символов).")
   return await self.reply(m,f"📊 Вероятность «{html.escape(payload,quote=False)}»: <b>{secrets.randbelow(101)}%</b>")
  if low in {"!кубик","кубик"}:
   return await self.reply(m,f"🎲 Выпало: <b>{secrets.randbelow(6)+1}</b>")
  if low in {"пинг","кинг","пиу","бот"}:
   answers={"пинг":"Понг!","кинг":"Конг!","пиу":"Пау!","бот":"На месте!"}
   return await self.reply(m,answers[low])
def register(dp,bot,F):
 m=Fun(dp,bot,F);m.register();return m

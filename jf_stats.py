"""Persistent per-chat activity statistics for Justice Faite Manager."""
from __future__ import annotations
import re
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timedelta, timezone

GROUP_TYPES={"group","supergroup"}
COMMAND_PATTERN=(r"(?ius)^\s*(?:/jf_stats(?:@[A-Za-z0-9_]+)?(?:\s|$)|[!.\/]{0,4}(?:(?:ирис(?:ка)?|iris)\s+)?(?:"
    r"чат инфо|чат стата(?: по часам)?|моя стата|стата по часам|список неактив(?:\s+\d+\s*(?:дн(?:ей|я)?|день|сут(?:ки)?))?|"
    r"список молчунов(?:\s+\d+\s*(?:дн(?:ей|я)?|день|сут(?:ки)?))?|олды|новички|"
    r"топ(?:\s+\d{1,3})?(?:\s+(?:сутки|неделя|месяц|вся))?|"
    r"стата(?:\s+\d{1,3})?(?:\s+(?:сутки|неделя|месяц|вся))?)\b.*$)")


def now(): return datetime.now(timezone.utc)

class Store:
    def __init__(self,path,lock=None): self.path=str(path); self.lock=lock or threading.RLock()
    def run(self,fn):
        with self.lock:
            c=sqlite3.connect(self.path,timeout=30); c.row_factory=sqlite3.Row
            c.execute("PRAGMA busy_timeout=30000")
            try:
                out=fn(c)
                if c.in_transaction: c.commit()
                return out
            except Exception:
                with suppress(Exception): c.rollback()
                raise
            finally: c.close()
    def init(self):
        self.run(lambda c:c.executescript("""
          CREATE TABLE IF NOT EXISTS jf_stats_daily(chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,day TEXT NOT NULL,message_count INTEGER NOT NULL DEFAULT 0,last_seen TEXT NOT NULL,PRIMARY KEY(chat_id,user_id,day));
          CREATE INDEX IF NOT EXISTS idx_jf_stats_daily_chat_day ON jf_stats_daily(chat_id,day,user_id);
          CREATE TABLE IF NOT EXISTS jf_stats_hourly(chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,day TEXT NOT NULL,hour INTEGER NOT NULL,message_count INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(chat_id,user_id,day,hour));
          CREATE INDEX IF NOT EXISTS idx_jf_stats_hourly_chat_day_hour ON jf_stats_hourly(chat_id,day,hour);
        """))
    def record(self,chat_id,user_id,stamp):
        day=stamp.date().isoformat(); hour=stamp.hour; ts=stamp.isoformat()
        def op(c):
            c.execute("INSERT INTO jf_stats_daily VALUES(?,?,?,?,?) ON CONFLICT(chat_id,user_id,day) DO UPDATE SET message_count=message_count+1,last_seen=excluded.last_seen",(chat_id,user_id,day,1,ts))
            c.execute("INSERT INTO jf_stats_hourly VALUES(?,?,?,?,1) ON CONFLICT(chat_id,user_id,day,hour) DO UPDATE SET message_count=message_count+1",(chat_id,user_id,day,hour))
        self.run(op)

class Stats:
    def __init__(self,dp,bot,F,db_path,lock=None):
        self.dp,self.bot,self.F=dp,bot,F; self.store=Store(db_path,lock); self.registered=False
    def init(self): self.store.init()
    def register(self):
        if not self.registered:
            self.dp.message.register(self.handle,self.F.chat.type.in_(GROUP_TYPES),self.F.text.regexp(COMMAND_PATTERN)); self.registered=True
    def record_message(self,message):
        """Count ordinary text messages that pass the existing group-message observer."""
        user=getattr(message,"from_user",None)
        if not user or getattr(user,"is_bot",False): return
        self.store.record(int(message.chat.id),int(user.id),now())
    async def reply(self,m,text):
        try: await m.reply(text,parse_mode="HTML",disable_web_page_preview=True)
        except TypeError: await m.reply(text)
    def _name(self,chat_id,user_id):
        try:
            r=self.store.run(lambda c:c.execute("SELECT username,display_name FROM jf_manager_member_names WHERE chat_id=? AND user_id=?",(chat_id,user_id)).fetchone())
        except sqlite3.Error: r=None
        if r and r[0]: return "@"+r[0]
        if r and r[1]: return r[1]
        return f"ID {user_id}"
    def _resolve_target(self,m,args):
        reply=getattr(m,"reply_to_message",None)
        if reply and getattr(reply,"from_user",None): return int(reply.from_user.id)
        if args:
            first=args.split()[0]
            if first.isdigit(): return int(first)
            if first.startswith("@"):
                try:
                    r=self.store.run(lambda c:c.execute("SELECT user_id FROM jf_manager_member_names WHERE chat_id=? AND lower(username)=? ORDER BY last_seen DESC LIMIT 1",(int(m.chat.id),first[1:].casefold())).fetchone())
                    if r: return int(r[0])
                except sqlite3.Error: pass
        return int(m.from_user.id)
    def _period(self,kind):
        today=now().date()
        if kind in {"сутки","день","today"}: return today.isoformat(),"за сегодня"
        if kind in {"неделя","week"}: return (today-timedelta(days=6)).isoformat(),"за 7 дней"
        if kind in {"месяц","month"}: return (today-timedelta(days=29)).isoformat(),"за 30 дней"
        return "0001-01-01","за всё время"
    async def handle(self,m):
        if not getattr(m,"from_user",None) or getattr(m.from_user,"is_bot",False): return
        raw=(m.text or "").strip(); lines=raw.splitlines(); first=lines[0] if lines else ""
        first=re.sub(r"^\s*[!.\/]{0,4}","",first).strip()
        first=re.sub(r"^(?:ирис(?:ка)?|iris)\s+","",first,flags=re.I)
        if first.lower().startswith("/jf_stats"):
            return await self.reply(m,"<b>Статистика Justice Faite</b>\n<code>Топ сутки</code>, <code>Топ неделя</code>, <code>Топ месяц</code>, <code>Топ вся</code>, <code>Моя стата</code>, <code>Стата по часам</code>, <code>Олды</code>, <code>Новички</code>, <code>Чат инфо</code>.")
        low=first.casefold(); chat_id=int(m.chat.id); uid=int(m.from_user.id)
        if low=="чат инфо":
            total=self.store.run(lambda c:c.execute("SELECT COALESCE(SUM(message_count),0) FROM jf_stats_daily WHERE chat_id=?",(chat_id,)).fetchone()[0])
            active=self.store.run(lambda c:c.execute("SELECT COUNT(DISTINCT user_id) FROM jf_stats_daily WHERE chat_id=? AND day>=?",(chat_id,(now().date()-timedelta(days=6)).isoformat())).fetchone()[0])
            known=0
            with suppress(Exception): known=int(await self.bot.get_chat_member_count(chat_id))
            title=getattr(m.chat,"title",None) or "Чат"
            return await self.reply(m,f"<b>{title}</b>\nУчастников в Telegram: {known or 'н/д'}\nСообщений учтено ботом: {total}\nАктивных авторов за 7 дней: {active}\n<i>Счётчики начали накапливаться после установки менеджера.</i>")
        if low in {"чат стата", "чат стата по часам"}:
            if low == "чат стата по часам":
                rows=self.store.run(lambda c:c.execute("SELECT hour,SUM(message_count) n FROM jf_stats_hourly WHERE chat_id=? AND day>=? GROUP BY hour ORDER BY hour",(chat_id,(now().date()-timedelta(days=6)).isoformat())).fetchall())
                counts={int(r["hour"]):int(r["n"]) for r in rows}
                lines=[f"{h:02d}:00 · {'█'*min(25,counts.get(h,0))} {counts.get(h,0)}" for h in range(24) if counts.get(h,0)]
                return await self.reply(m,"<b>Сообщения чата по часам · 7 дней</b>\n"+("\n".join(lines) or "Данных пока нет.")+"\n<i>Время UTC.</i>")
            total=self.store.run(lambda c:c.execute("SELECT COALESCE(SUM(message_count),0) FROM jf_stats_daily WHERE chat_id=?",(chat_id,)).fetchone()[0])
            today=self.store.run(lambda c:c.execute("SELECT COALESCE(SUM(message_count),0) FROM jf_stats_daily WHERE chat_id=? AND day=?",(chat_id,now().date().isoformat())).fetchone()[0])
            people=self.store.run(lambda c:c.execute("SELECT COUNT(DISTINCT user_id) FROM jf_stats_daily WHERE chat_id=?",(chat_id,)).fetchone()[0])
            return await self.reply(m,f"<b>Статистика чата</b>\nВсего сообщений учтено: {total}\nСегодня: {today}\nАвторов за всё время: {people}")
        if low in {"моя стата","стата по часам"} or low.startswith("стата по часам "):
            target=self._resolve_target(m,first.partition(" ")[2].strip() if " " in first else "") if low.startswith("стата по часам") else uid
            if low.startswith("стата по часам"):
                rows=self.store.run(lambda c:c.execute("SELECT hour,SUM(message_count) n FROM jf_stats_hourly WHERE chat_id=? AND user_id=? AND day>=? GROUP BY hour ORDER BY hour",(chat_id,target,(now().date()-timedelta(days=6)).isoformat())).fetchall())
                counts={int(r["hour"]):int(r["n"]) for r in rows}
                bars=[f"{h:02d}:00 · {'█'*min(20,counts.get(h,0))} {counts.get(h,0)}" for h in range(24) if counts.get(h,0)]
                return await self.reply(m,f"<b>Активность по часам · {self._name(chat_id,target)}</b>\n"+("\n".join(bars) or "За последние 7 дней сообщений не учтено.")+"\n<i>Время UTC.</i>")
            total=self.store.run(lambda c:c.execute("SELECT COALESCE(SUM(message_count),0) FROM jf_stats_daily WHERE chat_id=? AND user_id=?",(chat_id,target)).fetchone()[0])
            week=self.store.run(lambda c:c.execute("SELECT COALESCE(SUM(message_count),0) FROM jf_stats_daily WHERE chat_id=? AND user_id=? AND day>=?",(chat_id,target,(now().date()-timedelta(days=6)).isoformat())).fetchone()[0])
            return await self.reply(m,f"<b>Статистика · {self._name(chat_id,target)}</b>\nСообщений за всё время: {total}\nЗа последние 7 дней: {week}")
        if low in {"олды","новички"}:
            order="ASC" if low=="олды" else "DESC"
            rows=self.store.run(lambda c:c.execute(f"SELECT user_id,username,first_name,joined_at FROM group_members WHERE chat_id=? AND active=1 ORDER BY joined_at {order} LIMIT 20",(chat_id,)).fetchall())
            lines=[f"{i}. {'@'+r['username'] if r['username'] else (r['first_name'] or str(r['user_id']))} · {str(r['joined_at'])[:10]}" for i,r in enumerate(rows,1)]
            return await self.reply(m,f"<b>{'Старожилы' if low=='олды' else 'Недавно вступившие'}</b>\n"+("\n".join(lines) or "Пока нет данных об участниках."))
        if low.startswith("список неактив") or low.startswith("список молчунов"):
            p=first.split(); days=7
            if len(p)>2 and p[2].isdigit(): days=max(1,min(365,int(p[2])))
            cutoff=(now().date()-timedelta(days=days)).isoformat()
            rows=self.store.run(lambda c:c.execute("""SELECT gm.user_id,gm.username,gm.first_name,MAX(d.day) last_day,SUM(d.message_count) total FROM group_members gm LEFT JOIN jf_stats_daily d ON d.chat_id=gm.chat_id AND d.user_id=gm.user_id WHERE gm.chat_id=? AND gm.active=1 GROUP BY gm.user_id HAVING MAX(d.day) IS NULL OR MAX(d.day)<? ORDER BY last_day LIMIT 30""",(chat_id,cutoff)).fetchall())
            lines=[f"{('@'+r['username']) if r['username'] else (r['first_name'] or str(r['user_id']))} · последнее сообщение: {r['last_day'] or 'не учтено'}" for r in rows]
            return await self.reply(m,f"<b>Неактивные · {days} дн.</b>\n"+("\n".join(lines) or "Неактивных по этому условию не найдено."))
        match=re.match(r"^(топ|стата)(?:\s+(\d{1,3}))?(?:\s+(сутки|неделя|месяц|вся))?$",low)
        if match:
            limit=max(1,min(100,int(match.group(2) or 10))); period=match.group(3) or "сутки"
            start,label=self._period(period)
            rows=self.store.run(lambda c:c.execute("""SELECT d.user_id,SUM(d.message_count) n FROM jf_stats_daily d WHERE d.chat_id=? AND d.day>=? GROUP BY d.user_id ORDER BY n DESC,d.user_id LIMIT ?""",(chat_id,start,limit)).fetchall())
            lines=[f"{i}. {self._name(chat_id,int(r['user_id']))} — <b>{r['n']}</b>" for i,r in enumerate(rows,1)]
            return await self.reply(m,f"<b>Топ активности {label}</b>\n"+("\n".join(lines) or "Пока нет накопленной статистики."))


def register(dp,bot,F,db_path,lock=None):
    module=Stats(dp,bot,F,db_path,lock=lock); module.register(); return module

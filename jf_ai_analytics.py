"""Owner-only, privacy-minimizing analytics over the existing flood archive.

No second message listener is installed. The module reads the archive maintained
by jf_console, sends a bounded/redacted sample to the configured AI provider,
and stores generated reports (not a second copy of raw messages) in SQLite.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import aiohttp

from jf_ai import clean_ai_text

logger = logging.getLogger("justice-faite-ai-analytics")

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
TG_USER_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{4,}")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
TOKEN_RE = re.compile(r"\b\d{5,12}:[A-Za-z0-9_-]{25,}\b")
SENSITIVE_CONTEXT_RE = re.compile(
    r"(?i)\b(?:паспорт|снилс|инн|номер карты|банковск|медицинск|диагноз|болею|лекарств|адрес проживания|домашний адрес|дата рождения|документ|код подтверждения|одноразовый код)\b"
)

ANALYSIS_SYSTEM = """Ты — аналитик сообщества Justice Faite. Анализируй только предоставленную обезличенную выборку сообщений и агрегированные числа.
Составь полезный, спокойный отчёт для владельца чата на русском языке. Не пытайся устанавливать личности, оценивать характер участников, диагностировать психологическое состояние или делать выводы о конкретных людях. Не составляй рейтинги участников и не выделяй авторов. Не цитируй сообщения дословно. Не повторяй личные данные, даже если они случайно остались в тексте.

Структура:
1. Краткая картина активности.
2. Основные темы и интересы, которые действительно видны в сообщениях.
3. Что поддерживает разговор и где он затихает.
4. Повторяющиеся вопросы/темы или признаки нехватки новых тем.
5. 3–5 конкретных идей для оживления флуда/интерактивов.
6. Возможные сигналы нарушений правил: укажи только случаи, которые действительно видны в выборке, и номер пункта. Для каждого сигнала укажи наблюдаемый факт и уровень уверенности; сомнительные случаи помечай «требует проверки», не называй участника виновным. Если явных сигналов нет, так и напиши, но не утверждай, что нарушений не было во всём чате.
7. Что стоит проверить администрации (без автоматических санкций).

Краткое содержание правил: 1) возраст 16+ подтверждается документом; возраст нельзя угадывать по переписке. 2) запрещены оскорбления, травля, провокации, пассивная агрессия и повторение неприятных шуток после просьбы остановиться; учитывай контекст и систематичность. 3) запрещено целенаправленное унижение через сообщения, профиль, мемы, изображения и игровые действия. 4) запрещён спам: более трёх одинаковых действий подряд; чрезмерное однотипное медиа; после медиа от трёх разных участников четвёртый должен отправить обычный текст; технические повторы могут быть исключением. Пункты 4.3 и 4.5 отдельно проверяются локальным Justice Watch. 5) запрещены публичные выяснения отношений, разжигание конфликтов и вовлечение третьих лиц без согласия. 6) запрещены оскорбления и дискриминация по личным признакам. 7) запрещены политические споры, пропаганда и обсуждения военных конфликтов. 8) запрещён порнографический, жестокий, шокирующий и иной запрещённый правилами контент. 9) запрещено раскрывать личные данные, переписки и личный контент без согласия. 10) реклама и продвижение без согласования запрещены. 11) жалобы требуют доказательств и не должны использоваться для давления или мести; для вывода нужны данные самой жалобы. 12) смена и возврат ролей регулируются сроками и историей назначений; не выводи нарушение из обычной переписки. 13) приглашения регулируются отдельным правилом и контекстом. 14) правила и решения ведущего действуют во время ивента; для оценки нужен контекст игры. 15) запрещено выпрашивать наказания. 16) субординация администрации оценивается с учётом должностей и ситуации; конструктивная критика разрешена. 17) дискреционные решения администрации не являются автоматическим доказательством нарушения. Особенно пункты 2.3, 17.3 и 17.5 всегда помечай как требующие оценки администрации, а не как установленную вину. 18) действует последняя редакция правил.
Не делай выводов о возрасте, личности, намерениях, психическом состоянии или виновности по одному сообщению. Не оценивай эффективность конкретных администраторов и не составляй рейтинги участников. Отделяй факты от предположений. Если данных мало, прямо скажи об этом. Не выдумывай события, правила, активность или причины молчания. Выборка ограничена и не гарантирует обнаружение всех нарушений. Отчёт не должен превышать 3000 символов.

Отделяй факты от предположений. Если данных мало, прямо скажи об этом. Не выдумывай события, правила, активность или причины молчания. Отчёт не должен превышать 3000 символов."""


def _redact(text: str) -> str:
    value = str(text or "")
    value = TOKEN_RE.sub("[скрытый секрет]", value)
    value = EMAIL_RE.sub("[скрытый email]", value)
    value = URL_RE.sub("[ссылка]", value)
    value = TG_USER_RE.sub("[упоминание]", value)
    value = PHONE_RE.sub("[скрытый номер]", value)
    value = re.sub(r"(?i)\b(?:пароль|password|api[_ -]?key|secret|токен)\s*[:=]?\s*\S+", "[скрытые данные]", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:500]



class JusticeAIAnalytics:
    def __init__(self, bot, store, owner_id: int, chat_id: int):
        self.bot = bot
        self.store = store
        self.owner_id = int(owner_id or 0)
        self.chat_id = int(chat_id or 0)
        self.enabled = (os.getenv("JF_AI_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"} and os.getenv("JF_AI_ANALYTICS_ENABLED", "true").strip().casefold() in {"1", "true", "yes", "on"})
        self.periodic_hours = max(1, min(12, int(os.getenv("JF_AI_ANALYTICS_INTERVAL_HOURS", "3") or "3")))
        self.daily_hour = max(0, min(23, int(os.getenv("JF_AI_ANALYTICS_DAILY_HOUR", "21") or "21")))
        self.utc_offset = max(-12, min(14, int(os.getenv("JF_AI_ANALYTICS_UTC_OFFSET_HOURS", "3") or "3")))
        self.timeout = max(10, min(90, int(os.getenv("JF_AI_TIMEOUT", "45") or "45")))
        self.base_url = os.getenv("JF_AI_BASE_URL", "https://api.groq.com/openai/v1/chat/completions").strip()
        self.api_key = os.getenv("JF_AI_API_KEY", "").strip()
        self.model = os.getenv("JF_AI_MODEL", "openai/gpt-oss-20b").strip()
        self.watch_enabled = os.getenv("JF_AI_WATCH_ENABLED", "true").strip().casefold() in {"1", "true", "yes", "on"}
        self.watch_poll_seconds = max(15, min(300, int(os.getenv("JF_AI_WATCH_POLL_SECONDS", "30") or "30")))
        self._running = False
        self._last_watch_poll = 0.0

    def init(self):
        def op(conn):
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jf_ai_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_type TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    participant_count INTEGER NOT NULL DEFAULT 0,
                    report_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jf_ai_reports_created
                    ON jf_ai_reports(created_at DESC);
                CREATE TABLE IF NOT EXISTS jf_ai_analytics_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jf_ai_watch_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_key TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    sender_user_id INTEGER NOT NULL DEFAULT 0,
                    evidence TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                );
            """)
            now = datetime.now(timezone.utc)
            zone = timezone(timedelta(hours=self.utc_offset))
            periodic = now + timedelta(hours=self.periodic_hours)
            daily = self._next_daily_fixed(now, zone)
            conn.execute("INSERT OR IGNORE INTO jf_ai_analytics_state(key,value) VALUES('next_periodic',?)", (periodic.isoformat(),))
            conn.execute("INSERT OR IGNORE INTO jf_ai_analytics_state(key,value) VALUES('next_daily',?)", (daily.isoformat(),))
            # Start watching from the current archive tail. A new deployment must
            # never replay old chat history as fresh violations.
            archive_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jf_chat_message_log'").fetchone()
            tail = conn.execute("SELECT COALESCE(MAX(id),0) FROM jf_chat_message_log WHERE chat_id=?", (self.chat_id,)).fetchone()[0] if archive_exists else 0
            conn.execute("INSERT OR IGNORE INTO jf_ai_analytics_state(key,value) VALUES('watch_cursor',?)", (str(int(tail or 0)),))
        self.store.run(op)

    def _next_daily_fixed(self, now: datetime, zone):
        local = now.astimezone(zone)
        target = local.replace(hour=self.daily_hour, minute=0, second=0, microsecond=0)
        if target <= local:
            target += timedelta(days=1)
        return target.astimezone(timezone.utc)

    def register(self, dp, F):
        # Commands are private and owner-only; analytics reports never enter the group.
        pattern = r"(?isu)^\s*/jf_ai_(?:report|reports|watch|audit|owner)(?:@[A-Za-z0-9_]+)?(?:\s|$)"
        dp.message.register(self.handle_owner_command, F.chat.type == "private", F.from_user.id == self.owner_id, F.text.regexp(pattern))

    async def _source_data(self, hours: int):
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        rows = self.store.run(lambda c: c.execute(
            "SELECT message_at,body,message_type FROM jf_chat_message_log "
            "WHERE chat_id=? AND direction='in' AND message_at>=? AND message_at<=? "
            "ORDER BY message_at DESC,id DESC LIMIT 1200",
            (self.chat_id, start.isoformat(), end.isoformat()),
        ).fetchall())
        # Keep only useful text and a bounded recent sample. Do not export names,
        # usernames, Telegram IDs, message IDs, or reply relationships.
        lines = []
        unique_bodies = set()
        for row in reversed(rows):
            raw_body = str(row["body"] or "")
            # Exclude likely private/sensitive conversations rather than relying
            # only on redaction after they have entered the provider prompt.
            if SENSITIVE_CONTEXT_RE.search(raw_body):
                continue
            body = _redact(raw_body)
            if not body or body.startswith("[") or len(body) < 2:
                continue
            key = body.casefold()
            if key in unique_bodies:
                continue
            unique_bodies.add(key)
            stamp = str(row["message_at"] or "")
            try:
                dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                clock = dt.astimezone(timezone(timedelta(hours=self.utc_offset))).strftime("%H:%M")
            except Exception:
                clock = "--:--"
            lines.append(f"{clock}: {body}")
        participant_count = self.store.run(lambda c: c.execute(
            "SELECT COUNT(DISTINCT sender_user_id) FROM jf_chat_message_log "
            "WHERE chat_id=? AND direction='in' AND message_at>=? AND message_at<=? AND sender_user_id>0",
            (self.chat_id, start.isoformat(), end.isoformat()),
        ).fetchone()[0])
        # The model sees only the bounded, redacted sample and aggregate counts.
        sample = "\n".join(lines[-100:])[-14000:]
        return start, end, len(rows), int(participant_count or 0), sample

    async def _generate(self, report_type: str, hours: int):
        if not (self.enabled and self.api_key and self.base_url and self.model):
            raise RuntimeError("AI analytics is not configured")
        start, end, message_count, participant_count, sample = await self._source_data(hours)
        if message_count == 0 or not sample:
            report = "За выбранный период в архиве недостаточно текстовых сообщений для содержательного анализа."
        else:
            period = f"Период: {start.isoformat()} — {end.isoformat()} UTC.\n" \
                     f"Сообщений в архиве: {message_count}. Уникальных авторов по внутреннему счётчику: {participant_count}.\n\n" \
                     f"Обезличенная выборка (повторы удалены, до 100 сообщений):\n{sample}"
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": ANALYSIS_SYSTEM},
                    {"role": "user", "content": f"Тип отчёта: {report_type}.\n{period}"},
                ],
                "max_completion_tokens": 900,
                "stream": False,
            }
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.base_url, json=payload, headers=headers) as response:
                    if response.status >= 400:
                        detail = ""
                        try:
                            data = await response.json()
                            error = data.get("error", {}) if isinstance(data, dict) else {}
                            detail = error.get("message", "") if isinstance(error, dict) else str(error)
                        except Exception:
                            pass
                        raise RuntimeError(f"AI provider HTTP {response.status}: {detail[:180]}")
                    data = await response.json()
            content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
            report = clean_ai_text(str(content).strip())
            if not report:
                raise RuntimeError("AI provider returned an empty analytics report")
            report = clean_ai_text(report[:3000])
        def save_report(conn):
            conn.execute(
                "INSERT INTO jf_ai_reports(report_type,period_start,period_end,message_count,participant_count,report_text,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (report_type, start.isoformat(), end.isoformat(), message_count, participant_count, report, datetime.now(timezone.utc).isoformat()),
            )
            conn.execute("DELETE FROM jf_ai_reports WHERE id NOT IN (SELECT id FROM jf_ai_reports ORDER BY id DESC LIMIT 1000)")
        self.store.run(save_report)
        return report, message_count, participant_count

    async def create_and_send(self, report_type: str, hours: int):
        report, count, people = await self._generate(report_type, hours)
        title = "Ежедневная аналитика" if report_type == "daily" else ("Ручная аналитика" if report_type == "manual" else "Периодическая аналитика")
        message = f"🧠 {title}\nПериод: {hours} ч. · сообщений: {count} · авторов: {people}\n\n{report}"
        # Telegram limit safety; report is stored in full before delivery.
        await self.bot.send_message(self.owner_id, message[:3900], disable_web_page_preview=True)
        return True

    @staticmethod
    def _is_media(row):
        kind = str(row["message_type"] or "text").casefold()
        return kind not in {"text", "unknown", ""} and not kind.startswith("edit")

    @staticmethod
    def _normal_action(row):
        kind = str(row["message_type"] or "text").casefold().strip()
        body = re.sub(r"\s+", " ", str(row["body"] or "").casefold()).strip()
        return kind, body

    async def scan_watch_once(self):
        """Run deterministic checks over newly archived messages; never punish users."""
        if not self.watch_enabled or not self.chat_id or not self.owner_id:
            return 0
        state = self.store.run(lambda c: c.execute(
            "SELECT value FROM jf_ai_analytics_state WHERE key='watch_cursor'"
        ).fetchone())
        cursor = int(state[0]) if state else 0
        archive_exists = self.store.run(lambda c: c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jf_chat_message_log'").fetchone())
        if not archive_exists:
            return 0
        rows = self.store.run(lambda c: c.execute(
            "SELECT id,chat_id,message_id,sender_user_id,message_type,body,message_at "
            "FROM jf_chat_message_log WHERE chat_id=? AND direction='in' AND id>? "
            "ORDER BY id ASC LIMIT 500", (self.chat_id, cursor)
        ).fetchall())
        created = []
        for row in rows:
            rid = int(row["id"])
            evidence_rows = self.store.run(lambda c, r=rid: c.execute(
                "SELECT id,message_id,sender_user_id,message_type,body,message_at "
                "FROM jf_chat_message_log WHERE chat_id=? AND direction='in' AND id<=? "
                "ORDER BY id DESC LIMIT 5", (self.chat_id, r)
            ).fetchall())
            recent = list(reversed(evidence_rows))
            category = None
            evidence = []
            # Rule 4.5: three consecutive media posts by three distinct
            # participants; the fourth media post continues the chain.
            if len(recent) >= 4 and self._is_media(recent[-1]):
                previous = recent[-4:-1]
                if all(self._is_media(x) for x in previous):
                    authors = [int(x["sender_user_id"] or 0) for x in previous]
                    if all(authors) and len(set(authors)) == 3:
                        category = "media_chain_4_5"
                        evidence = previous + [recent[-1]]
            # Rule 4.3: four or more identical consecutive actions.
            if category is None and len(recent) >= 4:
                last = recent[-4:]
                actions = [self._normal_action(x) for x in last]
                if actions[0] == actions[1] == actions[2] == actions[3] and actions[0][1]:
                    category = "repeated_action_4_3"
                    evidence = last
            if category:
                # One notification per continuous burst, not one notification
                # for every subsequent message in the same spam/media chain.
                prior_ids = [int(x["message_id"]) for x in recent[:-1]]
                if prior_ids:
                    placeholders = ",".join("?" for _ in prior_ids)
                    already_alerted = self.store.run(lambda c, cat=category, ids=prior_ids, marks=placeholders: c.execute(
                        f"SELECT 1 FROM jf_ai_watch_alerts WHERE category=? AND chat_id=? AND message_id IN ({marks}) LIMIT 1",
                        (cat, self.chat_id, *ids),
                    ).fetchone())
                    if already_alerted:
                        category = None
            if category:
                alert_key = f"{category}:{self.chat_id}:{int(row['message_id'])}"
                lines = []
                for item in evidence:
                    body = _redact(str(item["body"] or ""))[:100]
                    lines.append(f"msg={int(item['message_id'])}, user_id={int(item['sender_user_id'] or 0)}, type={item['message_type']}, text={body}")
                payload = "\n".join(lines)
                def save(conn, key=alert_key, cat=category, msg=row, ev=payload):
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO jf_ai_watch_alerts(alert_key,category,chat_id,message_id,sender_user_id,evidence,created_at) VALUES(?,?,?,?,?,?,?)",
                        (key, cat, self.chat_id, int(msg["message_id"]), int(msg["sender_user_id"] or 0), ev, datetime.now(timezone.utc).isoformat()),
                    )
                    return cur.rowcount > 0
                if self.store.run(save):
                    created.append((category, int(row["message_id"]), payload))
            cursor = rid
        self.store.run(lambda c: c.execute(
            "UPDATE jf_ai_analytics_state SET value=? WHERE key='watch_cursor'", (str(cursor),)
        ))
        pending = self.store.run(lambda c: c.execute(
            "SELECT alert_key,category,message_id,evidence FROM jf_ai_watch_alerts "
            "WHERE delivered_at IS NULL ORDER BY id ASC LIMIT 20"
        ).fetchall())
        delivered = 0
        for alert in pending:
            category = str(alert["category"])
            message_id = int(alert["message_id"])
            title = "Медиа-цепочка (правило 4.5)" if category == "media_chain_4_5" else "Повторяющиеся действия (правило 4.3)"
            text = (f"Justice Watch · требует проверки\n{title}\n"
                    f"Чат: {self.chat_id} · сообщение: {message_id}\n"
                    "Это автоматический сигнал, не наказание и не окончательный вывод.\n\n"
                    f"Доказательный фрагмент из архива:\n{alert['evidence']}")
            try:
                await self.bot.send_message(self.owner_id, text[:3900], disable_web_page_preview=True)
                self.store.run(lambda c, key=str(alert["alert_key"]): c.execute(
                    "UPDATE jf_ai_watch_alerts SET delivered_at=? WHERE alert_key=? AND delivered_at IS NULL",
                    (datetime.now(timezone.utc).isoformat(), key),
                ))
                delivered += 1
            except Exception:
                logger.exception("Justice Watch alert delivery failed | category=%s message=%s", category, message_id)
        return delivered

    def self_audit(self):
        checks = []
        checks.append(("Owner ID", bool(self.owner_id)))
        checks.append(("Primary chat ID", bool(self.chat_id)))
        checks.append(("Chat archive table", bool(self.store.run(lambda c: c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jf_chat_message_log'").fetchone()))))
        checks.append(("AI provider configured", bool(self.api_key and self.base_url and self.model)))
        checks.append(("AI analytics enabled", bool(self.enabled)))
        checks.append(("Justice Watch enabled", bool(self.watch_enabled)))
        checks.append(("Report archive", bool(self.store.run(lambda c: c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jf_ai_reports'").fetchone()))))
        alerts = int(self.store.run(lambda c: c.execute("SELECT COUNT(*) FROM jf_ai_watch_alerts").fetchone()[0]))
        pending = int(self.store.run(lambda c: c.execute("SELECT COUNT(*) FROM jf_ai_watch_alerts WHERE delivered_at IS NULL").fetchone()[0]))
        lines = ["Justice AI · самопроверка", ""]
        lines.extend(("🟢 " if ok else "🔴 ") + name for name, ok in checks)
        lines += ["", f"Сигналов Watch в архиве: {alerts}", f"Не доставлено владельцу: {pending}"]
        if any(not ok for _, ok in checks):
            lines.append("\nЕсть пункты, требующие настройки или проверки.")
        else:
            lines.append("\nБазовые проверки пройдены. Это не заменяет тестирование всех команд.")
        return "\n".join(lines)

    async def handle_owner_command(self, message):
        command = (message.text or "").split()[0].split("@", 1)[0].casefold()
        if command == "/jf_ai_owner":
            return await message.answer(self.self_audit())
        if command == "/jf_ai_audit":
            return await message.answer(self.self_audit())
        if command == "/jf_ai_watch":
            rows = self.store.run(lambda c: c.execute(
                "SELECT category,message_id,created_at,delivered_at FROM jf_ai_watch_alerts ORDER BY id DESC LIMIT 10"
            ).fetchall())
            if not rows:
                return await message.answer("Justice Watch пока не зафиксировал автоматических сигналов.")
            lines = ["Justice Watch · последние сигналы:"]
            for row in rows:
                status = "доставлен" if row["delivered_at"] else "ожидает доставки"
                lines.append(f"{row['created_at'][:16]} · {row['category']} · msg {row['message_id']} · {status}")
            return await message.answer("\n".join(lines))
        if command == "/jf_ai_report":
            try:
                await message.answer("Готовлю аналитический отчёт за последние 24 часа…")
                await self.create_and_send("manual", 24)
            except Exception:
                logger.exception("Manual Justice AI analytics failed")
                await message.answer("Не удалось сформировать отчёт. Проверь настройки AI и журнал Render.")
            return
        rows = self.store.run(lambda c: c.execute(
            "SELECT id,report_type,period_start,created_at,message_count FROM jf_ai_reports ORDER BY id DESC LIMIT 10"
        ).fetchall())
        if not rows:
            return await message.answer("Архив аналитики пока пуст. Создать отчёт: /jf_ai_report")
        lines = ["Архив аналитических отчётов (последние 10):"]
        for row in rows:
            created = str(row["created_at"] or "")[:16].replace("T", " ")
            lines.append(f"#{row['id']} · {row['report_type']} · {created} UTC · сообщений: {row['message_count']}")
        lines.append("\nОткрыть отчёт: /jf_ai_reports ID · создать сейчас: /jf_ai_report")
        # Support /jf_ai_reports <id> without a second handler.
        parts = (message.text or "").split()
        if len(parts) > 1 and parts[1].isdigit():
            rid = int(parts[1])
            row = self.store.run(lambda c: c.execute("SELECT * FROM jf_ai_reports WHERE id=?", (rid,)).fetchone())
            if not row:
                return await message.answer("Отчёт с таким номером не найден.")
            return await message.answer(f"Отчёт №{rid} · {row['report_type']}\n\n{row['report_text']}"[:3900])
        await message.answer("\n".join(lines))

    async def run_forever(self):
        if not (self.enabled or self.watch_enabled):
            logger.info("Justice AI analytics and Justice Watch disabled by configuration")
            return
        if self._running:
            return
        self._running = True
        while True:
            try:
                now = datetime.now(timezone.utc)
                if self.watch_enabled:
                    try:
                        await self.scan_watch_once()
                    except Exception:
                        logger.exception("Justice Watch scan failed")
                if not self.enabled:
                    await asyncio.sleep(self.watch_poll_seconds)
                    continue
                state = self.store.run(lambda c: dict(c.execute("SELECT key,value FROM jf_ai_analytics_state").fetchall()))
                for key, report_type, hours in (("next_periodic", "periodic", self.periodic_hours), ("next_daily", "daily", 24)):
                    due_raw = state.get(key)
                    due = datetime.fromisoformat(due_raw) if due_raw else now + timedelta(hours=hours)
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=timezone.utc)
                    if now < due:
                        continue
                    # Advance the schedule before making the network request to
                    # prevent duplicate reports after a slow provider response.
                    next_due = now + timedelta(hours=self.periodic_hours) if key == "next_periodic" else self._next_daily_fixed(now, timezone(timedelta(hours=self.utc_offset)))
                    claimed = self.store.run(lambda c, k=key, old=due_raw, v=next_due.isoformat(): c.execute(
                        "UPDATE jf_ai_analytics_state SET value=? WHERE key=? AND value=?", (v, k, old)).rowcount)
                    if not claimed:
                        continue
                    try:
                        await self.create_and_send(report_type, hours)
                    except Exception:
                        logger.exception("Scheduled Justice AI analytics failed | type=%s", report_type)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Justice AI analytics scheduler failed")
            await asyncio.sleep(60)


def register(dp, bot, F, store, owner_id: int, chat_id: int):
    module = JusticeAIAnalytics(bot, store, owner_id, chat_id)
    module.register(dp, F)
    return module

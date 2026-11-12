"""Tracked lifecycle manager for every asyncio task owned by the bot.

Task metadata is durable in SQLite; coroutine objects are process-local.  A
process restart never pretends an arbitrary coroutine can be resumed: the row
is marked interrupted and domain-specific recovery workers restore durable jobs
from their own tables/queues.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import suppress
from datetime import datetime, timezone
from typing import Coroutine, Any
import jf_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


TASK_CATALOG = {
    "expire_game_poll": ("Завершение игрового опроса по таймеру", "domain-recovery"),
    "expire_mafia_lobby": ("Закрытие лобби Мафии по таймеру", "domain-recovery"),
    "_spy_launch_game_task": ("Личная доставка ролей и запуск игры Шпион", "domain-recovery"),
    "spy-recovery": ("Восстановление незавершённых игр Шпион", "startup-worker"),
    "jf-timers": ("Обработка запланированных напоминаний", "startup-worker"),
    "jf-ai-analytics": ("Периодическая аналитика Justice AI для владельца", "startup-worker"),
    "jf-role-sync": ("Синхронизация ролей участников", "startup-worker"),
    "game_poll_refresh_worker": ("Обновление активных игровых опросов", "startup-worker"),
    "game_poll_expiry_recovery_worker": ("Восстановление сроков игровых опросов", "startup-worker"),
    "mafia_expiry_recovery_worker": ("Восстановление сроков лобби Мафии", "startup-worker"),
    "schedule_worker": ("Проверка расписания и запланированных событий", "startup-worker"),
    "justice-feature-maintenance": ("Периодическое обслуживание функций Justice Faite", "startup-worker"),
    "justice-console-maintenance": ("Резервные копии и обслуживание консоли владельца", "startup-worker"),
    "delivery-": ("Очередь уведомлений владельцу и отчётов", "persistent-outbox"),
    "broadcast-": ("Очередь рассылок из постоянного журнала уведомлений", "persistent-outbox"),
    "telegram-polling": ("Основной цикл получения обновлений Telegram", "process-supervised"),
    "fatal-error-watcher": ("Остановка polling при критической ошибке", "process-supervised"),
}


class TaskManager:
    def __init__(self, db_path: str, lock=None):
        self.db_path = str(db_path)
        self.lock = lock or threading.RLock()
        self._tasks: dict[int, asyncio.Task] = {}
        self._rows: dict[int, int] = {}
        self._initialized = False

    def _run(self, fn):
        with self.lock:
            conn = jf_db.connect(self.db_path, timeout=30)
            try:
                value = fn(conn)
                conn.commit()
                return value
            except Exception:
                with suppress(Exception): conn.rollback()
                raise
            finally:
                conn.close()

    def init(self) -> None:
        if self._initialized:
            return
        def op(conn):
            conn.execute("""CREATE TABLE IF NOT EXISTS jf_background_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                stop_policy TEXT NOT NULL DEFAULT 'cancel-and-await',
                restart_policy TEXT NOT NULL DEFAULT 'domain-recovery',
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jf_background_tasks_status ON jf_background_tasks(status, id DESC)")
            conn.execute("UPDATE jf_background_tasks SET status='interrupted',updated_at=?,finished_at=? WHERE status='running'", (_now(), _now()))
            conn.execute("DELETE FROM jf_background_tasks WHERE status!='running' AND id NOT IN (SELECT id FROM jf_background_tasks ORDER BY id DESC LIMIT 1000)")
        self._run(op)
        self._initialized = True

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str | None = None,
              description: str | None = None, stop_policy: str = "cancel-and-await",
              restart_policy: str = "domain-recovery") -> asyncio.Task:
        self.init()
        task_name = name or getattr(coro, "__qualname__", type(coro).__name__)
        catalog = TASK_CATALOG.get(task_name)
        if catalog is None:
            catalog = next((value for prefix, value in TASK_CATALOG.items()
                            if prefix.endswith("-") and task_name.startswith(prefix)), None)
        desc = description or (catalog[0] if catalog else task_name.replace("_", " "))
        if catalog and restart_policy == "domain-recovery":
            restart_policy = catalog[1]
        row_id = self._run(lambda conn: conn.execute(
            "INSERT INTO jf_background_tasks(task_name,description,stop_policy,restart_policy,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (task_name, desc, stop_policy, restart_policy, "running", _now(), _now()),
        ).lastrowid)
        task = asyncio.create_task(coro, name=task_name)
        key = id(task)
        self._tasks[key] = task
        self._rows[key] = int(row_id)

        def completed(done: asyncio.Task) -> None:
            status, error = "finished", None
            if done.cancelled():
                status = "cancelled"
            else:
                try:
                    exc = done.exception()
                except asyncio.CancelledError:
                    exc = None
                    status = "cancelled"
                if exc is not None:
                    status, error = "failed", f"{type(exc).__name__}: {str(exc)[:500]}"
            row = self._rows.pop(key, None)
            self._tasks.pop(key, None)
            if row is not None:
                try:
                    self._run(lambda conn: conn.execute(
                        "UPDATE jf_background_tasks SET status=?,updated_at=?,finished_at=?,error=? WHERE id=?",
                        (status, _now(), _now(), error, row),
                    ))
                except Exception:
                    # Task completion must never crash the event loop because
                    # telemetry storage is unavailable.
                    pass
        task.add_done_callback(completed)
        return task

    async def cancel_all(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def snapshot(self, limit: int = 100) -> list[dict]:
        self.init()
        rows = self._run(lambda conn: conn.execute(
            "SELECT id,task_name,description,stop_policy,restart_policy,status,created_at,updated_at,finished_at,error "
            "FROM jf_background_tasks ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),)
        ).fetchall())
        return [dict(row) for row in rows]

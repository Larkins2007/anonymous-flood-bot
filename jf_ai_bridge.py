"""
Justice AI Job Bridge — durable long-running jobs + observability.

Изменения относительно 0.26.1:
- Job TTL: 1 час (было ~240 с)
- Lease: 15 минут
- Нет жёсткого отсева заданий через 240 с
- Понятная очистка просроченных заданий
- Сохранена HMAC-защита и совместимость с analytics
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, Optional

from aiohttp import web

logger = logging.getLogger("jf_ai_bridge")

JOB_TTL_SECONDS = int(os.getenv("JF_AI_JOB_TTL", "3600"))
LEASE_SECONDS = int(os.getenv("JF_AI_LEASE_SECONDS", "900"))
MAX_BODY = 128 * 1024


class AIJobBridge:
    def __init__(self) -> None:
        self.secret = os.environ.get("JF_AI_BRIDGE_SECRET", "").encode("utf-8")
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.nonces: Dict[str, float] = {}
        self.max_body = MAX_BODY

    @property
    def ready(self) -> bool:
        return len(self.secret) >= 32

    def register(self, app: web.Application) -> None:
        app.router.add_get("/internal/ai/poll", self.poll)
        app.router.add_post("/internal/ai/result", self.result)

    def authorized(self, request: web.Request, body: bytes) -> bool:
        if not self.ready:
            return False

        timestamp = request.headers.get("X-JF-Timestamp", "")
        nonce = request.headers.get("X-JF-Nonce", "")
        signature = request.headers.get("X-JF-Signature", "")

        try:
            stamp = int(timestamp)
        except (TypeError, ValueError):
            return False

        now = int(time.time())
        if abs(now - stamp) > 60:
            return False

        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", nonce or ""):
            return False
        if not re.fullmatch(r"[0-9a-f]{64}", signature or ""):
            return False

        for key, expiry in list(self.nonces.items()):
            if expiry < now:
                self.nonces.pop(key, None)

        if nonce in self.nonces:
            return False

        digest = hashlib.sha256(body).hexdigest()
        message = "\n".join((
            timestamp,
            nonce,
            request.method,
            request.path,
            digest,
        )).encode()

        expected = hmac.new(self.secret, message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return False

        self.nonces[nonce] = now + 120
        return True

    def _cleanup_expired(self, now: float) -> None:
        expired = []
        for job_id, job in list(self.jobs.items()):
            age = now - job["created"]
            if age > JOB_TTL_SECONDS:
                expired.append(job_id)
                continue
            if job["future"].done() and age > 180:
                expired.append(job_id)

        for job_id in expired:
            job = self.jobs.pop(job_id, None)
            if job and not job["future"].done():
                try:
                    job["future"].set_exception(
                        asyncio.TimeoutError("Задание истекло по TTL")
                    )
                except Exception:
                    pass
                logger.info("job_expired id=%s age=%.0fs", job_id[:8], now - job["created"])

    async def generate(
        self,
        messages: list,
        max_tokens: int,
        timeout: float,
        response_format: Optional[dict] = None,
    ) -> str:
        if not self.ready:
            raise RuntimeError("Локальный AI-мост не настроен.")

        loop = asyncio.get_running_loop()
        job_id = uuid.uuid4().hex
        future: asyncio.Future = loop.create_future()

        job = {
            "id": job_id,
            "messages": messages,
            "max_tokens": int(max_tokens),
            "response_format": response_format,
            "created": time.time(),
            "future": future,
            "queued": True,
            "lease_until": 0.0,
        }

        self.jobs[job_id] = job
        logger.info(
            "job_created id=%s max_tokens=%s timeout=%.0fs messages=%d",
            job_id[:8], max_tokens, timeout, len(messages),
        )

        try:
            self.queue.put_nowait(job_id)
        except asyncio.QueueFull:
            self.jobs.pop(job_id, None)
            raise RuntimeError("Очередь локального AI переполнена.")

        try:
            result = await asyncio.wait_for(future, timeout=float(timeout))
            logger.info("job_done id=%s", job_id[:8])
            return result
        except asyncio.TimeoutError:
            logger.warning("job_caller_timeout id=%s timeout=%.0fs", job_id[:8], timeout)
            raise
        finally:
            job = self.jobs.get(job_id)
            if job is not None:
                job["caller_done"] = True

    async def poll(self, request: web.Request) -> web.Response:
        body = await request.read()

        if len(body) > self.max_body or not self.authorized(request, body):
            raise web.HTTPUnauthorized()

        now = time.time()
        self._cleanup_expired(now)

        for job_id, job in list(self.jobs.items()):
            if job["future"].done():
                continue
            if now - job["created"] > JOB_TTL_SECONDS:
                continue
            if not job["queued"] and job["lease_until"] <= now:
                try:
                    self.queue.put_nowait(job_id)
                    job["queued"] = True
                    logger.info("job_requeued id=%s (lease expired)", job_id[:8])
                except asyncio.QueueFull:
                    break

        try:
            job_id = await asyncio.wait_for(self.queue.get(), timeout=20.0)
        except asyncio.TimeoutError:
            return web.Response(status=204)

        job = self.jobs.get(job_id)

        if not job or job["future"].done():
            return web.Response(status=204)

        if time.time() - job["created"] > JOB_TTL_SECONDS:
            return web.Response(status=204)

        job["queued"] = False
        job["lease_until"] = time.time() + LEASE_SECONDS
        logger.info("job_leased id=%s lease=%ss", job_id[:8], LEASE_SECONDS)

        return web.json_response({
            "id": job["id"],
            "messages": job["messages"],
            "max_tokens": job["max_tokens"],
            "response_format": job.get("response_format"),
        })

    async def result(self, request: web.Request) -> web.Response:
        body = await request.read()

        if len(body) > self.max_body or not self.authorized(request, body):
            raise web.HTTPUnauthorized()

        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            raise web.HTTPBadRequest()
        if not isinstance(data, dict):
            raise web.HTTPBadRequest()

        job_id = data.get("id")
        answer = data.get("answer")
        error = data.get("error")

        if not isinstance(job_id, str) or len(job_id) != 32:
            raise web.HTTPBadRequest(text="Некорректный id задания.")

        if (isinstance(answer, str)) == (isinstance(error, dict)):
            raise web.HTTPBadRequest(text="Нужен ровно один результат: answer или error.")

        job = self.jobs.get(job_id)
        if not job or job["future"].done():
            logger.info("job_result_late id=%s (already closed)", job_id[:8])
            return web.json_response({"accepted": False}, status=409)

        if isinstance(answer, str):
            if not answer.strip():
                raise web.HTTPBadRequest(text="Пустой ответ модели.")
            if len(answer) > 16000:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=16000, actual_size=len(answer)
                )
            job["future"].set_result(answer)
            logger.info("job_result_ok id=%s len=%d", job_id[:8], len(answer))
        else:
            error_type = str(error.get("type", "GenerationError"))[:80]
            error_message = str(error.get("message", "Ошибка локальной генерации."))[:500]
            job["future"].set_exception(
                RuntimeError(f"Локальная генерация: {error_type}: {error_message}")
            )
            logger.warning("job_result_error id=%s type=%s", job_id[:8], error_type)

        return web.json_response({"accepted": True})

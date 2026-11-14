import asyncio
import hashlib
import hmac
import json
import os
import re
import time
import uuid

from aiohttp import web


class AIJobBridge:
    def __init__(self):
        self.secret = os.environ.get("JF_AI_BRIDGE_SECRET", "").encode("utf-8")
        self.queue = asyncio.Queue(maxsize=32)
        self.jobs = {}
        self.nonces = {}
        self.max_body = 128 * 1024

    @property
    def ready(self):
        return len(self.secret) >= 32

    def register(self, app):
        app.router.add_get(
            "/internal/ai/poll", self.poll
        )
        app.router.add_post(
            "/internal/ai/result", self.result
        )

    def authorized(self, request, body):
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

        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", nonce):
            return False
        if not re.fullmatch(r"[0-9a-f]{64}", signature):
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

        expected = hmac.new(
            self.secret, message, hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected, signature):
            return False

        self.nonces[nonce] = now + 120
        return True

    async def generate(self, messages, max_tokens, timeout, response_format=None):
        if not self.ready:
            raise RuntimeError("Локальный AI-мост не настроен.")

        loop = asyncio.get_running_loop()
        job_id = uuid.uuid4().hex
        future = loop.create_future()

        job = {
            "id": job_id,
            "messages": messages,
            "max_tokens": int(max_tokens),
            "response_format": response_format,
            "created": time.time(),
            "future": future,
            "queued": True,
            "lease_until": 0,
        }

        self.jobs[job_id] = job

        try:
            self.queue.put_nowait(job_id)
        except asyncio.QueueFull:
            self.jobs.pop(job_id, None)
            raise RuntimeError("Очередь локального AI переполнена.")

        try:
            return await asyncio.wait_for(
                future, timeout=float(timeout)
            )
        finally:
            self.jobs.pop(job_id, None)

    async def poll(self, request):
        body = await request.read()

        if len(body) > self.max_body or not self.authorized(request, body):
            raise web.HTTPUnauthorized()

        now = time.time()
        for job_id, job in list(self.jobs.items()):
            if job["future"].done():
                continue
            if now - job["created"] > 240:
                continue
            if not job["queued"] and job["lease_until"] <= now:
                try:
                    self.queue.put_nowait(job_id)
                    job["queued"] = True
                except asyncio.QueueFull:
                    break

        try:
            job_id = await asyncio.wait_for(
                self.queue.get(), timeout=20
            )
        except asyncio.TimeoutError:
            return web.Response(status=204)

        job = self.jobs.get(job_id)

        if not job or job["future"].done():
            return web.Response(status=204)

        if time.time() - job["created"] > 240:
            return web.Response(status=204)

        job["queued"] = False
        job["lease_until"] = time.time() + 210

        return web.json_response({
            "id": job["id"],
            "messages": job["messages"],
            "max_tokens": job["max_tokens"],
            "response_format": job.get("response_format"),
        })

    async def result(self, request):
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
            return web.json_response({"accepted": False}, status=409)

        if isinstance(answer, str):
            if not answer.strip():
                raise web.HTTPBadRequest(text="Пустой ответ модели.")
            if len(answer) > 16000:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=16000, actual_size=len(answer)
                )
            job["future"].set_result(answer)
        else:
            error_type = str(error.get("type", "GenerationError"))[:80]
            error_message = str(error.get("message", "Ошибка локальной генерации."))[:500]
            job["future"].set_exception(
                RuntimeError(f"Локальная генерация: {error_type}: {error_message}")
            )

        return web.json_response({"accepted": True})

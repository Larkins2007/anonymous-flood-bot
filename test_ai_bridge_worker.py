import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ["JF_AI_BRIDGE_SECRET"] = "x" * 40

import jf_ai_bridge
import jf_ai_worker


class FakeRequest:
    method = "POST"
    path = "/internal/ai/result"

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    async def read(self):
        return self.payload


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = jf_ai_bridge.AIJobBridge()
        self.bridge.authorized = lambda request, body: True

    def test_success_result_resolves_future(self):
        async def run():
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            job_id = "a" * 32
            self.bridge.jobs[job_id] = {"future": future}
            response = await self.bridge.result(FakeRequest({"id": job_id, "answer": "готово"}))
            self.assertEqual(response.status, 200)
            self.assertEqual(future.result(), "готово")
        asyncio.run(run())

    def test_error_result_rejects_future_with_reason(self):
        async def run():
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            job_id = "b" * 32
            self.bridge.jobs[job_id] = {"future": future}
            response = await self.bridge.result(FakeRequest({
                "id": job_id,
                "error": {"type": "TimeoutError", "message": "model timed out"},
            }))
            self.assertEqual(response.status, 200)
            with self.assertRaisesRegex(RuntimeError, "TimeoutError"):
                future.result()
        asyncio.run(run())

    def test_closed_job_returns_409(self):
        async def run():
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            future.set_result("done")
            job_id = "c" * 32
            self.bridge.jobs[job_id] = {"future": future}
            response = await self.bridge.result(FakeRequest({"id": job_id, "answer": "late"}))
            self.assertEqual(response.status, 409)
        asyncio.run(run())


class WorkerTests(unittest.TestCase):
    def test_generation_payload_uses_configurable_sampling(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self):
                return b'{"choices":[{"message":{"content":"ok"}}]}'

        captured = {}
        def fake_urlopen(req, timeout):
            captured.update(json.loads(req.data.decode()))
            return Response()

        with patch.object(jf_ai_worker.urllib.request, "urlopen", fake_urlopen):
            answer = jf_ai_worker.generate([{"role": "user", "content": "test"}], 100)
        self.assertEqual(answer, "ok")
        self.assertEqual(captured["temperature"], 0.2)
        self.assertEqual(captured["max_tokens"], 100)
        self.assertEqual(captured["top_k"], 40)

    def test_http_409_is_returned_as_normal_result(self):
        import io
        http_error = jf_ai_worker.urllib.error.HTTPError(
            "https://example.invalid", 409, "Conflict", {}, io.BytesIO(b'{"accepted":false}')
        )
        with patch.object(jf_ai_worker.urllib.request, "urlopen", side_effect=http_error):
            result = jf_ai_worker.request("https://example.invalid", "POST", b"x"*32, "/x", b"{}")
        self.assertEqual(result, {"accepted": False})


if __name__ == "__main__":
    unittest.main()

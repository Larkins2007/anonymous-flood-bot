#!/usr/bin/env python3
"""Self-contained unit and HTTP protocol tests for AIJobBridge."""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import secrets
from unittest.mock import MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_SECRET = b"test-secret-that-is-at-least-32-bytes-long!!"
os.environ["JF_AI_BRIDGE_SECRET"] = TEST_SECRET.decode("ascii")

from jf_ai_bridge import AIJobBridge
from jf_ai_worker import signed_headers as worker_signed_headers


def signed_headers(secret, method, path, body=b"", *, timestamp=None, nonce=None):
    ts = str(int(time.time()) if timestamp is None else int(timestamp))
    request_nonce = nonce or secrets.token_hex(16)
    digest = hashlib.sha256(body).hexdigest()
    message = "\n".join((ts, request_nonce, method, path, digest)).encode()
    signature = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return {
        "X-JF-Timestamp": ts,
        "X-JF-Nonce": request_nonce,
        "X-JF-Signature": signature,
        "Content-Type": "application/json",
    }


def make_signed_request(secret: bytes, method: str, path: str, body: bytes = b""):
    headers = signed_headers(secret, method, path, body)
    req = MagicMock()
    req.headers = headers
    req.method = method
    req.path = path
    return req, body


async def test_hmac_nonce_timestamp_and_signature_shape():
    bridge = AIJobBridge()
    assert bridge.ready
    body = b'{"id":"abc","answer":"ok"}'
    req, body = make_signed_request(bridge.secret, "POST", "/internal/ai/result", body)
    assert bridge.authorized(req, body)
    assert not bridge.authorized(req, body), "nonce replay must fail"

    wrong, body2 = make_signed_request(bridge.secret, "POST", "/internal/ai/result", body)
    wrong.headers["X-JF-Signature"] = "0" * 64
    assert not bridge.authorized(wrong, body2)

    stale, body3 = make_signed_request(bridge.secret, "POST", "/internal/ai/result", body)
    stale.headers["X-JF-Timestamp"] = str(int(time.time()) - 120)
    assert not bridge.authorized(stale, body3)

    malformed, body4 = make_signed_request(bridge.secret, "POST", "/internal/ai/result", body)
    malformed.headers["X-JF-Signature"] = "not-a-signature"
    assert not bridge.authorized(malformed, body4)
    print("  [PASS] HMAC, signature shape, nonce replay, timestamp window")


async def test_worker_signature_matches_bridge():
    bridge = AIJobBridge()
    body = b'{"id":"job-1","answer":"ok"}'
    headers = worker_signed_headers(bridge.secret, "POST", "/internal/ai/result", body)
    req = MagicMock()
    req.headers = headers
    req.method = "POST"
    req.path = "/internal/ai/result"
    assert bridge.authorized(req, body), "worker and bridge must sign the same canonical request"
    print("  [PASS] worker HMAC matches bridge verification")


async def test_generate_timeout_queue_full_and_not_ready():
    bridge = AIJobBridge()
    try:
        await bridge.generate([{"role": "user", "content": "hi"}], 32, 0.05)
        raise AssertionError("generate() must time out without a worker")
    except asyncio.TimeoutError:
        pass

    bridge.queue = asyncio.Queue(maxsize=1)
    await bridge.queue.put("dummy")
    try:
        await bridge.generate([{"role": "user", "content": "hi"}], 32, 1)
        raise AssertionError("queue full must be rejected")
    except RuntimeError as exc:
        assert "переполнена" in str(exc).lower()

    old = os.environ.get("JF_AI_BRIDGE_SECRET")
    try:
        os.environ["JF_AI_BRIDGE_SECRET"] = "short"
        not_ready = AIJobBridge()
        assert not not_ready.ready
        try:
            await not_ready.generate([], 10, 1)
            raise AssertionError("not-ready bridge must reject generation")
        except RuntimeError:
            pass
    finally:
        if old is None:
            os.environ.pop("JF_AI_BRIDGE_SECRET", None)
        else:
            os.environ["JF_AI_BRIDGE_SECRET"] = old
    print("  [PASS] timeout, queue full, not-ready guard")


async def test_http_poll_result_and_rejections():
    bridge = AIJobBridge()
    app = web.Application(client_max_size=bridge.max_body)
    bridge.register(app)

    async with TestServer(app) as server:
        async with TestClient(server) as client:
            messages = [{"role": "user", "content": "protocol test"}]
            pending = asyncio.create_task(bridge.generate(messages, 48, 2.0))
            await asyncio.sleep(0)

            poll_path = "/internal/ai/poll"
            poll_headers = signed_headers(bridge.secret, "GET", poll_path)
            poll_response = await client.get(poll_path, headers=poll_headers)
            assert poll_response.status == 200
            job = await poll_response.json()
            assert job["messages"] == messages
            assert job["max_tokens"] == 48

            replay = await client.get(poll_path, headers=poll_headers)
            assert replay.status == 401, "replayed poll nonce must be rejected"

            result_path = "/internal/ai/result"
            result_body = json.dumps({"id": job["id"], "answer": "bridge-ok"}).encode()
            bad_headers = signed_headers(bridge.secret, "POST", result_path, result_body)
            bad_headers["X-JF-Signature"] = "0" * 64
            bad = await client.post(result_path, data=result_body, headers=bad_headers)
            assert bad.status == 401

            valid_headers = signed_headers(bridge.secret, "POST", result_path, result_body)
            result = await client.post(result_path, data=result_body, headers=valid_headers)
            assert result.status == 200
            assert (await result.json())["accepted"] is True
            assert await pending == "bridge-ok"

            malformed_body = b"[]"
            malformed_headers = signed_headers(bridge.secret, "POST", result_path, malformed_body)
            malformed = await client.post(result_path, data=malformed_body, headers=malformed_headers)
            assert malformed.status == 400

            oversized_body = b"x" * (bridge.max_body + 1)
            oversized_headers = signed_headers(bridge.secret, "POST", result_path, oversized_body)
            oversized = await client.post(result_path, data=oversized_body, headers=oversized_headers)
            assert oversized.status == 413

    print("  [PASS] HTTP poll → result, invalid HMAC, replay, malformed JSON, body limit")


async def main():
    print("Running AIJobBridge release tests...")
    await test_hmac_nonce_timestamp_and_signature_shape()
    await test_worker_signature_matches_bridge()
    await test_generate_timeout_queue_full_and_not_ready()
    await test_http_poll_result_and_rejections()
    print("ALL BRIDGE RELEASE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.request
import urllib.error
from pathlib import Path

BRIDGE_URL = os.getenv("JF_AI_BRIDGE_URL", "").rstrip("/")
LOCAL_URL = os.getenv(
    "JF_LOCAL_MODEL_URL",
    "http://127.0.0.1:8080"
).rstrip("/")

SECRET_FILE = Path.home() / ".config/justice-ai/bridge.secret"
LOCAL_MODEL = os.getenv("JF_LOCAL_MODEL", "local-model")

POLL_PATH = "/internal/ai/poll"
RESULT_PATH = "/internal/ai/result"


def load_secret():
    if not SECRET_FILE.is_file():
        raise RuntimeError("Файл секрета не найден.")

    secret = SECRET_FILE.read_bytes().strip()

    if len(secret) < 32:
        raise RuntimeError("Секрет слишком короткий.")

    return secret


def signed_headers(secret, method, path, body=b""):
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    digest = hashlib.sha256(body).hexdigest()

    message = "\n".join((
        timestamp,
        nonce,
        method,
        path,
        digest,
    )).encode()

    signature = hmac.new(
        secret, message, hashlib.sha256
    ).hexdigest()

    return {
        "X-JF-Timestamp": timestamp,
        "X-JF-Nonce": nonce,
        "X-JF-Signature": signature,
        "Content-Type": "application/json",
    }


def request(url, method, secret, path, body=b"", timeout=25):
    headers = signed_headers(secret, method, path, body)

    req = urllib.request.Request(
        url,
        data=body if method != "GET" else None,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 204:
                return None
            raw = response.read(256 * 1024)
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # A completed/expired job is an expected race, not a connection failure.
        raw = exc.read(64 * 1024)
        if exc.code == 409:
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {"accepted": False}
        raise


def _float_env(name, default, minimum, maximum):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _int_env(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def generate(messages, max_tokens, response_format=None):
    if not isinstance(messages, list) or not messages:
        raise ValueError("Некорректный список сообщений для модели.")

    token_limit = max(1, min(int(max_tokens), 650))
    payload_data = {
        "model": LOCAL_MODEL,
        "messages": messages,
        "max_tokens": token_limit,
        "temperature": _float_env("JF_LOCAL_TEMPERATURE", 0.2, 0.0, 2.0),
        "top_p": _float_env("JF_LOCAL_TOP_P", 0.9, 0.05, 1.0),
        "top_k": _int_env("JF_LOCAL_TOP_K", 40, 1, 200),
        "repeat_penalty": _float_env("JF_LOCAL_REPEAT_PENALTY", 1.1, 0.5, 2.0),
        "stream": False,
    }
    if isinstance(response_format, dict):
        payload_data["response_format"] = response_format
    payload = json.dumps(payload_data, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        LOCAL_URL + "/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=180) as response:
        data = json.loads(response.read().decode("utf-8"))

    answer = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )

    if not answer:
        raise RuntimeError("Пустой ответ модели.")

    return answer[:3800]


def send_result(secret, job_id, answer=None, error=None):
    result = {"id": job_id}
    if error is not None:
        result["error"] = {
            "type": str(error.get("type", "GenerationError"))[:80],
            "message": str(error.get("message", "Ошибка локальной генерации."))[:500],
        }
    else:
        result["answer"] = str(answer or "")
    body = json.dumps(result, ensure_ascii=False).encode("utf-8")

    return request(
        BRIDGE_URL + RESULT_PATH,
        "POST",
        secret,
        RESULT_PATH,
        body,
        timeout=25,
    )


def main():
    if not BRIDGE_URL.startswith("https://"):
        raise RuntimeError("Для моста требуется HTTPS URL Render.")

    secret = load_secret()

    print("Justice AI Worker запущен.")
    print("Локальная модель:", LOCAL_URL)
    print("Ожидание заданий...")
    bridge_confirmed = False

    while True:
        try:
            job = request(
                BRIDGE_URL + POLL_PATH,
                "GET",
                secret,
                POLL_PATH,
                timeout=25,
            )

            if not job:
                if not bridge_confirmed:
                    print("Подключение к bridge подтверждено (HTTP 204); ожидание заданий.")
                    bridge_confirmed = True
                continue

            job_id = job.get("id")
            messages = job.get("messages")
            max_tokens = job.get("max_tokens", 260)
            response_format = job.get("response_format")

            if not isinstance(job_id, str) or not isinstance(messages, list):
                continue

            print("Получено задание:", job_id[:8])

            result = None
            try:
                answer = generate(messages, max_tokens, response_format)
                result = send_result(secret, job_id, answer=answer)
            except Exception as exc:
                # Do not turn a model failure into a successful user-facing answer.
                print("Ошибка локальной генерации:", type(exc).__name__, str(exc)[:300])
                try:
                    result = send_result(
                        secret,
                        job_id,
                        error={"type": type(exc).__name__, "message": str(exc)[:500]},
                    )
                except Exception as report_exc:
                    print("Не удалось передать ошибку Render:", type(report_exc).__name__)

            if result and result.get("accepted"):
                print("Результат передан Render:", job_id[:8])
            else:
                print("Задание уже закрыто или истекло:", job_id[:8])

        except KeyboardInterrupt:
            print("\nWorker остановлен.")
            break
        except Exception as exc:
            print("Ошибка соединения:", type(exc).__name__)
            time.sleep(3)


if __name__ == "__main__":
    main()

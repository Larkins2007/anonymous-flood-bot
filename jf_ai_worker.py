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

    with urllib.request.urlopen(req, timeout=timeout) as response:
        if response.status == 204:
            return None
        raw = response.read(256 * 1024)
        return json.loads(raw.decode("utf-8"))


def generate(messages, max_tokens, response_format=None):
    payload = json.dumps({
        "model": LOCAL_MODEL,
        "messages": messages,
        "max_tokens": min(int(max_tokens), 650),
        "temperature": 0.8,
        "top_p": 0.9,
        "stream": False,
        **({"response_format": response_format} if isinstance(response_format, dict) else {}),
    }).encode("utf-8")

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


def send_result(secret, job_id, answer):
    body = json.dumps({
        "id": job_id,
        "answer": answer,
    }, ensure_ascii=False).encode("utf-8")

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

            try:
                answer = generate(messages, max_tokens, response_format)
            except Exception:
                answer = "Локальная модель временно недоступна. Попробуйте позднее."

            result = send_result(secret, job_id, answer)

            if result and result.get("accepted"):
                print("Ответ передан Render:", job_id[:8])
            else:
                print("Задание уже закрыто или истекло.")

        except KeyboardInterrupt:
            print("\nWorker остановлен.")
            break
        except Exception as exc:
            print("Ошибка соединения:", type(exc).__name__)
            time.sleep(3)


if __name__ == "__main__":
    main()

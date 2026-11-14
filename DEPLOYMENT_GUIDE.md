# Deployment Guide — Justice Faite 0.26.2 (Local Qwen)

## Prerequisites

- Render account (Free may run a test process, but it is not durable storage for production SQLite data)
- Xiaomi Redmi Pad SE (or equivalent) with Termux, llama-server 0.7.0 and the Qwen 2.5 3B Q4_K_M GGUF already working locally
- A strong shared secret (≥ 32 random bytes)

## 1. Prepare the secret

Generate once:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

You will need the same value in two places:

- Render environment variable `JF_AI_BRIDGE_SECRET`
- Tablet file `~/.config/justice-ai/bridge.secret` (mode 600)

## 2. Render setup

1. Create a new **Web Service** (or update the existing one) from the GitHub repository that contains this tree.
2. Runtime: Python.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Health check path: `/health` (already set in `render.yaml`)
6. Environment variables (minimum):

| Key | Value | Notes |
|-----|-------|-------|
| `BOT_TOKEN` | … | Telegram bot token |
| `ADMIN_ID` | … | Owner numeric Telegram ID |
| `PRIMARY_CHAT_ID` | … | Main group ID (negative) |
| `JF_AI_BRIDGE_SECRET` | (the 64-hex string) | **Required for local AI** |
| `JF_AI_ENABLED` | `false` | Leave false until worker is verified |
| `JF_ALLOW_EMPTY_DB_BOOTSTRAP` | `false` | Only true for first empty install |
| `JF_STORAGE_DURABLE` | `false` | Set true only if using a persistent disk |

7. Deploy. Confirm the service becomes healthy (`/health` returns 200).

**Database bootstrap:** for an existing installation, leave `JF_ALLOW_EMPTY_DB_BOOTSTRAP=false` so a missing database cannot silently become a blank one. For a genuinely new installation with no owner backup, set it to `true` only for the first successful boot, verify the bot and owner backup, then immediately return it to `false`.

**Storage:** Render Free uses an ephemeral filesystem. The owner-DM backup/restore mechanism is a recovery path, not the same as durable storage; updates since the most recent successful backup may be lost. For production continuity, attach persistent storage and set `JF_STORAGE_DURABLE=true` with `DB_PATH` pointing inside the mounted disk.

## 3. Verify the worker before enabling AI

1. Start `llama-server` and confirm `/health` responds on the tablet.
2. Start `jf_ai_worker.py` and wait for this line:

   `Подключение к bridge подтверждено (HTTP 204); ожидание заданий.`

   This confirms that Render accepted a signed poll request. It does not yet prove model generation.
3. Only then set `JF_AI_ENABLED=true` in Render and restart the service.
4. Send one test request to Justice AI. Confirm the worker logs `Получено задание` and `Ответ передан Render`, and the bot returns a model response.

If the test fails, set `JF_AI_ENABLED=false` again. Do not remove the old Groq environment key until the local path has been tested successfully and you have separately decided to retire that key.

## 4. Verify end-to-end

1. Owner sends a private message that triggers the AI path.
2. Worker log should show “Получено задание” followed by “Ответ передан Render”.
3. Bot replies with the local model output.
4. If the model is offline, the user receives a graceful “Локальная модель временно недоступна…” message (no external fallback).

## 5. SQLite persistence note

On Render free tier the filesystem is ephemeral. The project already has an owner-DM backup/restore path (`JF_CONSOLE`). Keep that healthy. Do not rely on the in-memory AI job queue surviving a sleep or restart.

## 6. Rollback

If the local path misbehaves, set `JF_AI_ENABLED=false`. The bot continues to operate; generative AI simply becomes unavailable until the worker is fixed.

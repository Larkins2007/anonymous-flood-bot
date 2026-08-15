# Staging smoke test

## 1. Prepare

```bash
cp .env.example .env
mkdir -p data log
```

Put only the staging bot token into `.env`.

Do not commit `.env`.

## 2. Build and start

```bash
docker compose up --build -d bot
```

Wait for health:

```bash
./healthcheck.sh
```

Or inspect it directly:

```bash
curl -s http://127.0.0.1:10000/health | python -m json.tool
```

Expected fields include:

- `polling_running`
- `last_polling_activity`
- `delivery_queue_size`
- `broadcast_queue_size`
- `db_ok`
- `fatal_error`

Healthy state:

```json
{
  "polling_running": true,
  "db_ok": true,
  "fatal_error": null
}
```

## 3. Run tests

```bash
docker compose run --rm tests
```

Expected:

```text
3 passed
```

## 4. Manual Telegram smoke test

### Feedback

1. Open the staging bot.
2. Send `/start`.
3. Press `Оставить сообщение`.
4. Send a short text.
5. Confirm it reaches the admin chat.
6. Inspect:

```bash
sqlite3 ./data/staging.db   "SELECT id,kind,ref_id,status,admin_message_ids FROM admin_notifications ORDER BY id DESC LIMIT 10;"
```

### Report

1. Open `/start`.
2. Press `Пожаловаться`.
3. Enter a test username or numeric ID.
4. Enter a reason.
5. Confirm.
6. Inspect:

```bash
sqlite3 ./data/staging.db   "SELECT id,target_text,delivery_status,admin_message_ids FROM reports ORDER BY id DESC LIMIT 10;"
```

### Admin reply

1. In the admin chat, press `Ответить`.
2. Send a response.
3. Verify the user receives it.

### Broadcast

1. Register several staging test users.
2. Open the admin panel.
3. Start a small broadcast.
4. Verify users receive it.
5. Check the final notification status and sent-user tracking in SQLite.

## 5. Recovery test

Start a broadcast with at least 3 test users.

Then force-stop the bot:

```bash
docker compose kill -s SIGKILL bot
```

Start it again:

```bash
docker compose up -d bot
```

Then inspect:

```bash
sqlite3 ./data/staging.db   "SELECT id,kind,status,broadcast_sent_user_ids FROM admin_notifications WHERE kind='broadcast' ORDER BY id DESC LIMIT 10;"
```

The broadcast must continue from the saved progress rather than restarting from zero.

## 6. Logs

```bash
docker logs -f afb_bot_staging
```

Look for:

- `TelegramRetryAfter`
- `TelegramNetworkError`
- `TelegramUnauthorizedError`
- `TelegramConflictError`
- delivery worker start/stop
- broadcast progress
- polling restart/backoff

## 7. Stop staging

```bash
docker compose down
```

Data remains under `./data` and logs under `./log`.

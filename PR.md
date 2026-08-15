# Staging: Docker + smoke-test environment

## Scope

This staging layer adds reproducible local test/deployment files around the already-reviewed `bot.py`.

It does not change bot logic.

## Files

- `Dockerfile`
- `docker-compose.yml`
- `.env.example`
- `requirements-dev.txt`
- `healthcheck.sh`
- `SMOKE_TEST.md`

## Runtime

Production dependencies remain in `requirements.txt`:

```text
aiogram>=3.7,<4.0
aiohttp>=3.9,<4.0
```

Development/test dependency is isolated in `requirements-dev.txt`.

## Security

- Real `BOT_TOKEN` is never committed.
- `.env.example` contains only a placeholder.
- `ADMIN_ID` remains in `bot.py` as agreed.

## Local validation

```bash
python -m py_compile bot.py
pytest -q
docker compose up --build -d bot
./healthcheck.sh
docker compose run --rm tests
```

## Render

Do not use `docker-compose` on Render for this staging layer.

Keep the existing Render commands:

- Build: `pip install -r requirements.txt`
- Start: `python bot.py`

Configure `BOT_TOKEN` only in Render Environment Variables.

## Acceptance

Before production deployment, complete the Telegram smoke tests in `SMOKE_TEST.md`, including:

- feedback
- report
- admin reply
- broadcast
- forced restart + recovery
- `/health`

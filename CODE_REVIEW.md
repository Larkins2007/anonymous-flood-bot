# Code review — Justice Faite Bot

Reviewed on 2026-10-21. This is a static/offline review of the supplied ZIP, not a live Telegram deployment test.

## What passed

- `python3 -m compileall -q .` — Python source compiles.
- `python3 tests/run_all.py` — all five test suites passed: `test_cmdcfg.py`, `test_awards.py`, `test_flows.py`, `test_roles.py`, and `test_schedule.py`.
- The tests exercise SQLite-backed flows through the project's aiogram stubs, including awards, settings, roles, moderation and scheduling.

These checks are useful, but do not prove that every handler works against the real installed aiogram version or Telegram API. The test environment uses local stubs; no live bot was started during this review.

## Important findings

### 1. Critical for deployment: SQLite data is not durable on Render Free

`render.yaml` selects `plan: free`, while `bot.py` defaults `DB_PATH` to the relative file `users.db`. The database contains persistent community state (roles, warnings, rests, birthdays, command settings, game state and award queue). Render documents that Free web services spin down after 15 minutes without inbound traffic and lose local filesystem changes on spin-down, restart or redeploy; Free web services cannot attach persistent disks.

Before relying on the bot for a real community, choose a persistence plan: use a paid Render service with a persistent disk and set `DB_PATH` to a file under the disk's mount path (for example `/var/data/users.db`), or deliberately migrate the data layer to a managed database. Back up/export the existing SQLite database before migration. An external health ping may affect idle behavior, but does not make the filesystem durable.

References:
- https://render.com/docs/free
- https://render.com/docs/disks

### 2. Personal Telegram IDs are embedded as fallback configuration

`bot.py` defines fallback values for `ADMIN_ID` and `PRIMARY_CHAT_ID`. These are not bot-token secrets, but they are personal identifiers and make a copy/fork behave as though it belongs to the original owner/chat unless environment variables override them. For a distributable version, require `ADMIN_ID` from the environment and make `PRIMARY_CHAT_ID` explicit (or use `0` only when the multi-group fallback mode is intended). Never publish `BOT_TOKEN`.

### 3. Runtime compatibility still needs a real dependency test

The offline tests replace aiogram with stubs. They verify application logic but cannot catch incompatibilities in actual aiogram models, filters, callback behavior, permissions, polling or Telegram API responses. Before production, create a clean environment from `requirements.txt` and run a private smoke test with `/start`, `/settings`, award forwarding, a callback button, a rest/warning flow, and game creation/expiration. Keep the test bot and test group separate from the live group.

### 4. SQLite operations are synchronous inside async handlers

The shared `db_transaction()` and much of the feature code use synchronous `sqlite3` calls. The 30-second busy timeout can block the asyncio event loop if the database is locked or storage is slow. For a small group this may be acceptable, but if commands begin lagging, move database work behind a consistent async boundary (for example `asyncio.to_thread` or `aiosqlite`) rather than converting only one or two call sites. Preserve transaction boundaries and rerun the flow tests after that refactor.

### 5. FSM states are memory-only

`bot.py` uses aiogram `MemoryStorage`. A process restart clears conversations currently waiting for text/input. Persistent game and queue data may survive in SQLite, but the user's in-progress conversation state will not. This is a limitation to be aware of during restarts/deploys; a persistent FSM backend is a later improvement if interrupted flows become a problem.

### 6. Maintainability

`bot.py` is very large and owns multiple unrelated concerns (database schema, moderation, role assignment, polls, Mafia, Spyfall, HTTP health, queues and startup). Avoid a broad rewrite in one step. A safer sequence is to add regression tests, extract one cohesive subsystem at a time, and keep its existing public functions stable while migrating.

## Changes included in this review package

- Added `.gitignore` entries for local databases, logs, Python caches and `.env` files.
- Added `.env.example` with placeholder values (no live credentials or IDs).
- Updated the Render Blueprint to request environment values instead of relying silently on defaults.
- Added a warning in the README about Render Free's storage/idle limitations.

The bot's application logic has not been broadly rewritten because this ZIP has no live integration test and a 9k-line core rewrite could change moderation/game behavior unexpectedly.

## Дополнение: формат «Олды чата» от Ирис

Пользователь подтвердил, что подписи участников кликабельные, но могут не совпадать с именами Telegram-профилей. Поэтому парсер считает ссылку/упоминание источником идентичности: сначала Telegram ID, затем точный username из Telegram-ссылки. Число активности в скобках удаляется из запасной текстовой подписи.

Если ссылка содержит неподдерживаемый URL или её пользователь не найден, код не подставляет совпадение по видимому имени и не создаёт рискованную награду. Запись попадает в список для ручной проверки. Добавлен регрессионный тест на все 29 строк приведённого примера. Проверены ссылки `tg://user?id=…`, `tg://openmessage?user_id=…`, `tg://resolve?domain=…`, `t.me` и `telegram.me`.

Тесты выполняются локально с заглушками Telegram; реальная пересылка из живого чата в этом окружении не проверялась.

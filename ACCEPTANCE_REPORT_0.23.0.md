# Acceptance report — Justice Faite Manager 0.23.0

## Verified in this build

- Both uploaded source lines were reconciled. The runtime `jf_registry.py` from the first line and static `jf_registry_audit.py` / Self-Audit from the second are both present and registered.
- The P1 fixes from 0.16.1 remain covered: rest phrase routing, dead `fm:confirmed:` removal, `economy` policy, `inline_guard` / `restlist` catalogue coverage, owner ID from environment, URL origin validation, safe HTML rendering.
- Raffle result publication is now an SQLite outbox. Explicit Telegram rejections get bounded backoff retries. Ambiguous failures and process restarts while `sending` become `uncertain`; they are not automatically resent because Telegram Bot API does not provide an idempotency key and an ambiguous timeout can occur after acceptance. This is the no-duplicate safety boundary; owner reconciliation is required for uncertain rows.
- `TaskManager` supervises bot-created asyncio tasks, persists lifecycle metadata and cancels/awaits tracked tasks at shutdown. Static search finds only the manager's own `asyncio.create_task` call in application Python sources.
- `jf_db` provides shared connection, safe idempotent additive migration, SQLite integrity and atomic backup primitives. Core `bot.migrate_db()` and the task inventory use the shared primitives.
- Static inventory records 26 remaining direct `sqlite3.connect` call sites outside tests. They were not mass-rewritten because doing so without module-specific transaction review risks data loss or behavioral regressions.
- The backup helper was tested by restoring a generated SQLite backup and checking both `PRAGMA integrity_check` and retained row data. Existing console backup/restore tests also pass offline.
- Existing game, command access, settings panel, console, Justice AI, Advisor/Watch and other feature tests are retained in the offline runner.
- The release gate completed successfully: 40/40 test modules, test-entrypoint audit, source compilation and basic credential-literal scan. The exact packaged ZIP was extracted into a clean directory and independently reran 40/40 modules with exit code 0.
- SQLite lock probe (disposable test database only): a writer lock held for 200 ms was acquired after approximately 232 ms with a 2000 ms timeout.

## Not verified / remaining external acceptance

- Live Telegram staging: permissions, callback behavior, actual message deletion, real raffle delivery and owner-DM backup restore were not exercised.
- Live Render deployment, persistence and service lifecycle were not exercised. No deployment or environment mutation was performed.
- Live Groq/provider behavior was not exercised. Tests use deterministic offline stubs; no real Telegram identifiers are sent to the provider.
- The 26 legacy direct SQLite connection sites remain for staged migration. The inventory and lock probe are diagnostic; they do not claim all SQLite access has already been unified.
- Full production database restore drill and production migration are not performed. Do not deploy without a fresh verified backup and staging acceptance.

## Operational safety

No commit, push, Render deployment, production DB change, live Telegram call, provider API request or destructive data operation was performed. `ADMIN_ID` must be configured by the operator in Render environment settings; the blueprint has no hard-coded owner ID.

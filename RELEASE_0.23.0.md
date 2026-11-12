# Justice Faite Manager 0.23.0 — consolidation release

## Scope and compatibility

This release consolidates the two supplied 0.16.0 archives and the verified 0.16.1/0.17.0 work in the supplied working tree. It preserves existing command handlers, feature modules, SQLite tables and the owner-only, analysis-only Justice AI policy. No production database migration, Render change, Telegram action, commit, push or deployment is performed by the release process.

## Version milestones represented in this release

- **0.16.1 — P1 hardening:** safe owner configuration, passive rest routing, dead callback removal, URL origin validation, HTML parse mode, honest raffle publication failure reporting.
- **0.17.0 — task lifecycle:** one `TaskManager` owns bot-created asyncio tasks, records task inventory and shutdown outcomes, marks interrupted tasks after restart. Durable domain queues remain responsible for replay; arbitrary coroutine objects are not falsely resumed.
- **0.18.0 — SQLite safety foundation:** added `jf_db` primitives for consistent connections, idempotent additive column migrations, integrity checks and atomic SQLite backup. Added a static connection inventory and disposable lock-wait probe. Existing connection sites are intentionally not mass-rewritten; `tools/sqlite_inventory.py` reports the remaining migration inventory.
- **0.19.0 — game regression gate:** retained the existing Spy/Mafia/game state-machine tests and command routing matrix; release runner verifies every test function is invoked. The matrix is offline and does not claim live Telegram coverage.
- **0.20.0 — owner console verification:** retains live-state console views and SQLite-backed backup/restore logic with offline tests. Live pinned-message restore on Telegram is not claimed by this offline release.
- **0.21.0 — Justice AI reliability:** retains identity pseudonyms, local redaction, provider failure handling, isolated owner memory and analysis-only behavior; no automated sanctions.
- **0.22.0 — Advisor / Watch:** retains persistent evidence and retry-oriented report delivery; outputs are owner-review signals, not sanctions. Live provider semantics remain unverified.
- **0.23.0 — release consolidation:** deterministic release gate, complete offline runner, bytecode-free source archive, SHA-256 manifest and upgrade documentation. A durable raffle publication outbox retries explicit Telegram rejections and marks ambiguous outcomes as `uncertain` rather than risking duplicate result posts.

## Database migration policy

`jf_db.ensure_columns` inspects `PRAGMA table_info` before every additive `ALTER TABLE`, is safe to call repeatedly, validates identifiers and preserves existing rows. New/modified modules should use this helper. The current codebase still contains legacy direct `sqlite3.connect` sites; they are inventoried, not silently rewritten in bulk. Run `python tools/sqlite_inventory.py` before planning further migration work.

## Verification

Run `python -B tests/run_all.py`. The runner statically checks that all module-level `test_*` functions are invoked, then executes modules in isolated child processes. `tools/release_gate.py` enforces version, tests, compilation and a basic credential-literal scan. `tools/sqlite_lock_probe.py` measures lock wait in a disposable database only.

Offline tests do not prove live Telegram permissions, callback delivery, Render persistence, Groq availability or Telegram backup restoration. Those require a separately authorized staging deployment and explicit owner approval.

## Upgrade

1. Keep a verified backup of the production SQLite database before deployment.
2. Set `ADMIN_ID` in the Render environment; there is no hard-coded owner fallback.
3. Keep existing environment variables and persistent-disk configuration unchanged.
4. Deploy only after reviewing the diff and running the release gate in the target environment.
5. Verify `/jf_console`, `/jf_self_audit`, command settings, game flows and owner backup in a staging chat before production.

## Explicitly not performed

No commit, push, Render deployment, live Telegram test, production migration, destructive data cleanup, owner-ID inference or Groq request containing real Telegram identifiers.

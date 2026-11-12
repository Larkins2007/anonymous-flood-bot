# 0.23.0 acceptance checklist

- [x] Both supplied archives consolidated; unique registry audit artifacts retained.
- [x] Hard-coded owner ID removed from Render blueprint; owner is environment-configured.
- [x] Central task lifecycle manager used for bot-owned asyncio tasks.
- [x] Additive, repeatable SQLite migration helper and validated atomic backup helper added.
- [x] SQLite connection inventory and disposable lock contention probe added.
- [x] Offline test runner checks test entrypoint coverage.
- [x] Offline command access, game, console, AI, Advisor/Watch and P1 test modules retained.
- [ ] Live Telegram staging verification (requires user-controlled bot and chat).
- [ ] Live Render deployment verification (not authorized/performed).
- [ ] Live Groq/provider verification (credentials and external request not used).
- [ ] Production database restore drill (requires explicit production backup and approval).

A checked offline item is not a claim of live service verification. The unchecked items are intentionally external acceptance gates.

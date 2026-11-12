# Upgrade guide: 0.17.0 → 0.23.0

- Preserve the existing SQLite database and take a verified backup first.
- Configure `ADMIN_ID` as a Render environment secret/value. Startup fails closed if it is missing or invalid.
- Keep `BOT_TOKEN`, database path, chat IDs and existing provider settings unchanged.
- The new `jf_db` helpers are additive; no automatic bulk schema rewrite is performed.
- Run `python -B tests/run_all.py` and `python tools/sqlite_inventory.py` before release.
- For staging, verify owner console, command access gates, self-rest routing, game start/recovery, raffle delivery, AI owner reports and pinned backup restoration.
- Do not deploy this archive until staging checks and a production backup are confirmed.

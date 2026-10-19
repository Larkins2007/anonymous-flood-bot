# Changelog — manual-control revision

- Disabled the scheduled announcement worker completely; it only sleeps and never posts game-start messages.
- Removed the join button and stopped registering the application, join-request, and application-review routers. Legacy application tables remain in SQLite only to avoid destructive migrations on existing installations.
- Reworked `/jf_awards` and `/jf_iris_sync`: the owner asks Iris for `!олды` in the flood, forwards Iris' response into the bot DM, and receives one separate award command per qualifying member/milestone. The bot does not send award commands into the flood.
- Fixed Iris old-list row parsing: numbered rows such as `1. 🔥 Name (36,2k) — 3 месяца 2 дн` are now recognized correctly; stylized Unicode names are normalized without fuzzy guessing.
- Fixed identity resolution returning `sqlite3.Row` where a dictionary is expected, and added support for forwarded caption text and Telegram `t.me` profile links.
- The owner DM now includes a complete roster of all generated commands (split safely if long), plus a separate list of unmatched/ambiguous old-list rows. A non-list message no longer consumes the pending scan.
- Added per-command enable/disable controls under `/admin` → `Настройки команд`, including custom commands. Settings persist in `bot_settings`.
- Added per-reward audit buttons: mark manually issued or cancel the prepared reward so it can be prepared again. This is an owner assertion, not an Iris delivery receipt.
- Updated `COMMANDS.md`, `README.md`, and offline regression tests.

## Verification

`python -m py_compile bot.py justice_features.py tests/simulation.py` and `python tests/simulation.py` pass in the build environment. The environment does not have `aiogram` installed and cannot reach Telegram, so runtime startup and live callback/API tests remain unverified. Do not treat static tests as proof of successful Telegram delivery.

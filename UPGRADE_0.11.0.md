# Upgrade to v0.11.0

1. Back up the current SQLite database and keep the pinned `JF_DB_BACKUP_V1` document.
2. Replace the code with this release; do not delete `DB_PATH`, the persistent disk, or `.env` values.
3. Keep the existing `ADMIN_ID`, `PRIMARY_CHAT_ID`, `BOT_TOKEN`, `DB_PATH`, and persistent-storage settings unchanged.
4. On startup, new social/pet tables are created additively. Existing wallet balances, item inventory, ledger history, warnings, rests, roles, awards and community data are not reset.
5. Confirm the startup log reports successful database initialization.
6. In a test group, check `/jf_pets`, `.отн запрос` (reply to a test participant), `.отн да`, `Мой питомец`, `Дом`, `.отн история доход`, `Ферма`, `Баланс`, and `/settings`.
7. AI remains off unless explicitly configured. Do not paste API keys into group messages or repository files.

The archive has only been tested offline with SQLite and Telegram stubs. It has not been deployed to Render or exercised against the live Telegram API.

# Smoke test

1. Set `BOT_TOKEN` on Render.
2. Make sure only one service/process uses the token.
3. Add the bot to a test group and give it admin rights including Manage Tags and permission to pin messages.
4. Check `/health` returns `polling_running=true`, `db_ok=true`, `fatal_error=null`.
5. In the group test:
   - `/help@justice_faite_bot`
   - `/roles@justice_faite_bot`
   - `/free_roles@justice_faite_bot`
   - `калл <роль>` after one new member joins
   - reply + `калл <роль>`
   - `/syncroles@justice_faite_bot`
   - `/mafia@justice_faite_bot`
6. Mafia test: join five players, verify the same lobby message updates after join/leave, verify it is pinned, then press `ЗАПУСТИТЬ MAFIA` and confirm the bot sends `/start@MafiaAzBot`.
7. Do not expect this bot to run the Mafia game itself.

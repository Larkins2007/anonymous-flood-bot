# Justice Faite Bot

Telegram bot for anonymous feedback, moderation/admin tools, group role tags, and a Mafia lobby that hands the game over to `MafiaAzBot`.

## Core group features

- `/help` — participant/admin help.
- `/roles` — roles currently occupied in this chat.
- `/free_roles` — currently free roles.
- `калл <роль>` — admin assigns the role to the newest single pending member.
- Reply to a member with `калл <роль>` — admin assigns the role to that member.
- `калл @username <роль>` — admin assigns the role to that member.
- `🎭 Назначить роль` — welcome-button flow that asks the admin to type the role name.
- `/syncroles` — re-check all active members against their real Telegram member tags.
- `/release <роль>` — clear a role and remove the Telegram tag.
- `/mafia` — open the lobby.
- `/mafia_leave` — leave the lobby.
- `флудик начни мафию` / `флудик статус` / `флудик закрой мафию` — natural shortcuts for the lobby.

## Mafia behavior

This bot does **not** run the Mafia game itself. It only maintains the lobby. At 5 players the creator/admin can press `ЗАПУСТИТЬ MAFIA`; the bot sends `/start@MafiaAzBot` and closes/transfers the lobby.

## Role behavior

The external roster/list editor has been removed. The bot no longer edits external roster messages, captures roster messages, or manages a linked info channel. Role state is based on members and Telegram member tags in the current chat.

`set_chat_member_tag` requires the bot to have the appropriate Telegram admin permission (Manage Tags).

## Render

Start command:

```bash
python bot.py
```

Required environment variable:

```text
BOT_TOKEN=...
```

The app exposes `/health` on port `10000`.

Only run **one** instance of the bot with the same token. A second long-polling instance causes Telegram `Conflict: terminated by other getUpdates request`.

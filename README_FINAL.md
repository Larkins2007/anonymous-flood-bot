# Flood Moderator Bot — FINAL STAGING

## Important
This package is for the first Telegram staging test. Do NOT put the real bot
token into GitHub.

## What is already implemented

- New members are detected through `chat_member`.
- New members are restricted until confirmation.
- Admin can assign roles with:
  - `калл Чжун Ли`
  - `калл чжун ли`
  - `калл @username Чжун Ли`
  - or by replying to the member's message with `калл Чжун Ли`.
- Role matching ignores case and repeated spaces.
- The Telegram member tag is generated automatically as `❦...❦`.
- `setChatMemberTag` is used for the actual Telegram member tag.
- Welcome/confirmation message is tied to the joining user.
- Only that user can press the confirmation button.
- Confirmation removes the temporary restriction.
- Leaving is logged with the saved role/tag and reported to the admin.
- A returning user can recover their previous free bot-managed role.
- Role changes release the old role automatically.
- `/id` shows chat/message/user IDs to the admin.
- `/capture_list 40` and `/capture_list 41` save the exact real message IDs and
  Telegram entities from the messages you reply to.
- The roster updater then edits those captured messages instead of guessing
  which message should be edited.
- Captured `custom_emoji` IDs are reapplied to generated occupied markers
  (for example the premium version of 💛/🧡/💚).
- `/sync_list` forces a roster refresh.
- SQLite stores members, role ownership, join/leave history and captured list
  templates.

## First Telegram test

1. Create a separate test supergroup.
2. Create a separate test info channel.
3. Add this bot as administrator in the test supergroup.
4. Give it at least:
   - Restrict Members
   - Manage Tags
5. In the test info channel, give it permission to edit messages.
6. Put your test roster messages in the info channel.
7. In the test group, run `/id` as admin. The bot will show the group ID.
8. In the info channel, reply to the first roster message with:
   `/capture_list 40`
9. Reply to the second roster message with:
   `/capture_list 41`
10. Add a test account to the group.
11. The bot should restrict it and send the rules/confirmation flow.
12. As admin, reply to that user's welcome/message with:
    `калл Навия`
13. The bot should set the Telegram member tag and update the roster.
14. Test the confirmation button from the actual test account.
15. Test leave/rejoin and verify the role is released/restored as expected.

## Important Telegram limitation

The member tag is not the same thing as an administrator custom title.
Telegram's `setChatMemberTag` is specifically for regular members and requires
the bot to be an administrator with `can_manage_tags`.

Custom emoji are preserved from the real captured roster messages. The bot
does not invent custom emoji IDs.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env
python moderation_bot.py
```

Before running, set the real `BOT_TOKEN`, `GROUP_CHAT_ID`, `INFO_CHANNEL_ID`,
and your numeric `ADMIN_ID` in `.env`.

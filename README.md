# Flood Moderator Bot — unified final staging build

This build is based on the current full `bot(4).py` (the 4,000+ line anonymous-feedback bot) and adds the agreed flood/group moderation system without removing the existing private-chat feedback/report/admin mechanics.

## Included group mechanics

- New member detection through Telegram `chat_member` updates.
- Immediate restriction of new members until the rules are confirmed.
- Welcome message with a per-user confirmation button.
- Only the joining member can confirm their own rules button.
- `калл <роль>` works without case sensitivity and with repeated spaces.
- `калл @username <роль>` is supported.
- Reply to a member's message with `калл <роль>` is supported.
- Plain `калл <роль>` safely targets the single pending newcomer when exactly one is waiting; if multiple newcomers are pending, the bot refuses to guess.
- Role assignment stores the Telegram ID, username, role and tag.
- The bot calls Telegram `setChatMemberTag` to set the real member tag. It first tries the requested decorative form (`❦...❦`) and falls back to a safe no-ornament form if Telegram rejects it.
- Role changes release the old role automatically.
- Leaving a group is recorded; the role is released and the owner `@Belochki_Rulyat` is notified.
- A returning participant may automatically recover their previous free role.
- `/pending`, `/member`, `/roles`, `/release` and `/id` are available to the owner.
- `/bind_group` binds the current test group.
- `/bind_info` binds the info channel.
- `/capture_list 40` and `/capture_list 41` store the exact roster messages you reply to, so the bot never guesses which messages to edit.
- Captured Telegram custom-emoji entities are stored by `custom_emoji_id` and reapplied when the roster is rebuilt.
- `/sync_list` updates the captured roster messages.
- The 148-role catalog from the conversation is embedded in `group_logic.py`.

## Important Telegram permissions

For the test group the bot must be an administrator with, at minimum:

- Restrict Members
- Manage Tags
- permission to send messages

Telegram documents `setChatMemberTag` for regular members in groups/supergroups and requires `can_manage_tags`. `chat_member` updates require the bot to be an administrator and to explicitly receive `chat_member` updates. The final bot uses aiogram's registered `chat_member` observer, so `resolve_used_update_types()` includes it.

For the info channel, the bot must be an administrator with permission to edit messages posted by other users.

## First test procedure

1. Create a separate test supergroup.
2. Add the bot and make it an administrator.
3. Disable BotFather Group Privacy for the bot.
4. Create a separate test info channel.
5. Add the bot as an administrator with message-edit permission.
6. In the test group, send `/bind_group` as `@Belochki_Rulyat`.
7. In the test channel, publish the two roster messages you want to control.
8. Reply to the first roster message with `/capture_list 40`.
9. Reply to the second roster message with `/capture_list 41`.
10. Add a test Telegram account to the group.
11. The bot should restrict the new member and send the welcome/confirmation message.
12. As owner, reply to the member's message or use `калл <роль>` while exactly one newcomer is pending.
13. Example:

```text
калл Чжун Ли
```

The following are equivalent:

```text
калл чжун ли
КАЛЛ ЧЖУН ЛИ
кАлЛ   Чжун   Ли
```

14. The bot will try to apply `❦𝒁𝒉𝒐𝒏𝒈𝒍𝒊❦` and update the welcome message.
15. The member presses `✅ Подтвердить`; the bot removes the restriction and changes the button to `✅ Подтверждено`.
16. Check that the corresponding roster message updates.
17. Remove the member from the group. The bot should store the leave event, release the role and notify `@Belochki_Rulyat`.
18. Re-add the same test account and verify that the previous free role can be restored.

## Existing private-chat bot mechanics

The original anonymous-feedback/report/admin/broadcast/SQLite persistence system remains in this same `bot.py`. The group manager is additive; it does not replace `/start`, reports, feedback, admin replies, delivery recovery or the existing HTTP health endpoint.

## Offline checks included

- Python syntax compilation.
- Static handler/schema validation.
- Role normalization and tag safety checks for all 148 roles.
- Deterministic role lifecycle simulation: join -> role assignment -> tag -> confirmation -> leave -> role release.

## Canonical English naming

The catalog uses the current English forms used by official HoYoLAB/HoYoverse material where available. This includes recent names such as Dahlia, Lohen, Varesa, Aino, Lauma, Prune, Nefer and others. Lore-only names are kept in their established English forms rather than invented translations.

## Runtime environment

```text
aiogram>=3.30,<4.0
aiohttp>=3.9,<4.0
```

`BOT_TOKEN` stays only in the environment. The owner numeric ID is kept in `bot.py` as requested.

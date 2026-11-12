# Upgrade 0.23.0 → 0.24.0

1. Back up the persistent SQLite database before replacing the application archive.
2. Deploy the 0.24.0 source with the same `DB_PATH`, `ADMIN_ID`, `PRIMARY_CHAT_ID`, and AI environment variables.
3. Ensure the bot can call `getChatMember` for known users in the main group.
4. Grant the bot `can_edit_messages` in the info channel. `ROLE_INFO_CHANNEL` defaults to `@Justice_faite`.
5. In the bot's private chat, register the owner-supplied role mappings with `/roleid Character — TelegramID` (one per line for bulk import).
6. Run `/syncroles` as the owner. Review its report, including both post 40 and post 41 results.
7. Test `/help` in the primary group and open the private command guide. Test AI with `Джастис привет`; the comma form and `помощник` are intentionally no longer triggers.

No deployment, database migration of user data, or Telegram API call is performed by the offline release gate.

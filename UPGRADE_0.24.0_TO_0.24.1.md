# Upgrade 0.24.0 → 0.24.1

1. Back up the existing SQLite database.
2. Replace the source with this release while preserving `.env` and the existing database path.
3. Confirm the bot is an administrator in the main group and has permission to edit messages in `@Justice_faite`.
4. Confirm polling includes `chat_member` updates (the dispatcher resolves registered update types automatically).
5. In a private chat, use `/help` to open the interactive guide.
6. In the main group, use `/syncroles` as owner to reconcile known members and refresh posts 40 and 41.

No migration deletes role mappings or existing member records.

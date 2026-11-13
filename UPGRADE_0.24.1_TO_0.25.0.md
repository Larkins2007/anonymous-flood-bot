# Upgrade 0.24.1 → 0.25.0

## Critical change: /syncroles

`/syncroles` **no longer edits** historical channel messages 40 and 41.

On every successful run it:

1. Reconciles known members and owner-registered role IDs.
2. Detects left members and frees their roles.
3. **Sends two new messages** with the current role lists (💛 = confirmed taken).
4. Reports counts honestly (known IDs, in-chat, left, taken, free estimate, errors, published message count).

Old posts 40 and 41 remain untouched. You may leave them as historical snapshots or delete them manually if desired.

## Rate limit

Non-owner callers are limited to one successful `/syncroles` every ~90 seconds. The owner is not hard-blocked.

## Migration steps

1. Back up the existing SQLite database.
2. Replace the source with this release while preserving `.env` and the existing database path.
3. Confirm the bot is an administrator in the main group and can **send** messages to `ROLE_INFO_CHANNEL` (default `@Justice_faite`).
4. Confirm polling includes `chat_member` updates.
5. Run `/syncroles` once as owner and verify two new list messages appear in the channel.
6. Optionally remove or archive the old messages 40 and 41 by hand.

No migration deletes role mappings or existing member records.

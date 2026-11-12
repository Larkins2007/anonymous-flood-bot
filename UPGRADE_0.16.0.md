# Upgrade to 0.16.0

1. Back up the current SQLite database before replacing application files.
2. Replace application code with the 0.16.0 release archive.
3. Keep existing Render environment variables and database path. No new required env vars.
4. Start the service normally. No destructive migrations.
5. From the owner private chat run `/jf_self_audit` and review the report.
6. Optional: open `/settings` and confirm command switches still match expectations.

## What changed for operators

- New owner command: `/jf_self_audit` (private only). Diagnostics only.
- Safer link whitelist matching (explicit host/origin, no LIKE wildcards).
- Raffle completion no longer claims "published" when Telegram delivery failed.
- Fun commands render HTML correctly.

## What did not change

- No production deploy is performed by this package.
- No GitHub main changes, no Render plan/disk changes.
- Command access policies and ranks are unchanged.
- AI still does not issue punishments or change roles.

## Rollback

Restore the previous application files and the database backup taken in step 1.

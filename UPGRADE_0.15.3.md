# Upgrade to 0.15.3

1. Back up the current SQLite database before replacing application files.
2. Replace the application code with the 0.15.3 release archive.
3. Keep the existing Render environment variables and database path. No new environment variables are required.
4. Start the service normally. `JusticeAIAnalytics.init()` runs additive, idempotent SQLite migrations.
5. Verify `/jf_ai_audit` from the owner's private chat. It should include the Watch interaction-counter table and terminal delivery queue status.

## Database migration

- Existing `jf_ai_reports`, `jf_ai_watch_alerts` and `jf_ai_analytics_state` data is preserved.
- The alert migration adds any missing compatibility columns, including `occurrence_count`; it backfills the new count from `repeat_count` and fills missing `last_seen_at` from `created_at`.
- Creates `jf_ai_watch_interactions` with one unique row per chat / actor / recipient pair. It stores counters and timestamps only, not message text or profile labels.
- Adds `watch_interactions_refreshed_at` to `jf_ai_analytics_state` (no key collision with existing cursors or schedule keys).
- No records are deleted during startup migration. Retention cleanup runs later according to the effective archive retention.

## Owner configuration

`/jf_ai_schedule` retains the current Advisor and Watch controls. Set `retention` to a value from 1 day through the configured chat archive retention. The Watch history cannot be retained longer than the archive.

To intentionally clear Watch signal and interaction history for the managed chat, use `/jf_ai_watch_clear confirm` in the owner's private chat. This leaves chat messages, revisions and analytics reports untouched.

## Safety and deployment boundary

This archive contains no secrets and does not change GitHub branches or Render configuration. Do not copy the test database into production. Live Telegram, Groq and Render behavior must be verified after deployment by the owner; no deployment is performed by this upgrade package.

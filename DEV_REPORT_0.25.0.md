# Development report — Justice Faite Manager 0.25.0

## Scope of this release

This release addresses the **critical broken contract** of `/syncroles` that was editing (and failing on) historical channel messages 40 and 41.

The master prompt requested a full multi-system audit and rework (members, roles, help, AI, economy, games, moderation, DB, Render, full test matrix, interactive help cards for every command, etc.). That is multi-week professional work. **0.25.0 delivers the highest-priority correctness fix** and establishes the release baseline for continued work.

## What changed in 0.25.0

### `/syncroles` (critical)

- **No longer calls `edit_message_text` on message_ids 40 and 41.**
- On success, **sends two new messages** to `ROLE_INFO_CHANNEL` (default `@Justice_faite`) with the current role lists:
  - 💛 = confirmed taken (registered ID still in chat)
  - empty = free
- Report now includes:
  - known members checked
  - registered IDs
  - confirmed in chat / left
  - taken / free estimate
  - number of new list messages published
  - explicit statement that posts 40/41 were not modified
  - honesty note about Bot API member-list limits
- Cooldown (~90s) for non-owner callers to reduce accidental duplicate publications
- Parallel runs serialized via asyncio.Lock (lazy-initialized)
- Console sync button uses the same publish path

### Version

- `VERSION` → `0.25.0`
- `CHANGELOG.md` entry added
- `UPGRADE_0.24.1_TO_0.25.0.md` written

## What was verified

- `bot.py` / core modules compile cleanly
- Existing offline `test_roles` suite passes under stubs
- No remaining hard-coded edit of posts 40/41 in the sync path

## What was **not** completed in this session (honest)

The master prompt criteria 1–30 require substantially more work:

| Area | Status |
|------|--------|
| Full command inventory table with handler linkage | Not produced end-to-end |
| Unified members system redesign | Partially present in 0.24.x; not fully reworked |
| Role conflict detection UX | Existing registry one-to-one; conflicts not fully surfaced in admin UX |
| Interactive `/help` deep cards for every command | Base interactive help exists; full 17-field cards not rewritten for all commands |
| Justice AI context + observer mode hardening | Existing; not re-audited in depth this release |
| Economy item real functions | Items remain cosmetic inventory |
| Games full scenario verification | Not re-run against live Telegram |
| Social mechanics full path tests | Not expanded |
| DB unified access layer | Partial (`jf_db`); many call sites still local |
| Render persistent disk / path audit | Not re-validated against live Render |
| Full test matrix + release gate on unpacked ZIP | Partial only |
| Production Telegram / Groq live tests | Not performed (no tokens / access) |

## Recommended next iterations

1. Expand `/help` command cards from real handler signatures and cmdspecs.
2. Centralize member event history (join/leave/role change) into a single event log table.
3. Give economy items at least one real effect (profile display, temporary title, etc.).
4. Add regression test that asserts `/syncroles` never calls `edit_message_text` with ids 40/41.
5. Continue SQLite access consolidation behind `jf_db`.

## Limits

- No live Telegram token was available; no real channel publish was executed.
- No production database was modified.
- Secrets were not included in the archive.

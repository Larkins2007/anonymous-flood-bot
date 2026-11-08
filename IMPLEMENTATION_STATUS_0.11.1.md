# Implementation status — v0.11.1

## Implemented in this build

- Registry: 282 entries, including 279 command/button policies and 3 automatic-feature policies. Every new social, pet, and economy alias is registered for per-command settings.
- Celestiana-safe social layer: consent-based invitations (`.отн запрос`, `.отн да`, `.отн нет`), status/list/top, certificate, stats, invite history, consent/privacy info, end-anytime and transfer/relocation explanations. Known usernames are resolved only from saved participant records; unknown names require reply. Invitations expire after seven days.
- 60 neutral social actions: 30 standard and 30 additional. Actions require replying to a participant and have a short anti-spam interval.
- Separate per-command access policy for `.отн история связи`; added `.отн пт похвалить` alias to the pet interaction handler and settings catalog. Command parsing now safely ignores empty/non-text updates instead of raising `IndexError`.
- Economy aliases: balance, token transfer, item gifts, public/personal gift showcase (separate from the store and inventory), daily quests, and paginated income/expense ledger history. All balance changes continue through the common transactional SQLite ledger.
- Virtual companion: one pet per user/chat, selection, rename, friendly interactions, history, achievements.
- Personal den: decor, rooms, and levels. Room/upgrade costs are fixed and debited through the shared economy service in a transaction. Pet actions enforce their 30-second anti-spam window inside a single SQLite write transaction.
- No pet decay, illness, hunger penalties, or loss of progress after inactivity. No paid random eggs, real-money premium, or moderation perks for sale.
- Additive SQLite migrations only; existing wallet, inventory, ledger, community, and moderation tables are retained.

## Explicit exclusions / remaining integrations

- Sexual/adult commands and roleplay, romantic immersive roleplay, pregnancy, and biological child simulation are not implemented. Neutral buddy links and virtual pets are the safe alternative. No sex/gender field is collected by these modules.
- Iris sections that require closed Iris services or external platform features remain partial/out of scope: full Inline Mode, Telegram Business, closed global anti-scam databases, global market/exchange, and full cross-chat public catalog.
- This build does not claim exact one-to-one parity with all Iris commands. The 32-section migration map distinguishes implemented features, safe analogues, partial features, and unavailable integrations.
- AI remains optional and disabled by default; a valid external provider, model ID and API key are required. AI provider and production Telegram/Render deployment were not live-tested here.

## Tests

- `python -m compileall -q .` passed.
- All 30 offline test modules pass when run individually with SQLite and aiogram stubs, including the added pet-praise alias, social-history policy, and empty-update parser regression tests. A complete aggregate run was not confirmed within this environment’s execution window; no claim of live integration testing is made.
- Live Telegram Bot API and Render deployment are not verified.

## Deployment safety

- The provided Render Blueprint intentionally remains on the Free plan with `JF_STORAGE_DURABLE=false`; this is not durable SQLite storage. Do not set `JF_STORAGE_DURABLE=true` unless an actual persistent disk is attached and `DB_PATH` points inside its mount path.
- Replace source files only after taking a fresh DB backup; do not delete the existing database, `.env`, or persistent volume.

## Live-log finding carried into this build

- The currently deployed service logged `IndexError: list index out of range` from the old command parser when handling updates without text. v0.11.1 fixes the empty-input path and adds a regression test.
- Recent startup logs showed SQLite quarantine followed by a successful restore from the pinned owner-DM backup on two deploys. This confirms the restore path was exercised, but does not make a Free Render filesystem durable or prove that the backup is always current.
- The same log window includes past OOM-kill and transient Telegram `getUpdates` conflict events. They are observations about the existing service, not a live test of v0.11.1. Check memory and ensure only one polling process is running after deployment.

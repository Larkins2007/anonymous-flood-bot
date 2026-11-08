# Implementation status — v0.11.0

## Implemented in this build

- Registry: 281 entries, including 278 command/button policies and 3 automatic-feature policies. Every new social, pet, and economy alias is registered for per-command settings.
- Celestiana-safe social layer: consent-based invitations (`.отн запрос`, `.отн да`, `.отн нет`), status/list/top, certificate, stats, invite history, consent/privacy info, end-anytime and transfer/relocation explanations. Known usernames are resolved only from saved participant records; unknown names require reply. Invitations expire after seven days.
- 60 neutral social actions: 30 standard and 30 additional. Actions require replying to a participant and have a short anti-spam interval.
- Economy aliases: balance, token transfer, item gifts, public/personal gift showcase, daily quests, and paginated income/expense ledger history. All balance changes continue through the common transactional SQLite ledger.
- Virtual companion: one pet per user/chat, selection, rename, friendly interactions, history, achievements.
- Personal den: decor, rooms, and levels. Room/upgrade costs are fixed and debited through the shared economy service in a transaction.
- No pet decay, illness, hunger penalties, or loss of progress after inactivity. No paid random eggs, real-money premium, or moderation perks for sale.
- Additive SQLite migrations only; existing wallet, inventory, ledger, community, and moderation tables are retained.

## Explicit exclusions / remaining integrations

- Sexual/adult commands and roleplay, romantic immersive roleplay, pregnancy, and biological child simulation are not implemented. Neutral buddy links and virtual pets are the safe alternative. No sex/gender field is collected by these modules.
- Iris sections that require closed Iris services or external platform features remain partial/out of scope: full Inline Mode, Telegram Business, closed global anti-scam databases, global market/exchange, and full cross-chat public catalog.
- This build does not claim exact one-to-one parity with all Iris commands. The 32-section migration map distinguishes implemented features, safe analogues, partial features, and unavailable integrations.
- AI remains optional and disabled by default; a valid external provider, model ID and API key are required. AI provider and production Telegram/Render deployment were not live-tested here.

## Tests

- `python -m compileall -q .` passed.
- The 30 test modules each pass when run individually with the offline SQLite/aiogram stubs. The aggregate runner printed PASS for all modules but did not exit cleanly in this environment; therefore the individual test results are the verified claim.
- Live Telegram Bot API and Render deployment are not verified.

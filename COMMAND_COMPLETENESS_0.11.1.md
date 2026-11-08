# Command completeness audit — 0.11.1

## Verified implementation scope

The repository command registry has 282 entries: 279 individual command/button policies and 3 automatic-feature policies. The complete registered command list is discoverable in `cmdcatalog.py`, and `tests/test_command_access.py` verifies that all registered aliases have access/settings policies.

### Celestiana-derived safe features

- Voluntary partner link: request, accept, decline, profile, list, top by duration/activity, certificate, stats, invite history and end-anytime. Each person can have one active partner per chat.
- 60 neutral social actions: 30 standard + 30 extra.
- Pet companion: choose, view, rename, pet/play/praise/feed, history and milestones.
- Personal den: styles, rooms and fixed-cost upgrades.
- Gifts, public/personal gift showcases (distinct from store/inventory), balance, transfer, quests, achievements and transaction history.
- v0.11.1 completes separate access control for `.отн история связи` and the pet-praise alias `.отн пт похвалить`.

### Iris migration map

`IRIS_MIGRATION.md` tracks all 32 functional areas with a status per area: implemented, partial, safe analogue, or not portable. This project does not connect to Iris private services or copy private databases. Exact one-to-one parity is not claimed. The remaining gaps include full Inline Mode, Telegram Business, global closed anti-scam feeds, Iris internal economy/market, broader Telegram permission panels, and some parts of profile/statistics and timer workflows. The clean/Telegram API constraints and permissions are documented per section.

## Source-material limitation

The current project library contains the Iris 32-section migration map and a Celestiana feature/command adaptation spec. It does not contain a complete raw export of every original Celestiana command or a formal complete Iris command dump. The implementation therefore covers the saved command and feature requirements documented in this repository; it cannot responsibly claim exhaustive parity with an unavailable original export.

## Runtime limitations

- All 30 offline test modules pass individually with local SQLite and aiogram stubs. The aggregate run was not confirmed within this execution window; live Telegram API tests were not run.
- Render deployment, host variables and actual bot permissions have not been verified.
- AI requires an external provider, configured model ID and API key; it is disabled by default.
- For durable SQLite data, `DB_PATH` must point to a real persistent disk or managed database. A periodic chat backup alone does not make ephemeral hosting storage durable.

## v0.11.1 runtime hardening

- `normalize_command_candidates()` handles `None`, empty text, and whitespace-only content as “not a command”; this prevents a non-text Telegram update from raising `IndexError` inside the command-access middleware.
- The command-config test runner now executes test functions declared after the earlier regression suite, and includes empty-update parser checks.

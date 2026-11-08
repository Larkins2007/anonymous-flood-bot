# Command completeness audit — 0.11.2

## Verified implementation scope

The repository command registry has 287 entries: 284 individual command/button policies and 3 automatic-feature policies. The complete registered command list is discoverable in `cmdcatalog.py`, and `tests/test_command_access.py` verifies that all registered aliases have access/settings policies.

### Celestiana-derived safe features

- Voluntary partner link: request, accept, decline, profile, list, top by duration/activity, certificate, stats, invite history and end-anytime. Each person can have one active partner per chat.
- 60 neutral social actions: 30 standard + 30 extra.
- Pet companion: choose, view, rename, pet/play/praise/feed, history and milestones.
- Personal den: styles, rooms and fixed-cost upgrades.
- Gifts, public/personal gift showcases (distinct from store/inventory), balance, transfer, quests, achievements and transaction history.
- v0.11.2 fixes actual aiogram dispatch ownership for overlapping commands and keeps `.отн история связи` in the social module; the pet-praise alias remains covered.

### Iris migration map

`IRIS_MIGRATION.md` tracks all 32 functional areas with a status per area: implemented, partial, safe analogue, or not portable. This project does not connect to Iris private services or copy private databases. Exact one-to-one parity is not claimed. The remaining gaps include full Inline Mode, Telegram Business, global closed anti-scam feeds, Iris internal economy/market, broader Telegram permission panels, and some parts of profile/statistics and timer workflows. The clean/Telegram API constraints and permissions are documented per section.

## Source-material limitation

The current project library contains the Iris 32-section migration map and a Celestiana feature/command adaptation spec. It does not contain a complete raw export of every original Celestiana command or a formal complete Iris command dump. The implementation therefore covers the saved command and feature requirements documented in this repository; it cannot responsibly claim exhaustive parity with an unavailable original export.

## Runtime limitations

- All 30 offline test modules pass individually with local SQLite and aiogram stubs. The aggregate run was not confirmed within this execution window; live Telegram API tests were not run.
- Render deployment, host variables and actual bot permissions have not been verified.
- AI requires an external provider, configured model ID and API key; it is disabled by default.
- For durable SQLite data, `DB_PATH` must point to a real persistent disk or managed database. A periodic chat backup alone does not make ephemeral hosting storage durable.

## v0.11.2 runtime hardening

- `normalize_command_candidates()` handles `None`, empty text, and whitespace-only content as “not a command”; this prevents a non-text Telegram update from raising `IndexError` inside the command-access middleware.
- The command-config test runner now executes test functions declared after the earlier regression suite, and includes empty-update parser checks.


## v0.11.2 dispatch and gameplay audit

- A real-dispatch smoke test now probes every textual command policy with at least one complete alias/sample and confirms its first matching aiogram handler belongs to the expected module. Callback-only policy entries are covered by their dedicated callback policy tests.
- Confirmed regressions fixed: `Топ богатых` → `jf_economy`, `Топ рейтинга` → `jf_reputation`, `.отн история связи` → `jf_social`; `Данет`, `Выбери`, `Инфа`, and `Пург` work without a forced exclamation mark.
- Personal `Жетоны` and pair `Искры` are separate ledgers. Personal transfers keep the `.отн искры подарок N` alias; `.отн искры` is now the pair-balance command.
- Farm: 5–50 personal tokens every four hours; transaction replay is idempotent, old daily farm timestamps are migrated to a cooldown, and starter bonuses are preserved.
- Social pair: 100 opening sparks are represented in the spark ledger; 30 levels; paired actions grant XP and sparks under a per-actor/day cap. Old pair balances are backfilled by additive migration.
- Optional photos: owner configures each neutral action by replying to a Telegram photo with `/jf_social_media_set <action>`; text fallback is automatic if Telegram rejects the file ID.
- All 30 offline test modules passed individually after the changes. The aggregate runner timed out in this execution environment, and no live Telegram/Render test is claimed.

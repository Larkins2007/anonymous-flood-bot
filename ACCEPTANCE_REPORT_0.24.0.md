# Acceptance report — 0.24.0

## Implemented

- Private command guide: `/help` in the primary group provides a deep link to the bot's private chat; `/start help` and private `/help` render descriptions based on the user's access in the primary group and private chat.
- Owner role registry: `/roleid` accepts one or multiple `Character — TelegramID` lines; `/roleid_remove` releases a mapping. Mapping is one-to-one and persisted in SQLite.
- `/syncroles` retains the previous Telegram tag reconciliation and additionally checks owner-registered IDs. Active members receive a yellow heart in the matching role line; confirmed departed members lose it. Unknown status caused by API errors does not clear the published roster.
- The roster renderer uses the existing 148-character catalogue and splits its region groups between info-channel messages 40 and 41.
- Justice AI now triggers only on `Джастис ` (no comma). `помощник` and `Джастис,` are not triggers.
- Command catalog and owner console sync were updated.

## Offline verification

- `python -B tests/run_all.py`: 41/41 test modules passed.
- `python -B tools/release_gate.py`: RELEASE GATE PASS.
- Python source compilation passed.
- New role-registry tests cover parsing, persistent one-to-one mapping, reassignment and removal.

## Not verified live

- No live Telegram calls were made. The bot still needs membership lookup access in the primary group and `can_edit_messages` in the info channel.
- The bot's username must be available for the `/help` deep link.
- The user has not yet supplied numeric Telegram IDs for the character list; therefore the production role mapping table is intentionally empty in this archive.
- Editing two Telegram posts is not transactional. If one edit succeeds and the other fails, the command reports each post separately.
- Groq/provider behaviour and Render deployment were not tested; no deployment or production database was touched.

## Source file consolidation decision

No unrelated feature modules were merged. Their responsibilities, registration points, and test boundaries are distinct; combining them merely to lower the file count would create a larger coupled module and make regressions harder to isolate. `jf_role_registry.py` is a separate persistence boundary for the new owner-managed role mapping. A later consolidation should be based on an import/dependency map and retain all existing tests.

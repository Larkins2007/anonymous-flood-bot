# Justice Faite Manager 0.24.1

Patch release correcting the command guide and automatic role roster reconciliation.

## Changes
- `/help` in private chat now opens an interactive category menu with inline buttons, paginated command lists, and individual command cards.
- The guide recalculates access for every menu and callback view. Commands unavailable to a member are not listed or opened.
- `/syncroles` now publishes both manually registered ID roles and roles automatically learned from Telegram `sender_tag` / member-tag observations.
- Membership join/leave updates and observed group messages feed the role state. A confirmed leave releases the occupied role; temporary Telegram lookup failures do not.
- The public posts keep the canonical regional ordering and mark only active, confirmed occupied roles with 💛.

## Important Telegram limitation
Telegram Bot API does not provide a method to enumerate all ordinary members of a group. The bot learns IDs from membership updates, incoming messages, existing roster records, and administrator lists. It can infer a character only when a recognized role tag is present or an owner has explicitly registered the ID. An entry with no known ID/tag cannot be safely assigned by guesswork.

## Verification
- Offline suite: 41/41 test modules passed.
- No live Telegram or Render deployment was performed.

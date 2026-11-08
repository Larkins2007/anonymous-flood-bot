# Justice AI — identity and voice 0.15.1

## Identity resolution

The source of current character/flood roles is the existing `group_members` row scoped to the configured primary chat and active membership. `role_key`/`role_name` is validated against `role_catalog`. Telegram `Message.sender_tag` is not a reliable per-message source and is not required. This fixes the previous false negative where the archive had no `sender_tag` column at all.

The model receives only a persistent pseudonym such as `Участник 14`, the validated current character role (when assigned), and a separately resolved internal manager rank (when assigned). The project owner is labeled from the trusted configured owner ID. It never receives the raw Telegram user ID, username, display name, raw title/tag, internal role key or message ID.

Pseudonym mappings are stored locally in `jf_ai_identity_aliases`, scoped by chat. This provides stable continuity between turns and restarts without exporting a reversible identifier. Aliases are for internal model context; the assistant should not expose them in normal replies.

## Voice

Justice is a friendly, informal and responsible assistant for the Justice Faite community. It can respond naturally to banter, answer questions and help clarify disputes without acting like a bot support desk or constantly turning conversation into moderation. It must not force slang, imitate profanity or pretend to be a real person.

The default is 1–3 concise sentences. It expands only when a user explicitly requests details, comparisons, a plan, an explanation with examples, a list or an analysis. It must distinguish playful character roles from actual moderation ranks, avoid guessing when identity context is incomplete, and never perform sanctions or change access rights.

## Database change

Additive only: `jf_ai_identity_aliases` plus `idx_jf_ai_identity_aliases_chat`. The table stores `(chat_id, user_id, alias_no, created_at)` with uniqueness constraints on `(chat_id,user_id)` and `(chat_id,alias_no)`. It does not modify or purge message history, roster, roles, moderator assignments or memory records.

## Verification

`tests/test_ai.py` covers role resolution without message-level sender tags, stable aliases, admin-rank separation and concise voice. `tests/test_console.py`, `tests/test_manager.py` and `tests/test_startup_safety.py` cover the touched storage/manager/startup boundaries. Actual Groq responses and live Telegram events still require an isolated test chat.

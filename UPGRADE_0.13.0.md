# Justice Faite Manager 0.13.0

## Justice Watch contextual review

This release extends Justice Watch beyond deterministic spam checks. It now periodically reviews bounded batches of newly archived text for contextual signals related to harassment, repeated provocation, conflicts, discriminatory language, privacy violations, unauthorized promotion, punishment-baiting, and administration-related conduct.

### Token use

- Rules 4.3 and 4.5 remain deterministic/local and use no Groq tokens.
- Semantic review uses Groq in bounded batches, by default every 10 minutes, after at least 12 reviewable messages, with a maximum of 80 messages per request.
- Disable semantic review with `JF_AI_WATCH_SEMANTIC_ENABLED=false`.
- Semantic review also requires `JF_AI_ENABLED=true`, `JF_AI_ANALYTICS_ENABLED=true`, and a valid Groq configuration.

### Privacy and authority

- Telegram user IDs, usernames, real names, and message IDs are not sent to Groq. The provider receives temporary participant aliases and local labels.
- Exact evidence and Telegram identifiers are stored locally and sent only to the configured owner.
- The model can only create review signals. It cannot warn, mute, ban, delete messages, change roles, or alter member data.
- A model signal is not proof of a violation. The owner/administration must review the evidence and surrounding context.
- Messages are untrusted input; instructions embedded in messages are not followed.

### Configuration

See `.env.example` for `JF_AI_WATCH_SEMANTIC_ENABLED`, `JF_AI_WATCH_SEMANTIC_INTERVAL_SECONDS`, `JF_AI_WATCH_SEMANTIC_MIN_MESSAGES`, and `JF_AI_WATCH_SEMANTIC_BATCH_SIZE`.

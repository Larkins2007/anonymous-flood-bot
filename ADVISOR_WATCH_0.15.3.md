# Advisor / Watch — 0.15.3

## Persistence and privacy

`jf_ai_watch_interactions` stores only `chat_id`, `actor_user_id`, `target_user_id`, `interaction_count`, `first_seen_at` and `last_seen_at`. The unique key is `(chat_id, actor_user_id, target_user_id)`. It has no message body, evidence, username or display-name columns. IDs are only resolved locally; semantic context sent to the provider contains temporary aliases and counts.

Interaction counts are recomputed from a bounded 72-hour archive window at most once every five minutes. Dormant pairs remain only as timestamp/counter records until retention cleanup. Watch interaction and alert retention use the same effective duration, and cannot exceed `JF_CHAT_LOG_RETENTION_DAYS`.

## Delivery

All Advisor report and Watch delivery queues use the same retry intervals after failed attempts: 1, 5, 15 and 60 minutes. Attempt five is terminal. The record remains stored, is marked failed, has no next retry timestamp, and is visible in `/jf_ai_audit`. A private owner notice is sent once; when Telegram is unavailable, the notice remains pending and is retried by a later scheduler cycle until successful.

## Local signals

- 10.1: a promotional Telegram link with an explicit promotion phrase, or multiple Telegram invite links. A bare link is not enough.
- 9.1: an explicit phone number/address combined with third-party disclosure language. A person's own contact details are not automatically classified as doxxing.
- 4.4: three consecutive multi-line text messages from one author, each with at least four non-empty lines, at least fifteen lines in total, within ten minutes. A single long message is not enough. The evidence explicitly asks administration to check the technical-malfunction exception under 4.6.
- 2.6 continues to require a direct stop request linked by reply and a matching subsequent repeat from the same sender.
- 4.3 and 4.5 retain the previously approved exact definitions.

Every signal is a request for human review, not a finding of guilt or an automatic moderation action. Constructive criticism allowed under 16.6 / 16.8 is explicitly excluded from semantic rule-16 violation reasoning.

## Provider resilience

Semantic Watch requests ask the configured OpenAI-compatible endpoint for JSON-object output. Groq gpt-oss requests additionally use low reasoning effort and hidden reasoning output. HTTP 429 token-per-day responses back off for six hours by default; temporary request limits honor `Retry-After` when available. Malformed/empty JSON is deferred for six hours without moving the semantic cursor. Flood and owner memory compaction mark their next permitted run before network I/O, so an empty summary or quota error does not trigger repeated requests every Watch cycle.

## Owner controls

- `/jf_ai_schedule` shows the effective Watch retention and its archive cap.
- `/jf_ai_schedule retention N` accepts only values no greater than the chat archive retention.
- `/jf_ai_watch` includes occurrence counts and delivery-attempt status.
- `/jf_ai_watch_clear confirm` clears Watch alerts and interaction counters for the managed chat only. It does not delete chat archive rows, message revisions or Advisor reports.

No new environment variables are required. Use the existing `JF_CHAT_LOG_RETENTION_DAYS` and owner-only schedule command.

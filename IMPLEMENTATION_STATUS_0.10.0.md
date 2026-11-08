# Implementation status — v0.10.0

## Implemented in this build

- Economy: display currency «Жетоны»; the previous `jf_wallets` balances and ledger are reused without reset. One-time onboarding bonus remains 30 tokens. `Ферма` immediately grants 25 tokens once per Moscow calendar day; legacy `Работа`, `Собрать`, and `Фарм` share the same claim. Daily limits and transactional idempotency remain.
- Shop, inventory, gifts, transfers, achievements and leaderboard remain. Cosmetic purchases only. Daily transfer cap is 500 tokens.
- Social: consent-based non-romantic partner links with invitation, acceptance, decline, status and termination. Only one active partner per member per chat. Both participants can end the link.
- AI: optional external model adapter, explicit group invocation, short per-user/chat in-memory conversation context (up to six exchanges), context reset, per-user rate limit, provider timeout, no SQLite persistence and no bot/admin tool execution. Disabled by default.
- Menu, catalog policies, settings and documentation updated.

## Deliberately not implemented

- No sexual functions, sexual roleplay, explicit content or relationship/child simulation built around sexual scenarios. Neutral social links are available; a future mentorship/pet-care feature can be considered as a safe alternative.
- No complete one-to-one reproduction of every closed Iris service. Inline mode, Telegram Business features, private/global Iris balances, Iris market and full cross-chat public catalog remain outside this build.
- AI provider connection is not live-tested. A valid external API key/model/endpoint and provider billing/access are required. Without them, `Justice AI` reports that it is not configured.
- Telegram Bot API and Render deployment were not exercised in this environment.

## Tests

Run `python -m compileall -q .` and `python tests/run_all.py`. Tests use local SQLite and fake Telegram handlers; they do not prove remote API or production deployment connectivity.

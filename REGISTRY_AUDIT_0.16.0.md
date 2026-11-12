# Registry audit 0.16.0 — what it does, what it found

Generated offline against the 0.16.0 code with the project's aiogram test stubs. Full machine-readable output: `REGISTRY_REPORT_0.16.0.json`; human-readable: `REGISTRY_REPORT_0.16.0.md`. Live Telegram was not involved.

## How it works

`jf_registry.build_audit(dp, ...)` flattens every observer of the dispatcher (message, callback_query, chat_member, …) in registration order, then for each catalog entry probes the handlers' real filters with synthetic messages (private / supergroup, owner / ordinary user) and records the first accepting handler, as aiogram would. Callback handlers are probed with the literals and prefixes found in their filters. `callback_data` strings built in the source are extracted by AST scan and checked against the handlers. Authorization of a callback is classified as: `cmdcfg` policy, owner filter, authorization marker found in handler source (heuristic), allow-listed navigation-only, inert, or `none-found`.

## Result on 0.16.0 (285 catalog commands + 3 features)

- Handlers: message 98, callback_query 35, edited_message 1, chat_member 1, errors 1.
- Commands: 276 routed (incl. 4 covered by handler regex), 7 callback-policy-only, 2 swallowed, **0 unrouted**.
- Callbacks: 35 handlers, **none without authorization evidence**; 27 are governed by a `cmdcfg` policy.
- No duplicate handlers, no duplicate callback namespaces, no alias collisions, no callback_data over 64 bytes, no callback policy pointing outside the catalog.
- Findings: 19 errors, 4 warnings, 28 info.

## Findings that need a decision (all reported, none fixed in this stage)

| # | Code | Subject | What the probe saw | Confidence |
|---|---|---|---|---|
| 1 | REG011 error | `rest_self` ("взять рест") in groups | `bot.group_passive_member_sync` is registered earlier and its regex excludes `калл`, `рестники`, `выдать/снять рест` but **not** `взять рест`, so aiogram hands the text to it and `natural_take_rest_command` is never reached. | High: confirmed by reading the regex and registration order. Not run live. `/take_rest` and `/jf_rest` style slash forms are unaffected. |
| 2 | REG011 error | `unrest` ("снять рест") | `jf_manager.Manager.handle` accepts it before `justice_features.natural_unrest_command`. Whether this is intended delegation or a lost handler needs a human look. | Medium |
| 3 | REG012 error ×17 | all `economy_*` | Category `economy` is used by 17 catalog entries but missing from `cmdcfg.CATEGORIES`; `set_category_enabled("economy", …)` refuses it and the settings panel falls back to a raw label. | High (code read) |
| 4 | REG003 warning | `/inline_guard`, `/restlist` | Handlers use `Command(...)` but there is no catalog policy, so the central switch cannot disable them. `/inline_guard` is also in the hard-coded menu (REG008). | High |
| 5 | REG006 warning | `bot.py` `fm:confirmed:` | A button prefix that no callback handler accepts. | Medium: may be a message-edit marker, not a live button. |

Info-level items: 14 state/opaque handlers cannot be probed (REG014), 3 dynamically built callback_data (REG013), and a few text commands also accepted by later handlers (REG011 info).

## Limits of this audit

- Not run against live Telegram: privacy mode, real admin rights and webhook/polling behaviour are not covered.
- FSM-state handlers and opaque Python predicates are listed, not probed.
- Authorization inside a handler body is a static source heuristic; it is evidence for a reviewer, not proof.
- Probing uses the offline aiogram stub in tests; in production the same code walks the real `Dispatcher.observers`. That path is implemented but was not exercised against real aiogram here (aiogram is not installed in this environment).

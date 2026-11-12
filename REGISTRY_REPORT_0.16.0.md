# Justice Faite registry audit — 0.16.0

- generated: 2026-11-12T02:31:50Z (introspection: offline-stub)
- catalog commands: 285 (+3 features); routed 276, unrouted 0
- handlers: {"message": 98, "edited_message": 1, "callback_query": 35, "chat_member": 1, "errors": 1}
- callback handlers: 35; callback_data sites in source: 157; menu entries: 66
- findings: {"error": 19, "warning": 4, "review": 0, "info": 28}

## Findings

- **error REG011** `rest_self` — supergroup: catch-all bot.group_passive_member_sync is registered before justice_features.natural_take_rest_command and accepts the same text, so the specific handler is never reached (aiogram stops at the first match)
- **error REG011** `unrest` — supergroup: jf_manager.Manager.handle runs first but category 'community' belongs to ['bot', 'jf_community', 'jf_pets', 'jf_social', 'justice_features']; the owning handler justice_features.natural_unrest_command is later and unreachable
- **error REG012** `economy_achievements` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_balance` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_education` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_farm` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_gift` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_gift_showcase` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_help` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_history` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_inventory` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_jobs` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_premium_info` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_quests` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_shop` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_sparks_transfer` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_top` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_transfer` — category 'economy' is not defined in CATEGORIES
- **error REG012** `economy_work` — category 'economy' is not defined in CATEGORIES
- **warning REG003** `jf_inline_guard.InlineGuard.open_panel` — Command('inline_guard') has no catalog entry, so cmdcfg cannot gate, disable or log it
- **warning REG003** `justice_features.cmd_restlist` — Command('restlist') has no catalog entry, so cmdcfg cannot gate, disable or log it
- **warning REG006** `bot.py:5176` — button callback_data 'fm:confirmed:'* is accepted by no callback handler (dead button: Telegram shows a spinner until it times out)
- **warning REG008** `setup_commands:inline_guard` — hard-coded menu command /inline_guard has no catalog policy
- **info REG011** `chat_description_remove` — supergroup: jf_chatconfig.ChatConfig.handle_command wins; later non-catch-all handlers also accept the same text: ['jf_profiles.Profiles.handle']
- **info REG013** `awards.py:644` — callback_data is built dynamically; not statically verified
- **info REG013** `settings_panel.py:310` — callback_data is built dynamically; not statically verified
- **info REG013** `settings_panel.py:54` — callback_data is built dynamically; not statically verified
- **info REG014** `awards.olds_forward` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `awards.olds_in_state` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `bot.admin_broadcast` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `bot.admin_broadcast_non_text` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `bot.admin_reply` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `bot.admin_reply_non_text` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `bot.admin_search` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `bot.admin_search_non_text` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `bot.feedback` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `bot.feedback_non_text` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `bot.report_non_text` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `bot.report_non_text` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `bot.report_reason` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `bot.report_target` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `bot.role_assign_from_text` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `jf_chatconfig.ChatConfig.filter_links` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `jf_inline_guard.register.<locals>.add_text` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `jf_inline_guard.register.<locals>.remove_text` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `justice_features.birthday_state_text` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `settings_panel.on_cc_name` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `settings_panel.on_cc_response` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `settings_panel.on_search_text` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `settings_panel.on_user_access_text` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed
- **info REG014** `settings_panel.on_value_text` — state/opaque filter: reachable only with runtime FSM state or custom predicate; routing for this handler is not probed

## Finding codes

- REG001: catalog entry has no handler that accepts its trigger
- REG002: handler registered more than once with an identical filter signature
- REG003: Command handler has no catalog policy
- REG004: callback handler without authorization evidence
- REG005: callback namespace duplicated or shadowed by an earlier handler
- REG006: inline button callback_data accepted by no handler (stale UI)
- REG007: alias collision inside the catalog
- REG008: Telegram menu entry malformed, duplicated or unrouted
- REG009: callback policy target missing from the catalog
- REG010: callback_data exceeds the Telegram 64-byte limit
- REG011: command text accepted by several handlers or routed to an unexpected module
- REG012: catalog policy inconsistent
- REG013: callback_data built dynamically and not verifiable statically
- REG014: handler with a state or opaque filter cannot be probed

## Callback handlers

| handler | callbacks | policy | auth |
|---|---|---|---|
| `jf_inline_guard.InlineGuard.on_callback` | `ig:*` | — | handler-source (heuristic) |
| `jf_console.Console.handle_callback` | `jfcon:*` | console_open | cmdcfg |
| `bot.user_home` | `u:home` | — | navigation-only (allowlisted) |
| `bot.user_info` | `u:info` | — | navigation-only (allowlisted) |
| `bot.send_info` | `u:send_info` | — | navigation-only (allowlisted) |
| `bot.report_info` | `u:report_info` | — | navigation-only (allowlisted) |
| `bot.user_send` | `u:send` | anon | cmdcfg |
| `bot.user_cancel` | `u:cancel` | — | navigation-only (allowlisted) |
| `bot.admin_cancel` | `a:cancel` | admin_panel | cmdcfg |
| `bot.report_start` | `u:report` | report | cmdcfg |
| `bot.report_edit` | `u:report_edit` | report | cmdcfg |
| `bot.report_confirm` | `u:report_confirm` | report | cmdcfg |
| `bot.admin_reply_start` | `reply:*` | anon_admin | cmdcfg |
| `bot.admin_reply_user_start` | `a:replyuser:*` | anon_admin | cmdcfg |
| `bot.admin_home` | `a:home` | admin_panel | cmdcfg |
| `bot.admin_stats` | `a:stats` | admin_panel | cmdcfg |
| `bot.admin_messages` | `a:messages` | admin_panel | cmdcfg |
| `bot.admin_users` | `a:users:*` | admin_panel | cmdcfg |
| `bot.admin_search_start` | `a:search` | admin_panel | cmdcfg |
| `bot.admin_user` | `a:user:*` | admin_panel | cmdcfg |
| `bot.admin_toggle` | `a:toggle:*` | admin_panel | cmdcfg |
| `bot.admin_block` | `a:block:*` | admin_panel | cmdcfg |
| `bot.admin_blocked` | `a:blocked` | admin_panel | cmdcfg |
| `bot.admin_reports` | `a:reports` | admin_panel | cmdcfg |
| `bot.admin_report` | `a:report:*` | admin_panel | cmdcfg |
| `bot.admin_close_report` | `a:close_report:*` | admin_panel | cmdcfg |
| `bot.admin_broadcast_start` | `a:broadcast` | admin_panel | cmdcfg |
| `bot.group_confirm` | `fm:confirm:*` | member_confirm | cmdcfg |
| `bot.game_poll_callback` | `gp:*` | game_poll | cmdcfg |
| `bot.role_assign_button` | `fm:role:*` | call | cmdcfg |
| `bot.role_assign_close` | `fm:close:*` | — | inert (only answers the callback) |
| `bot.mafia_callback` | `mf:*` | mafia | cmdcfg |
| `bot.spy_callback` | `sp:*` | — | handler-source (heuristic) |
| `awards.on_callback` | `aw:*` | awards | cmdcfg |
| `settings_panel.on_callback` | `cs:*` | settings | cmdcfg |

## Modules

| module | lines | handlers | catalog cmds | tables created | tests |
|---|---|---|---|---|---|
| awards | 1006 | 5 | 2 | jf_award_queue | test_awards.py, test_command_access.py, test_flows.py |
| bot | 10147 | 72 | 127 | admin_notifications, custom_commands, game_catalog, game_poll_votes, game_polls, game_schedule_overrides, group_members, mafia_bans, mafia_games, mafia_players, managed_group, participant_roster, reports, role_catalog, role_history, role_snapshot_state, role_state, schedule_cycle, schedule_runs, sent_messages, spy_custom_words, spy_games, spy_guesses, spy_players, spy_turns, spy_votes, users, weekly_schedule, weekly_schedule_messages | test_ai_analytics.py, test_automod.py, test_chatconfig.py, test_clean.py, test_command_access.py, test_console.py, test_economy.py, test_flows.py, test_inline_guard.py, test_manager.py, test_modvote.py, test_network.py, test_registry.py, test_roles.py, test_schedule.py, test_spy_packs.py, test_startup_safety.py, test_timers.py |
| cmdcatalog | 378 | 0 | 0 | — | test_cmdcfg.py, test_command_access.py, test_justice_core.py, test_settings_panel.py |
| cmdcfg | 1178 | 0 | 0 | cmd_feature_switches, cmd_settings, cmd_settings_audit, cmd_user_access | test_awards.py, test_cmdcfg.py, test_command_access.py, test_flows.py, test_justice_core.py, test_manager.py, test_pets.py, test_registry.py, test_roles.py, test_schedule.py, test_settings_panel.py, test_social.py, test_spy_packs.py, test_startup_safety.py |
| cmdspecs | 458 | 0 | 0 | — | test_awards.py, test_cmdcfg.py, test_command_access.py, test_settings_panel.py |
| jf_ai | 874 | 2 | 3 | jf_ai_identity_aliases | test_ai.py, test_command_access.py, test_justice_core.py |
| jf_ai_analytics | 1922 | 1 | 3 | jf_ai_analytics_state, jf_ai_reports, jf_ai_watch_alerts, jf_ai_watch_interactions | test_ai_analytics.py, test_justice_core.py |
| jf_automod | 105 | 1 | 14 | jf_automod_hits, jf_automod_triggers | test_command_access.py |
| jf_chatconfig | 253 | 2 | 32 | jf_chatconfig_audit, jf_chatconfig_invites, jf_chatconfig_settings, jf_chatconfig_whitelist | test_command_access.py |
| jf_clean | 82 | 1 | 3 | jf_clean_messages | test_command_access.py, test_social.py |
| jf_community | 337 | 1 | 78 | jf_community_bookmarks, jf_community_circle_members, jf_community_circles, jf_community_clan_members, jf_community_clans, jf_community_notes, jf_community_people | test_command_access.py, test_community.py |
| jf_console | 1601 | 9 | 10 | jf_ai_flood_memory, jf_ai_identity_aliases, jf_ai_owner_memory, jf_chat_message_log, jf_chat_message_revisions, jf_console_messages, jf_owner_message_log, jf_project_memory, jf_runtime_log | test_ai.py, test_ai_analytics.py, test_command_access.py, test_console.py, test_justice_core.py, test_registry.py |
| jf_economy | 419 | 1 | 17 | jf_economy_achievements, jf_economy_daily, jf_economy_farm_state, jf_economy_farms, jf_economy_gifts, jf_economy_inventory, jf_economy_ledger, jf_economy_profiles, jf_wallets | test_command_access.py, test_economy.py, test_pets.py, test_registry.py, test_social.py |
| jf_fun | 46 | 1 | 7 | — | test_command_access.py, test_social.py |
| jf_inline_guard | 408 | 4 | 10 | jf_inline_guard_bots, jf_inline_guard_events, jf_inline_guard_settings | test_inline_guard.py |
| jf_manager | 1099 | 1 | 41 | jf_manager_actions, jf_manager_chat_settings, jf_manager_command_access, jf_manager_member_names, jf_manager_moderators, jf_manager_rank_labels, jf_manager_restrictions, jf_manager_warnings | test_command_access.py, test_flows.py, test_manager.py, test_registry.py |
| jf_modvote | 89 | 1 | 41 | jf_mod_vote_ballots, jf_mod_votes | test_command_access.py |
| jf_network | 158 | 1 | 11 | jf_network_bans, jf_network_chats, jf_networks | test_command_access.py |
| jf_pets | 245 | 1 | 78 | jf_pet_action_log, jf_pet_rooms, jf_pets | test_command_access.py, test_pets.py |
| jf_profiles | 168 | 1 | 20 | jf_profiles | test_command_access.py, test_profiles.py |
| jf_raffles | 95 | 1 | 7 | jf_raffle_entries, jf_raffle_winners, jf_raffles | test_command_access.py, test_raffles.py |
| jf_registry | 1265 | 1 | 10 | — | test_command_access.py, test_registry.py |
| jf_reports | 78 | 1 | 14 | jf_reports | test_command_access.py, test_reports.py |
| jf_reputation | 105 | 1 | 5 | jf_reputation, jf_reputation_votes | test_command_access.py, test_reputation.py, test_social.py |
| jf_social | 534 | 1 | 78 | jf_social_action_log, jf_social_invites, jf_social_media, jf_social_pairs, jf_social_spark_ledger | test_command_access.py, test_social.py |
| jf_stats | 148 | 1 | 10 | jf_stats_daily, jf_stats_hourly | test_command_access.py |
| jf_timers | 96 | 1 | 14 | jf_timers | test_command_access.py, test_timers.py |
| justice_features | 1129 | 16 | 154 | jf_achievements, jf_award_audit, jf_birthday_directory, jf_birthday_notices, jf_birthdays, jf_manager_warnings, jf_rest, jf_warnings | test_command_access.py, test_flows.py |
| settings_panel | 844 | 8 | 10 | — | test_flows.py, test_settings_panel.py |
| ui | 255 | 0 | 0 | — | test_flows.py, test_roles.py, test_ui.py |

## Not verified

- live Telegram delivery, Telegram permissions and privacy-mode behaviour
- FSM-state-only handlers and opaque Python predicates (listed as REG014)
- authorization inside handler bodies beyond the static marker heuristic

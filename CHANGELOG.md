# Changelog

## 0.15.1 — Identity-aware Justice AI and natural flood voice (2026-11-08)

- Fixed the core role-recognition defect: role context no longer depends on `sender_tag` being present in every Telegram message. The live `group_members` assignment is authoritative and is validated against `role_catalog`; current roles are now available even when the archive has no `sender_tag` column.
- Added persistent, chat-scoped pseudonyms in `jf_ai_identity_aliases`. The same person now keeps the same local alias across separate messages, allowing the model to follow who is speaking and preserve continuity. The Telegram user ID ↔ alias mapping remains in SQLite and is never sent to Groq.
- Added separate trusted identity facts: assigned flood/character role, owner status, and internal Justice Faite moderation rank. Character role and moderation authority are explicitly distinct; Telegram IDs, usernames, display names, raw tags and role keys remain local.
- Reworked the Justice voice: concise by default (normally 1–3 sentences), friendly and informal without forced slang, warm but responsible as an admin-assistant, and less likely to moralize during normal banter. Detailed answers are reserved for explicit requests.
- Added adaptive completion budgets: 260 tokens for routine replies, 650 for clearly detailed requests.
- Added tests for recognition without `sender_tag`, persistent aliases across calls, rank/character-role separation, concise style and adaptive response budgets.
- Added one additive SQLite table and one index. No existing participant rows, roles, warnings, message history or memories are modified or deleted. No commit, push, production migration or deployment.
- Offline regression checks passed. Live Telegram/Groq behavior and qualitative model output remain unverified.

## 0.15.0 — Low-noise Watch, scheduled Self-Audit and event statistics (2026-11-08)

- Reworked Watch notifications: regular signals are merged into a single owner-only digest, default once every 24 hours; configurable through `/jf_ai_schedule watch 1..24` or `JF_AI_WATCH_DIGEST_INTERVAL_HOURS`.
- Strictly limited urgent notifications to corroborated high-severity/high-confidence semantic signals with at least three source messages and a target resolved from a real reply/mention relationship; urgent messages are rate-limited by a 12-hour cooldown. The bot still never applies sanctions.
- Low-confidence/low-severity semantic observations remain in the local journal without owner DMs. Bare links, phone numbers/addresses, and long formatted text are no longer treated as proof of advertising, doxxing, or flooding by regex alone.
- On first upgrade to 0.15.0, previously pending Watch alerts are marked silent but preserved with their evidence; the new policy does not replay a backlog of stale alerts to the owner's private chat.
- Reduced defaults: Watch archive polling from 30 to 120 seconds; semantic review from 10 to 30 minutes with a minimum batch of 20 messages. Settings remain configurable within safe bounds.
- Added scheduled local Self-Audit (default once per 24 hours). It sends an owner DM only when a new critical failure appears; recurring identical failures and ordinary warnings do not create message spam. Audit is local/token-free and does not repair or modify the project.
- Expanded deterministic game/event analytics with actual session counts, started sessions, terminal statuses and lobby-to-start conversion for existing Spyfall/Mafia tables. No inferred outcomes or participant rankings are produced.
- Expanded callback UI regression coverage: every generated `/settings` screen is checked for duplicate callback IDs within the keyboard and Telegram's 64-byte callback limit; replay/idempotency tests remain in place.
- Preserved split private-owner memory and flood memory from 0.14.1. Private owner memory remains restricted to the owner's actual private chat; it is not exposed to group prompts.
- Existing SQLite schema is unchanged by 0.15.0's main Watch/Self-Audit/statistics changes: the policy uses existing `jf_ai_analytics_state` keys and existing alert rows. No commit, push, production migration or deployment.
- Render durability remains an operator choice: persistent disks require a paid Render service; the default free blueprint is not silently upgraded or billed. See `RENDER_PERSISTENCE_0.15.0.md`.
- Offline tests only; live Telegram/Groq/Render behavior remains unverified.

## 0.14.1 — Justice AI context and separated memory (2026-11-08)

- Added a private owner-only Justice AI conversation in the owner's Telegram DM, guarded by both `from_user.id == ADMIN_ID` and `chat.id == ADMIN_ID`; it uses the existing `jf_owner_message_log` and archives assistant replies in the same journal.
- Added physically separate long-term memories: `jf_ai_owner_memory` for owner preferences/projects and `jf_ai_flood_memory` for broad community topics/events. Natural requests (`Джастис, запомни...`, `Джастис, запомни для флуда...`) and explicit owner-only save/list/search commands route to the correct scope. Group context can never read owner memory; owner context can never read flood memory.
- Added low-frequency compaction of both memories inside the existing `jf-ai-analytics` worker (one bounded request per scope per configured interval); no extra message collector or background task. Memory retrieval ranks notes locally and always retains the rolling summary.
- Expanded local context resolution for Telegram replies, `mention` and `text_mention` entities, and `sender_tag`. Telegram identifiers remain local; the provider sees temporary participant aliases, relation labels and only a role matched against the project's role catalog. Raw sender tags are stored locally but never forwarded. Known archived names/usernames are redacted before provider requests.
- Added additive schema changes: two memory tables; `role` on `jf_owner_message_log`; `entities_json` and `sender_tag` on `jf_chat_message_log`; associated indexes. Existing archives remain the single source of group messages.
- Added tests for separate memory scopes, natural-language save routing, private owner gating, history isolation, Telegram relationship context, tag/catalog matching and schema compatibility. `tests/run_all.py` now statically audits that test functions are invoked by module entrypoints.
- No commit, push, production migration or Render deployment. Live Telegram/Groq/Render behavior remains unverified.

## 0.14.0 — Advisor/Watch foundation (2026-11-08)

- Added deterministic local Advisor aggregates from the existing message archive, hourly/daily statistics, complaint tables, game participation tables and Watch signals; aggregates are appended to generated reports without sending them to Groq.
- Added persistent owner controls `/jf_ai_schedule interval 1..12` and `/jf_ai_schedule daily 0..23`, stored in the existing `jf_ai_analytics_state` key/value table.
- Added additive delivery metadata to existing report and Watch tables, bounded exponential retries (maximum 5 attempts), durable report retry and a configurable retention cleanup for Watch alerts, defaulting to the message-archive retention policy.
- Added conservative local formal signals for invitation links/CTA, explicit phone/address patterns, multi-line flows, and exact repeated content after a reply-linked stop request; sensitive strings are masked in stored evidence and sensitive-context messages are excluded from the provider sample.
- Added local rolling-window reply/mention interaction aggregates and cross-batch accumulation of semantic signals when a target can be grounded through a reply link. Telegram identifiers remain local and are never sent to Groq.
- Added urgent/digest separation, a maximum of 3 urgent notifications per hour, deduplication by existing alert keys, bounded retry delivery (maximum 5 attempts), and configurable Watch retention with `/jf_ai_schedule retention N`.
- Added owner-confirmed Watch signal cleanup: `/jf_ai_watch_clear` previews scope; `/jf_ai_watch_clear confirm` removes only `jf_ai_watch_alerts` for the configured chat.
- SQLite migration is additive: 0 new tables, 14 columns and 3 indexes. No production DB migration, commit, push or deployment was performed.
- Verification is offline only. Remaining limitations: urgent technical health alerts are not implemented; semantic cross-batch accumulation is possible only when the target is grounded by a reply relationship; event participation is reported only where existing game tables record it. Live Telegram, Groq and Render remain untested.

## 0.13.2 — 2026-11-08

Решения владельца по аудиту и сопоставление с текстом правил (`RULES_JUSTICE_FAITE.txt`, `RULES_MAPPING_0.13.2.md`). Деплой не выполнялся.

- **4.5:** сигнал при *любом* четвёртом медиа после трёх медиа от трёх разных участников (раньше — только от нового участника).
- **4.3:** подтверждено — одинаковые действия считаются независимо от участников; прежнее условие для медиа 0.13.1 убрано.
- Служебные события Telegram (вход/выход, закрепление и т.п.) больше не считаются медиа и повторяющимися действиями.
- Контекстный анализ Watch дополнен пунктами 5.3 и 9.2.
- Тесты: решения R-1/R-2, служебные события. 32/32 модулей.

## 0.13.1 — 2026-11-08

Аудит и безопасные аддитивные исправления Justice AI / Watch. Деплой и GitHub не выполнялись. Подробности: `AUDIT_0.13.1.md`, `UPGRADE_0.13.1.md`.

- **Журнал правок сообщений.** Редактирование сообщения больше не уничтожает первоначальный текст: прежняя версия сохраняется в новой таблице `jf_chat_message_revisions` (аддитивная миграция `CREATE TABLE IF NOT EXISTS`). Версии удаляются вместе с сообщением по сроку хранения архива. Секреты по-прежнему маскируются до записи.
- **Новые owner-only команды (только личный чат владельца, проверка по Telegram ID):** `/jf_ai_evidence ID` — сообщение, все прежние версии и связанные сигналы; `/jf_ai_cmd КЛЮЧ` — диагностика команды из реестра (глобальный/категорийный/собственный переключатель, доступ, итог).
- **Самопроверка `/jf_ai_audit`** расширена: целостность SQLite, все таблицы Justice AI, пульс фонового цикла, отставание Watch от архива, недоставленные сигналы; статусы ok/warn/fail. Работает локально, без токенов.
- **Исправлено:** при включённом AI цикл Justice Watch всегда спал 60 секунд и игнорировал `JF_AI_WATCH_POLL_SECONDS`; теперь `min(60, настройка)`.
- **Исправлено правило 4.3:** одинаковые медиа от нескольких участников больше не считаются 4.3 (для них есть 4.5). Поведение для одинакового текста не менялось (вопрос R-1 в аудите).
- **Системный промпт Justice AI:** отвечать на конкретный вопрос вместо общего приветствия, учитывать адресата, различать шутку/спор/конфликт, не выдумывать команды и функции, признавать отсутствие информации, в играх разделять «Подтверждённо» и «Предположение».
- **Фоновые задачи** (`jf-ai-analytics`, `jf-timers`, `jf-role-sync`, `spy-recovery`) теперь удерживаются по ссылке и корректно отменяются при остановке.
- **Тесты:** 7 тестов в `test_ai_analytics.py` и `test_settings_panel.py` фактически не запускались из-за блока запуска посреди файла — исправлено; добавлен `test_justice_core.py`; `run_all.py` подключает офлайн-заглушки. 32/32 модулей. Живой Telegram, Groq и Render не проверялись.
- `render.yaml`: `ADMIN_ID=1682289834`.
- В `CHANGELOG` восстановлены пропущенные записи 0.11.5–0.13.0 (краткие, по `UPGRADE_*.md`).

## 0.13.0 — Justice Watch: контекстный анализ

Контекстная проверка текста пакетами через Groq (по умолчанию раз в 10 минут, от 12 сообщений, до 80 за запрос); сигналы только владельцу, без наказаний; Telegram ID и имена провайдеру не передаются. Подробности: `UPGRADE_0.13.0.md`.

## 0.12.0 — Justice AI: контекст основного чата

Контекст из существующего архива `jf_chat_message_log`: до 100 последних сообщений, до 15 релевантных старых, ответы на найденные; маскирование секретов; поиск с Unicode casefold. Подробности: `UPGRADE_0.12.0.md`.

## 0.11.5 — Justice Watch (локальный)

Локальные проверки правил 4.3 и 4.5 без токенов, архив сигналов `jf_ai_watch_alerts`, owner-only уведомления, самопроверка `/jf_ai_audit`, просмотр `/jf_ai_watch`. Подробности: `UPGRADE_0.11.5.md`.

## 0.11.4 — 2026-11-08

- Добавлен отдельный модуль аналитики Justice AI: периодические отчёты за последние 3 часа и ежедневные отчёты за 24 часа (по умолчанию 21:00 UTC+3).
- Отчёты отправляются только владельцу в личные сообщения; `/jf_ai_report` запускает отчёт вручную, `/jf_ai_reports` показывает архив, `/jf_ai_reports ID` открывает сохранённый отчёт.
- Аналитика использует существующий архив `jf_chat_message_log`, не добавляет второй сборщик сообщений и хранит до 1000 готовых отчётов в SQLite.
- Перед передачей провайдеру выборка ограничивается 100 уникальными сообщениями; из неё исключаются имена отправителей, username, Telegram ID и сообщения с распознаваемыми чувствительными темами, маскируются email, ссылки, номера телефонов и распространённые секреты.
- Улучшен системный промпт Justice: меньше выдуманных механик Genshin Impact; модель просит уточнять неопределённые игровые факты. Маркеры Markdown удаляются из ответов, чтобы в Telegram не отображались `**`, `*` и обратные кавычки.
- Добавлены тесты аналитики и очистки ответов. Проверка: compileall и 31/31 offline test modules. Живой запрос Groq, Telegram DM и Render deployment не проверялись.
- Миграция SQLite аддитивная: новые таблицы отчётов и состояния расписания; существующие данные не удаляются.

## 0.11.3 — 2026-11-08

- Переключён стандартный AI endpoint с OpenAI на Groq OpenAI-compatible Chat Completions; задана штатная модель `openai/gpt-oss-20b`. OpenAI API-ключ и платный OpenAI API больше не требуются для Justice AI. Из запроса удалён параметр `store`, который Groq не поддерживает.
- `/jf_ai` и подсказки Justice AI отправляются обычным текстом без видимых HTML-тегов; оболочки Telegram HTML удаляются и из ответа модели.
- Добавлены понятные ответы для ошибок провайдера: превышение лимита 429, неверный API-ключ 401/403 и недоступная модель/endpoint 404.
- Обновлены `.env.example`, README, справочник и инструкция перехода на Groq. База SQLite, экономика, роли и история модерации не меняются.
- Проверки: compileall, тест AI и полный offline suite; живой ключ Groq и Telegram API не проверяются офлайн.

## 0.11.2 — 2026-11-08

- Исправлена фактическая маршрутизация пересекающихся команд: `Топ богатых` → экономика, `Топ рейтинга` → репутация, `.отн история связи` → отношения; `Данет`, `Выбери`, `Инфа` и `Пург` работают с алиасами без обязательного `!`.
- Добавлен регрессионный dispatch-аудит всех текстовых политик каталога с алиасами; проверяется не только наличие записи в `/settings`, но и модуль, который реально получает сообщение.
- Личный фарм изменён на 5–50 жетонов раз в 4 часа; добавлен отдельный кулдаун в SQLite, идемпотентность по Telegram message ID и миграция старых ежедневных начислений без мгновенного повторного сбора.
- `.отн искры` теперь означает общий баланс пары; личные жетоны остаются в `Баланс` / `Кошелёк`, а `.отн искры подарок N` по-прежнему переводит личные жетоны.
- Добавлены общий баланс 100 искр при создании пары, 30 уровней, XP за действия и отдельный журнал искр. Старые пары получают журнал стартового баланса аддитивной миграцией; повторное создание той же пары не переиспользует ключ старой записи.
- Добавлена опциональная отправка изображения к нейтральному действию. Владелец настраивает Telegram `file_id` командой `/jf_social_media_set` ответом на фото; доступны `/jf_social_media_clear` и `/jf_social_media_list`. При ошибке Telegram-файла используется текстовый ответ.
- Каталог `/settings` расширен до 287 записей: 284 политики команд/кнопок и 3 политики автоматических функций.
- Проверены все 30 автономных тестовых модулей по отдельности; добавлены тесты маршрутизации реальных обработчиков, фарма, миграций пар, журнала искр и отправки фото. Compileall проходит. Реальный Telegram Bot API и Render не запускались.
- Все изменения SQLite аддитивные; ни существующие личные балансы, ни инвентарь, ни история модерации не сбрасываются.

## 0.11.1 — 2026-11-08

- Added an individually configurable access policy for `.отн история связи`.
- Completed the pet praise action aliases (`Питомец похвалить`, `.отн пт похвалить`) in handler, registry and tests.
- Implemented real public/personal gift showcases, separate from the item shop and inventory.
- Made pet interaction cooldown checks atomic in SQLite to prevent concurrent duplicate actions.
- Fixed command parsing for empty/non-text Telegram updates: `normalize_command_candidates()` now returns no match instead of raising `IndexError`.
- Moved the `test_cmdcfg.py` runner to the end of the module so all test functions, including later regression cases, actually execute.
- Central command registry: 282 entries (279 command/button policies + 3 automatic-feature policies).
- Re-ran compile checks; all 30 offline test modules pass individually with the project’s SQLite/aiogram stubs. The live Telegram API and Render were not tested.
- No destructive SQLite migration.


## 0.11.0 — Celestiana-safe слой и питомцы

- Добавлен безопасный набор дружеских связей: `.отн запрос`, `.отн да`, `.отн нет`, список, топ по длительности/активности, сертификат, статистика, история, контроль, расторжение и нейтральные действия. Связь добровольная; неизвестные username не угадываются; приглашения истекают через 7 дней.
- Добавлены 30 базовых и 30 дополнительных нейтральных действий с ответом на сообщение и антиспам-паузой.
- Добавлен виртуальный питомец: выбор, имя, взаимодействия, история и достижения. Добавлен личный дом с комнатами, оформлением и уровнями; траты проходят через единый SQLite-журнал экономики.
- Добавлены алиасы Celestiana для подарков, витрины, перевода жетонов, истории доходов/расходов и ежедневных заданий. Премиум-раздел информирует только о декоративных предметах; платные модерационные преимущества отсутствуют.
- Сохранены старые SQLite-данные; миграции аддитивные. Обновлён каталог команд и панель `/settings`: 281 запись (278 политик команд/кнопок и 3 автоматические функции).
- Проверки: compileall и 30 отдельных тестовых модулей; тесты используют SQLite и заглушки Telegram. Живой Telegram/Render не запускался.
- Не переносятся сексуальные/взрослые команды, сексуальный roleplay, беременность и биологическая симуляция детей. Inline Mode, Telegram Business, Iris market и закрытые глобальные сервисы Iris остаются отдельными интеграционными задачами.

## 0.10.0 — простая экономика, социальные связи и Justice AI

- Экономика упрощена: `Ферма` сразу начисляет 25 жетонов раз в календарный день (00:00 MSK), без культур, семян, ожидания, профессий и обучения. Старые `Работа`/`Собрать` — алиасы одной выплаты.
- Отображаемое название валюты заменено с «Искры» на «Жетоны» без изменения таблиц, числовых балансов, инвентаря и журнала SQLite. Стартовый бонус с уникальным ключом сохранён.
- Сохранены магазин, инвентарь, предметные подарки, переводы, достижения и защита от повторных начислений; добавлен дневной лимит переводов 500 жетонов.
- Добавлено взаимное напарничество: приглашение, принятие/отклонение, просмотр и отмена. Статус не романтический; согласие обязательно.
- Добавлен опциональный Justice AI: динамические ответы по внешнему API, только по обращению `Джастис, ...`, короткий RAM-контекст, сброс контекста и лимиты частоты. По умолчанию выключен; секреты не хранятся в коде.
- Обновлены каталог команд, меню, README, справочник, миграционная карта и тесты. Центральный реестр: 252 записи (249 политик команд/кнопок и 3 автоматические функции).
- Проверки: compileall и тестовый набор проекта. Живой Telegram/Render и подключение внешнего AI API не запускаются офлайн-тестами.

## 0.9.1 — продуманный старт экономики

- Добавлен единоразовый стартовый бонус 30 Искр с записью в SQLite-журнал и уникальным ключом, исключающим повторное начисление.
- Стартовый бонус автоматически доступен новым участникам и существующим пользователям 0.9.0 при первом обращении после обновления.
- `Работа` больше не заблокирована отсутствием профессии: стартовая смена приносит 15 Искр один раз в день; бесплатное обучение и профессия повышают ежедневную выплату.
- Обновлены справка и документация; добавлены тесты стартового бонуса, повторного вызова и работы без профессии.
- Проверки: `python -m compileall -q .`, `python tests/run_all.py`. Telegram/Render вживую не запускались.

## 0.9.0 — экономика, рекомендательные голосования и каталог

- Добавлена постоянная внутренняя валюта «Искры» с единым SQLite-журналом транзакций, защитой от отрицательного баланса и идемпотентностью повторных обновлений.
- Добавлены профессии, обучение, ежедневная работа, ферма с фиксированными сроками/наградами, магазин, инвентарь, подарки, переводы, задания и достижения.
- Добавлены рекомендательные голосования модерации с одним голосом на пользователя; автоматическая выдача наказаний по результату запрещена.
- Добавлен каталог кланов и кружков текущего чата.
- Добавлены команды в `/settings` и меню Telegram; всего 243 записи реестра (240 политик команд и 3 автоматические функции).
- Добавлены офлайн-тесты экономики и жизненного цикла голосования.
- Проверки: `python -m compileall -q .` и `python tests/run_all.py`. Реальный Telegram/Render не запускался.

# 0.8.5 — предсказуемые настройки команд и единая спецификация функций

- Исправлены индикаторы панели настроек: 🟢 означает, что команда реально доступна; ⚫ — выключена собственным переключателем; 🟡 — индивидуально включена, но приостановлена глобальным переключателем или разделом.
- Карточка команды отдельно показывает итоговое состояние и родительские ограничения. Сохранённые личные настройки команд не затираются при отключении глобального режима или раздела.
- Убрано повторное отображение параметра «Команда включена»: теперь он управляется только отдельной верхней кнопкой.
- Кнопки индивидуальных команд, разделов и глобального режима передают конкретную цель (включить/выключить), а не абстрактное «переключить». Повторный или задержанный callback не меняет состояние обратно. Старые toggle-callback без целевого значения безопасно отклоняются с предложением открыть свежую `/settings`.
- Булевы значения из SQLite читаются без зависимости от регистра и с распознаванием распространённых вариантов true/false.
- Неизвестная команда в прямой проверке политики теперь закрывается с причиной `unknown`, а не считается разрешённой.
- Добавлена единая функция `command_state()` для получения глобального, категорийного, локального и итогового состояния команды.
- Добавлены регрессионные тесты на трёхуровневые переключатели, сохранение локальных значений, отсутствие дублирующей кнопки и повтор callback.
- Добавлен `FEATURE_ARCHITECTURE_0.8.5.md`: идеи участников и Celestiana сведены в единую спецификацию с приоритетами, ограничениями, экономикой, реестром и этапами реализации. Новые игровые модули из документа пока не включены в рабочий код.
- Проверки: `python -m py_compile cmdcfg.py settings_panel.py`; `python tests/run_all.py` — все 25 автономных офлайн-наборов прошли. Это не заменяет тест в Telegram и на Render.

# 0.8.4 — маршрутизация личных команд владельца

- Шесть команд личной консоли (`/jf_console`, `/jf_console_refresh`, `/jf_memory`, `/jf_memory_save`, `/jf_memory_search`, `/jf_chatlog`) остаются доступны в ЛС владельца.
- Если владелец случайно отправляет одну из этих команд в основной флуд, бот пытается удалить исходное сообщение и выполняет команду с ответами строго в личном чате владельца.
- При отсутствии права удаления сообщение команды не выполняется; владелец получает предупреждение в ЛС.
- Текст этих команд не сохраняется в архив переписки флуда. Это особенно важно для содержимого `/jf_memory_save`.
- Консоль централизованно блокирует любую попытку отправить её карточки в чат, отличный от `ADMIN_ID`.
- Добавлены регрессионные тесты личной/групповой маршрутизации, удаления публичной команды и приватного адреса ответа.
- Офлайн-проверки не заменяют тест с живым Telegram Bot API и текущей конфигурацией Render.

# 0.8.3 — исправлена публикация карты ролей в общий чат

- Исправлена маршрутизация раздела «Роли»: карточка личной консоли отправляется в чат владельца (`ADMIN_ID`), а данные участников по-прежнему считываются из основного флуда.
- При первом обновлении после установки исправления бот пытается удалить старую карточку ролей, сохранённую как сообщение общего чата, чтобы она не оставалась в ленте.
- Добавлены регрессионные проверки адреса отправки и переноса сохранённой карточки из группы в личную консоль.
- Синхронизация ролей, история назначений и перелистывание карты не меняют поведение.

# 0.8.2 — восстановление SQLite на Render

- Исправлено восстановление резервной SQLite-базы из закреплённого сообщения Telegram на Render: файл сначала копируется во временный файл в каталоге целевой базы, проверяется через `PRAGMA integrity_check` и только затем устанавливается через `os.replace()` в пределах одной файловой системы.
- Перед восстановлением повреждённая локальная база по-прежнему сохраняется в `.corrupt.<timestamp>`; если её не удалось безопасно переместить, восстановление останавливается, не перезаписывая исходный файл. Оставшиеся `-wal` / `-shm` не переносятся на новую БД.
- Добавлена синхронизация файла и каталога после записи (где поддерживается) и очистка временного staging-файла при ошибке.
- Добавлен регрессионный тест, имитирующий `EXDEV` при попытке `os.replace()` между разными каталогами/файловыми системами.
- Проверки офлайн не заменяют запуск на реальном Render и проверку Telegram Bot API.

# 0.8.1 — inline-защита и многоуровневые переключатели

- Добавлена владельческая панель `/inline_guard`: выключено / выбранные inline-боты / все inline-боты; добавление и удаление usernames, настройка сохраняется в SQLite. Перед включением проверяется реальное право удаления, переход на режим всех ботов требует подтверждения, доступен журнал событий.
- Inline-сообщения определяются по Telegram `Message.via_bot`, а не по совпадениям текста. Обработчик зарегистрирован как ранний message outer middleware: совпавшее сообщение не попадает в статистику, архив, автомодерацию и игровые обработчики.
- Добавлен журнал с результатом удаления и ошибками Telegram; хранится не более 500 последних событий, без сохранения текста сообщений.
- В `/settings` добавлены глобальный переключатель пользовательских команд и переключатель каждого раздела. Отключение родительского уровня не перезаписывает персональные настройки команд; при обратном включении прежнее состояние восстанавливается.
- Отключение всех команд требует подтверждения. `/settings` остаётся доступным владельцу, чтобы не заблокировать возможность восстановления панели.
- Состояния глобального/категорийного управления и аудит изменений сохраняются в `cmd_feature_switches` / `cmd_settings_audit` и переживают повторное открытие SQLite.
- Добавлен тест реального дискового round-trip для настроек, тесты иерархии переключателей и middleware inline-сообщений, включая отказ Telegram удалить сообщение, подтверждение режима «все» и проверку прав удаления.
- Исправлена последовательность старта: наличие файла БД фиксируется до импорта модулей; регистрации модулей и очередь наград не создают таблицы при импорте. Схемы инициализируются только после проверки/восстановления базы. Добавлен регрессионный тест, подтверждающий, что импорт приложения не открывает и не перезаписывает повреждённую БД.
- Проверки: компиляция Python и все 25 офлайн-тестовых наборов. Тесты используют SQLite и заглушки aiogram; живой Telegram/Render запуск не выполнялся.

# 0.8.0 — целостный цикл «Шпиона» и живая карта ролей

- Исправлен критический разрыв финальной догадки: текст шпиона в фазах `GUESS` и `FINAL_GUESS` больше не отбрасывается ранним выходом из обработчика личных сообщений.
- Ответы «Да» и «Нет» сохраняются один раз, публикуются в общей группе вместе с исходным вопросом и подтверждаются в личке задавшему вопрос. Последний ответ также остаётся в основной игровой панели.
- Вопрос принимается только тогда, когда после удаления пробелов последний символ — `?`; пустые, слишком длинные и некорректные вопросы не создают ход.
- Игровая панель теперь восстанавливается новым сообщением, если Telegram удалил старое или его нельзя отредактировать. Это применяется к ходам, обсуждению, голосованию и финалу. При невозможности показать панель голосование не остаётся в невидимой фазе: игра возвращается к обсуждению.
- Голосование обновляет видимые кнопки и счётчики после каждого голоса; после завершения голосования шпион раскрывается, получает последнюю попытку, а игра атомарно фиксирует единственный финальный результат.
- Старт лобби защищён условным атомарным переходом. Если процесс перезапустился посреди приватной раздачи карточек, оставшаяся партия переводится в `ABORTED`, участники деактивируются, а повторная раздача той же партии не допускается.
- Исправлен расчёт числа полных кругов: для партий до пяти игроков сохраняются два круга, для более крупных — три; число кругов больше не подменяется количеством участников.
- Карта ролей в основной личной консоли обновляется вместе с отдельным снимком при `/syncroles`, назначении/привязке роли и смене статуса роли. Повторное редактирование неизменившегося снимка больше не создаёт новые сообщения.
- Блок «Роли» показывает количество активных участников, назначенных и нераспознанных ролей, размер каталога, свободные/занятые/ожидающие/зарезервированные роли, историю назначений, ожидающие username и время последней синхронизации.
- Проверка SQLite перед пропуском восстановления теперь проверяет целостность и наличие обязательных таблиц проекта. Повреждённая или частичная база не считается корректным постоянным хранилищем.
- Добавлены сквозные офлайн-тесты: полный цикл «вопрос → Да/Нет → обсуждение → кнопки голосования → раскрытие шпиона → правильная догадка → единственный финал», атомарный старт и отмена прерванной раздачи, а также отклонение неполной базы.
- Проверки выполняются на локальных SQLite/aiogram-заглушках. Живой Telegram/Render запуск не выполнялся.

# 0.7.6

- Исходящие ответы фиксируются на уровне session middleware aiogram: покрыты как `bot.send_message()`, так и `message.answer()` и другие отправки из обработчиков. Добавлена обработка текста/подписей при редактировании сообщений.
- Подпись закреплённой резервной копии показывает число сообщений флуда и технических записей; обе таблицы остаются внутри одной SQLite-базы.
- Срок хранения сообщений считается от первоначальной даты сообщения, а не от последней редакции.
- Усилено распознавание секретов: Bearer-токены, API-ключи, access/refresh-токены и секреты в URL не сохраняются в открытом виде.
- Добавлены отдельные проверки session middleware, сохранения ошибок в SQLite, счётчиков резервной копии и срока хранения.

# 0.7.5

- Добавлен архив переписки основного флуда в SQLite: входящие текстовые сообщения/подписи, исходящие ответы бота (включая `message.answer()` через middleware сессии aiogram), авторы, даты, reply-связи и обновления сообщений. Только одна настроенная группа; личные чаты не архивируются. Медиафайлы не скачиваются.
- Добавлено сохранение технических логов INFO и выше в SQLite; чат-архив и техлог включаются в закреплённый JF_DB_BACKUP_V1. Команды `/jf_chatlog` и `/jf_memory_search` показывают состояние и ищут по переписке/логам.
- Включены ограничения хранения: 30 дней и настраиваемые лимиты строк; чувствительные фрагменты скрываются, архив не влияет на обработку сообщений.
- Полностью отключено сопоставление дат рождения персонажей Genshin с ролью участника. При старте удаляются старые `community_seed`-записи; список показывает только даты из `jf_birthdays`, указанные самим участником или владельцем.

# 0.7.4

- Исправлено определение основного чата: при единственной привязке `managed_group` восстановленный ID используется как источник истины, если `PRIMARY_CHAT_ID` пустой или устаревший; консоль и функции сообщества получают тот же ID.
- Исправлена повторная обработка неизменившегося `sender_tag`: если тег уже сохранён, но роль ещё не записана, следующий апдейт теперь заполняет `role_key` и `role_name`.
- Награды за «олды» теперь нормализуют декоративные Unicode-имена, принимают URL-сущности и формируют минимальные двухстрочные команды без дополнительного текста.
- Для участника без `@username` создаётся HTML-упоминание `tg://user?id=…`, чтобы команда при пересылке сохраняла Telegram ID.

# Changelog

## 0.11.4 — 2026-11-08

- Добавлен отдельный модуль аналитики Justice AI: периодические отчёты за последние 3 часа и ежедневные отчёты за 24 часа (по умолчанию 21:00 UTC+3).
- Отчёты отправляются только владельцу в личные сообщения; `/jf_ai_report` запускает отчёт вручную, `/jf_ai_reports` показывает архив, `/jf_ai_reports ID` открывает сохранённый отчёт.
- Аналитика использует существующий архив `jf_chat_message_log`, не добавляет второй сборщик сообщений и хранит до 1000 готовых отчётов в SQLite.
- Перед передачей провайдеру выборка ограничивается 100 уникальными сообщениями; из неё исключаются имена отправителей, username, Telegram ID и сообщения с распознаваемыми чувствительными темами, маскируются email, ссылки, номера телефонов и распространённые секреты.
- Улучшен системный промпт Justice: меньше выдуманных механик Genshin Impact; модель просит уточнять неопределённые игровые факты. Маркеры Markdown удаляются из ответов, чтобы в Telegram не отображались `**`, `*` и обратные кавычки.
- Добавлены тесты аналитики и очистки ответов. Проверка: compileall и 31/31 offline test modules. Живой запрос Groq, Telegram DM и Render deployment не проверялись.
- Миграция SQLite аддитивная: новые таблицы отчётов и состояния расписания; существующие данные не удаляются.

## 0.7.3 — финальная сборка ветки 0.7

- Защита старта БД: отсутствующая/повреждённая база больше не заменяется пустой автоматически. Восстановление из закреплённой резервной копии выполняется до миграций; первый пустой запуск разрешается только отдельным временным флагом `JF_ALLOW_EMPTY_DB_BOOTSTRAP=true`. Добавлена регрессионная проверка всех вариантов startup policy.
- Единая конфигурация основной группы: владелец и личная консоль используют одно значение `PRIMARY_CHAT_ID`; запуск без `ADMIN_ID` или корректного отрицательного ID супергруппы останавливается с понятной ошибкой.
- Добавлен реальный фоновый цикл синхронизации известных Telegram ID каждые 300 секунд (настраиваемый минимум 300 секунд). ID, неизвестные Telegram, остаются в ожидающем списке — поиск ID по одному username Bot API не поддерживает.
- Объединён журнал предупреждений: старые записи `jf_warnings` переносятся в `jf_manager_warnings` без удаления исходной истории и без дублирования при повторном старте. Команды, менеджер и сводка читают общий журнал.
- Доведена игра «Шпион»: встроенные категории, несколько категорий на партию, пользовательские слова/алиасы/подсказки, выход из лобби и безопасное завершение партии, если не удалось доставить хотя бы одну секретную карточку. Добавлены регрессионные тесты новых пакетов и ошибки приватной доставки.
- Исправлена сводка предупреждений: теперь она учитывает единый журнал, а не только историческую таблицу.
- Обновлены инструкции первого запуска, переменные окружения и описание реальных ограничений резервирования/синхронизации.
- Полный офлайн-набор: `python3 -m compileall -q .` и `python3 tests/run_all.py` — 24 тестовых модуля проходят. Тесты используют SQLite и aiogram-заглушки; живой Telegram/Render запуск не выполнялся.

## 0.7.2 — самостоятельный рест, запись дня рождения и исправление карты ролей

- Добавлена команда участника `взять рест на 7 дней` / `/take_rest`: рест оформляется только на самого автора сообщения, срок по умолчанию ограничен 14 днями, максимум задаётся настройкой команды. Поддерживается и дата окончания (`до 15.11`). Повторное оформление продлевает действующий рест.
- Участник может сохранить дату рождения прямо в основном флуде сообщением `др 24.03` или `др 24 марта`; `др удалить` удаляет свою дату. Существующая личная команда `/jf_birthday` сохранена.
- Исправлена загрузка предварительного списка участников: известные `@username` теперь добавляются в базу при старте и отображаются как ожидающие привязки Telegram ID. Сравнение username нормализовано без учёта регистра.
- Карточка ролей теперь отдельно показывает участников с известным Telegram ID, участников без распознанной роли и предварительные username без ID. Добавлена кнопка «Синхронизировать известные ID», которая действительно запускает проверку известных ID, формирует отчёт и обновляет блок консоли.
- Добавлено явное объяснение ограничений Bot API: неизвестный Telegram ID нельзя получить по одному `@username`; автоматический сбор новых участников зависит от того, видит ли бот их сообщения.
- Исправлена проверка доступа в группе: реальное входящее сообщение считается подтверждением членства, даже если `getChatMember` недоступен обычному боту; Telegram-администратор при этом не определяется автоматически. В middleware ролей личность участника сохраняется до резервного запроса тега.
- Добавлены офлайн-проверки самостоятельного реста, ограничения его срока, записи/удаления дня рождения в группе, карточки предварительных участников и каталога команды.
- Обновлена версия личной консоли до 0.7.2.

Проверки: `python -m compileall -q .` и `python tests/run_all.py` — 22 тестовых модуля успешно. Реальный Telegram-токен и работающий Render-сервис не запускались.

## 0.7.1 — исправление разделения Telegram-администраторов и внутренних рангов

- Исправлена проверка доступа: уровень «администраторы Telegram» теперь определяется только фактическим статусом администратора/создателя в Telegram и не выдаётся автоматически участникам с внутренним рангом 3+.
- Проверка «с 1-го…5-го ранга» теперь читает только явно назначенный внутренний ранг Justice Faite из базы менеджера. Telegram-права модерации не повышают этот ранг.
- Добавлен отдельный метод `assigned_rank()`; прежний `Manager._rank()` сохранён для иерархии модерации и проверки прав на ресурсы.
- Добавлены регрессионные тесты на обе стороны: Telegram-администратор с низким внутренним рангом и обычный участник с высоким внутренним рангом.
- Устранено предупреждение о незапущенной coroutine при попытке обновить меню в синхронном офлайн-режиме.
- Проверены компиляция Python и все 22 офлайн-набора тестов. Живой запуск с Telegram API не выполнялся.

## 0.7.0 — единая гибкая система доступа команд

- Реестр расширен до 212 индивидуально настраиваемых командных политик и 3 автоматических функций; добавлены отдельные политики для действий кнопок административной панели.
- Любая зарегистрированная команда имеет индивидуальные настройки: включение, требуемый уровень доступа, место использования, область групп, паузу между вызовами, автоудаление исходного сообщения и собственные параметры.
- Отключённые команды молча игнорируются в группах и личных сообщениях; сообщения «команда отключена» не отправляются. Отключённые команды также исключаются из /help и меню Telegram.
- Разделены уровни Telegram-администратора и внутренние ранги Justice Faite. Доступ поддерживает всех пользователей, участников конкретного чата, Telegram-администраторов, внутренние ранги 1–5 и владельца бота.
- Персональные исключения поддерживают глобальное разрешение/запрет или правило для одной группы через формат `USER_ID@CHAT_ID`; список исключений разбит на страницы.
- Команды со сходной логикой теперь имеют отдельные настройки, включая включение и отключение ссылок, уведомлений и других режимов.
- Настройки inline-кнопок связаны с политиками соответствующих команд: голосование, мафия, шпион, роли, анонимная связь, жалобы, панель администрации, награды и консоль.
- Добавлена проверка членства в текущей группе (а не всегда только в основном чате), а паузы между вызовами учитываются отдельно для каждой группы.
- Обновление меню Telegram выполняется автоматически после изменения включения, уровня доступа или места/области работы команды.
- Добавлены тесты на полноту реестра и алиасов, тихое отключение, callback-действия, Telegram-администраторов и внутренние ранги, индивидуальные исключения, мультичат-проверку и независимые таймеры вызова.
- Полный офлайн-набор: 22 тестовых модуля пройдены; живая проверка с реальным токеном Telegram/Render в этой среде не выполнялась.

## 0.6.0 — консоль ролей и завершение оформления

- Карта ролей разбита на страницы по 20 участников; добавлены рабочие кнопки назад, вперёд и обновления.
- Добавлен отдельный офлайн-тест содержимого страниц, callback-кнопок и переходов между страницами.
- Поиск `/jf_memory_search` объединяет структурированную память проекта и архив личных сообщений владельца.
- Расширена стартовая память по разделам Iris, тематическим событиям и архитектуре конечного автомата игры «Шпион».
- Обновлена справка по личной консоли и обработке Telegram-тегов.
- Все 20 офлайн-наборов тестов проходят; это не заменяет живую проверку бота в Telegram и на Render.

## 0.5.0 — память владельца, карта ролей и резервное восстановление

- Переработана личная консоль: пять редактируемых блоков — система, игровые сессии, модули, карта ролей и память проекта.
- Добавлен журнал личных текстовых сообщений и подписей владельца с фильтрацией команд и очевидных шаблонов секретов; добавлен поиск `/jf_memory_search`.
- Добавлены команды `/jf_console_refresh` и `/jf_memory_search` в меню владельца Telegram.
- Роли участников читаются напрямую из `Message.sender_tag`; при отсутствии поля используется ограниченный по частоте `getChatMember`.
- `/roles_audit` исключена из пользовательского интерфейса: карта ролей показывается в личной консоли.
- Резервная копия полной общей SQLite-базы отправляется в ЛС владельца и обновляется в одном закреплённом документе раз в 5 минут; при старте бот проверяет целостность и восстанавливает базу из актуального закрепа.
- Если локальная БД повреждена и восстановление не удалось, файл перемещается в `.corrupt.<UTC timestamp>`, чтобы не блокировать начальную инициализацию.
- Добавлены проверки журнала владельца, секрет-фильтра, сборки ролей из sender_tag, fallback-получения тега, ротации закреплённой резервной копии и восстановления БД.
- Обновлён общий `ui.py`: декоративные линии убраны из карточек и разделителей, заголовки и пункты оформлены эмодзи; добавлен `test_ui.py`.
- Все 20 офлайн-наборов тестов проходят; реальная проверка токена, хостинга и живой группы Telegram не выполнялась.

## 0.4.0 — личная консоль и расширение «Шпиона»

- Добавлен `jf_console.py`: закрытая консоль владельца в ЛС с отдельными редактируемыми блоками состояния, игровых сессий, модулей и памяти проекта.
- Добавлена таблица `jf_project_memory` для сохранения требований и проектных решений в общей SQLite-базе; таблица `jf_console_messages` хранит ID сообщений панели.
- Добавлены команды владельца `/jf_console`, `/jf_console_refresh`, `/jf_memory`, `/jf_memory_save`; доступ ограничен личным чатом и `ADMIN_ID`.
- Добавлено периодическое обновление открытой панели раз в 5 минут. Telegram-сообщения остаются отображением БД, а не самостоятельным источником истины.
- Максимум участников «Шпиона» увеличен до 15; добавлена настройка 1–3 шпионов и проверка, чтобы в игре оставалось не менее двух мирных участников.
- Добавлены отдельные офлайн-тесты консоли: запись памяти, индекс сообщений, проверка доступа и редактирование существующих блоков вместо повторной отправки.


## 0.1.0 — Justice Faite Manager core

- Добавлен самостоятельный модуль `jf_manager.py` с отдельными таблицами `jf_manager_*`.
- Добавлены внутренние ранги 0–5, назначение/снятие, повышение/понижение, список модерации и журнал действий.
- Добавлены бан/разбан, кик/тихий кик, мут/размут, предупреждения и автоматический бан по лимиту.
- Добавлены списки банов/мутов/предупреждений и просмотр причины наказания.
- Добавлены настройки сроков и лимита предупреждений.
- Добавлены ранговые настройки доступа к командам и журнал изменений доступа.
- Сохранена отдельная legacy-система `/jf_warn`, `/jf_warnings`, `/jf_warn_remove`; её данные не мигрируются и не перезаписываются.
- Исправлен разбор префиксов `Ирис` / `Ириска`.
- Добавлены регрессионные тесты на синтаксис рангов, тихий кик, удаление конкретного предупреждения и маршрутизацию старых команд.

## Предшествующие версии

См. историю изменений в архиве проекта и `CODE_REVIEW.md`.

## 0.2.0 — 2026-11-03

- Добавлены `jf_community.py`: кланы, кружки, заметки, чатбук и закладки.
- Добавлен `jf_chatconfig.py`: правила, название/описание, закреп, собственная invite-ссылка и фильтры ссылок с белым списком.
- Добавлен `jf_stats.py`: сохранённая посуточная/почасовая статистика сообщений, топы, личная статистика, олды/новички/неактивность.
- Добавлены slash-справки `/jf_community`, `/jf_chatconfig`, `/jf_stats`.
- Схемы новых модулей изолированы отдельными таблицами `jf_*`; существующие данные не перезаписываются.
- Добавлены офлайн-тесты `test_community.py`, `test_chatconfig.py`, `test_stats.py`.

## 0.3.0 — 2026-11-03

- Добавлены профили участников (`jf_profiles.py`) с добровольными полями и приватностью.
- Добавлены положительная репутация (`jf_reputation.py`) и система жалоб (`jf_reports.py`).
- Добавлены сохраняемые таймеры-напоминания (`jf_timers.py`) с безопасным текстовым исполнением.
- Добавлены текстовые триггеры автоматической модерации (`jf_automod.py`) с лимитами и паузами.
- Добавлены безопасные случайные команды (`jf_fun.py`), сетка чатов (`jf_network.py`) и бесплатные розыгрыши (`jf_raffles.py`).
- Добавлена ограниченная очистка известных сообщений (`jf_clean.py`) с учётом срока хранения Telegram.
- Добавлены пользовательские названия рангов и приветствия/уведомления входа и выхода.
- Добавлены офлайн-тесты новых модулей; полный набор тестов выполняется на SQLite и заглушках Telegram.


## 0.12.0 — Justice AI conversation context

- Added contextual retrieval from the existing main-flood archive for explicit Justice AI mentions.
- Includes recent messages, relevant older snippets, participant display names and direct reply relationships.
- Added Unicode-aware Russian text matching and context redaction for common secrets.
- Reuses the existing archive; no second collector or duplicate raw-message store.


## 0.13.0 — Justice Watch contextual review

- Added bounded semantic review of newly archived chat messages for contextual signals involving harassment, repeated provocation, public conflicts, discrimination, privacy, advertising, punishment-baiting, and administration-related conduct.
- Groq receives anonymized participant aliases and local message labels only; Telegram user IDs and message IDs remain local and are attached only to owner-facing evidence.
- Semantic checks run in batches (default: every 10 minutes, at least 12 messages, up to 80 messages per request), not on every message.
- Deterministic rules 4.3 and 4.5 remain local and do not consume AI tokens.
- All semantic signals are review requests, not verdicts or automatic moderation. Ambiguous/discretionary cases remain for the owner/administration.
- Added separate semantic scan cursor and retry backoff so provider failures do not discard the pending batch or cause rapid retry loops.

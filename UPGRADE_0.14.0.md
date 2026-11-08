# Обновление 0.14.0

## Статус

Локальная сборка этапа 2. Не развёрнута. Перед любым использованием на сервере сделайте резервную копию SQLite и отдельно проверьте изменения.

## Изменения БД

Новых таблиц нет. Аддитивно добавляются 5 колонок в `jf_ai_reports`: `delivery_status`, `delivery_attempts`, `next_delivery_at`, `last_delivery_error`, `delivered_at`. Старые отчёты получают `delivery_status='sent'`, чтобы исключить повторную отправку.

В `jf_ai_watch_alerts` добавляются 9 колонок: `target_user_id`, `repeat_count`, `last_seen_at`, `severity`, `confidence`, `notification_level`, `delivery_attempts`, `next_delivery_at`, `last_delivery_error`. Создаются три индекса для доставки и выборки сигналов. Все изменения выполняются проверкой `PRAGMA table_info` и `CREATE INDEX IF NOT EXISTS`.

В `jf_ai_analytics_state` используются существующие ключи-значения; новая таблица не создаётся.

## Настройки владельца

- `/jf_ai_schedule` — просмотр текущих настроек.
- `/jf_ai_schedule interval 1..12` — интервал отчётов в часах, по умолчанию 3.
- `/jf_ai_schedule daily 0..23` — час ежедневного отчёта по МСК, по умолчанию 21.
- `/jf_ai_schedule retention 1..365` — срок хранения Watch, по умолчанию равен `JF_CHAT_LOG_RETENTION_DAYS` (30 дней при стандартном env).
- `/jf_ai_watch_clear` — запрос подтверждения очистки только архива сигналов Watch.
- `/jf_ai_watch_clear confirm` — подтверждённая очистка `jf_ai_watch_alerts` текущего чата. Архив сообщений и журнал правок не затрагиваются.

## Новые настройки БД

`periodic_hours`, `daily_hour`, `watch_retention_days`, `urgent_hour_bucket`, `urgent_hour_count`, `next_periodic`, `next_daily` и существующие курсоры Watch хранятся в `jf_ai_analytics_state`.

## Переменные Render

Новые обязательные env-переменные не добавлены. Используются существующие: `ADMIN_ID`, `DB_PATH`, `JF_AI_ENABLED`, `JF_AI_ANALYTICS_ENABLED`, `JF_AI_ANALYTICS_INTERVAL_HOURS`, `JF_AI_ANALYTICS_DAILY_HOUR`, `JF_AI_ANALYTICS_UTC_OFFSET_HOURS`, `JF_CHAT_LOG_RETENTION_DAYS`, `JF_AI_API_KEY`, `JF_AI_BASE_URL`, `JF_AI_MODEL`, `JF_AI_WATCH_ENABLED`, `JF_AI_WATCH_POLL_SECONDS`, `JF_AI_WATCH_SEMANTIC_ENABLED`, `JF_AI_WATCH_SEMANTIC_INTERVAL_SECONDS`, `JF_AI_WATCH_SEMANTIC_MIN_MESSAGES`, `JF_AI_WATCH_SEMANTIC_BATCH_SIZE`, `JF_AI_TIMEOUT`.

## Обновление

1. Остановить приложение и сделать резервную копию БД.
2. Развернуть файлы только после отдельного решения владельца.
3. При запуске `JusticeAIAnalytics.init()` выполнит только аддитивные изменения схемы.
4. Проверить расписание, ручной отчёт, архив, Watch и аудит в личном чате владельца.
5. Не использовать эфемерное хранилище Render Free для постоянной SQLite без отдельного решения по storage.

## Ограничения

Срочные уведомления о технических сбоях/потере доступа к БД не входят в текущую реализацию. Семантическое накопление по адресату работает только при наличии reply-связи. Участие в мероприятиях отражается только там, где оно фиксируется существующими игровыми таблицами. Живые Telegram, Groq и Render не проверялись.

# Инструкция обновления до 0.15.0

0.15.0 — локальная сборка поверх 0.14.1. Главная цель этапа — снизить число ложных/частых owner DM от Watch, сохранить доказательства, добавить плановый Self-Audit и улучшить фактическую статистику игровых сессий.

1. Сделайте резервную копию `users.db`, проверьте, что копия открывается SQLite и содержит основные таблицы. Не обновляйте единственный экземпляр production-БД.
2. Распакуйте архив 0.15.0 в отдельную директорию. Не накладывайте файлы на запущенный сервис.
3. Проверьте `BOT_TOKEN`, `ADMIN_ID`, `PRIMARY_CHAT_ID`, `DB_PATH`, `JF_AI_ENABLED`, `JF_AI_ANALYTICS_ENABLED`, `JF_AI_WATCH_ENABLED`, `JF_AI_API_KEY`, `JF_AI_BASE_URL`, `JF_AI_MODEL`.
4. По умолчанию Watch опрашивает архив каждые 120 секунд, семантически анализирует пакет раз в 30 минут/от 20 сообщений и отправляет обычную сводку раз в 24 часа. Изменить период сводки можно через `/jf_ai_schedule watch 1..24`.
5. Новые настройки: `JF_AI_WATCH_DIGEST_INTERVAL_HOURS=24`, `JF_AI_WATCH_URGENT_COOLDOWN_HOURS=12`, `JF_AI_WATCH_MAX_DIGEST_ITEMS=8`, `JF_AI_SELF_AUDIT_ENABLED=true`, `JF_AI_SELF_AUDIT_INTERVAL_HOURS=24`.
6. При первом `init()` новой версии старые pending Watch alerts становятся `silent`, но сами записи и evidence остаются. Это предотвращает отправку старого накопленного спама. Новые сигналы обрабатываются по новой политике.
7. Личная память владельца и память основного чата из 0.14.1 сохраняются отдельно; никакие записи памяти не переносятся между этими областями.
8. Запустите `python3 -m compileall -q .`, `python3 tests/run_all.py`, затем проверьте `PRAGMA integrity_check` и количество строк важных таблиц на копии БД.
9. Если используете Render Free, не включайте `JF_STORAGE_DURABLE=true`: Free web services не поддерживают persistent disk. Инструкция для платного persistent disk находится в `RENDER_PERSISTENCE_0.15.0.md`.
10. Не выполняйте production-миграцию, commit, push или deployment без отдельного решения владельца. Эта сборка сама ничего из этого не делает.

## Изменения SQLite

Новые таблицы и новые столбцы для 0.15.0 не добавляются. Используются уже имеющиеся таблицы `jf_ai_analytics_state`, `jf_ai_watch_alerts`, `spy_games`, `spy_players`, `mafia_games`, `mafia_players`. Миграция меняет только notification policy у уже существующих незавершённых Watch-записей, сохраняя их evidence. Записей пользователей, чатов, ролей, наказаний и истории сообщений она не удаляет.

## Проверка и ограничения

Offline regression tests/compile/import only. Live Telegram API, Groq, paid Render disk attachment, production backup restore, production traffic and disk durability have not been tested.

# Инструкция обновления до 0.14.1

0.14.1 — кумулятивная локальная сборка поверх исходной 0.13.2: включает изменения Advisor/Watch из 0.14.0 и новые контекст/память Justice AI из 0.14.1.

1. Сохраните резервную копию SQLite и исходный архив 0.13.2. Не используйте для обновления единственный экземпляр production-БД.
2. Распакуйте архив 0.14.1 в отдельную директорию; не накладывайте файлы на работающий production-сервис.
3. Укажите `DB_PATH` на копию исходной БД и проверьте `ADMIN_ID` по настоящему Telegram ID владельца и `PRIMARY_CHAT_ID`.
4. Проверьте базовые настройки: `JF_AI_ENABLED`, `JF_AI_ANALYTICS_ENABLED`, `JF_AI_WATCH_ENABLED`, `JF_AI_API_KEY`, `JF_AI_BASE_URL`, `JF_AI_MODEL`, `ADMIN_ID`, `PRIMARY_CHAT_ID`, `DB_PATH`, `JF_CHAT_LOG_RETENTION_DAYS`.
5. Параметры памяти: `JF_AI_FLOOD_MEMORY_ENABLED=true`, `JF_AI_FLOOD_MEMORY_INTERVAL_HOURS=24`, `JF_AI_FLOOD_MEMORY_MIN_MESSAGES=50`, `JF_AI_OWNER_MEMORY_ENABLED=true`, `JF_AI_OWNER_MEMORY_INTERVAL_HOURS=24`, `JF_AI_OWNER_MEMORY_MIN_MESSAGES=12`. Их можно отключить/изменить через env. Интервалы памяти ограничены диапазоном 12–168 часов.
6. Запустите `python3 tests/run_all.py`, `python3 -m compileall -q .` и импорт-проверку. Проверьте `PRAGMA integrity_check` на копии базы и сравните количество строк архива до/после миграции.
7. Только после успешного локального тестирования отдельно принимайте решение о production-обновлении. Этот архив сам по себе не выполняет миграцию, commit, push или deployment.

## Кумулятивная схема с 0.13.2

Аддитивная миграция включает:

- 2 новые таблицы: `jf_ai_owner_memory`, `jf_ai_flood_memory`;
- 17 новых колонок суммарно: 5 в `jf_ai_reports`, 9 в `jf_ai_watch_alerts`, 1 в `jf_owner_message_log`, 2 в `jf_chat_message_log`;
- 5 индексов: 3 для доставки/дедупликации 0.14.0 и по одному индексу для каждой таблицы памяти;
- ключи расписания, доставки, хранения Watch и курсоры памяти в уже существующей `jf_ai_analytics_state`.

Добавление колонок выполняется после проверки `PRAGMA table_info`; новые таблицы и индексы создаются через `CREATE ... IF NOT EXISTS`. История сообщений, первоначальные версии правок и личный журнал не очищаются. Новый сборщик сообщений или дополнительная фоновая задача не создаются: память компактизируется внутри существующего worker `jf-ai-analytics`.

## Ограничения

Это не production-релиз и не гарантия работы живой среды. Telegram Bot API, Groq, Render disk persistence, сетевые ошибки и рабочая нагрузка production в этой среде не проверялись. Перед любой production-миграцией нужна отдельная проверка копии и резервная копия.

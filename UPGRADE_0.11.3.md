# Обновление Justice Faite Manager до 0.11.3

1. Сделай актуальную резервную копию SQLite перед заменой исходников. Архив с кодом не содержит твою рабочую базу.
2. Замени файлы проекта содержимым архива `Justice-Faite-Manager-v0.11.3.zip`. Не удаляй `users.db`/файл из `DB_PATH`, `.env`, Render Environment Variables или Persistent Disk.
3. В Render → `anonymous-flood-bot` → Environment добавь/обнови:
   - `JF_AI_ENABLED` = `true`
   - `JF_AI_API_KEY` = твой секретный ключ из Groq Console
   - `JF_AI_BASE_URL` = `https://api.groq.com/openai/v1/chat/completions`
   - `JF_AI_MODEL` = `openai/gpt-oss-20b`
4. Не добавляй API key в GitHub, `.env.example`, сообщения Telegram или чат с помощником. Храни его только в Render Environment Variables.
5. После сохранения переменных Render должен выполнить redeploy. Затем в группе проверь `/jf_ai`, `Джастис, привет` и `/jf_ai_reset`.
6. На бесплатном тарифе Groq действуют квоты и ограничения скорости. При HTTP 429 бот сообщит, что провайдер ограничил запросы; проверь актуальные лимиты в Groq Console.
7. Проверки в архиве выполняются без живого Telegram/Groq API. Успешные офлайн-тесты не подтверждают наличие действующего ключа.

Версия не меняет SQLite-схему, балансы, предметы, роли или историю модерации.

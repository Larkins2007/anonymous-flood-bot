# Smoke test

## 1. Проверка Python

```bash
python -m py_compile bot.py
```

## 2. Тесты

```bash
pytest -q test_delivery_recovery.py
```

## 3. Группа

Проверить:

```text
/help@justice_faite_bot
/roles@justice_faite_bot
/free_roles@justice_faite_bot
/all_roles@justice_faite_bot
/mafia@justice_faite_bot
```

## 4. Роль нового участника

После входа нового участника проверить:

```text
калл <роль>
```

Также проверить ответом на сообщение участника и вариантом `калл @username <роль>`.

## 5. Mafia

```text
/mafia
```

Собрать минимум 5 участников. После этого нажать:

```text
▶️ Запустить MafiaAzBot
```

Бот должен отправить:

```text
/start@MafiaAzBot
```

Наш бот не должен раздавать собственные игровые роли.

## 6. Health

```bash
curl -fsS http://127.0.0.1:10000/health
```

Ожидаются поля:

- `polling_running`
- `db_ok`
- `fatal_error`

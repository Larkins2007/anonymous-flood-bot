# Test Results — Justice Faite 0.26.2

**Дата:** 2026-11-14  
**Среда:** Python 3.13, aiohttp 3.13.3. Пакет `aiogram` отсутствовал; сетевой доступ к PyPI не предоставлялся.

## Результаты

| Проверка | Результат | Примечание |
|---|---|---|
| AST parse + `compile()` всех корневых Python-файлов | PASS | 39 файлов |
| `python tests_release.py` | PASS | Версия, обязательные файлы, порядок старта, настройки Render, артефакты |
| `python tests_bridge.py` | PASS | Unit + HTTP protocol tests на aiohttp TestServer, включая worker signature compatibility |
| HMAC: корректная подпись | PASS | Принимается |
| HMAC: неверный формат/значение | PASS | Отклоняется |
| Worker HMAC → bridge verification | PASS | Подпись из `jf_ai_worker.signed_headers()` принимается bridge |
| Nonce replay | PASS | Повтор отклоняется |
| Timestamp window | PASS | Просроченная подпись отклоняется |
| `generate()` timeout | PASS | `asyncio.TimeoutError` |
| Queue full | PASS | Запрос отклоняется контролируемой ошибкой |
| Bridge not-ready | PASS | Генерация не запускается |
| HTTP poll → result | PASS | Ответ возвращается ожидающему `generate()` |
| Некорректный JSON / JSON не-объект | PASS | HTTP 400 |
| Тело запроса больше лимита | PASS | HTTP 413 |
| AI disabled by default | PASS | `JF_AI_ENABLED=false` в Render config и default в AI-модуле |
| Внешний AI endpoint в активных Python-модулях | PASS | Статический поиск не обнаружил вызовов |
| Импорт и запуск полного `bot.py` с aiogram | NOT RUN | `aiogram` отсутствовал; установка невозможна без сети |
| Живой Telegram / Render / Termux / Qwen E2E | NOT RUN | Требует credentials и работающего сервиса/устройства |

## Команды, воспроизводящие проверки

```bash
PYTHONDONTWRITEBYTECODE=1 python tests_release.py
PYTHONDONTWRITEBYTECODE=1 python tests_bridge.py
```

Для синтаксической проверки всех модулей:

```bash
python - <<'PY'
from pathlib import Path
import ast
files = sorted(Path('.').glob('*.py'))
for path in files:
    source = path.read_text(encoding='utf-8')
    compile(source, str(path), 'exec')
    ast.parse(source, filename=str(path))
print(f'PASS: {len(files)} Python files')
PY
```

## Важные оговорки

- В ZIP отсутствует историческая папка `tests/` и runner `tests/run_all.py`. Этот релиз не утверждает, что прежние 30+ регрессионных наборов входят в архив или были повторно запущены.
- Успешные HTTP-тесты используют aiohttp TestServer локально. Они не являются тестом сетевого маршрута через реальный Render.
- Заявленные ранее 3.6–4 токена/сек на планшете не измерялись повторно при этой проверке.
- Не включайте `JF_AI_ENABLED=true`, пока worker не подключился к deployed bridge и не вернул успешный ответ от работающей Qwen.

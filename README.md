# Anonymous Feedback Bot — Final V4

Финальная версия для Render Web Service.

Функции:
- единый эстетичный интерфейс;
- анонимная обратная связь для других участников;
- администрация видит отправителя;
- ответы администратора через бота;
- регистрация пользователей по /start;
- список, поиск, карточка, блокировка/разблокировка;
- статистика;
- жалобы с предупреждением, что заявитель виден администрации;
- одна жалоба на одного пользователя с одного аккаунта;
- cooldown жалоб;
- подтверждение жалобы;
- интерактивные справочные экраны;
- админская рассылка;
- SQLite.

Важно: BOT_TOKEN хранится только в Render Environment Variables. ADMIN_ID уже задан в bot.py. WEBHOOK_SECRET не нужен.

Render:
Build Command: pip install -r requirements.txt
Start Command: python bot.py
Instance Type: Free

WEBHOOK_SECRET убран, потому что предыдущая версия отклоняла webhook-запросы Telegram с 401 Unauthorized.

"""Реестр всех команд и функций бота вместе с их персональными настройками.

Здесь только данные. Логика проверки доступа, хранение и панель настроек
находятся в `cmdcfg.py` и `settings_panel.py`.
"""
from __future__ import annotations

from cmdcfg import Param, Spec, register

DEFAULT_WELCOME = (
    "Добро пожаловать, {name}!\n\n"
    "✦ Рады видеть тебя в Justice Faite.\n"
    "✦ Правила флуда доступны в информационном канале.\n"
    "✦ По важным вопросам используй обратную связь бота."
)
DEFAULT_BIRTHDAY_TEXT = "🎂 Сегодня день рождения у {name}!\n\n✦ Поздравляем от Justice Faite ♡"
DEFAULT_REST_END_TEXT = "🥹 {name} снова свободен от реста."
DEFAULT_AWARD_DESC = "{title}. Участник находится во флуде {age}."


def _tier(n: int, days: int, level: int, title: str) -> tuple:
    return (
        Param(f"t{n}_days", f"Порог {n}: дней в чате", "int", days, "Сколько дней участник должен быть во флуде.",
              min=1, max=3650, step=5, unit="дн", presets=(days,)),
        Param(f"t{n}_level", f"Порог {n}: степень награды", "int", level, "Номер степени, который пойдёт в команду Ирис.",
              min=1, max=8, step=1),
        Param(f"t{n}_title", f"Порог {n}: название", "text", title, "Первая часть описания награды.", max_len=120),
    )


def load() -> None:
    S = register

    # ------------------------------------------------------------------ community
    S(Spec(
        "start", "Главное меню", "🏠", "community",
        "Стартовый экран бота в личных сообщениях.",
        triggers=("/start",), access="all", access_options=("all", "members"),
        where="private", where_options=("private",), cooldown=False, autodelete=False,
        menu=(("start", "🏠 Главное меню"),),
        params=(
            Param("show_anon", "Кнопка «Анонимная связь»", "bool", True),
            Param("show_report", "Кнопка «Жалоба»", "bool", True),
            Param("home_note", "Дополнительная строка на экране", "text", "",
                  "Показывается под приветствием. Можно оставить пустой.", max_len=300, allow_empty=True),
        ),
    ))
    S(Spec(
        "help", "Справка", "✦", "community",
        "Список команд, доступных именно этому человеку в этом чате.",
        triggers=("/help",), access="all", where="both",
        menu=(("help", "✦ Команды и навигация"),),
        params=(
            Param("show_custom", "Показывать свои команды", "bool", True),
            Param("compact", "Компактный вид (без описаний)", "bool", False),
        ),
    ))
    S(Spec(
        "birthday", "Мой день рождения", "🎂", "community",
        "Участник сохраняет свою дату (ДД.ММ). Владелец может задать дату любому: /jf_birthday @user 24.03",
        triggers=("/jf_birthday",), access="members", access_options=("owner", "members"),
        where="private", where_options=("private",),
        menu=(("jf_birthday", "🎂 Сохранить день рождения"),),
    ))
    S(Spec(
        "birthdays", "Дни рождения", "🎉", "community",
        "Ближайшие дни рождения участников флуда.",
        triggers=("/jf_birthdays",), access="members", access_options=("owner", "members"),
        where="private", where_options=("private",),
        menu=(("jf_birthdays", "🎉 Ближайшие дни рождения"),),
        params=(
            Param("limit", "Сколько показывать", "int", 30, min=5, max=100, step=5, presets=(10, 30, 50)),
            Param("window_days", "Только ближайшие N дней (0 — все)", "int", 0, min=0, max=366, step=10, unit="дн",
                  presets=(0, 14, 30, 60)),
        ),
    ))
    S(Spec(
        "birthday_greet", "Поздравления во флуде", "🎈", "community",
        "Бот сам поздравляет именинников в основном чате.",
        where="group", where_options=("group",), access="owner", access_options=("owner",),
        cooldown=False, autodelete=False, feature=True,
        params=(
            Param("hour", "Во сколько поздравлять", "hour", 10, "Местное время бота (см. DEFAULT_TIMEZONE_OFFSET_HOURS)."),
            Param("text", "Текст поздравления", "text", DEFAULT_BIRTHDAY_TEXT,
                  "Доступно: {name} — имя или @username.", max_len=600),
        ),
    ))
    S(Spec(
        "dashboard", "Сводка", "📊", "community",
        "Общая картина по флуду: участники, рест, предупреждения, награды.",
        triggers=("/jf_dashboard",), access="owner", access_options=("owner", "admins"), where="both",
        menu=(("jf_dashboard", "📊 Сводка Justice Faite"),),
        params=(
            Param("show_warnings", "Блок предупреждений", "bool", True),
            Param("show_awards", "Блок наград", "bool", True),
            Param("show_birthdays", "Блок дней рождения", "bool", True),
        ),
    ))
    S(Spec(
        "rest", "Рест", "🥹", "community",
        "Оформить или продлить рест. «выдать рест @user 15.09», «выдать рест @user на 7 дней», ответом на сообщение.",
        triggers=("выдать рест @user 15 09", "/rest @user 15.09"),
        access="owner", access_options=("owner", "admins"), where="both",
        menu=(("rest", "🥹 Оформить или продлить рест"),),
        params=(
            Param("max_days", "Максимальный срок реста", "int", 90, min=1, max=365, step=5, unit="дн", presets=(30, 60, 90, 180)),
            Param("announce", "Объявлять во флуде, если выдан из ЛС", "bool", False),
            Param("notify_end", "Сообщать об окончании реста во флуде", "bool", True),
            Param("end_text", "Текст об окончании", "text", DEFAULT_REST_END_TEXT, "Доступно: {name}.", max_len=300),
        ),
    ))
    S(Spec(
        "unrest", "Снять рест", "↩️", "community",
        "Досрочно завершить рест: «снять рест @user» или /unrest @user.",
        triggers=("снять рест @user", "/unrest @user"),
        access="owner", access_options=("owner", "admins"), where="both",
        menu=(("unrest", "↩️ Снять рест"),),
    ))
    S(Spec(
        "restlist", "Рестники", "📋", "community",
        "Кто сейчас на ресте и до какого числа.",
        triggers=("рестники", "/jf_restlist"), access="members", access_options=("owner", "admins", "members", "all"),
        where="both", cooldown_default=15,
        menu=(("jf_restlist", "📋 Кто на ресте"),),
        params=(
            Param("show_reason", "Показывать заметки к ресту", "bool", True),
        ),
    ))

    # ------------------------------------------------------------------ moderation
    S(Spec(
        "warn", "Предупреждение", "⚠️", "moderation",
        "Выдать предупреждение: /jf_warn @user причина — или ответом на сообщение.",
        triggers=("/jf_warn @user причина",), access="owner", access_options=("owner", "admins"), where="both",
        menu=(("jf_warn", "⚠️ Выдать предупреждение"),),
        params=(
            Param("l1_days", "Через сколько дней можно снять 1-е", "int", 14, min=1, max=365, step=1, unit="дн", presets=(7, 14, 30)),
            Param("l2_days", "Через сколько дней можно снять 2-е", "int", 30, min=1, max=365, step=5, unit="дн", presets=(14, 30, 60)),
            Param("announce", "Сообщать о предупреждении во флуде", "bool", False),
            Param("notify_user", "Писать участнику в ЛС (если он открыл бота)", "bool", False),
        ),
    ))
    S(Spec(
        "warnings", "История предупреждений", "📜", "moderation",
        "История предупреждений. Участник видит свои, чужие — только администрация.",
        triggers=("/jf_warnings @user",), access="members", access_options=("owner", "admins", "members"), where="both",
        menu=(("jf_warnings", "📜 История предупреждений"),),
        params=(
            Param("limit", "Сколько записей показывать", "int", 20, min=5, max=50, step=5, presets=(10, 20, 50)),
        ),
    ))
    S(Spec(
        "warn_remove", "Снять предупреждение", "↩️", "moderation",
        "Снять предупреждение по номеру: /jf_warn_remove ID.",
        triggers=("/jf_warn_remove ID",), access="owner", access_options=("owner", "admins"), where="both",
        menu=(("jf_warn_remove", "↩️ Снять предупреждение"),),
        params=(
            Param("enforce_wait", "Соблюдать срок ожидания", "bool", True,
                  "Если выключить, предупреждение можно снять в любой момент."),
        ),
    ))

    # ------------------------------------------------------------------ roles
    S(Spec(
        "newcomers", "Новички во флуде", "🚪", "roles",
        "Что бот делает, когда кто-то заходит или выходит из флуда.",
        where="group", where_options=("group",), access="owner", access_options=("owner",),
        cooldown=False, autodelete=False, feature=True,
        params=(
            Param("restrict", "Ограничивать новичка до выдачи роли", "bool", True,
                  "Новичок не может писать, пока вы не напишете «калл <роль>»."),
            Param("welcome", "Приветствие новичка", "bool", True),
            Param("welcome_text", "Текст приветствия", "text", DEFAULT_WELCOME, "Доступно: {name}.", max_len=800),
            Param("rules_url", "Ссылка на правила (кнопка)", "text", "", "Оставьте пустым — кнопки не будет.",
                  max_len=300, allow_empty=True),
            Param("notify_leave", "Сообщать мне о выходе участника", "bool", True),
        ),
    ))
    S(Spec(
        "call", "Калл — роль новичку", "🎭", "roles",
        "«калл Кокоми» — назначить роль последнему новичку, поставить тег и снять ограничение.",
        triggers=("калл <роль>",), access="owner", access_options=("owner", "admins"),
        where="group", where_options=("group",), cooldown=False,
        params=(
            Param("window_minutes", "Окно после входа новичка", "int", 10, "Калл работает на новичка, зашедшего не раньше, чем N минут назад.",
                  min=1, max=1440, step=5, unit="мин", presets=(10, 30, 60, 180)),
            Param("lift_restriction", "Снимать ограничение", "bool", True),
            Param("notify_owner", "Присылать мне отчёт в ЛС", "bool", True),
        ),
    ))
    S(Spec(
        "setrole", "Назначить роль", "🎭", "roles",
        "/setrole @user Роль — или ответом на сообщение: /setrole Роль.",
        triggers=("/setrole @user Роль",), access="owner", access_options=("owner", "admins"),
        where="group", where_options=("group",), cooldown=False,
        menu=(("setrole", "🎭 Назначить роль участнику"),),
        params=(
            Param("lift_restriction", "Снимать ограничение", "bool", True),
            Param("notify_owner", "Присылать мне отчёт в ЛС", "bool", True),
        ),
    ))
    S(Spec(
        "bindrole", "Привязать роль (ЛС)", "🔗", "roles",
        "/bindrole @user Роль — из ЛС. Можно по ID и с указанием чата: /bindrole CHAT USER Роль.",
        triggers=("/bindrole @user Роль",), access="owner", access_options=("owner",),
        where="private", where_options=("private",), cooldown=False, autodelete=False,
        menu=(("bindrole", "🔗 Привязать роль"),),
        params=(
            Param("lift_restriction", "Снимать ограничение", "bool", True),
        ),
    ))
    S(Spec(
        "signrole", "Подпиши роль", "🏷", "roles",
        "Участник пишет боту в ЛС «подпиши роль», и бот заново ставит тег его роли.",
        triggers=("подпиши роль",), access="members", access_options=("owner", "members"),
        where="private", where_options=("private",), cooldown_default=30, autodelete=False,
    ))
    S(Spec(
        "syncroles", "Синхронизация ролей", "♻️", "roles",
        "Сверить теги участников в Telegram с базой бота.",
        triggers=("/syncroles",), access="owner", access_options=("owner", "admins"), where="both",
        menu=(("syncroles", "♻️ Синхронизировать роли"),),
        params=(
            Param("report_to", "Куда присылать отчёт", "choice", "dm", choices=(("dm", "💬 мне в ЛС"), ("here", "📍 туда, где вызвана"))),
        ),
    ))
    S(Spec(
        "roles_audit", "Аудит ролей", "🔎", "roles",
        "Сводка по ролям и последние изменения.",
        triggers=("/roles_audit",), access="owner", access_options=("owner", "admins"),
        where="private", where_options=("private",), autodelete=False,
        menu=(("roles_audit", "🔎 Проверить роли"),),
        params=(
            Param("history_limit", "Сколько последних изменений", "int", 10, min=3, max=30, step=1, presets=(5, 10, 20)),
        ),
    ))
    S(Spec(
        "who", "Карточка участника", "🪪", "roles",
        "/who @user или ответом на сообщение: роль, тег, подтверждение, активность.",
        triggers=("/who @user",), access="owner", access_options=("owner", "admins"), where="both",
        menu=(("who", "🪪 Карточка участника"),),
    ))

    # ------------------------------------------------------------------ awards
    S(Spec(
        "awards", "Автонаграды за срок", "🏆", "awards",
        "Перешлите боту в ЛС ответ Ирис на «олды» — бот сам определит, кому что выдать, и пришлёт готовые команды.",
        triggers=("/jf_awards", "/jf_iris_sync"), access="owner", access_options=("owner",),
        where="private", where_options=("private",), cooldown=False, autodelete=False,
        menu=(("jf_awards", "🏆 Награды за срок в чате"),),
        params=(
            *_tier(1, 30, 5, "🌱 30 дней в Justice Faite"),
            *_tier(2, 60, 6, "🌿 60 дней в Justice Faite"),
            *_tier(3, 120, 7, "🌳 120 дней в Justice Faite"),
            Param("mode", "Что выдавать", "choice", "all",
                  "«Все пороги» — команда на каждый достигнутый порог. «Только высший» — одна команда на человека.",
                  choices=(("all", "все достигнутые пороги"), ("best", "только высший порог"))),
            Param("command_word", "Слово команды Ирис", "text", "наградить", max_len=30),
            Param("prefix", "Префикс команды Ирис", "text", "!", max_len=3),
            Param("desc_template", "Шаблон описания", "text", DEFAULT_AWARD_DESC,
                  "Доступно: {title}, {age}, {days}, {name}.", max_len=300),
            Param("iris_username", "Username Ирис", "text", "iris_dp_bot",
                  "Нужен, чтобы узнавать пересланные ответы Ирис сами, без команды.", max_len=40),
            Param("marks", "Кнопки «Выдано / Пропустить» под командами", "bool", True),
        ),
    ))
    S(Spec(
        "award", "Выдать награду вручную", "🏵", "awards",
        "/jf_award @user [степень] [описание] — бот пришлёт готовую команду для Ирис, а награда запишется в базу.",
        triggers=("/jf_award @user 5 за активность",), access="owner", access_options=("owner",),
        where="private", where_options=("private",), cooldown=False, autodelete=False,
        menu=(("jf_award", "🏵 Выдать награду"),),
        params=(
            Param("default_level", "Степень по умолчанию", "int", 5, min=1, max=8, step=1),
        ),
    ))

    # ------------------------------------------------------------------ games
    S(Spec(
        "mafia", "Мафия — лобби", "🎭", "games",
        "Собирает игроков и передаёт запуск MafiaAzBot.",
        triggers=("/mafia",), access="members", access_options=("owner", "admins", "members", "all"),
        where="group", where_options=("group",), cooldown_default=10,
        menu=(("mafia", "🎭 Открыть лобби мафии"),),
        params=(
            Param("min_players", "Минимум игроков", "int", 5, min=2, max=20, step=1, presets=(4, 5, 6, 8)),
            Param("lobby_minutes", "Сколько живёт лобби", "int", 5, min=1, max=60, step=1, unit="мин", presets=(3, 5, 10, 15)),
            Param("pin_lobby", "Закреплять лобби", "bool", True),
        ),
    ))
    S(Spec(
        "mafia_leave", "Мафия — выйти", "🚪", "games",
        "Покинуть лобби мафии.",
        triggers=("/mafia_leave",), access="members", access_options=("owner", "admins", "members", "all"),
        where="group", where_options=("group",),
        menu=(("mafia_leave", "🚪 Выйти из лобби"),),
    ))
    S(Spec(
        "spy", "Шпион", "🕵️", "games",
        "Игра «Шпион» с лобби, вопросами в ЛС, голосованием и финальной догадкой.",
        triggers=("/spy",), access="members", access_options=("owner", "admins", "members", "all"),
        where="group", where_options=("group",), cooldown_default=10,
        menu=(("spy", "🕵️ Запустить «Шпион»"),),
        params=(
            Param("min_players", "Минимум игроков", "int", 3, min=2, max=10, step=1),
            Param("max_players", "Максимум игроков", "int", 10, min=3, max=20, step=1, presets=(6, 8, 10, 12)),
            Param("round_minutes", "Длительность раунда", "int", 8, min=1, max=30, step=1, unit="мин", presets=(5, 8, 12)),
            Param("discussion_seconds", "Обсуждение перед голосом", "int", 120, min=30, max=600, step=30, unit="с", presets=(60, 120, 180)),
            Param("final_vote_seconds", "Финальное голосование", "int", 120, min=30, max=600, step=30, unit="с"),
            Param("final_guess_seconds", "Финальная догадка шпиона", "int", 60, min=20, max=300, step=10, unit="с"),
            Param("max_hints", "Подсказок за игру", "int", 2, min=0, max=5, step=1),
        ),
    ))
    S(Spec(
        "spy_kick", "Шпион — исключить", "🕵️", "games",
        "Исключить игрока из текущей игры «Шпион».",
        triggers=("/spy_kick",), access="admins", access_options=("owner", "admins"),
        where="group", where_options=("group",), cooldown=False,
        menu=(("spy_kick", "🕵️ Исключить из игры"),),
    ))
    S(Spec(
        "spy_end", "Шпион — завершить", "🏁", "games",
        "Принудительно завершить игру «Шпион».",
        triggers=("/spy_end",), access="admins", access_options=("owner", "admins"),
        where="group", where_options=("group",), cooldown=False,
        menu=(("spy_end", "🏁 Завершить «Шпион»"),),
    ))
    S(Spec(
        "game_poll", "Опрос на игру", "🗳", "games",
        "Опрос: во что играем в ближайший слот расписания.",
        triggers=("/game_poll",), access="owner", access_options=("owner", "admins"),
        where="group", where_options=("group",), cooldown=False,
        menu=(("game_poll", "🗳 Опрос на игру"),),
        params=(
            Param("max_minutes", "Максимальная длительность опроса", "int", 10080, min=10, max=43200, step=60, unit="мин",
                  presets=(60, 1440, 10080)),
        ),
    ))
    S(Spec(
        "schedule", "Напоминания об играх", "⏰", "games",
        "Бот напоминает в флуде, что скоро начнётся игра по расписанию.",
        where="group", where_options=("group",), access="owner", access_options=("owner",),
        cooldown=False, autodelete=False, feature=True,
        params=(
            Param("lead_minutes", "За сколько минут напоминать", "int", 30, min=5, max=180, step=5, unit="мин", presets=(10, 30, 60)),
        ),
    ))

    # ------------------------------------------------------------------ anonymous / reports
    S(Spec(
        "anon", "Анонимная связь", "🤫", "anon",
        "Сообщение администрации без раскрытия автора в интерфейсе обращения.",
        triggers=("/anon", "кнопка в меню"), access="all", access_options=("all", "members"),
        where="private", where_options=("private",), cooldown_default=30, autodelete=False,
        menu=(("anon", "🤫 Анонимное обращение"),),
        params=(
            Param("max_len", "Максимальная длина", "int", 4000, min=100, max=4000, step=100, unit="симв", presets=(500, 1000, 4000)),
            Param("thanks_text", "Ответ после отправки", "text", "Ваше сообщение успешно передано.", max_len=300),
        ),
    ))
    S(Spec(
        "report", "Жалоба", "⚠️", "anon",
        "Жалоба на участника или нарушение — через кнопку в меню.",
        triggers=("кнопка в меню",), access="all", access_options=("all", "members"),
        where="private", where_options=("private",), cooldown=False, autodelete=False,
        params=(
            Param("cooldown_seconds", "Пауза между жалобами", "int", 600, min=0, max=86400, step=60, unit="с",
                  presets=(0, 300, 600, 3600)),
            Param("max_reason", "Максимальная длина причины", "int", 2000, min=100, max=4000, step=100, unit="симв",
                  presets=(500, 1000, 2000)),
        ),
    ))

    # ------------------------------------------------------------------ system
    S(Spec(
        "settings", "Настройки команд", "⚙️", "system",
        "Эта панель. Открывается в ЛС владельца.",
        triggers=("/settings",), access="owner", access_options=("owner",),
        where="private", where_options=("private",), cooldown=False, autodelete=False,
        menu=(("settings", "⚙️ Настройки команд"),),
    ))
    S(Spec(
        "manage_commands", "Свои команды", "✨", "system",
        "Создание, правка и удаление собственных команд бота (/commands, /manage_commands, /addcommand, /delcommand).",
        triggers=("/commands", "/addcommand", "/delcommand"), access="owner", access_options=("owner",),
        where="private", where_options=("private",), cooldown=False, autodelete=False,
        menu=(("commands", "✨ Свои команды"),),
    ))

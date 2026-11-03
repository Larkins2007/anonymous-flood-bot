"""Complete command catalog for the Justice Faite command-access panel.

Every user-facing built-in command is represented by a Spec. Aliases are used
by the global message middleware, so disabled commands are silently ignored
before their feature handlers can respond.
"""
from __future__ import annotations

from dataclasses import replace

import cmdcfg
from cmdcfg import Spec


EXISTING_ALIASES = {
    "start": ("/start",),
    "help": ("/help",),
    "birthday": ("/jf_birthday", "др", "мой др", "день рождения"),
    "birthdays": ("/jf_birthdays",),
    "dashboard": ("/jf_dashboard",),
    "rest": ("выдать рест", "/rest", "/jf_rest"),
    "rest_self": ("взять рест", "/take_rest"),
    "unrest": ("снять рест", "/unrest"),
    "restlist": ("рестники", "/jf_restlist"),
    "warn": ("/jf_warn",),
    "warnings": ("/jf_warnings",),
    "warn_remove": ("/jf_warn_remove",),
    "call": ("калл",),
    "setrole": ("/setrole",),
    "bindrole": ("/bindrole",),
    "signrole": ("подпиши роль",),
    "syncroles": ("/syncroles",),
    "who": ("/who",),
    "awards": ("/jf_awards", "/jf_iris_sync"),
    "award": ("/jf_award",),
    "mafia": ("/mafia",),
    "mafia_leave": ("/mafia_leave",),
    "spy": ("/spy",),
    "spy_kick": ("/spy_kick",),
    "spy_end": ("/spy_end",),
    "game_poll": ("/game_poll",),
    "anon": ("/anon",),
    "settings": ("/settings",),
    "manage_commands": ("/commands", "/manage_commands", "/addcommand", "/delcommand"),
    "reputation_vote": (),
}

# key, title, emoji, category, aliases, access, where, description
EXTRA = [
    # All manager and module commands use this shared command policy as the
    # single configurable access layer. Resource ownership and hierarchy checks
    # remain inside the relevant module as data-integrity safeguards.
    ("role_status", "Статус персонажа", "🏷️", "roles", ("/role_status",), "members", "group", "Установить статус персонажа: свободна, ожидает или резерв. Дополнительно проверяется Telegram-администратор/владелец."),
    ("mgr_help", "Справка модерации", "🛡️", "moderation", ("/jf_manager",), "members", "group", "Справка по модерации Justice Faite."),
    ("community_help", "Справка по сообществу", "👥", "community", ("/jf_community",), "members", "group", "Справка по кланам, кружкам, заметкам и закладкам."),
    ("mgr_mod_add", "Назначить модератора", "➕", "moderation", ("+модер", "+админ", "!модер", "!!модер", "!!!модер", "!!!!модер", "/jf_mod"), "rank_3", "Назначить модераторский ранг участнику."),
    ("mgr_mod_remove", "Снять модератора", "➖", "moderation", ("снять модер", "разжаловать"), "rank_3", "Снять модераторский ранг."),
    ("mgr_mod_promote", "Повысить модератора", "⬆️", "moderation", ("повысить",), "rank_3", "Повысить модератора на один ранг."),
    ("mgr_mod_demote", "Понизить модератора", "⬇️", "moderation", ("понизить",), "rank_3", "Понизить модератора на один ранг."),
    ("mgr_mod_list", "Список администрации", "👮", "moderation", ("кто админ", "а судьи кто", "кто здесь власть", "/jf_mods"), "members", "Показать состав администрации."),
    ("mgr_who_assigned", "Кто назначил модератора", "🔎", "moderation", ("кто назначил",), "rank_1", "Показать автора назначения ранга."),
    ("mgr_mod_log", "Журнал модерации", "📜", "moderation", ("модер лог", "мой модер лог", "твой модер лог", "модер лог от", "/jf_modlog"), "rank_2", "История изменения рангов и действий модерации."),
    ("mgr_warn", "Выдать предупреждение", "⚠️", "moderation", ("варн", "пред", "предупреждение"), "rank_2", "Выдать предупреждение участнику."),
    ("mgr_warnings", "Предупреждения участника", "📋", "moderation", ("варны", "мои варны"), "rank_1", "Посмотреть активные предупреждения."),
    ("mgr_warn_list", "Журнал предупреждений", "📜", "moderation", ("варнлист",), "rank_1", "Посмотреть последние предупреждения чата."),
    ("mgr_warn_remove", "Снять предупреждения", "↩️", "moderation", ("-варн", "снять варны", "снять все варны", "снять варн номер"), "rank_2", "Удалить одно или несколько предупреждений."),
    ("mgr_mute", "Выдать мут", "🔇", "moderation", ("мут", "/jf_mute"), "rank_2", "Ограничить возможность писать участнику."),
    ("mgr_unmute", "Снять мут", "🔊", "moderation", ("-мут", "снять мут", "размут", "/jf_unmute"), "rank_2", "Снять ограничение на отправку сообщений."),
    ("mgr_mute_list", "Список мутов", "📋", "moderation", ("муты",), "rank_1", "Показать пользователей с активным мутом."),
    ("mgr_mute_check", "Проверить мут", "🔍", "moderation", ("проверить мут",), "rank_1", "Проверить наличие активного мута."),
    ("mgr_ban", "Выдать бан", "🔨", "moderation", ("бан", "/jf_ban"), "rank_3", "Заблокировать участника на срок или навсегда."),
    ("mgr_unban", "Снять бан", "🔓", "moderation", ("разбан", "вернуть", "/jf_unban"), "rank_3", "Снять блокировку участника."),
    ("mgr_kick", "Исключить участника", "👢", "moderation", ("кик", "кик тихо", "/jf_kick"), "rank_2", "Исключить участника из чата."),
    ("mgr_ban_list", "Список банов", "📋", "moderation", ("банлист",), "rank_1", "Показать активные блокировки."),
    ("mgr_reason", "Причина наказания", "🧾", "moderation", ("причина",), "rank_1", "Показать причину последнего наказания."),
    ("mgr_access_view", "Просмотр доступа команд", "⚙️", "moderation", ("доступ команд", "/jf_access"), "rank_1", "Показать действующие ранги доступа."),
    ("mgr_access_view_self", "Мой доступ команд", "🔐", "moderation", ("мой доступ команд", "мой доступ"), "members", "Показать персональный уровень доступа."),
    ("mgr_access_set", "Настройка доступа команд", "🛠️", "moderation", ("дк",), "rank_4", "Изменить ранговый доступ к командам в этом чате."),
    ("mgr_access_log", "Журнал доступа команд", "📚", "moderation", ("лог дк",), "rank_4", "История изменений доступа команд."),
    ("mgr_setting_warn_limit", "Лимит предупреждений", "⚙️", "moderation", ("варны лимит",), "rank_4", "Настроить число предупреждений до автобана."),
    ("mgr_setting_warn_period", "Срок предупреждений", "⚙️", "moderation", ("варны период",), "rank_4", "Настроить срок хранения предупреждений."),
    ("mgr_setting_warn_ban", "Срок автобана за варны", "⚙️", "moderation", ("варны чс",), "rank_4", "Настроить срок блокировки после лимита варнов."),
    ("mgr_setting_mute_period", "Срок мута по умолчанию", "⚙️", "moderation", ("мут период",), "rank_4", "Настроить срок мута по умолчанию."),
    ("mgr_setting_ban_period", "Срок бана по умолчанию", "⚙️", "moderation", ("бан период",), "rank_4", "Настроить срок бана по умолчанию."),
    ("mgr_rank_labels", "Названия рангов", "🏷️", "moderation", ("модераторы названия", "ранг название"), "rank_4", "Изменить названия модераторских рангов."),
    ("mgr_rank_labels_reset", "Сбросить названия рангов", "↩️", "moderation", ("сбросить названия рангов",), "rank_4", "Вернуть стандартные названия рангов."),

    # Community: clans, circles, notes and bookmarks.
    ("clan_create", "Создать клан", "🏰", "community", ("создать клан",), "members", "Создать клан в чате."),
    ("clan_join", "Вступить в клан", "🤝", "community", ("клан вступить", "+клан"), "members", "Вступить в существующий клан."),
    ("clan_leave", "Выйти из клана", "🚪", "community", ("выйти из клана", "-клан"), "members", "Покинуть свой клан."),
    ("clan_edit", "Изменить клан", "✏️", "community", ("клан изменить",), "members", "Изменить название или описание своего клана."),
    ("clan_mine", "Мой клан", "🏰", "community", ("мой клан",), "members", "Показать информацию о своём клане."),
    ("clan_delete", "Удалить клан", "🗑️", "community", ("удалить клан",), "members", "Удалить созданный клан."),
    ("clan_list", "Список кланов", "📜", "community", ("кланы",), "members", "Показать кланы чата."),
    ("clan_members", "Участники клана", "👥", "community", ("участники клана",), "members", "Показать состав выбранного клана."),
    ("circle_create", "Создать кружок", "🫂", "community", ("создать кружок",), "members", "Создать тематический кружок."),
    ("circle_join", "Вступить в кружок", "🫶", "community", ("кружок вступить", "+кружок"), "members", "Вступить в кружок."),
    ("circle_leave", "Выйти из кружка", "🚪", "community", ("выйти из кружка", "-кружок"), "members", "Покинуть кружок."),
    ("circle_main", "Основной кружок", "⭐", "community", ("кружок основа",), "members", "Выбрать основной кружок."),
    ("circle_edit", "Изменить кружок", "✏️", "community", ("кружок изменить",), "members", "Изменить свой кружок."),
    ("circle_members", "Участники кружка", "👥", "community", ("участники кружка",), "members", "Показать участников кружка."),
    ("circle_mine", "Мои кружки", "🫂", "community", ("мои кружки",), "members", "Показать кружки, в которых состоишь."),
    ("circle_list", "Список кружков", "🫂", "community", ("кружки",), "members", "Показать все кружки чата."),
    ("circle_delete", "Удалить кружок", "🗑️", "community", ("удалить кружок",), "members", "Удалить созданный кружок."),
    ("note_create", "Создать заметку", "📝", "community", ("+заметка",), "members", "Сохранить шаблон текста."),
    ("note_rename", "Переименовать заметку", "✏️", "community", ("~название заметки",), "members", "Изменить название заметки."),
    ("note_edit", "Изменить заметку", "📝", "community", ("~заметка", "текст заметки"), "members", "Изменить текст заметки."),
    ("note_delete", "Удалить заметку", "🗑️", "community", ("-заметка",), "members", "Удалить свою заметку."),
    ("note_list", "Список заметок", "📚", "community", ("заметки",), "members", "Показать сохранённые заметки."),
    ("note_get", "Вызвать заметку", "📄", "community", ("заметка",), "members", "Отправить сохранённую заметку."),
    ("bookmark_create", "Создать закладку", "🔖", "community", ("+закладка",), "members", "Добавить закладку к теме или сообщению."),
    ("bookmark_mine", "Мои закладки", "🔖", "community", ("мои закладки",), "members", "Показать свои закладки."),
    ("bookmark_user", "Закладки участника", "🔖", "community", ("закладки",), "members", "Показать закладки выбранного участника."),
    ("bookmark_book", "Чатбук", "📖", "community", ("чатбук",), "members", "Открыть общий каталог закладок."),
    ("bookmark_get", "Открыть закладку", "📎", "community", ("закладка",), "members", "Открыть закладку по номеру."),
    ("bookmark_delete", "Удалить закладку", "🗑️", "community", ("удалить закладку", "-закладка"), "members", "Удалить свою закладку."),
    ("bookmark_hide", "Убрать из чатбука", "🙈", "community", ("исключить закладку", "убрать закладку"), "members", "Скрыть закладку из общего каталога."),

    # Chat configuration and basic safety.
    ("chat_rules_set", "Установить правила", "📜", "chat", ("+правила",), "rank_3", "Установить правила чата."),
    ("chat_rules_remove", "Удалить правила", "🗑️", "chat", ("-правила",), "rank_3", "Удалить правила чата."),
    ("chat_rules_show", "Показать правила", "📜", "chat", ("правила",), "members", "Показать правила чата."),
    ("chat_welcome_set", "Установить приветствие", "👋", "chat", ("+приветствие",), "rank_3", "Установить текст приветствия."),
    ("chat_welcome_remove", "Удалить приветствие", "👋", "chat", ("-приветствие",), "rank_3", "Удалить приветствие."),
    ("chat_welcome_show", "Показать приветствие", "👋", "chat", ("приветствие",), "members", "Показать текущее приветствие."),
    ("chat_entry_enable", "Включить уведомления о входах", "🚪", "chat", ("+входы",), "rank_3", "Включить уведомления о входе участников."),
    ("chat_entry_disable", "Выключить уведомления о входах", "🚪", "chat", ("-входы",), "rank_3", "Выключить уведомления о входе участников."),
    ("chat_exit_enable", "Включить уведомления о выходах", "🚶", "chat", ("+выходы",), "rank_3", "Включить уведомления о выходе участников."),
    ("chat_exit_disable", "Выключить уведомления о выходах", "🚶", "chat", ("-выходы",), "rank_3", "Выключить уведомления о выходе участников."),
    ("chat_io_enable", "Включить уведомления входа и выхода", "🔔", "chat", ("+входы-выходы",), "rank_3", "Включить общие уведомления об изменении состава."),
    ("chat_io_disable", "Выключить уведомления входа и выхода", "🔔", "chat", ("-входы-выходы",), "rank_3", "Выключить общие уведомления об изменении состава."),
    ("chat_description_set", "Изменить описание чата", "📝", "chat", ("+описание чата",), "rank_3", "Изменить описание группы."),
    ("chat_description_remove", "Удалить описание чата", "🗑️", "chat", ("-описание чата",), "rank_3", "Удалить описание группы."),
    ("chat_title_set", "Изменить название чата", "🏷️", "chat", ("!название", "название чата"), "rank_3", "Изменить название группы."),
    ("chat_pin", "Закрепить сообщение", "📌", "chat", ("!закреп",), "rank_3", "Закрепить сообщение в чате."),
    ("chat_unpin", "Открепить сообщение", "📍", "chat", ("!открепить",), "rank_3", "Открепить сообщение."),
    ("chat_link_set", "Установить ссылку чата", "🔗", "chat", ("+чат ссылка", "чат-ссылка", "чат ссылка"), "rank_3", "Сохранить ссылку на чат."),
    ("chat_link_remove", "Удалить ссылку чата", "🔗", "chat", ("-чат ссылка",), "rank_3", "Удалить сохранённую ссылку."),
    ("chat_links_enable", "Запретить ссылки", "🔒", "chat", ("+ссылки",), "rank_3", "Запретить ссылки в чате."),
    ("chat_links_disable", "Разрешить ссылки", "🔓", "chat", ("-ссылки",), "rank_3", "Разрешить ссылки в чате."),
    ("chat_chats_enable", "Запретить ссылки на беседы", "🔒", "chat", ("+чаты",), "rank_3", "Запретить ссылки-приглашения в беседы."),
    ("chat_chats_disable", "Разрешить ссылки на беседы", "🔓", "chat", ("-чаты",), "rank_3", "Разрешить ссылки-приглашения в беседы."),
    ("chat_sites_enable", "Запретить сторонние сайты", "🔒", "chat", ("+сайты",), "rank_3", "Запретить ссылки на сторонние сайты."),
    ("chat_sites_disable", "Разрешить сторонние сайты", "🔓", "chat", ("-сайты",), "rank_3", "Разрешить ссылки на сторонние сайты."),
    ("chat_tags_enable", "Запретить ссылки на профили", "🔒", "chat", ("+теги",), "rank_3", "Запретить ссылки на профили пользователей."),
    ("chat_tags_disable", "Разрешить ссылки на профили", "🔓", "chat", ("-теги",), "rank_3", "Разрешить ссылки на профили пользователей."),
    ("chat_bots_enable", "Запретить ссылки на ботов", "🔒", "chat", ("+боты",), "rank_3", "Запретить приглашение ботов."),
    ("chat_bots_disable", "Разрешить ссылки на ботов", "🔓", "chat", ("-боты",), "rank_3", "Разрешить приглашение ботов."),
    ("chat_link_allow", "Разрешить ссылку", "✅", "chat", ("!разрешить ссылку",), "rank_3", "Добавить ссылку в разрешённый список."),
    ("chat_link_deny", "Удалить разрешённую ссылку", "🚫", "chat", ("!удалить ссылку",), "rank_3", "Удалить ссылку из разрешённого списка."),
    ("chat_config_help", "Справка по настройкам чата", "⚙️", "chat", ("/jf_chatconfig", "jf_chatconfig"), "members", "Справка по настройкам чата."),

    # Statistics.
    ("stats_help", "Справка по статистике", "📊", "statistics", ("/jf_stats",), "members", "Справка по статистике."),
    ("stats_chat_info", "Информация о чате", "ℹ️", "statistics", ("чат инфо",), "members", "Общая информация о чате."),
    ("stats_chat_image", "Статистика чата", "📊", "statistics", ("чат стата",), "members", "Статистика чата."),
    ("stats_hourly", "Статистика по часам", "🕒", "statistics", ("стата по часам",), "members", "Активность по часам."),
    ("stats_mine", "Моя статистика", "📈", "statistics", ("моя стата",), "members", "Личная статистика участника."),
    ("stats_inactive", "Список неактивных", "🌙", "statistics", ("список неактив",), "rank_1", "Участники без активности за выбранный период."),
    ("stats_silent", "Список молчунов", "🤫", "statistics", ("список молчунов",), "rank_1", "Участники, которые ещё не написали ни одного сообщения."),
    ("stats_oldies", "Старожилы", "🏅", "statistics", ("олды",), "members", "Список старожилов чата."),
    ("stats_newcomers", "Новички", "🌱", "statistics", ("новички",), "members", "Список недавно вступивших."),
    ("stats_top", "Топ активности", "🏆", "statistics", ("топ", "стата"), "members", "Топ сообщений за выбранный период."),

    # Profiles.
    ("profile_help", "Справка по анкетам", "👤", "profiles", ("/jf_profile",), "members", "Справка по профилям."),
    ("profile_me", "Моя анкета", "👤", "profiles", ("моя анкета", "кто я"), "members", "Открыть свою анкету."),
    ("profile_view", "Анкета участника", "🪪", "profiles", ("анкета", "кто ты"), "members", "Открыть анкету участника."),
    ("profile_public_enable", "Открыть анкету", "👁️", "profiles", ("+анкета",), "members", "Разрешить просмотр анкеты."),
    ("profile_public_disable", "Скрыть анкету", "🙈", "profiles", ("-анкета",), "members", "Запретить просмотр анкеты другим участникам."),
    ("profile_nick_set", "Установить ник", "✏️", "profiles", ("+ник",), "members", "Установить свой ник в чате."),
    ("profile_nick_view", "Посмотреть ник", "🏷️", "profiles", ("ник",), "members", "Показать ник участника."),
    ("profile_nick_remove", "Удалить ник", "🗑️", "profiles", ("ник удалить", "-ник"), "members", "Удалить свой ник."),
    ("profile_nick_admin_set", "Назначить ник участнику", "✍️", "profiles", ("назначить ник",), "rank_3", "Задать ник другому участнику."),
    ("profile_nick_admin_remove", "Удалить ник участника", "🗑️", "profiles", ("удалить ник",), "rank_3", "Удалить ник другого участника."),
    ("profile_title_set", "Установить звание", "🎖️", "profiles", ("+звание",), "members", "Установить личное звание."),
    ("profile_title_view", "Посмотреть звание", "🎖️", "profiles", ("звание",), "members", "Посмотреть звание участника."),
    ("profile_title_remove", "Удалить звание", "🗑️", "profiles", ("звание удалить", "-звание"), "members", "Удалить личное звание."),
    ("profile_title_admin_set", "Назначить звание участнику", "🏅", "profiles", ("назначить звание",), "rank_3", "Задать звание другому участнику."),
    ("profile_title_admin_remove", "Удалить звание участника", "🗑️", "profiles", ("удалить звание",), "rank_3", "Удалить звание другого участника."),
    ("profile_about_set", "О себе", "📝", "profiles", ("о себе", "описание"), "members", "Изменить описание профиля."),
    ("profile_about_remove", "Удалить описание", "🧹", "profiles", ("-о себе", "-описание"), "members", "Удалить описание профиля."),
    ("profile_motto_set", "Установить девиз", "💬", "profiles", ("+девиз",), "members", "Установить девиз."),
    ("profile_motto_remove", "Удалить девиз", "🧹", "profiles", ("-девиз",), "members", "Удалить девиз."),
    ("profile_motto_view", "Посмотреть девиз", "💬", "profiles", ("!девиз", "девиз"), "members", "Посмотреть девиз."),

    # Reputation.
    ("reputation_help", "Справка по репутации", "✨", "reputation", ("/jf_reputation",), "members", "Справка по репутации."),
    ("reputation_board", "Рейтинг участников", "🏆", "reputation", ("рейтинг", "топ рейтинга"), "members", "Показать рейтинг чата."),
    ("reputation_mine", "Моя репутация", "⭐", "reputation", ("моя репутация", "моя звёздность", "моя звездность"), "members", "Показать свою репутацию."),
    ("reputation_user", "Звёздность участника", "⭐", "reputation", ("звёздность", "звездность"), "members", "Показать уровень репутации участника."),
    ("reputation_vote", "Поставить плюс", "➕", "reputation", ("+1", "+2", "+3"), "members", "Поставить положительную оценку ответом на сообщение."),

    # Reports, timers and automation.
    ("reports_help", "Справка по жалобам", "🚩", "automation", ("/jf_reports",), "members", "Справка по жалобам."),
    ("report_create", "Отправить жалобу", "🚩", "automation", ("репорт", "!репорт"), "members", "Отправить жалобу ответом на сообщение."),
    ("report_list", "Список жалоб", "📋", "automation", ("репортлист",), "rank_2", "Показать открытые жалобы."),
    ("report_close", "Закрыть жалобу", "✅", "automation", ("репорт закрыть",), "rank_2", "Закрыть обработанную жалобу."),
    ("report_reset", "Очистить список жалоб", "🧹", "automation", ("сброс репортов", "!сброс репортов"), "rank_4", "Очистить очередь жалоб."),
    ("timers_help", "Справка по таймерам", "⏰", "automation", ("/jf_timers",), "members", "Справка по таймерам."),
    ("timer_create", "Создать таймер", "⏱️", "automation", ("таймер через",), "rank_1", "Создать напоминание через заданное время."),
    ("timer_list", "Мои таймеры", "📋", "automation", ("таймеры",), "rank_1", "Показать свои таймеры."),
    ("timer_delete", "Удалить таймер", "🗑️", "automation", ("-таймер",), "rank_1", "Удалить свой таймер."),
    ("timer_clear", "Удалить все свои таймеры", "🧹", "automation", ("сбросить таймеры", "удалить все таймеры"), "rank_1", "Удалить все свои активные таймеры."),
    ("automod_help", "Справка по триггерам", "🧩", "automation", ("/jf_automod",), "members", "Справка по автоматическим триггерам."),
    ("trigger_create", "Создать триггер", "➕", "automation", ("+триггер",), "rank_3", "Создать автоматическое правило реакции."),
    ("trigger_list", "Список триггеров", "📋", "automation", ("триггеры",), "rank_1", "Посмотреть активные триггеры."),
    ("trigger_delete", "Удалить триггер", "🗑️", "automation", ("-триггер",), "rank_3", "Удалить триггер."),

    # Fun commands.
    ("fun_help", "Справка по развлечениям", "🎲", "fun", ("/jf_fun",), "members", "Справка по развлекательным командам."),
    ("fun_random", "Случайное число", "🎲", "fun", ("рандом",), "members", "Выбрать случайное число."),
    ("fun_yesno", "Да или нет", "🔮", "fun", ("данет",), "members", "Случайный ответ да/нет."),
    ("fun_choose", "Выбрать вариант", "🤔", "fun", ("выбери",), "members", "Выбрать один из вариантов."),
    ("fun_info", "Случайный шанс", "🎯", "fun", ("инфа",), "members", "Сгенерировать случайный процент."),
    ("fun_dice", "Кубик", "🎲", "fun", ("кубик", "!кубик"), "members", "Бросить виртуальный кубик."),
    ("fun_ping", "Проверка бота", "📡", "fun", ("пинг", "кинг", "пиу", "бот"), "members", "Проверить, отвечает ли бот."),

    # Chat network.
    ("network_help", "Справка по сетке чатов", "🌐", "network", ("/jf_network",), "members", "Справка по сетке чатов."),
    ("network_create", "Создать сетку", "🌐", "network", ("сетка создать",), "rank_4", "Создать сеть связанных чатов."),
    ("network_bind", "Привязать чат к сетке", "🔗", "network", ("сетка привязать",), "rank_4", "Привязать текущий чат к существующей сети."),
    ("network_unbind", "Отвязать чат от сетки", "✂️", "network", ("сетка отвязать",), "rank_4", "Отвязать текущий чат."),
    ("network_list", "Список чатов сетки", "📋", "network", ("сетка чаты",), "rank_1", "Показать связанные чаты."),
    ("network_mod_add", "Глобальный модератор", "➕", "network", ("сетка +модер",), "rank_4", "Назначить глобального модератора."),
    ("network_mod_remove", "Снять глобального модератора", "➖", "network", ("сетка -модер",), "rank_4", "Снять глобального модератора."),
    ("network_mod_list", "Глобальные модераторы", "👮", "network", ("сетка модер",), "rank_1", "Показать глобальных модераторов."),
    ("network_bans", "Глобальные блокировки", "🔨", "network", ("сетка баны",), "rank_2", "Показать блокировки в сетке."),
    ("network_ban", "Глобальный бан", "⛔", "network", ("глобан",), "rank_4", "Заблокировать пользователя во всех связанных чатах."),
    ("network_unban", "Глобальный разбан", "🔓", "network", ("глоразбан",), "rank_4", "Снять глобальную блокировку."),

    # Owner console / memory commands.
    ("console_open", "Личная консоль", "🧠", "system", ("/jf_console",), "owner", "private", "Открыть личную консоль владельца."),
    ("console_refresh", "Обновить консоль", "🔄", "system", ("/jf_console_refresh",), "owner", "private", "Обновить блоки личной консоли."),
    ("memory_view", "Память проекта", "🗂️", "system", ("/jf_memory",), "owner", "private", "Просмотреть структурированную память проекта."),
    ("memory_save", "Сохранить память", "📝", "system", ("/jf_memory_save",), "owner", "private", "Добавить новую запись в память проекта."),
    ("memory_search", "Поиск по памяти", "🔎", "system", ("/jf_memory_search",), "owner", "private", "Найти сохранённые сообщения и требования."),

    # Free community raffles and bounded cleanup.
    ("raffles_help", "Справка по розыгрышам", "🎉", "events", ("/jf_raffles",), "members", "Справка по бесплатным розыгрышам."),
    ("raffle_create", "Создать розыгрыш", "🎉", "events", ("+розыгрыш",), "rank_3", "Создать бесплатный розыгрыш символической награды."),
    ("raffle_status", "Статус розыгрыша", "📊", "events", ("розыгрыш статус",), "members", "Показать текущий розыгрыш."),
    ("raffle_join", "Участвовать в розыгрыше", "🙋", "events", ("участвую",), "members", "Бесплатно войти в текущий розыгрыш."),
    ("raffle_leave", "Выйти из розыгрыша", "🚪", "events", ("выйти из розыгрыша",), "members", "Выйти из розыгрыша."),
    ("raffle_finish", "Завершить розыгрыш", "🏁", "events", ("завершить розыгрыш",), "rank_3", "Подвести итоги розыгрыша."),
    ("raffle_winners", "Победители розыгрыша", "🏆", "events", ("победители розыгрыша",), "members", "Показать последних победителей."),
    ("clean_help", "Справка по чистке", "🧹", "cleanup", ("/jf_clean",), "rank_1", "Справка по чистке сообщений."),
    ("clean_reply", "Удалить сообщение", "🗑️", "cleanup", ("-смс",), "rank_2", "Удалить сообщение или последние сообщения из истории."),
    ("clean_purge", "Очистить блок сообщений", "🧹", "cleanup", ("пург", "!пург"), "rank_2", "Удалить ограниченное число сообщений от выбранного сообщения."),
]


def _all_options():
    return tuple(cmdcfg.ALL_ACCESS_OPTIONS)


def load() -> int:
    """Attach aliases to core specs and register every additional command."""
    count = 0
    for key, aliases in EXISTING_ALIASES.items():
        spec = cmdcfg.REGISTRY.get(key)
        if spec is not None:
            menu = spec.menu or tuple(
                (str(alias)[1:], f"{spec.emoji} {spec.title}")
                for alias in aliases
                if str(alias).startswith("/") and str(alias)[1:].replace("_", "").isalnum()
            )
            cmdcfg.register(replace(spec, aliases=tuple(aliases), menu=menu))

    for item in EXTRA:
        if len(item) == 7:
            key, title, emoji, category, aliases, access, desc = item
            where = "group"
        elif len(item) == 8:
            key, title, emoji, category, aliases, access, where, desc = item
        else:
            raise ValueError(f"Invalid command catalog entry: {item!r}")
        if key in cmdcfg.REGISTRY:
            continue
        slash_menu = tuple(
            (str(alias)[1:], f"{emoji} {title}")
            for alias in aliases
            if str(alias).startswith("/") and str(alias)[1:].replace("_", "").isalnum()
        )
        cmdcfg.register(Spec(
            key=key,
            title=title,
            emoji=emoji,
            category=category,
            desc=desc,
            triggers=tuple(aliases),
            aliases=tuple(aliases),
            access=access,
            access_options=_all_options(),
            where=where,
            where_options=(("private", "group", "both") if where == "both" else (where,)),
            cooldown=True,
            cooldown_default=0,
            autodelete=True,
            menu=slash_menu,
        ))
        count += 1
    return count

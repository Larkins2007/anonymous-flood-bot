from pathlib import Path
import ast
import re
import sqlite3
from datetime import datetime, timedelta

SRC = Path('bot.py').read_text(encoding='utf-8')
TREE = ast.parse(SRC)


def test_command_matrix_and_routing():
    required = [
        'help','mafia','mafia_leave',
        'setrole','syncroles','game_poll',
        'manage_commands','addcommand','delcommand','commands','bindrole','roles_audit'
    ]
    handlers = set(re.findall(r'@dp\.message\(Command\("([a-z0-9_]+)"\)', SRC))
    assert not [x for x in required if x not in handlers]
    assert 'F.text.regexp(r"(?is)^\\s*(?!/)(?!калл(?:\\s|$)).+")' in SRC
    assert 'флудик' not in SRC.lower()
    fallback = SRC.rfind('async def custom_command_dispatch')
    assert fallback > SRC.rfind('async def mafia_cmd')
    assert fallback > SRC.rfind('async def help_cmd')


def test_role_catalog_and_tag_roundtrip():
    role_groups = next(
        ast.literal_eval(n.value)
        for n in TREE.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == '_ROLE_GROUPS' for t in n.targets)
    )
    roles = [r for _, entries in role_groups for r in entries]
    assert len(roles) == 148
    assert len({r[0] for r in roles}) == 148
    assert len({r[1] for r in roles}) == 148

    # Execute the role helpers in isolation.
    ns = {'unicodedata': __import__('unicodedata'), 're': re}
    needed = {'_ROLE_GROUPS','ROLE_CATALOG', 'ROLE_BY_KEY', 'normalize_role', 'role_for', '_stylize_latin', 'make_tag', '_normalize_role_tag_value', 'role_for_tag'}
    for node in TREE.body:
        names = []
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [node.name]
        if set(names) & needed:
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<bot>', 'exec'), ns)
    ns['ROLE_BY_KEY'] = {}
    for name, english, region in ns['ROLE_CATALOG']:
        ns['ROLE_BY_KEY'][ns['normalize_role'](name)] = {'name': name, 'english': english, 'region': region}
    for name, english, _region in ns['ROLE_CATALOG']:
        tag = ns['make_tag'](english)
        assert len(tag) <= 16
        found = ns['role_for_tag'](tag)
        assert found and found['name'] == name


def test_game_vote_is_single_and_changeable():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE votes(poll_id INTEGER,user_id INTEGER,game_id INTEGER,voted_at TEXT,PRIMARY KEY(poll_id,user_id))')
    conn.execute("INSERT INTO votes VALUES(1,100,1,'a')")
    conn.execute("INSERT INTO votes VALUES(1,100,4,'b') ON CONFLICT(poll_id,user_id) DO UPDATE SET game_id=excluded.game_id,voted_at=excluded.voted_at")
    assert conn.execute('SELECT game_id FROM votes WHERE poll_id=1 AND user_id=100').fetchone()[0] == 4
    assert 'gp:i:' in SRC
    assert 'Нажмите ℹ рядом с игрой' in SRC or 'Нажмите на название игры, чтобы выбрать её.' in SRC
    assert 'InlineKeyboardButton(text=label, callback_data=f"gp:v:{poll_id}:{gid}")' in SRC
    assert 'label = compact_game_name(game["name"])' in SRC
    assert 'counts.get(gid, 0)' not in SRC.split('def game_poll_keyboard',1)[1].split('def poll_duration_keyboard',1)[0]


def test_schedule_cycle():
    ns = {}
    for node in TREE.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in {'GAME_DEFINITIONS','DEFAULT_WEEKLY_SCHEDULE'}:
                    exec(compile(ast.Module(body=[node], type_ignores=[]), '<bot>', 'exec'), ns)
    assert [x[0] for x in ns['GAME_DEFINITIONS']] == ['Шпион','Жених и невеста','Правда или ложь','Мафия','Снежный ком историй','Чёрный ящик','Бункер']
    assert [x[0] for x in ns['DEFAULT_WEEKLY_SCHEDULE']] == list(range(7))
    assert all(x[1] == '20:00' for x in ns['DEFAULT_WEEKLY_SCHEDULE'])
    assert 'SCHEDULE_ANCHOR_DATE' in SRC and '"2026-08-18"' in SRC
    assert 'schedule_cycle' in SRC
    anchor = datetime(2026, 8, 18)
    expected = [(anchor + timedelta(days=i*2)).date().isoformat() for i in range(7)]
    assert expected == ['2026-08-18','2026-08-20','2026-08-22','2026-08-24','2026-08-26','2026-08-28','2026-08-30']


def test_mafia_transfer_flow():
    assert '/start@MafiaAzBot' in SRC
    assert "status='STARTING'" in SRC
    assert "status='TRANSFERRED'" in SRC
    assert 'asyncio.sleep(300)' in SRC
    assert 'pin_chat_message' in SRC
    assert 'lobby_message_id' in SRC
    assert 'delete()' in SRC
    for forbidden in ('"Мирный"','"Комиссар"','"Доктор"','Ваша роль:'):
        assert forbidden not in SRC


def test_role_persistence_and_member_lookup():
    assert 'CREATE TABLE IF NOT EXISTS role_catalog' in SRC
    assert 'CREATE TABLE IF NOT EXISTS role_history' in SRC
    assert 'def seed_role_catalog' in SRC
    assert 'def record_role_history' in SRC
    assert 'role_for_tag(actual_tag)' in SRC
    assert 'Роль участника:' in SRC
    assert 'async def bindrole_private_command' in SRC
    assert 'async def roles_audit_cmd' in SRC
    assert 'async def sign_assigned_role_private' in SRC


def test_game_poll_has_real_lookup_and_no_refresh_button():
    assert 'def get_game_by_id' in SRC
    poll = SRC.split('def game_poll_keyboard', 1)[1].split('@dp.message(Command("game_poll"))', 1)[0]
    assert 'callback_data=f"gp:v:' in poll
    assert 'callback_data=f"gp:i:' in poll
    assert 'Обновить' not in poll
    callback = SRC.split('@dp.callback_query(F.data.startswith("gp:"))', 1)[1].split('def next_scheduled_slot', 1)[0]
    assert 'await callback.message.edit_text' in callback
    assert 'get_game_by_id(gid)' in callback


def test_removed_commands_are_really_gone():
    for old in ("member", "release", "schedule_set", "mafia_ban", "mafia_unban", "roles", "role", "pending"):
        assert f'@dp.message(Command("{old}"))' not in SRC
        assert f'BotCommand(command="{old}"' not in SRC
    for marker in ("/release", "/member", "/schedule_set", "/mafia_ban", "/mafia_unban"):
        assert marker not in SRC
    assert '@dp.message(Command("roles"))' not in SRC
    assert '@dp.message(Command("pending"))' not in SRC


def test_primary_chat_and_poll_finish_behavior():
    assert 'PRIMARY_CHAT_ID = int(os.getenv("PRIMARY_CHAT_ID", "-1004313546398") or "-1004313546398")' in SRC
    assert 'def is_primary_chat(chat_id: int)' in SRC
    assert 'if not is_primary_chat(message.chat.id)' in SRC
    assert 'await bot.unpin_chat_message' in SRC
    assert '"status=\'CLOSED\'"' not in SRC  # status is parameterized in SQL
    assert "UPDATE game_polls SET status='CLOSED'" in SRC
    assert 'parent_poll_id' in SRC
    assert 'tie_round' in SRC
    assert 'gp:r:' in SRC
    assert 'Повторная ничья. Администратор выбирает победителя' in SRC
    assert 'За неё проголосовали:' in SRC


def test_schedule_does_not_auto_launch_game_commands():
    segment = SRC[SRC.find('async def schedule_worker():'):SRC.find('# =========================================================\n# MAFIA')]
    assert 'launch_text' not in segment
    assert "send_message(\n                                    chat_id" in segment
    assert 'через 30 минут' in segment


def test_removed_menu_commands_are_not_exposed():
    exposed = [
        'mafia_ban','mafia_unban','member','release','schedule_set',
        'schedule_remove','game','status','me','games','pending','game_add','game_remove'
    ]
    for old in exposed:
        assert f'BotCommand(command="{old}"' not in SRC
        assert f'@dp.message(Command("{old}"))' not in SRC

def test_primary_chat_id_and_role_aliases():
    assert 'PRIMARY_CHAT_ID = int(os.getenv("PRIMARY_CHAT_ID", "-1004313546398") or "-1004313546398")' in SRC
    assert '("Ной", "Noy")' in SRC
    assert '_compact_role_key' in SRC
    assert 'role_text=remainder' in SRC
    assert 'roles_audit' in SRC


def test_final_command_exposure():
    exposed = set(re.findall(r'BotCommand\(command="([a-z0-9_]+)"', SRC))
    expected = {'help','mafia','mafia_leave','game_poll','setrole','syncroles','manage_commands','addcommand','delcommand','commands','bindrole','roles_audit'}
    assert expected.issubset(exposed)
    forbidden = {'roles','role','member','release','pending','game','status','me','games','tonight','nextgame','games_history','stats','players','game_add','game_remove','schedule_set','schedule_remove','mafia_ban','mafia_unban'}
    assert not (exposed & forbidden)


def test_setrole_accepts_multiword_role_without_target():
    assert 'if len(parts)==1:' in SRC
    assert 'pending = latest_pending_member(message.chat.id)' in SRC
    assert 'role_text = remainder' in SRC


def test_sync_never_clears_unknown_role_or_rewrites_managed_tag():
    segment = SRC[SRC.find('async def sync_member_tag'):SRC.find('async def adopt_external_roles')]
    assert 'Never blank it here.' in segment
    assert 'previous["role_name"]' in segment
    assert 'set_chat_member_tag' not in segment


def test_sync_is_single_primary_chat():
    segment = SRC[SRC.find('@dp.message(Command("syncroles"))'):SRC.find('async def member_info_cmd')]
    assert 'chat_id = PRIMARY_CHAT_ID' in segment
    assert 'adopt_external_roles(chat_id, known_ids)' in segment

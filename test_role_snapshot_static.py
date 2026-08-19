import ast
from pathlib import Path

src = Path('bot.py').read_text(encoding='utf-8')
tree = ast.parse(src)
funcs = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
assert 'update_role_snapshot' in funcs
assert 'schedule_role_snapshot_update' in funcs
assert 'role_snapshot_state' in src
assert 'await update_role_snapshot(chat_id)' in src
assert 'GAME_DEFINITIONS' in src
assert len([1 for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name) and t.id=='ROLE_CATALOG']) == 1
for forbidden in ['флудик', 'BotCommand(command="schedule"', 'BotCommand(command="roles"', 'BotCommand(command="pending"', 'BotCommand(command="member"', 'BotCommand(command="mafia_ban"', 'BotCommand(command="mafia_unban"']:
    assert forbidden not in src, forbidden
# critical persistence safety: generic upsert must not turn a previously managed tag off
assert 'tag_set_by_bot=CASE WHEN excluded.tag_set_by_bot=1 THEN 1 ELSE group_members.tag_set_by_bot END' in src
assert 'confirmed=CASE WHEN excluded.confirmed=1 THEN 1 ELSE group_members.confirmed END' in src
print('ROLE SNAPSHOT STATIC PASS')

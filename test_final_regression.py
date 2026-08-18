import ast
from pathlib import Path
s=Path('bot.py').read_text(encoding='utf-8')
t=ast.parse(s)
for n in t.body:
    if isinstance(n, ast.Assign) and any(isinstance(x,ast.Name) and x.id=='_ROLE_GROUPS' for x in n.targets):
        groups=ast.literal_eval(n.value); roles=[r for _,e in groups for r in e]
        assert len(roles)==148 and len({r[0] for r in roles})==148 and len({r[1] for r in roles})==148
        break
else: raise AssertionError('_ROLE_GROUPS missing')
assert 'CREATE TABLE IF NOT EXISTS role_catalog' in s
assert 'def seed_role_catalog' in s and 'seed_role_catalog()' in s
segment=s[s.find('def latest_pending_member'):s.find('def role_is_occupied')]
assert 'LIMIT 1' in segment
assert 'async def call_assign_role' in s
for marker in ['@dp.message(Command("manage_commands"))','@dp.message(Command("addcommand"))','@dp.message(Command("delcommand"))','@dp.message(Command("commands"))']:
    assert marker in s
for bad in ['BotCommand(command="start"','BotCommand(command="cancel"','BotCommand(command="free_roles"','BotCommand(command="all_roles"','BotCommand(command="checkbot"','BotCommand(command="id"','BotCommand(command="admin"','BotCommand(command="bind_group"']:
    assert bad not in s, bad
assert '@dp.message(Command("cancel"))' not in s
assert 'fludik_sign_role_private' in s and 'parse_fludik_command' in s
assert '/start@MafiaAzBot' in s and 'status=\'TRANSFERRED\'' in s
print('FINAL REGRESSION PASS')

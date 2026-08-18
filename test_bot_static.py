import ast
import re
from pathlib import Path

SRC = Path('bot.py').read_text(encoding='utf-8')
TREE = ast.parse(SRC)

# syntax / duplicate top-level functions
names = {}
for node in TREE.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        names.setdefault(node.name, []).append(node.lineno)
assert not {k:v for k,v in names.items() if len(v) > 1}

# 148-role catalog
role_groups = next(
    ast.literal_eval(n.value)
    for n in TREE.body
    if isinstance(n, ast.Assign)
    and any(isinstance(t, ast.Name) and t.id == '_ROLE_GROUPS' for t in n.targets)
)
roles = [role for _, entries in role_groups for role in entries]
assert len(roles) == 148
assert len({r[0] for r in roles}) == 148
assert len({r[1] for r in roles}) == 148

# important commands exposed in the menu
for command in ('help', 'roles', 'mafia', 'mafia_leave', 'setrole', 'release', 'syncroles', 'member', 'pending', 'mafia_ban', 'mafia_unban'):
    assert f'BotCommand(command="{command}"' in SRC

# commands explicitly removed from menu / old list mechanics
for command in ('start', 'cancel', 'free_roles', 'all_roles', 'checkbot', 'id', 'admin', 'bind_group'):
    assert f'BotCommand(command="{command}"' not in SRC
for legacy in ('update_group_roster', 'capture_list', 'sync_list', 'bind_info', 'roster_sources', 'TEST_ROSTER', 'build_roster_message'):
    assert legacy not in SRC

# kall must remain the text shortcut, not a Command("kall") handler.
assert 'async def call_assign_role' in SRC
assert 'Command("kall")' not in SRC
assert 'async def call_assign_role' in SRC

# Mafia transfer-only
for forbidden in ('"Мирный"', '"Комиссар"', '"Доктор"', 'Ваша роль:'):
    assert forbidden not in SRC
assert '/start@MafiaAzBot' in SRC
assert 'status=\'TRANSFERRED\'' in SRC or 'status="TRANSFERRED"' in SRC
assert 'pin_chat_message' in SRC
assert 'lobby_message_id' in SRC

print('STATIC TEST PASSED')
print('roles=148')
print('commands=curated')
print('list-system=removed')
print('kall=restored')
print('mafia=transfer-only+pinned-lobby')

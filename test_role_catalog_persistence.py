import ast
from pathlib import Path
s = Path('bot.py').read_text(encoding='utf-8')
t = ast.parse(s)
role_groups = None
for node in t.body:
    if isinstance(node, ast.Assign) and any(isinstance(tg, ast.Name) and tg.id == '_ROLE_GROUPS' for tg in node.targets):
        role_groups = ast.literal_eval(node.value)
        break
assert role_groups is not None
roles = [r for _, entries in role_groups for r in entries]
assert len(roles) == 148
assert len({r[0] for r in roles}) == 148
assert len({r[1] for r in roles}) == 148
assert 'CREATE TABLE IF NOT EXISTS role_catalog' in s
assert 'def seed_role_catalog' in s
assert 'seed_role_catalog()' in s
assert 'INSERT INTO role_catalog' in s
assert 'ON CONFLICT(role_key) DO UPDATE SET' in s
# Persistent assignments
assert 'CREATE TABLE IF NOT EXISTS role_state' in s
assert 'CREATE TABLE IF NOT EXISTS group_members' in s
assert 'BEGIN IMMEDIATE' in s
print('ROLE CATALOG / PERSISTENCE PASS')

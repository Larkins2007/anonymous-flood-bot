import ast, re
from pathlib import Path
p=Path('/mnt/data/final_bot.py')
s=p.read_text(encoding='utf-8')
t=ast.parse(s)
# duplicates
names={}
for n in t.body:
    if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
        names.setdefault(n.name,[]).append(n.lineno)
dups={k:v for k,v in names.items() if len(v)>1}
assert not dups, dups
# catalog
role_groups=None
for n in t.body:
    if isinstance(n, ast.Assign) and any(isinstance(x,ast.Name) and x.id=='_ROLE_GROUPS' for x in n.targets):
        role_groups=ast.literal_eval(n.value); break
assert role_groups is not None
roles=[r for _, entries in role_groups for r in entries]
assert len(roles)==148
assert len({r[0] for r in roles})==148
assert len({r[1] for r in roles})==148
# tag source compile funcs
ns={"re": re}
for n in t.body:
    if isinstance(n, ast.FunctionDef) and n.name in {'_stylize_latin','make_tag','normalize_role','role_for','parse_kall','parse_fludik_intent'}:
        exec(compile(ast.Module([n],[]),'<extract>','exec'),ns)
tags=[]
for _,en in roles:
    tags.append(ns['make_tag'](en))
assert len(set(tags))==148
assert all(len(x)<=16 for x in tags)
# required handlers / forbidden list mechanics
required=[
'@dp.message(Command("help"))','@dp.message(Command("roles"))','@dp.message(Command("free_roles"))',
'@dp.message(Command("mafia"))','@dp.message(Command("mafia_leave"))',
'@dp.message(Command("setrole"))','@dp.message(Command("release"))','@dp.message(Command("syncroles"))'
]
for x in required: assert x in s, x
forbidden=['@dp.message(Command("all_roles"))','@dp.message(Command("bind_group"))','@dp.message(Command("bind_info"))','@dp.message(Command("capture_list"))','@dp.message(Command("sync_list"))','update_group_roster','roster_rows_for','TEST_ROSTER_CHANNEL_ID']
for x in forbidden: assert x not in s, x
assert 'BotCommandScopeAllGroupChats' in s
assert 'BotCommandScopeAllChatAdministrators' in s
assert 'BotCommandScopeChatAdministrators' not in s
assert re.search(r'@dp\.message\(F\.chat\.type\.in_\(\{"group", "supergroup"\}\), F\.text, ~F\.text\.startswith\("/"\), ~F\.text\.regexp\(r"\^\\s\*\(\?:калл\|флудик\)\\b"\)\)', s)
# kall
parse_kall=ns['parse_kall']
assert parse_kall('калл') == ''
assert parse_kall('калл Кокоми') == 'Кокоми'
# fludik
parse=ns['parse_fludik_intent']
for x in ['флудик начни мафию','Флудик, запусти мафию','флудик давай мафию']:
    assert parse(x)=='start',x
for x in ['флудик статус','флудик сколько игроков','Флудик, кто в лобби']:
    assert parse(x)=='status',x
for x in ['флудик закрой мафию','флудик останови мафию']:
    assert parse(x)=='stop',x
for x in ['я хочу мафию','мафия завтра','флудик привет']:
    assert parse(x) is None,x
# mafia
assert '/start@MafiaAzBot' in s
assert "status='TRANSFERRED'" in s
assert 'Ваша роль' not in s
for bad in ['"Мирный"','"Комиссар"','"Доктор"']:
    assert bad not in s,bad
# pinning
assert 'pin_chat_message' in s
# role assignment button + FSM
assert 'fm:assign:' in s
assert 'RoleAssignState.waiting_for_role' in s
assert 'set_chat_member_tag' in s
assert 'restrict_chat_member' not in s
print('PASS roles=148 tags=148')
print('PASS commands/list-removal/routing')
print('PASS kall/fludik/mafia')
print('PASS pinning/role UI/tag API')

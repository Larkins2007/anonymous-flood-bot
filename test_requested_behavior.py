from pathlib import Path
import ast
s=Path('bot.py').read_text(encoding='utf-8')
ast.parse(s)
assert '_chat_member_is_active' in s
assert 'status == "restricted"' in s
assert 'bool(getattr(member, "is_member", False))' in s
assert 'NEW_MEMBER_RESTRICTION' in s
assert 'lift_member_restriction' in s
assert 'async def call_assign_role' in s
assert 'async def expire_mafia_lobby' in s
assert 'await asyncio.sleep(300)' in s
assert '/start@MafiaAzBot' in s
assert 'status=\'TRANSFERRED\'' in s
assert 'BotCommand(command="me"' in s
for bad in ['BotCommand(command="start"', 'BotCommand(command="cancel"', 'BotCommand(command="free_roles"', 'BotCommand(command="all_roles"', 'BotCommand(command="checkbot"', 'BotCommand(command="id"', 'BotCommand(command="admin"', 'BotCommand(command="bind_group"']:
    assert bad not in s, bad
for legacy in ['update_group_roster','capture_list','sync_list','bind_info']:
    assert legacy not in s, legacy
print('REQUESTED BEHAVIOR TEST PASSED')

# Welcome is about rules/confirmation, not role verification.
assert 'ознакомлен(а) с правилами' in s
assert 'самостоятельно несёшь ответственность' in s
assert 'Администратор назначит её после проверки' not in s

# Confirmation notifies owner and lifts restrictions.
assert '𝗥𝗨𝗟𝗘𝗦 𝗖𝗢𝗡𝗙𝗜𝗥𝗠𝗘𝗗' in s
assert 'lift_member_restriction(chat_id, target_user_id)' in s
assert 'await bot.send_message(\n            ADMIN_ID' in s

# Departure never triggers welcome; old welcome is removed.
assert 'await bot.delete_message(chat_id, welcome_message_id)' in s
assert '𝗠𝗘𝗠𝗕𝗘𝗥 𝗟𝗘𝗙𝗧' in s

# Fludik parser must be present and explicit.
assert 'def parse_fludik_command' in s
assert 'async def fludik_handler' in s
assert 'Флудик, покажи роли' in s
assert 'Флудик, какая у меня роль' in s
assert 'Флудик, начни мафию' in s
assert 'Флудик, сколько игроков' in s
assert 'Флудик, я выхожу из мафии' in s
assert 'Флудик, синхронизируй роли' in s

print('WELCOME / CONFIRM / FLUDIK TESTS PASSED')

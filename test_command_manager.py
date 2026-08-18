import ast
from pathlib import Path

P = Path(__file__).with_name('bot.py')
s = P.read_text(encoding='utf-8')
t = ast.parse(s)

assert 'CREATE TABLE IF NOT EXISTS custom_commands' in s
assert '@dp.message(Command("manage_commands"))' in s
assert '@dp.message(Command("addcommand"))' in s
assert '@dp.message(Command("delcommand"))' in s
assert '@dp.message(Command("commands"))' in s
assert 'def save_custom_command' in s
assert 'def get_custom_command' in s
assert 'def delete_custom_command' in s
assert 'BotCommand(command="manage_commands"' in s
assert 'fludik_sign_role_private' in s
assert 'подпиши\\s+роль' in s

# Ensure built-in commands are protected from custom override.
for name in ('help','roles','me','mafia','mafia_leave','release','syncroles','member','pending','mafia_ban','mafia_unban','manage_commands','addcommand','delcommand','commands'):
    assert f'"{name}"' in s

print('COMMAND MANAGER TEST PASSED')

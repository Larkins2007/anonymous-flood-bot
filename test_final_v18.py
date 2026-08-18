from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parent
SRC = (ROOT / 'bot.py').read_text(encoding='utf-8')
TREE = ast.parse(SRC)

# schedule command must be absent everywhere in runtime code
assert '@dp.message(Command("schedule"))' not in SRC
assert 'BotCommand(command="schedule"' not in SRC

# /start must remain a real handler and be visible in command setup
assert '@dp.message(CommandStart())' in SRC
assert 'BotCommand(command="start"' in SRC
assert 'create_feedback_notification' in SRC
assert 'async def delivery_worker' in SRC

# Anonymous UI must still exist
for marker in ['Анонимная обратная связь', 'u:send', 'u:report', 'FeedbackState.waiting']:
    assert marker in SRC, marker

# Primary chat must be fixed
assert 'PRIMARY_CHAT_ID = int(os.getenv("PRIMARY_CHAT_ID", "-1004313546398")' in SRC

print('V18 STATIC REGRESSION PASS')

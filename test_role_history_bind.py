import os, sqlite3, tempfile
os.environ.setdefault('BOT_TOKEN','test')
import bot

def test_role_history_table_and_catalog():
    with tempfile.TemporaryDirectory() as d:
        old=bot.DB_PATH; bot.DB_PATH=os.path.join(d,'t.db')
        bot.init_db(); bot.migrate_db(); bot.init_group_db(); bot.seed_role_catalog(); bot.migrate_group_state()
        conn=sqlite3.connect(bot.DB_PATH)
        assert conn.execute('select count(*) from role_catalog').fetchone()[0] == 148
        assert conn.execute("select name from sqlite_master where type='table' and name='role_history'").fetchone()
        bot.DB_PATH=old

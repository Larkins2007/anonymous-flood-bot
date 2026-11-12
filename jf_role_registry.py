"""Owner-managed character-to-Telegram-ID roster for Justice Faite."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone


def init(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS jf_role_id_registry (
        chat_id INTEGER NOT NULL,
        role_key TEXT NOT NULL,
        role_name TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(chat_id, role_key),
        UNIQUE(chat_id, user_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jf_role_id_user ON jf_role_id_registry(chat_id,user_id)")


def normalize(value: str) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def parse_lines(text: str):
    """Accept one `Character - numeric_id` mapping per line."""
    result, errors = [], []
    for line_no, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip().lstrip("•✦- ").strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^(.+?)\s*(?:—|-|:|\|)\s*(\d{5,16})\s*$", line)
        if not match:
            errors.append((line_no, raw[:100]))
            continue
        name, user_id = match.group(1).strip(), int(match.group(2))
        if not name or user_id <= 0:
            errors.append((line_no, raw[:100]))
            continue
        result.append((name, user_id))
    return result, errors


def upsert(conn: sqlite3.Connection, chat_id: int, role_key: str, role_name: str,
           user_id: int, updated_by: int = 0) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Release any previous character held by this ID and any previous ID
    # registered for the same character; the registry is one-to-one.
    old_for_user = conn.execute("SELECT role_key FROM jf_role_id_registry WHERE chat_id=? AND user_id=? AND role_key<>?",
                                (int(chat_id), int(user_id), str(role_key))).fetchall()
    old_for_role = conn.execute("SELECT user_id FROM jf_role_id_registry WHERE chat_id=? AND role_key=? AND user_id<>?",
                                (int(chat_id), str(role_key), int(user_id))).fetchall()
    for row in old_for_user:
        old_key = str(row["role_key"])
        conn.execute("UPDATE role_state SET user_id=NULL,status='free' WHERE chat_id=? AND role_key=? AND user_id=?",
                     (int(chat_id), old_key, int(user_id)))
        conn.execute("UPDATE group_members SET role_key=NULL,role_name=NULL WHERE chat_id=? AND user_id=? AND role_key=?",
                     (int(chat_id), int(user_id), old_key))
    for row in old_for_role:
        old_user = int(row["user_id"])
        conn.execute("UPDATE role_state SET user_id=NULL,status='free' WHERE chat_id=? AND role_key=? AND user_id=?",
                     (int(chat_id), str(role_key), old_user))
        conn.execute("UPDATE group_members SET role_key=NULL,role_name=NULL WHERE chat_id=? AND user_id=? AND role_key=?",
                     (int(chat_id), old_user, str(role_key)))
    conn.execute("DELETE FROM jf_role_id_registry WHERE chat_id=? AND user_id=? AND role_key<>?",
                 (int(chat_id), int(user_id), str(role_key)))
    conn.execute("""INSERT INTO jf_role_id_registry
        (chat_id,role_key,role_name,user_id,updated_at,updated_by) VALUES(?,?,?,?,?,?)
        ON CONFLICT(chat_id,role_key) DO UPDATE SET role_name=excluded.role_name,
        user_id=excluded.user_id,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
        (int(chat_id), str(role_key), str(role_name), int(user_id), now, int(updated_by)))


def remove(conn: sqlite3.Connection, chat_id: int, role_key: str) -> int:
    row = conn.execute("SELECT user_id FROM jf_role_id_registry WHERE chat_id=? AND role_key=?",
                       (int(chat_id), str(role_key))).fetchone()
    cur = conn.execute("DELETE FROM jf_role_id_registry WHERE chat_id=? AND role_key=?",
                       (int(chat_id), str(role_key)))
    if row:
        uid = int(row["user_id"])
        conn.execute("UPDATE role_state SET user_id=NULL,status='free' WHERE chat_id=? AND role_key=? AND user_id=?",
                     (int(chat_id), str(role_key), uid))
        conn.execute("UPDATE group_members SET role_key=NULL,role_name=NULL WHERE chat_id=? AND user_id=? AND role_key=?",
                     (int(chat_id), uid, str(role_key)))
    return cur.rowcount


def rows(conn: sqlite3.Connection, chat_id: int):
    return conn.execute("SELECT role_key,role_name,user_id FROM jf_role_id_registry WHERE chat_id=? ORDER BY role_name COLLATE NOCASE",
                        (int(chat_id),)).fetchall()

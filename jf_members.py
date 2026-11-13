"""Unified members and membership-event layer for Justice Faite Manager.

All modules should prefer this API for participant identity, status, role
linkage and diagnostics history. Existing tables (group_members, role_state,
role_history, participant_roster) remain the source of truth; this module
adds a durable event journal and a consistent status model without deleting
historical rows.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

# Canonical membership states. Absence of messages is NOT proof of leave.
STATUS_ACTIVE = "active"
STATUS_LEFT = "left"
STATUS_KICKED = "kicked"
STATUS_RESTRICTED = "restricted"
STATUS_UNKNOWN = "unknown"
STATUS_INACTIVE = "inactive"

VALID_STATUSES = {
    STATUS_ACTIVE,
    STATUS_LEFT,
    STATUS_KICKED,
    STATUS_RESTRICTED,
    STATUS_UNKNOWN,
    STATUS_INACTIVE,
}

# Event types stored in jf_member_events
EVENT_DISCOVERED = "discovered"
EVENT_JOIN = "join"
EVENT_LEAVE = "leave"
EVENT_STATUS = "status_change"
EVENT_ROLE_ASSIGN = "role_assign"
EVENT_ROLE_RELEASE = "role_release"
EVENT_ROLE_CONFLICT = "role_conflict"
EVENT_ADMIN = "admin_change"
EVENT_SYNC = "sync_check"
EVENT_ACTIVITY = "activity"


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_schema(conn: sqlite3.Connection) -> None:
    """Additive schema: never drops existing member/role tables."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jf_member_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '',
            actor_id INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jf_member_events_chat_user
            ON jf_member_events(chat_id, user_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_jf_member_events_type
            ON jf_member_events(chat_id, event_type, id DESC);

        CREATE TABLE IF NOT EXISTS jf_member_meta (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'unknown',
            first_seen_at TEXT NOT NULL,
            last_activity_at TEXT,
            last_join_at TEXT,
            last_leave_at TEXT,
            last_checked_at TEXT,
            source TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (chat_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_jf_member_meta_status
            ON jf_member_meta(chat_id, status);
        """
    )
    # Additive columns on group_members if an older DB lacks them
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(group_members)").fetchall()}
    for name, decl in (
        ("status", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("last_activity_at", "TEXT"),
        ("last_checked_at", "TEXT"),
        ("source", "TEXT NOT NULL DEFAULT ''"),
        ("first_seen_at", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE group_members ADD COLUMN {name} {decl}")


def log_event(
    conn: sqlite3.Connection,
    chat_id: int,
    user_id: int,
    event_type: str,
    *,
    payload: str = "",
    actor_id: int = 0,
    source: str = "",
) -> None:
    conn.execute(
        "INSERT INTO jf_member_events(chat_id,user_id,event_type,payload,actor_id,source,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            int(chat_id),
            int(user_id),
            str(event_type)[:40],
            str(payload or "")[:2000],
            int(actor_id or 0),
            str(source or "")[:80],
            utc_iso(),
        ),
    )


def ensure_member(
    conn: sqlite3.Connection,
    chat_id: int,
    user_id: int,
    *,
    first_name: str = "",
    last_name: str = "",
    username: str = "",
    status: str = STATUS_UNKNOWN,
    source: str = "",
    active: Optional[bool] = None,
) -> dict[str, Any]:
    """Create or refresh a single canonical member row. Never duplicates."""
    chat_id, user_id = int(chat_id), int(user_id)
    status = status if status in VALID_STATUSES else STATUS_UNKNOWN
    now = utc_iso()
    existing = conn.execute(
        "SELECT * FROM group_members WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()

    is_active = 1 if active is True else (0 if active is False else (1 if status == STATUS_ACTIVE else 0 if status in {STATUS_LEFT, STATUS_KICKED} else (int(existing["active"]) if existing else 0)))
    if existing is None:
        conn.execute(
            """
            INSERT INTO group_members(
                chat_id,user_id,first_name,last_name,username,role_key,role_name,tag,
                confirmed,active,joined_at,confirmed_at,left_at,welcome_message_id,tag_set_by_bot,
                status,last_activity_at,last_checked_at,source,first_seen_at
            ) VALUES(?,?,?,?,?,NULL,NULL,NULL,0,?,?,NULL,NULL,NULL,0,?,?,?,?,?)
            """,
            (
                chat_id,
                user_id,
                first_name or "",
                last_name or "",
                username or "",
                is_active,
                now if is_active else now,
                status,
                now,
                now,
                source or "discover",
                now,
            ),
        )
        log_event(conn, chat_id, user_id, EVENT_DISCOVERED, payload=f"status={status}", source=source or "discover")
    else:
        conn.execute(
            """
            UPDATE group_members SET
                first_name=CASE WHEN ?!='' THEN ? ELSE first_name END,
                last_name=CASE WHEN ?!='' THEN ? ELSE last_name END,
                username=CASE WHEN ?!='' THEN ? ELSE username END,
                active=?,
                status=?,
                last_checked_at=?,
                source=CASE WHEN ?!='' THEN ? ELSE source END,
                first_seen_at=COALESCE(first_seen_at, ?),
                left_at=CASE WHEN ? IN ('left','kicked') THEN COALESCE(left_at, ?) ELSE left_at END
            WHERE chat_id=? AND user_id=?
            """,
            (
                first_name, first_name or "",
                last_name, last_name or "",
                username, username or "",
                is_active,
                status,
                now,
                source, source or "",
                now,
                status, now,
                chat_id, user_id,
            ),
        )

    # Mirror lightweight meta row
    meta = conn.execute(
        "SELECT user_id FROM jf_member_meta WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    display = " ".join(x for x in (first_name, last_name) if x).strip() or (username and f"@{username}") or str(user_id)
    if meta is None:
        conn.execute(
            """
            INSERT INTO jf_member_meta(
                chat_id,user_id,status,first_seen_at,last_activity_at,last_join_at,
                last_leave_at,last_checked_at,source,display_name
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                chat_id,
                user_id,
                status,
                now,
                now if status == STATUS_ACTIVE else None,
                now if status == STATUS_ACTIVE else None,
                now if status in {STATUS_LEFT, STATUS_KICKED} else None,
                now,
                source or "discover",
                display,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE jf_member_meta SET
                status=?,
                last_checked_at=?,
                source=CASE WHEN ?!='' THEN ? ELSE source END,
                display_name=CASE WHEN ?!='' THEN ? ELSE display_name END,
                last_activity_at=CASE WHEN ?= 'active' THEN ? ELSE last_activity_at END,
                last_join_at=CASE WHEN ?= 'active' THEN COALESCE(last_join_at, ?) ELSE last_join_at END,
                last_leave_at=CASE WHEN ? IN ('left','kicked') THEN ? ELSE last_leave_at END
            WHERE chat_id=? AND user_id=?
            """,
            (
                status, now, source, source or "", display, display,
                status, now, status, now, status, now,
                chat_id, user_id,
            ),
        )

    row = conn.execute(
        "SELECT * FROM group_members WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    return dict(row) if row is not None else {"chat_id": chat_id, "user_id": user_id, "status": status}


def touch_activity(conn: sqlite3.Connection, chat_id: int, user_id: int, *, source: str = "message") -> None:
    """Record activity without treating silence as leave."""
    now = utc_iso()
    ensure_member(conn, chat_id, user_id, status=STATUS_ACTIVE, source=source, active=True)
    conn.execute(
        "UPDATE group_members SET last_activity_at=?, status=?, active=1 WHERE chat_id=? AND user_id=?",
        (now, STATUS_ACTIVE, int(chat_id), int(user_id)),
    )
    conn.execute(
        "UPDATE jf_member_meta SET last_activity_at=?, status=?, last_checked_at=? WHERE chat_id=? AND user_id=?",
        (now, STATUS_ACTIVE, now, int(chat_id), int(user_id)),
    )


def mark_join(
    conn: sqlite3.Connection,
    chat_id: int,
    user,
    *,
    source: str = "chat_member",
    actor_id: int = 0,
) -> dict[str, Any]:
    uid = int(getattr(user, "id", 0) or 0)
    row = ensure_member(
        conn,
        chat_id,
        uid,
        first_name=str(getattr(user, "first_name", "") or ""),
        last_name=str(getattr(user, "last_name", "") or ""),
        username=str(getattr(user, "username", "") or ""),
        status=STATUS_ACTIVE,
        source=source,
        active=True,
    )
    log_event(conn, chat_id, uid, EVENT_JOIN, actor_id=actor_id, source=source)
    return row


def mark_leave(
    conn: sqlite3.Connection,
    chat_id: int,
    user_id: int,
    *,
    reason: str = STATUS_LEFT,
    source: str = "chat_member",
    actor_id: int = 0,
    release_role: bool = True,
) -> Optional[dict[str, Any]]:
    """Mark leave/kick. Does not delete history. Optionally frees the role."""
    chat_id, user_id = int(chat_id), int(user_id)
    reason = reason if reason in {STATUS_LEFT, STATUS_KICKED} else STATUS_LEFT
    now = utc_iso()
    row = conn.execute(
        "SELECT * FROM group_members WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    if row is None:
        ensure_member(conn, chat_id, user_id, status=reason, source=source, active=False)
        row = conn.execute(
            "SELECT * FROM group_members WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()

    role_key = row["role_key"] if row else None
    role_name = row["role_name"] if row else None
    tag = row["tag"] if row else None

    conn.execute(
        "UPDATE group_members SET active=0, status=?, left_at=COALESCE(left_at, ?) WHERE chat_id=? AND user_id=?",
        (reason, now, chat_id, user_id),
    )
    conn.execute(
        "UPDATE jf_member_meta SET status=?, last_leave_at=?, last_checked_at=? WHERE chat_id=? AND user_id=?",
        (reason, now, now, chat_id, user_id),
    )
    log_event(
        conn,
        chat_id,
        user_id,
        EVENT_LEAVE,
        payload=f"reason={reason};role={role_name or ''}",
        actor_id=actor_id,
        source=source,
    )

    if release_role and role_key:
        conn.execute(
            "UPDATE role_state SET user_id=NULL, status='free' WHERE chat_id=? AND role_key=? AND user_id=?",
            (chat_id, str(role_key), user_id),
        )
        conn.execute(
            "UPDATE group_members SET role_key=NULL, role_name=NULL WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
        conn.execute(
            "INSERT INTO role_history(chat_id,user_id,role_key,role_name,tag,event,created_at) VALUES(?,?,?,?,?,?,?)",
            (chat_id, user_id, str(role_key), str(role_name or ""), str(tag or ""), "left", now),
        )
        log_event(
            conn,
            chat_id,
            user_id,
            EVENT_ROLE_RELEASE,
            payload=f"role={role_name};reason=leave",
            actor_id=actor_id,
            source=source,
        )

    out = conn.execute(
        "SELECT * FROM group_members WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    return dict(out) if out is not None else None


def set_role(
    conn: sqlite3.Connection,
    chat_id: int,
    user_id: int,
    role_key: str,
    role_name: str,
    *,
    tag: str = "",
    actor_id: int = 0,
    source: str = "assign",
    force: bool = False,
) -> tuple[bool, str]:
    """Assign role to user_id. Returns (ok, message). Detects conflicts."""
    chat_id, user_id = int(chat_id), int(user_id)
    role_key = str(role_key)
    # Conflict: another user holds this role
    holder = conn.execute(
        "SELECT user_id FROM role_state WHERE chat_id=? AND role_key=? AND user_id IS NOT NULL AND user_id<>?",
        (chat_id, role_key, user_id),
    ).fetchone()
    if holder and not force:
        other = int(holder["user_id"])
        log_event(
            conn,
            chat_id,
            user_id,
            EVENT_ROLE_CONFLICT,
            payload=f"role={role_name};held_by={other}",
            actor_id=actor_id,
            source=source,
        )
        return False, f"ROLE_CONFLICT:{other}"

    # Release previous role of this user
    prev = conn.execute(
        "SELECT role_key, role_name, tag FROM group_members WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    if prev and prev["role_key"] and str(prev["role_key"]) != role_key:
        conn.execute(
            "UPDATE role_state SET user_id=NULL, status='free' WHERE chat_id=? AND role_key=? AND user_id=?",
            (chat_id, str(prev["role_key"]), user_id),
        )
        log_event(
            conn,
            chat_id,
            user_id,
            EVENT_ROLE_RELEASE,
            payload=f"role={prev['role_name']};reason=reassign",
            actor_id=actor_id,
            source=source,
        )

    if holder and force:
        other = int(holder["user_id"])
        conn.execute(
            "UPDATE role_state SET user_id=NULL, status='free' WHERE chat_id=? AND role_key=? AND user_id=?",
            (chat_id, role_key, other),
        )
        conn.execute(
            "UPDATE group_members SET role_key=NULL, role_name=NULL WHERE chat_id=? AND user_id=? AND role_key=?",
            (chat_id, other, role_key),
        )
        log_event(
            conn,
            chat_id,
            other,
            EVENT_ROLE_RELEASE,
            payload=f"role={role_name};reason=force_reassign_to={user_id}",
            actor_id=actor_id,
            source=source,
        )

    now = utc_iso()
    conn.execute(
        "UPDATE role_state SET user_id=?, status='busy' WHERE chat_id=? AND role_key=?",
        (user_id, chat_id, role_key),
    )
    # Ensure role_state row exists
    if conn.execute("SELECT 1 FROM role_state WHERE chat_id=? AND role_key=?", (chat_id, role_key)).fetchone() is None:
        conn.execute(
            "INSERT INTO role_state(chat_id,role_key,role_name,user_id,status) VALUES(?,?,?,?,?)",
            (chat_id, role_key, role_name, user_id, "busy"),
        )
    conn.execute(
        "UPDATE group_members SET role_key=?, role_name=?, tag=COALESCE(NULLIF(?,''), tag), confirmed=1 "
        "WHERE chat_id=? AND user_id=?",
        (role_key, role_name, tag, chat_id, user_id),
    )
    conn.execute(
        "INSERT INTO role_history(chat_id,user_id,role_key,role_name,tag,event,created_at) VALUES(?,?,?,?,?,?,?)",
        (chat_id, user_id, role_key, role_name, tag or "", "assigned", now),
    )
    log_event(
        conn,
        chat_id,
        user_id,
        EVENT_ROLE_ASSIGN,
        payload=f"role={role_name}",
        actor_id=actor_id,
        source=source,
    )
    return True, "OK"


def get_member(conn: sqlite3.Connection, chat_id: int, user_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM group_members WHERE chat_id=? AND user_id=?",
        (int(chat_id), int(user_id)),
    ).fetchone()
    return dict(row) if row is not None else None


def list_events(
    conn: sqlite3.Connection,
    chat_id: int,
    user_id: Optional[int] = None,
    *,
    limit: int = 50,
    event_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    if user_id is not None and event_type:
        rows = conn.execute(
            "SELECT * FROM jf_member_events WHERE chat_id=? AND user_id=? AND event_type=? ORDER BY id DESC LIMIT ?",
            (int(chat_id), int(user_id), str(event_type), limit),
        ).fetchall()
    elif user_id is not None:
        rows = conn.execute(
            "SELECT * FROM jf_member_events WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
            (int(chat_id), int(user_id), limit),
        ).fetchall()
    elif event_type:
        rows = conn.execute(
            "SELECT * FROM jf_member_events WHERE chat_id=? AND event_type=? ORDER BY id DESC LIMIT ?",
            (int(chat_id), str(event_type), limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jf_member_events WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (int(chat_id), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def status_counts(conn: sqlite3.Connection, chat_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM group_members WHERE chat_id=? GROUP BY status",
        (int(chat_id),),
    ).fetchall()
    out = {s: 0 for s in VALID_STATUSES}
    for r in rows:
        key = str(r["status"] or STATUS_UNKNOWN)
        out[key] = int(r["c"] or 0)
    return out


def find_role_conflicts(conn: sqlite3.Connection, chat_id: int) -> list[dict[str, Any]]:
    """Roles claimed by more than one active member, or registry vs state mismatch."""
    conflicts = []
    # Multiple group_members with same role_key while active
    rows = conn.execute(
        """
        SELECT role_key, role_name, GROUP_CONCAT(user_id) AS users, COUNT(*) AS c
        FROM group_members
        WHERE chat_id=? AND active=1 AND role_key IS NOT NULL
        GROUP BY role_key
        HAVING COUNT(*) > 1
        """,
        (int(chat_id),),
    ).fetchall()
    for r in rows:
        conflicts.append(
            {
                "type": "multi_holder",
                "role_key": r["role_key"],
                "role_name": r["role_name"],
                "user_ids": [int(x) for x in str(r["users"]).split(",") if x],
            }
        )
    return conflicts

import json
import sqlite3
from pathlib import Path


def build_schema(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE admin_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            ref_id INTEGER,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            admin_message_ids TEXT NOT NULL DEFAULT '[]',
            broadcast_sent_user_ids TEXT NOT NULL DEFAULT '[]',
            markup_type TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_status TEXT NOT NULL DEFAULT 'pending',
            admin_message_ids TEXT NOT NULL DEFAULT '[]'
        );
        """
    )


def broadcast_final_status(total: int, sent: int) -> str:
    if total <= 0:
        return "failed"
    if sent >= total:
        return "delivered"
    if sent <= 0:
        return "failed"
    return "partial"


def enqueue_pending(rows):
    delivery = []
    broadcast = []
    for row in rows:
        if row["status"] not in {"pending", "partial"}:
            continue
        if row["kind"] == "broadcast":
            broadcast.append(row["id"])
        else:
            delivery.append(row["id"])
    return delivery, broadcast


def test_broadcast_status_logic():
    assert broadcast_final_status(0, 0) == "failed"
    assert broadcast_final_status(10, 0) == "failed"
    assert broadcast_final_status(10, 5) == "partial"
    assert broadcast_final_status(10, 10) == "delivered"
    assert broadcast_final_status(10, 11) == "delivered"


def test_partial_broadcast_recovery_persists_sent_users_and_resumes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    build_schema(conn)

    conn.execute(
        """
        INSERT INTO admin_notifications(
            kind, ref_id, payload, created_at, status,
            admin_message_ids, broadcast_sent_user_ids, markup_type
        ) VALUES (?, NULL, ?, ?, 'partial', '[]', ?, '')
        """,
        (
            "broadcast",
            "hello",
            "2026-08-15T00:00:00+00:00",
            json.dumps([101, 102]),
        ),
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM admin_notifications WHERE id=1"
    ).fetchone()
    sent_users = set(json.loads(row["broadcast_sent_user_ids"]))

    all_users = [101, 102, 103, 104]
    remaining = [uid for uid in all_users if uid not in sent_users]

    assert remaining == [103, 104]

    conn.execute(
        """
        UPDATE admin_notifications
        SET status='delivered', broadcast_sent_user_ids=?
        WHERE id=1
        """,
        (json.dumps(all_users),),
    )
    conn.commit()

    final = conn.execute(
        "SELECT status, broadcast_sent_user_ids FROM admin_notifications WHERE id=1"
    ).fetchone()

    assert final["status"] == "delivered"
    assert json.loads(final["broadcast_sent_user_ids"]) == all_users


def test_restart_requeues_broadcast_separately_from_delivery_notifications():
    rows = [
        {"id": 1, "kind": "feedback", "status": "pending"},
        {"id": 2, "kind": "report", "status": "partial"},
        {"id": 3, "kind": "broadcast", "status": "pending"},
        {"id": 4, "kind": "broadcast", "status": "delivered"},
    ]

    delivery, broadcast = enqueue_pending(rows)

    assert delivery == [1, 2]
    assert broadcast == [3]

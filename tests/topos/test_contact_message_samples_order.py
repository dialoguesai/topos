"""Contact message samples: true recency order (not SQLite TEXT sort)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sqlite3

from topos.storage.canonical.conversations_tables import (
    ConversationsTablesManager,
    ensure_all_tables,
)


def test_get_contact_message_samples_newest_first_despite_string_order():
    conn = sqlite3.connect(":memory:")
    ensure_all_tables(conn)
    mgr = ConversationsTablesManager(conn)
    ds, src, sender = "d1", "imessage", "+15551234567"
    # Lexicographic order would put "2024-..." after "2025-..." wrong if compared badly;
    # here we use dates where naive TEXT sort reverses true chronology.
    rows = [
        ("m-old", "c1", "old", "2025-02-01T10:00:00Z"),
        ("m-mid", "c1", "mid", "2025-06-01T10:00:00Z"),
        ("m-new", "c1", "new", "2025-12-01T10:00:00Z"),
    ]
    for mid, cid, body, ev in rows:
        conn.execute(
            """
            INSERT INTO conversation_messages
            (message_id, conversation_id, dataset_id, sender_id, content, event_at, source_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (mid, cid, ds, sender, body, ev, src),
        )
    conn.commit()

    samples = mgr.get_contact_message_samples(
        dataset_id=ds, source_id=src, identifier=sender, limit=5
    )
    bodies = [s["content"] for s in samples]
    assert bodies == ["new", "mid", "old"]


def test_get_contact_message_samples_falls_back_to_created_at_when_event_at_empty():
    conn = sqlite3.connect(":memory:")
    ensure_all_tables(conn)
    mgr = ConversationsTablesManager(conn)
    ds, src, sender = "d1", "imessage", "+15559998888"
    conn.execute(
        """
        INSERT INTO conversation_messages
        (message_id, conversation_id, dataset_id, sender_id, content, event_at, source_id, created_at)
        VALUES ('a', 'c1', ?, ?, 'first', '', ?, '2020-01-01T00:00:00Z')
        """,
        (ds, sender, src),
    )
    conn.execute(
        """
        INSERT INTO conversation_messages
        (message_id, conversation_id, dataset_id, sender_id, content, event_at, source_id, created_at)
        VALUES ('b', 'c1', ?, ?, 'second', '', ?, '2025-01-01T00:00:00Z')
        """,
        (ds, sender, src),
    )
    conn.commit()

    samples = mgr.get_contact_message_samples(
        dataset_id=ds, source_id=src, identifier=sender, limit=5
    )
    assert [s["content"] for s in samples] == ["second", "first"]

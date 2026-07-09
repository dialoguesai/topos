"""Regression tests for load_canonical_records_for_signal.

Guards the re-enrich loader against the schema drift that crashed live
re-enrichment: the conversations branch queried a nonexistent ``ts`` column
(live schema uses ``event_at``) and dropped the owner-identity fields the
provenance role gates need (P1.3). See canonical_pipeline.py.
"""

import sqlite3

from topos.ingestion.canonical_pipeline import load_canonical_records_for_signal
from topos.sources.registry import REGISTRY


def _conversation_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE conversation_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            sender_type TEXT,
            sender_id TEXT,
            is_from_self INTEGER,
            actor_role TEXT,
            content TEXT,
            event_at TEXT,
            source_id TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("m1", "c1", "self", "self", 1, "authored", "owner line", "2026-01-02T00:00:00Z", "demo_messenger_file"),
            ("m2", "c1", "contact", "bram", 0, "observed", "other line", "2026-01-03T00:00:00Z", "demo_messenger_file"),
        ],
    )
    conn.commit()
    return conn


def test_conversations_loader_uses_event_at_and_carries_identity():
    conn = _conversation_db()
    source_def = REGISTRY["demo_messenger_file"]

    records = load_canonical_records_for_signal(conn, source_def)

    assert len(records) == 2
    # ORDER BY event_at DESC — newest first (would have raised on the old `ts`).
    assert [r["message_id"] for r in records] == ["m2", "m1"]

    owner = next(r for r in records if r["message_id"] == "m1")
    assert owner["is_from_self"] == 1
    assert owner["sender_id"] == "self"
    assert owner["actor_role"] == "authored"
    # both `ts` and `event_at` are populated from the event_at column
    assert owner["ts"] == owner["event_at"] == "2026-01-02T00:00:00Z"

    other = next(r for r in records if r["message_id"] == "m2")
    assert other["is_from_self"] == 0
    assert other["actor_role"] == "observed"


def test_loader_empty_on_missing_args():
    assert load_canonical_records_for_signal(None, REGISTRY["demo_messenger_file"]) == []
    conn = _conversation_db()
    assert load_canonical_records_for_signal(conn, None) == []

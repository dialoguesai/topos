"""Force/normal enrichment reprocess must be source-aware, not ai_chat-only.

2026-07-09: POST /enrichment/process silently returned "No unprocessed
messages" for every non-chat source — BOTH branches (force and the
_find_unprocessed_messages diff) read only ai_chat_messages. The fix routes
canonical loading through load_canonical_records_for_signal (all groups),
keeping the enriched-ids diff for the non-force path.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from topos.api.enrichment import _load_canonical_messages_for_enrichment
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "e.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _source(source_id: str, group: str):
    return SimpleNamespace(source_id=source_id, canonical_group_id=group)


def test_journal_source_loads_canonical_messages(conn):
    conn.execute(
        "INSERT INTO journal_entries (entry_id, source_id, entry_at, content) "
        "VALUES ('j1', 'grow_journal', '2026-06-01T10:00:00Z', 'ran with Ada at Zilker')"
    )
    conn.commit()
    msgs = _load_canonical_messages_for_enrichment(
        conn, _source("grow_journal", "journal"), dataset_id=None
    )
    assert len(msgs) == 1
    msg = msgs[0]
    # EntitiesJob keys on message_id + content + event_at + a table stamp.
    assert msg.get("message_id") == "j1"
    assert "ran with Ada" in str(msg.get("content"))
    assert msg.get("event_at") == "2026-06-01T10:00:00Z"
    assert msg.get("_table") == "journal_entries"


def test_activity_source_maps_title_to_content(conn):
    conn.execute(
        "INSERT INTO activity_events (event_id, activity_type, url, title, occurred_at, source_id) "
        "VALUES ('ev1', 'visit', 'https://x', 'Alpha School pilot notes', '2026-06-02T09:00:00Z', 'browser_visits')"
    )
    conn.commit()
    msgs = _load_canonical_messages_for_enrichment(
        conn, _source("browser_visits", "activity"), dataset_id=None
    )
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.get("message_id") == "ev1"
    assert msg.get("content") == "Alpha School pilot notes"  # title → content for NER
    assert msg.get("event_at") == "2026-06-02T09:00:00Z"
    assert msg.get("_table") == "activity_events"


def test_conversations_source_carries_identity_fields(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_messages ("
        "message_id TEXT PRIMARY KEY, conversation_id TEXT, sender_type TEXT, "
        "sender_id TEXT, is_from_self INTEGER, actor_role TEXT, content TEXT, "
        "event_at TEXT, source_id TEXT)"
    )
    conn.execute(
        "INSERT INTO conversation_messages "
        "(message_id, conversation_id, sender_type, sender_id, is_from_self, actor_role, content, event_at, source_id) "
        "VALUES ('m1', 'c1', 'human', 'Speaker 1', 0, 'observed', 'Marcus said hi', '2026-06-01T10:00:00Z', 'voxterm_transcripts')"
    )
    conn.commit()
    msgs = _load_canonical_messages_for_enrichment(
        conn, _source("voxterm_transcripts", "conversations"), dataset_id=None
    )
    assert len(msgs) == 1
    # identity fields ride through so record_role doesn't fail closed
    assert msgs[0].get("actor_role") == "observed"
    assert msgs[0].get("_table") == "conversation_messages"


def test_unknown_group_returns_empty_not_error(conn):
    msgs = _load_canonical_messages_for_enrichment(
        conn, _source("mystery", "no_such_group"), dataset_id=None
    )
    assert msgs == []

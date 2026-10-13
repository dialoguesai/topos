"""Canonical evidence must beat submission dates and lossy discovery previews."""
import sqlite3
from datetime import datetime, timezone
import pytest
from topos.query.retrieval import (
    _load_recent_summary_items, _restore_index_event_clocks,
    _hydrate_owner_index_previews,
)

NOW = datetime(2026, 10, 12, 23, tzinfo=timezone.utc)
WINDOW = ("2026-10-11T00:00:00Z", "2026-10-11T23:59:59Z")

def database(kind="ai_chat_message"):
    c = sqlite3.connect(":memory:")
    c.executescript("""
    CREATE TABLE signal_embeddings(record_id TEXT,source_id TEXT,signal_dimension TEXT,
      text_preview TEXT,event_at TEXT,chunk_index INTEGER,record_type TEXT);
    CREATE TABLE ai_chat_messages(message_id TEXT,event_at TEXT,content TEXT,content_disclosure TEXT);
    CREATE TABLE conversation_messages(message_id TEXT,event_at TEXT,content TEXT,content_disclosure TEXT);
    CREATE TABLE journal_entries(entry_id TEXT,entry_at TEXT,content TEXT,content_disclosure TEXT);
    """)
    table = {"ai_chat_message":"ai_chat_messages", "conversation_message":"conversation_messages", "journal_entry":"journal_entries"}[kind]
    c.execute(f"INSERT INTO {table} VALUES ('old','2026-08-01T12:00:00Z','RAW SECRET','old event')")
    c.execute(f"INSERT INTO {table} VALUES ('current','2026-10-11T15:00:00Z','RAW SECRET','I proposed a review. It has not happened yet.')")
    for rid, indexed in [("old", "2026-10-11T23:00:00Z"), ("current", "2026-10-12T12:00:00Z")]:
        c.execute("INSERT INTO signal_embeddings VALUES (?, 'source','memory','I proposed a review.',?,0,?)", (rid,indexed,kind))
    return c

@pytest.mark.parametrize("kind", ["ai_chat_message", "conversation_message", "journal_entry"])
def test_canonical_clock_is_applied_before_filter_and_limit(kind):
    items = _load_recent_summary_items(database(kind), time_range=WINDOW, now=NOW, limit=1)
    assert [x["record_id"] for x in items] == ["current"]
    assert items[0]["event_at"] == "2026-10-11T15:00:00Z"
    assert "not happened yet" in items[0]["summary_text"]
    assert "SECRET" not in str(items)

def test_semantic_hit_dates_are_corrected_and_missing_sources_are_undated():
    items = _restore_index_event_clocks([
        {"record_id":"old", "record_type":"ai_chat_message", "event_at":"2026-10-11T23:00:00Z"},
        {"record_id":"missing", "record_type":"ai_chat_message", "event_at":"2026-10-11T23:00:00Z"},
    ], database())
    assert items[0]["event_at"] == "2026-08-01T12:00:00Z"
    assert items[1]["event_at"] is None

def test_owner_hydration_never_falls_back_to_raw_content():
    c = database()
    c.execute("UPDATE ai_chat_messages SET content_disclosure=NULL")
    items = _load_recent_summary_items(c, time_range=WINDOW, now=NOW)
    assert items[0]["summary_text"] == "I proposed a review."
    assert "SECRET" not in str(items)

def test_grantee_hydration_does_not_expand_payload():
    items = [{"record_id":"current", "record_type":"ai_chat_message", "summary_text":"preview"}]
    assert _hydrate_owner_index_previews(items, database(), disclosure_tier="summary") == items

def test_long_disclosure_has_explicit_truncation():
    c = database()
    c.execute("UPDATE ai_chat_messages SET content_disclosure=?", ("x"*7000,))
    items = _load_recent_summary_items(c, time_range=WINDOW, now=NOW)
    assert items[0]["text_truncated"] is True
    assert items[0]["summary_text"].endswith("[truncated]")
    assert len(items[0]["summary_text"]) < 6100

def test_missing_optional_canonical_table_retains_legacy_preview():
    c = database()
    c.execute("DROP TABLE ai_chat_messages")
    items = _load_recent_summary_items(c, time_range=WINDOW, now=NOW)
    assert items[0]["record_id"] == "old"
    assert items[0]["summary_text"] == "I proposed a review."

def test_unknown_source_does_not_borrow_a_same_id_canonical_body():
    c = database()
    c.execute("UPDATE signal_embeddings SET record_type=NULL")
    items = _load_recent_summary_items(c, time_range=WINDOW, now=NOW)
    assert "text_basis" not in items[0]

def test_nonwindow_recency_compares_instants_and_excludes_future():
    c = database()
    c.execute("UPDATE ai_chat_messages SET event_at='2026-10-12T18:30:00-06:00' WHERE message_id='current'")
    assert not _load_recent_summary_items(c, now=NOW)

"""WS-D: enrichment-backed request filters (SQL pushdown + fail-closed) and narrowing."""

from __future__ import annotations

import sqlite3

import pytest

from shared.filtering import FILTER_CATALOG, FilterRuntimeStatus, filter_manifest_from_storage
from topos.uma_filters import (
    apply_filter_manifest,
    build_sql_constraints,
    enrichment_filters_in_manifest,
    strip_enrichment_retrieval_filters,
)


def _manifest(*filters):
    return filter_manifest_from_storage({"filters": list(filters)})


def test_catalog_promotes_enrichment_filters():
    for fid in ("topic_filter", "entity_filter", "emotion_filter"):
        assert fid in FILTER_CATALOG
        assert FILTER_CATALOG[fid].runtime_status == FilterRuntimeStatus.SUPPORTED_NOW


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE conversation_messages (
            message_id TEXT PRIMARY KEY,
            dataset_id TEXT,
            event_at TEXT,
            content TEXT,
            source_id TEXT
        );
        CREATE TABLE message_topics (
            topic_id TEXT PRIMARY KEY,
            record_id TEXT,
            message_id TEXT,
            source_id TEXT,
            topic TEXT,
            payload_json TEXT
        );
        CREATE TABLE message_emotions (
            emotion_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            emotion_label TEXT,
            payload_json TEXT
        );
        """
    )
    for i in range(4):
        c.execute(
            "INSERT INTO conversation_messages VALUES (?, 'ds', ?, ?, 'src')",
            (f"m{i}", f"2026-07-0{i + 1}T00:00:00Z", f"content {i}"),
        )
    c.execute("INSERT INTO message_topics VALUES ('t1','m1','m1','src','fundraising','{}')")
    c.execute("INSERT INTO message_topics VALUES ('t2','m2','m2','src','cooking','{}')")
    c.execute("INSERT INTO message_emotions VALUES ('e1','m3','src','joy','{}')")
    c.commit()
    yield c
    c.close()


def _fetch_ids(conn, manifest):
    where, params = build_sql_constraints(
        manifest, "m.", logical_table_id="conversation_messages", conn=conn
    )
    rows = conn.execute(
        f"SELECT m.message_id FROM conversation_messages m WHERE m.dataset_id='ds'{where}",
        params,
    ).fetchall()
    return {row[0] for row in rows}


def test_topic_filter_sql_pushdown(conn):
    manifest = _manifest({"filter_id": "topic_filter", "params": {"topics": ["Fundraising"]}})
    assert _fetch_ids(conn, manifest) == {"m1"}


def test_emotion_filter_sql_pushdown(conn):
    manifest = _manifest({"filter_id": "emotion_filter", "params": {"emotions": ["JOY"]}})
    assert _fetch_ids(conn, manifest) == {"m3"}


def test_enrichment_filter_fails_closed_without_table(conn):
    # message_entities table does not exist in this fixture.
    manifest = _manifest({"filter_id": "entity_filter", "params": {"entities": ["Sarah"]}})
    assert _fetch_ids(conn, manifest) == set()


def test_enrichment_filter_combines_with_time_filters(conn):
    manifest = _manifest(
        {"filter_id": "topic_filter", "params": {"topics": ["fundraising", "cooking"]}},
        {"filter_id": "date_range", "params": {"start": "2026-07-03 00:00:00", "end": "2026-07-10 00:00:00"}},
    )
    # m1 (07-02) is excluded by date_range; m2 (07-03) matches both.
    assert _fetch_ids(conn, manifest) == {"m2"}


def test_enrichment_filter_skipped_for_other_tables(conn):
    manifest = _manifest({"filter_id": "topic_filter", "params": {"topics": ["fundraising"]}})
    where, params = build_sql_constraints(
        manifest, "", logical_table_id="calendar_events", conn=conn
    )
    assert where == ""
    assert params == []


def test_strip_and_detect_enrichment_filters():
    manifest = _manifest(
        {"filter_id": "topic_filter", "params": {"topics": ["a"]}},
        {"filter_id": "max_rows", "params": {"count": 5}},
    )
    assert enrichment_filters_in_manifest(manifest) == ["topic_filter"]
    stripped = strip_enrichment_retrieval_filters(manifest)
    assert [f.filter_id for f in stripped.filters] == ["max_rows"]


def test_post_fetch_fallback_fails_closed():
    manifest = _manifest({"filter_id": "topic_filter", "params": {"topics": ["fundraising"]}})
    rows = [{"message_id": "m1", "content": "no topic fields here"}]
    assert apply_filter_manifest(rows, manifest) == []


def test_post_fetch_fallback_matches_row_fields():
    manifest = _manifest({"filter_id": "emotion_filter", "params": {"emotions": ["joy"]}})
    rows = [
        {"message_id": "m1", "emotion": "joy"},
        {"message_id": "m2", "emotion": "anger"},
    ]
    out = apply_filter_manifest(rows, manifest)
    assert [r["message_id"] for r in out] == ["m1"]


def test_narrowing_sql_helper():
    from topos.core.handlers.uma import _narrowing_sql

    where, params = _narrowing_sql(None, "m.")
    assert where == "" and params == []
    where, params = _narrowing_sql(set(), "m.")
    assert where == " AND 1=0"
    where, params = _narrowing_sql({"a", "b"}, "m.")
    assert "m.message_id IN (?,?)" in where
    assert sorted(params) == ["a", "b"]

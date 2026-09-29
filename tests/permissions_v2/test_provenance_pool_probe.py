"""RD0/RD1: the count-only provenance pool probe reads a copy, reports staleness and drain, and leaks nothing.

  P1  a published recovery reads as current, with its links by event day and the day the pool empties
  P2  a watched source-clock move reads as stale; a sync receipt does not
  P3  the report names no message, dataset or enrollment, and holds no content
  P4  the copy is opened immutable (no sidecar appears) and the live tree is refused
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401
from topos.permissions_v2.reconciliation_provenance import publish_existing

import scripts.permissions_v2.p2c_provenance_pool as pool

EVENT = datetime(2023, 3, 8, 20, 26, 40, tzinfo=timezone.utc)


def published_copy(fixture, tmp_path) -> Path:
    service, conn, _, enrollment = fixture
    with owner():
        publish_existing(service, conn, enrollment_id=enrollment)
    return copy_of(service, conn, tmp_path)


def copy_of(service, conn, tmp_path) -> Path:
    conn.commit()
    copy = tmp_path / "census-copy"
    (copy / "permissions-v2").mkdir(parents=True)
    with sqlite3.connect(copy / "database.db") as target:
        conn.backup(target)
    shutil.copy(service.marker, copy / "permissions-v2" / "ingest-snapshots.enrollment.json")
    return copy


def test_P1_published_recovery_reads_current_with_its_drain(legacy, tmp_path):
    copy = published_copy(legacy, tmp_path)
    report = pool.probe(copy, now=EVENT + timedelta(days=10))
    assert report["store"]["installed"] is True
    assert report["store"]["schema_matches_clock_version"] == 2
    assert report["store"]["trigger_forms"]["user_ingestion_sources"]["update_form"] == "update_of"
    assert report["store"]["marker"]["state"] == "active"
    [enrollment] = report["enrollments"]
    assert enrollment["lane"] == pool.RECONCILIATION_LANE
    assert (enrollment["state"], enrollment["stale"], enrollment["jobs"]) == ("active", False, {"done": 1})
    assert (enrollment["linked"], enrollment["linked_row_present"], enrollment["linked_dataset_match"]) == (1, 1, 1)
    assert enrollment["linked_revision_match"] == 1
    assert enrollment["linked_by_event_day"] == {"2023-03-08": 1}
    assert enrollment["interval_end_between"][0] == "2023-03-08"
    assert report["pool"]["in_window_now"] == 1
    # The last linked day is inside the window until its whole UTC day is older than 30 days.
    assert report["pool"]["empty_by"] == "2023-04-08"
    assert report["pool"]["series"][-1] == {"at": "2023-04-08", "in_window": 0}
    assert report["pool"]["series"][-2] == {"at": "2023-04-07", "in_window": 1}


@pytest.mark.parametrize("sql", [
    "UPDATE user_ingestion_sources SET enabled=0",
    "UPDATE user_ingestion_sources SET posture='ambient'",
    "INSERT INTO user_ingestion_sources VALUES('another-dataset','another-source',1,NULL)",
])
def test_P2_a_watched_source_change_reads_stale(legacy, tmp_path, sql):
    service, conn, _, enrollment = legacy
    with owner():
        publish_existing(service, conn, enrollment_id=enrollment)
    conn.execute(sql)
    copy = copy_of(service, conn, tmp_path)
    report = pool.probe(copy, now=EVENT + timedelta(days=10))
    assert report["enrollments"][0]["stale"] is True
    assert report["store"]["generation"] > report["enrollments"][0]["source_generation"]


def test_P2_a_sync_receipt_leaves_it_current(legacy, tmp_path):
    service, conn, _, enrollment = legacy
    with owner():
        publish_existing(service, conn, enrollment_id=enrollment)
    conn.execute("ALTER TABLE user_ingestion_sources ADD COLUMN last_sync_at TEXT")
    conn.execute("UPDATE user_ingestion_sources SET last_sync_at='2023-03-09T00:00:00+00:00'")
    copy = copy_of(service, conn, tmp_path)
    assert pool.probe(copy, now=EVENT + timedelta(days=10))["enrollments"][0]["stale"] is False


def _strings(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)
    elif isinstance(value, str):
        yield value


def test_P3_the_report_names_nothing(legacy, tmp_path):
    service, conn, _, enrollment = legacy
    copy = published_copy(legacy, tmp_path)
    report = pool.probe(copy, now=EVENT + timedelta(days=10))
    text, leaves = json.dumps(report), set(_strings(report))
    row = conn.execute("SELECT message_id, conversation_id, dataset_id, content FROM conversation_messages").fetchone()
    job = conn.execute("SELECT job_id FROM ingest_provenance_jobs").fetchone()[0]
    snapshot = json.loads(conn.execute("SELECT snapshot_json FROM ingest_provenance_enrollments").fetchone()[0])
    for value in (*row, enrollment, job, snapshot["snapshot_id"], snapshot["snapshot_sha256"], "owner-1"):
        assert value not in leaves
        # Short synthetic ids (a conversation ROWID) also occur inside dates; long ones must not occur anywhere.
        assert len(value) < 8 or value not in text


def test_P4_immutable_open_and_live_refusal(legacy, tmp_path, monkeypatch):
    copy = published_copy(legacy, tmp_path)
    before = sorted(path.name for path in copy.iterdir())
    pool.probe(copy, now=EVENT + timedelta(days=10))
    assert sorted(path.name for path in copy.iterdir()) == before
    monkeypatch.setattr(pool, "_live_home", lambda: copy.resolve())
    with pytest.raises(SystemExit):
        pool.probe(copy)


def test_P5_boundary_sizes_only(legacy, tmp_path):
    service, conn, _, enrollment = legacy
    copy = published_copy(legacy, tmp_path)
    caps = pool.probe(copy, now=EVENT + timedelta(days=10), boundary=True)["caps"]
    assert caps["boundary_active"] is False and caps["max_rows"] == 100_000
    measured = caps["boundary"]
    # Inactive (no Off-limits entity) or unavailable on this minimal schema: either way sizes and codes only.
    assert set(measured) <= {"available", "code", "active", "protected_ids", "contacts", "terms", "handles",
                             "mentions", "mentions_headroom", "vocabulary_chars", "vocabulary_cap", "vocabulary_headroom"}


def test_P6_index_members_are_read_by_date_only(legacy, tmp_path):
    from topos.permissions_v2.search_index import _DDL
    copy = published_copy(legacy, tmp_path)
    root = copy / "permissions-v2" / "message-search"
    root.mkdir()
    with sqlite3.connect(root / "grant-synthetic.db") as index:
        for sql in _DDL:
            index.execute(sql)
        index.execute("INSERT INTO meta VALUES (1,'topos-p2c-index/v1','{}','ready',NULL,NULL,2)")
        for opaque, day in (("r.a", EVENT), ("r.b", EVENT + timedelta(days=5))):
            index.execute("INSERT INTO members VALUES (?,?,?,?,?)",
                          (opaque, int(day.timestamp() * 1_000_000), 3, '{"sentinelterm": 1}', b"sealed"))
        index.execute("INSERT INTO vectors VALUES ('r.a', 0, x'00')")
    report = pool.probe(copy, now=EVENT + timedelta(days=10))
    [found] = report["indexes"]
    assert (found["member_count"], found["members_with_vectors"], found["undated"]) == (2, 1, 0)
    assert found["members_by_event_day"] == {"2023-03-08": 1, "2023-03-13": 1}
    assert found["drain"]["empty_by"] == "2023-04-13"
    assert "sentinelterm" not in json.dumps(report) and "grant-synthetic" not in json.dumps(report)

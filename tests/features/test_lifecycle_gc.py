"""Derived-layer GC: stale top_topics, brief compaction, audit retention,
junk-embedding purge, deprecation marking.

Pins the audit fixes for unbounded growth: 11k stale top_topics objects for
6 live clusters, ~128 full brief snapshots/day, ~100 audit rows/day with no
retention, and NSKeyedArchiver junk still embedded.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from topos.features.lifecycle.gc import (
    apply_audit_retention,
    compact_brief_revisions,
    mark_deprecated_tables,
    purge_junk_embeddings,
    reconcile_top_topics_objects,
    run_gc,
)


@pytest.fixture()
def conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE signal_objects (
            object_id TEXT PRIMARY KEY, signal_dimension TEXT, object_type TEXT,
            object_key TEXT, payload_json TEXT, confidence REAL,
            source_refs_json TEXT, valid_from TEXT, valid_to TEXT,
            extractor_version TEXT, created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT, created_by TEXT, updated_by TEXT
        );
        CREATE TABLE topic_clusters (cluster_id TEXT PRIMARY KEY, label TEXT);
        CREATE TABLE signal_dimension_briefs (
            brief_id TEXT PRIMARY KEY, signal_dimension TEXT,
            head_revision_id TEXT
        );
        CREATE TABLE signal_dimension_brief_revisions (
            revision_id TEXT PRIMARY KEY, brief_id TEXT,
            parent_revision_id TEXT, revision_number INTEGER,
            change_kind TEXT, structured_json TEXT, markdown_body TEXT,
            provenance_json TEXT, created_at TEXT, created_by TEXT
        );
        CREATE TABLE uma_access_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT
        );
        CREATE TABLE mcp_request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, requested_at TEXT
        );
        CREATE TABLE signal_embeddings (
            embedding_id TEXT PRIMARY KEY, record_id TEXT, text_preview TEXT
        );
        CREATE TABLE topic_cluster_members (
            member_id TEXT PRIMARY KEY, cluster_id TEXT, record_id TEXT
        );
        CREATE TABLE wiki_table_catalog (
            table_name TEXT PRIMARY KEY, authoritative_table TEXT,
            status TEXT, deprecation_note TEXT, updated_at TEXT
        );
        CREATE TABLE persons (person_id TEXT PRIMARY KEY);
        """
    )
    yield conn
    conn.close()


class TestTopTopicsReconciliation:
    def test_stale_objects_removed_live_kept(self, conn):
        conn.execute("INSERT INTO topic_clusters VALUES ('tc_live', 'training')")
        for key in ("tc_live", "tc_dead_1", "tc_dead_2"):
            conn.execute(
                "INSERT INTO signal_objects (object_id, object_type, object_key)"
                " VALUES (?, 'top_topics', ?)",
                (uuid.uuid4().hex, key),
            )
        conn.execute(
            "INSERT INTO signal_objects (object_id, object_type, object_key)"
            " VALUES (?, 'user_goals', 'tc_dead_1')",
            (uuid.uuid4().hex,),
        )
        removed = reconcile_top_topics_objects(conn)
        assert removed == 2
        remaining = conn.execute(
            "SELECT object_type, object_key FROM signal_objects ORDER BY object_type"
        ).fetchall()
        # live top_topics kept; unrelated object types never touched
        assert ("top_topics", "tc_live") in remaining
        assert ("user_goals", "tc_dead_1") in remaining


class TestBriefCompaction:
    def _seed_revisions(self, conn, brief_id: str, *, days_ago: int, per_day: int, start_num: int) -> list[str]:
        # Anchor at MIDDAY of the target day, not at "now minus N days". These
        # tests assert one-survivor-per-day, so the seeds must all land on the
        # same calendar day — and datetime('now') is UTC, so a suite running in
        # the last few minutes before UTC midnight pushed the '+i minutes'
        # offsets across the boundary. Compaction then correctly kept one per
        # day for TWO days and the assertion failed. Observed for real: the
        # release gate on 2026-08-10 ran 23:54 -> 00:00 UTC and went red here
        # with removed=4, survivors=[12, 15].
        ids = []
        num = start_num
        for i in range(per_day):
            rid = f"rev_{brief_id}_{days_ago}_{i}"
            conn.execute(
                """
                INSERT INTO signal_dimension_brief_revisions
                    (revision_id, brief_id, revision_number, change_kind, created_at)
                VALUES (?, ?, ?, 'ingest_merge',
                        datetime('now', ?, 'start of day', '+12 hours', ?))
                """,
                (rid, brief_id, num, f"-{days_ago} days", f"+{i} minutes"),
            )
            ids.append(rid)
            num += 1
        return ids

    def test_recent_revisions_all_survive(self, conn):
        conn.execute("INSERT INTO signal_dimension_briefs VALUES ('b1', 'interests', NULL)")
        self._seed_revisions(conn, "b1", days_ago=2, per_day=5, start_num=10)
        removed = compact_brief_revisions(conn)
        assert removed == 0

    def test_mid_window_keeps_one_per_day(self, conn):
        conn.execute("INSERT INTO signal_dimension_briefs VALUES ('b1', 'interests', NULL)")
        self._seed_revisions(conn, "b1", days_ago=30, per_day=6, start_num=10)
        removed = compact_brief_revisions(conn)
        assert removed == 5
        left = conn.execute(
            "SELECT revision_number FROM signal_dimension_brief_revisions"
        ).fetchall()
        assert [r[0] for r in left] == [15]  # newest of the day survives

    def test_head_revision_protected(self, conn):
        ids = None
        conn.execute("INSERT INTO signal_dimension_briefs VALUES ('b1', 'interests', 'rev_b1_30_0')")
        ids = self._seed_revisions(conn, "b1", days_ago=30, per_day=3, start_num=10)
        assert ids[0] == "rev_b1_30_0"
        compact_brief_revisions(conn)
        left = {
            r[0]
            for r in conn.execute(
                "SELECT revision_id FROM signal_dimension_brief_revisions"
            ).fetchall()
        }
        assert "rev_b1_30_0" in left  # head survives even though not newest

    def test_old_revisions_keep_one_per_week(self, conn):
        conn.execute("INSERT INTO signal_dimension_briefs VALUES ('b1', 'interests', NULL)")
        # Two consecutive days deep in the past, several revisions each.
        # They land in one or two ISO weeks depending on today's date, so the
        # invariant is: at most one keeper per week touched.
        self._seed_revisions(conn, "b1", days_ago=200, per_day=4, start_num=10)
        self._seed_revisions(conn, "b1", days_ago=201, per_day=4, start_num=2)
        removed = compact_brief_revisions(conn)
        left = conn.execute(
            "SELECT COUNT(*) FROM signal_dimension_brief_revisions"
        ).fetchone()[0]
        assert left in (1, 2)  # weekly keeper(s)
        assert removed == 8 - left


class TestAuditRetention:
    def test_old_rows_trimmed_recent_kept(self, conn):
        conn.execute("INSERT INTO uma_access_requests (created_at) VALUES (datetime('now', '-120 days'))")
        conn.execute("INSERT INTO uma_access_requests (created_at) VALUES (datetime('now', '-5 days'))")
        conn.execute("INSERT INTO mcp_request_log (requested_at) VALUES (datetime('now', '-120 days'))")
        removed = apply_audit_retention(conn, days=90)
        assert removed == {"uma_access_requests": 1, "mcp_request_log": 1}
        assert conn.execute("SELECT COUNT(*) FROM uma_access_requests").fetchone()[0] == 1

    def test_zero_days_disables(self, conn):
        conn.execute("INSERT INTO uma_access_requests (created_at) VALUES (datetime('now', '-120 days'))")
        removed = apply_audit_retention(conn, days=0)
        assert sum(removed.values()) == 0


class TestJunkPurge:
    def test_junk_previews_removed_with_cluster_members(self, conn):
        conn.executemany(
            "INSERT INTO signal_embeddings VALUES (?, ?, ?)",
            [
                ("e1", "r1", "a normal message about lunch"),
                ("e2", "r2", "streamtyped NSKeyedArchiver \x01 blob"),
            ],
        )
        conn.execute("INSERT INTO topic_cluster_members VALUES ('m1', 'tc_a', 'r2')")
        purged = purge_junk_embeddings(conn)
        assert purged == 1
        assert conn.execute("SELECT COUNT(*) FROM signal_embeddings").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM topic_cluster_members").fetchone()[0] == 0


class TestDeprecationMarking:
    def test_existing_deprecated_tables_marked_not_dropped(self, conn):
        marked = mark_deprecated_tables(conn)
        assert marked == 1  # only `persons` exists in this fixture
        row = conn.execute(
            "SELECT status, deprecation_note FROM wiki_table_catalog WHERE table_name='persons'"
        ).fetchone()
        assert row[0] == "deprecated"
        assert "entities" in row[1]
        # Never dropped: live code still references these tables.
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='persons'"
        ).fetchone()


def test_run_gc_reports_all_sections(conn):
    report = run_gc(conn)
    assert set(report) == {
        "top_topics_objects_removed",
        "brief_revisions_compacted",
        "junk_embeddings_purged",
        "deprecated_tables_marked",
        "audit_rows_trimmed",
    }

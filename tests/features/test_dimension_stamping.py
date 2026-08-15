"""Real signal dimensions at ingest + backfill of the 'memory' monoculture.

The audit found all 21k live embeddings carried signal_dimension='memory'
(the embeddings job defaulted it), turning faceted clustering, dimension
filters, and brief scoping into no-ops. These tests pin the write-time
mapping and the one-time backfill migration.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.signal.embed_context import dimension_for_record
from topos.storage.db.migrations.journal_origin_dimension_v1 import (
    restamp_journal_origin_dimensions,
)
from topos.storage.db.migrations.signal_dimension_backfill_v1 import (
    apply_signal_dimension_backfill_v1_up,
    backfill_signal_dimensions,
)


class TestDimensionForRecord:
    @pytest.mark.parametrize(
        "msg,record_type,expected",
        [
            ({}, "conversation_message", "relationships"),
            ({}, "journal_entry", "wellbeing"),
            ({}, "activity_event", "interests"),
            ({}, "calendar_event", "time"),
            ({}, "experience", "work"),
            ({}, "ai_chat_message", "memory"),
            ({"canonical_table": "financial_transactions"}, None, "resources"),
            ({"_table": "location_events"}, None, "places"),
            ({"record_type": "journal_entry"}, None, "wellbeing"),
            ({}, None, "memory"),
        ],
    )
    def test_mapping(self, msg, record_type, expected):
        assert dimension_for_record(msg, record_type=record_type) == expected

    def test_explicit_dimension_wins(self):
        msg = {"signal_dimension": "work", "record_type": "journal_entry"}
        assert dimension_for_record(msg) == "work"


class TestBackfill:
    @pytest.fixture()
    def conn(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE signal_embeddings (
                embedding_id TEXT PRIMARY KEY, record_type TEXT,
                signal_dimension TEXT
            );
            CREATE TABLE timeline (
                record_id TEXT PRIMARY KEY, canonical_table TEXT,
                record_type TEXT, signal_dimension TEXT
            );
            CREATE TABLE wiki_schema_migrations (
                migration_id TEXT PRIMARY KEY,
                applied_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        conn.executemany(
            "INSERT INTO signal_embeddings VALUES (?, ?, ?)",
            [
                ("e1", "conversation_message", "memory"),
                ("e2", "journal_entry", "memory"),
                ("e3", "ai_chat_message", "memory"),
                ("e4", "experience", None),
                ("e5", "conversation_message", "work"),  # explicit — untouched
                ("e6", "", "memory"),  # unknown kind — stays memory
            ],
        )
        conn.executemany(
            "INSERT INTO timeline VALUES (?, ?, ?, ?)",
            [
                ("t1", "conversation_messages", "", ""),
                ("t2", "journal_entries", "", None),
                ("t3", "ai_chat_messages", "", ""),
            ],
        )
        yield conn
        conn.close()

    def test_backfill_maps_known_kinds(self, conn):
        counts = backfill_signal_dimensions(conn)
        assert counts["signal_embeddings"] == 3  # e1, e2, e4
        assert counts["timeline"] == 2  # t1, t2

        rows = dict(
            conn.execute(
                "SELECT embedding_id, signal_dimension FROM signal_embeddings"
            ).fetchall()
        )
        assert rows["e1"] == "relationships"
        assert rows["e2"] == "wellbeing"
        assert rows["e3"] == "memory"  # ai chat stays default
        assert rows["e4"] == "work"
        assert rows["e5"] == "work"  # explicit non-memory value untouched
        assert rows["e6"] == "memory"

        t_rows = dict(
            conn.execute("SELECT record_id, signal_dimension FROM timeline").fetchall()
        )
        assert t_rows["t1"] == "relationships"
        assert t_rows["t2"] == "wellbeing"
        assert t_rows["t3"] in ("", None)  # ai chat timeline untouched

    def test_migration_is_gated_and_recorded(self, conn):
        apply_signal_dimension_backfill_v1_up(conn)
        row = conn.execute(
            "SELECT 1 FROM wiki_schema_migrations WHERE migration_id='signal_dimension_backfill_v1'"
        ).fetchone()
        assert row is not None
        # Re-running is harmless (idempotent WHERE clauses).
        counts = backfill_signal_dimensions(conn)
        assert counts == {"signal_embeddings": 0, "timeline": 0}


class TestJournalOriginBeatsRecordKind:
    """A commit is work; what a person writes in a journal is wellbeing.

    The GitHub connector writes one journal row per authored commit, and the
    kind-only map stamped every one of them `wellbeing`. On one live node that
    was 123 embeddings and 19 of 163 clusters named "Wellbeing Tracker (…)"
    over terms like "merge branch" and "gitignore".
    """

    @pytest.mark.parametrize(
        "source_id,expected",
        [
            ("github_activity", "work"),
            ("GitHub_Activity", "work"),  # source ids are matched case-insensitively
            ("grow_journal", "wellbeing"),
            ("grow_data_file", "wellbeing"),
            ("", "wellbeing"),
        ],
    )
    def test_journal_dimension_follows_its_source(self, source_id, expected):
        msg = {"source_id": source_id} if source_id else {}
        assert dimension_for_record(msg, record_type="journal_entry") == expected

    def test_other_kinds_from_the_same_source_are_untouched(self):
        """Only the journal lane is overridden — github activity stays interests."""
        msg = {"source_id": "github_activity"}
        assert dimension_for_record(msg, record_type="activity_event") == "interests"

    def test_an_explicit_dimension_still_wins(self):
        msg = {"source_id": "github_activity", "signal_dimension": "memory"}
        assert dimension_for_record(msg, record_type="journal_entry") == "memory"

    def _db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE signal_embeddings (record_type TEXT, source_id TEXT, signal_dimension TEXT)"
        )
        conn.executemany(
            "INSERT INTO signal_embeddings VALUES (?,?,?)",
            [
                ("journal_entry", "github_activity", "wellbeing"),
                ("journal_entry", "github_activity", "wellbeing"),
                ("journal_entry", "grow_journal", "wellbeing"),
                ("activity_event", "github_activity", "interests"),
                ("journal_entry", "github_activity", "resources"),
            ],
        )
        return conn

    def test_restamp_moves_only_the_commit_journal_rows(self):
        conn = self._db()
        counts = restamp_journal_origin_dimensions(conn)
        assert counts["signal_embeddings"] == 2
        got = dict(
            conn.execute(
                "SELECT signal_dimension, COUNT(*) FROM signal_embeddings GROUP BY 1"
            ).fetchall()
        )
        # two moved to work; the hand-written journal, the activity row and the
        # deliberately-set 'resources' row are all left alone.
        assert got == {"work": 2, "wellbeing": 1, "interests": 1, "resources": 1}

    def test_restamp_is_idempotent(self):
        conn = self._db()
        restamp_journal_origin_dimensions(conn)
        assert restamp_journal_origin_dimensions(conn)["signal_embeddings"] == 0

    def test_restamp_survives_a_db_without_the_tables(self):
        conn = sqlite3.connect(":memory:")
        assert restamp_journal_origin_dimensions(conn) == {"signal_embeddings": 0, "timeline": 0}

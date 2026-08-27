"""SGU-12 — reads stay reads: no write lock, no 500 on a mid-upgrade schema, fast.

Found while measuring: `ensure_directed_tables_present` ran DDL on the read path, which
takes SQLite's WRITE LOCK on every page load and raises outright on a read-only connection.
The reads now check for the table instead, and select only the columns that exist.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from topos.analytics.relationship_reads import (read_bench, read_directed_edges,
                                                read_relationship_signals, read_relationships)

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
DS = "ds"


def _populate(path):
    c = sqlite3.connect(str(path))
    c.execute("""CREATE TABLE conversation_messages (
        conversation_id TEXT, message_id TEXT PRIMARY KEY, dataset_id TEXT,
        sender_id TEXT, event_at TEXT, is_from_self INTEGER, source_id TEXT,
        reply_to_message_id TEXT)""")
    n = 0
    for i in range(10):
        for is_self in (0, 1):
            n += 1
            c.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?)",
                      ("c1", f"m{n}", DS, None if is_self else "+15125551234",
                       (T0 + timedelta(days=i, minutes=is_self * 3)).isoformat(),
                       is_self, "imessage", None))
    c.commit()
    from topos.analytics.messenger_communities import _compute_directed_lane
    _compute_directed_lane(c, DS, None)
    c.close()


def test_reads_work_on_a_read_only_connection(tmp_path):
    """The strongest statement of 'a read is a read': if any read still ran DDL, a
    read-only connection would raise 'attempt to write a readonly database'."""
    db = tmp_path / "ro.db"
    _populate(db)
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    assert read_relationships(ro, dataset_id=DS)["count"] >= 1
    assert read_relationship_signals(ro, dataset_id=DS)["dyads_considered"] >= 1
    assert read_directed_edges(ro, dataset_id=DS)["count"] >= 1
    assert "roles" in read_bench(ro)
    ro.close()


def test_a_node_with_no_tables_answers_empty_rather_than_creating_them(tmp_path):
    """A read whose table is absent has an honest answer already: nothing computed yet."""
    c = sqlite3.connect(str(tmp_path / "bare.db"))
    rel = read_relationships(c, dataset_id=DS)
    assert rel == {"dataset_id": DS, "count": 0, "unnamed_count": 0, "relationships": []}
    assert read_directed_edges(c, dataset_id=DS)["edges"] == []
    assert read_relationship_signals(c, dataset_id=DS)["dyads_considered"] == 0
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "messenger_dyad_stats" not in tables, "a read must not create tables"
    c.close()


def test_a_pre_upgrade_schema_serves_what_it_has(tmp_path):
    """warmth_band, affect_* and topic_* arrived after their tables did. A node whose lane
    has not re-run must still serve counts and latencies rather than 500."""
    db = tmp_path / "old.db"
    _populate(db)
    c = sqlite3.connect(str(db))
    for col in ("warmth_band",):
        c.execute(f"ALTER TABLE messenger_dyad_stats DROP COLUMN {col}")
    for col in ("affect_counts_json", "affect_coverage", "topic_counts_json", "topic_coverage"):
        c.execute(f"ALTER TABLE messenger_directed_edges DROP COLUMN {col}")
    c.commit()

    rel = read_relationships(c, dataset_id=DS)
    assert rel["count"] >= 1
    assert rel["relationships"][0]["warmth_band"] is None, "absent reads as unknown"
    assert rel["relationships"][0]["total_msgs"] >= 1, "the rest is still true"

    edges = read_directed_edges(c, dataset_id=DS)
    assert edges["count"] >= 1
    e = edges["edges"][0]
    # null, never 0.0 — a measured "nothing" and "not measured" are different claims
    assert e["affect_coverage"] is None and e["topic_coverage"] is None
    assert e["msgs"] >= 1
    c.close()


@pytest.mark.parametrize("read", ["relationships", "signals", "edges", "bench"])
def test_reads_stay_within_the_render_budget(tmp_path, read):
    """300ms budget: these run on the relay of a single-worker CP, where a slow read is
    every tenant's slow read. Measured on the live corpus copy: 5.6 / 1.6 / 0.3 / 2.4 ms."""
    db = tmp_path / "budget.db"
    _populate(db)
    c = sqlite3.connect(str(db))
    fns = {
        "relationships": lambda: read_relationships(c, dataset_id=DS, limit=500),
        "signals": lambda: read_relationship_signals(c, dataset_id=DS),
        "edges": lambda: read_directed_edges(c, dataset_id=DS),
        "bench": lambda: read_bench(c),
    }
    fns[read]()  # warm
    start = time.time()
    fns[read]()
    elapsed_ms = (time.time() - start) * 1000
    c.close()
    assert elapsed_ms < 300, f"{read} took {elapsed_ms:.0f}ms"

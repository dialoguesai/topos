"""A topic-cluster edge must not claim the owner wrote it.

Live regression (2026-08-10): every materialized ``discusses`` edge carried
``actor_role='authored'``. Nobody asserts a topic cluster, so these edges passed
no ``asserted_by``, and ``_asserted_by_role(None)`` defaults to "owner" —
stamping GitHub-feed and browser exposure in the owner's own voice. The
attribution overlay read 91.5% authored on a 6-day window where the owner had
authored a fraction of it.

roles.py is explicit that ambiguity must fail toward the LESS-attributing role,
never toward authored.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.fact_materializer import _cluster_actor_role
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"), check_same_thread=False)
    apply_all_migrations(c)
    yield c
    c.close()


def _member(conn, cluster_id, record_id, table, source_id):
    conn.execute(
        "INSERT INTO topic_cluster_members (cluster_id, record_id, source_id) VALUES (?, ?, ?)",
        (cluster_id, record_id, source_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO timeline (event_at, record_id, source_id, canonical_table) "
        "VALUES (datetime('now'), ?, ?, ?)",
        (record_id, source_id, table),
    )


def test_browser_and_github_clusters_are_ambient_not_authored(conn):
    for i in range(5):
        _member(conn, "c1", f"r{i}", "activity_events", "github_activity")
    conn.commit()
    assert _cluster_actor_role(conn, "c1") == "ambient"


def test_owner_journal_cluster_stays_authored(conn):
    """The fix must not under-attribute the owner's own writing."""
    for i in range(5):
        _member(conn, "c2", f"j{i}", "journal_entries", "grow_journal")
        conn.execute(
            "INSERT INTO journal_entries (entry_id, entry_at, content, source_id) "
            "VALUES (?, datetime('now'), 'x', 'grow_journal')",
            (f"j{i}",),
        )
    conn.commit()
    assert _cluster_actor_role(conn, "c2") == "authored"


def test_unresolvable_cluster_fails_toward_ambient(conn):
    """No members we can place → say so, do not claim authorship."""
    assert _cluster_actor_role(conn, "does-not-exist") == "ambient"


def test_mixed_cluster_takes_the_majority(conn):
    for i in range(6):
        _member(conn, "c3", f"a{i}", "activity_events", "browser_visits")
    for i in range(2):
        _member(conn, "c3", f"b{i}", "journal_entries", "grow_journal")
        conn.execute(
            "INSERT INTO journal_entries (entry_id, entry_at, content, source_id) "
            "VALUES (?, datetime('now'), 'x', 'grow_journal')",
            (f"b{i}",),
        )
    conn.commit()
    assert _cluster_actor_role(conn, "c3") == "ambient"

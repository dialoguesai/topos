"""Retiring url_classification takes its tags and nothing else.

The dangerous half of this migration is the DELETE, not the DROP: interests
holds other things — keyword-rule activity tags from the same materializer, and
personal `fact` objects (goes_by, hails_from) that have nothing to do with URL
classification. A filter on dimension+object_type alone would take all of them.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.storage.db.migrations.retire_url_classification_v1 import (
    apply_retire_url_classification_v1_up,
)


def _obj(dim, otype, key, payload, extractor="scope_materializer_v1"):
    return (dim, otype, key, json.dumps(payload), extractor)


@pytest.fixture
def conn() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(
        """
        CREATE TABLE signal_objects (
            object_id INTEGER PRIMARY KEY,
            signal_dimension TEXT, object_type TEXT, object_key TEXT,
            payload_json TEXT, extractor_version TEXT
        )
        """
    )
    db.execute("CREATE TABLE browser_url_classification (record_id TEXT, url_category TEXT)")
    db.execute("CREATE TABLE wiki_schema_migrations (migration_id TEXT PRIMARY KEY)")
    db.executemany(
        "INSERT INTO browser_url_classification (record_id, url_category) VALUES (?, ?)",
        [("r1", "Reference"), ("r2", "Computers")],
    )
    db.executemany(
        "INSERT INTO signal_objects "
        "(signal_dimension, object_type, object_key, payload_json, extractor_version) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            _obj("interests", "activity_tags", "r1:Reference",
                 {"tag": "Reference", "source_kind": "browser_url_classification"}),
            _obj("interests", "activity_tags", "r2:Computers",
                 {"tag": "Computers", "source_kind": "browser_url_classification"}),
            # The keyword-rule fallback in the same materializer — must survive.
            _obj("interests", "activity_tags", "e9:edtech", {"tag": "edtech"}),
            # A personal fact that happens to live in interests — must survive.
            _obj("interests", "fact", "fact:self:goes_by",
                 {"predicate": "goes_by", "object_value": "Jonny Johnson"}, "facts_v1"),
            # Another dimension entirely — must survive.
            _obj("work", "activity_tags", "w1:Business",
                 {"tag": "Business", "source_kind": "browser_url_classification"}),
        ],
    )
    yield db
    db.close()


def _keys(db):
    return {r[0] for r in db.execute("SELECT object_key FROM signal_objects")}


def _has_table(db, name):
    return bool(db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def test_drops_the_table(conn) -> None:
    assert _has_table(conn, "browser_url_classification")
    apply_retire_url_classification_v1_up(conn)
    assert not _has_table(conn, "browser_url_classification")


def test_removes_only_the_tags_it_wrote(conn) -> None:
    apply_retire_url_classification_v1_up(conn)
    remaining = _keys(conn)

    assert "r1:Reference" not in remaining
    assert "r2:Computers" not in remaining
    assert "e9:edtech" in remaining, "keyword-rule tags are not this job's output"
    assert "fact:self:goes_by" in remaining, "personal facts live in interests too"


def test_leaves_other_dimensions_alone(conn) -> None:
    apply_retire_url_classification_v1_up(conn)
    assert "w1:Business" in _keys(conn)


def test_leaves_no_open_transaction(conn) -> None:
    """The migration must commit, including when it matched nothing.

    Migrations commit internally — the runner does not. A DELETE that matched
    zero rows still opens an implicit transaction, and leaving it open holds the
    write lock for the rest of the process: every later migration in the chain
    then fails with "database is locked". That is how this was caught, and it is
    the same failure shape as the 2026-08-06 clear_derivation_retry leak.
    """
    apply_retire_url_classification_v1_up(conn)
    assert not conn.in_transaction
    conn.execute("BEGIN IMMEDIATE")  # the statement that used to die
    conn.execute("ROLLBACK")

    # And again on the second, no-op pass.
    apply_retire_url_classification_v1_up(conn)
    assert not conn.in_transaction


def test_records_itself_in_the_ledger(conn) -> None:
    """Without this the runner re-reports it as pending forever."""
    apply_retire_url_classification_v1_up(conn)
    row = conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id='retire_url_classification_v1'"
    ).fetchone()
    assert row is not None


def test_is_idempotent(conn) -> None:
    apply_retire_url_classification_v1_up(conn)
    before = _keys(conn)
    apply_retire_url_classification_v1_up(conn)  # must not raise on the missing table
    assert _keys(conn) == before


def test_runs_on_a_node_that_never_had_the_table() -> None:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE signal_objects (object_id INTEGER PRIMARY KEY, signal_dimension TEXT, "
        "object_type TEXT, object_key TEXT, payload_json TEXT, extractor_version TEXT)"
    )
    db.execute("CREATE TABLE wiki_schema_migrations (migration_id TEXT PRIMARY KEY)")
    apply_retire_url_classification_v1_up(db)  # no table, no rows — must be a clean no-op
    db.close()

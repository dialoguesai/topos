"""The A3 quarantine queue must survive the orphan sweep.

`sweep_orphans` deletes `fact_conflicts` rows whose incumbent fact is gone — a
real orphan, because the row exists to challenge that fact. A3 quarantine rows
have no incumbent by design: an unroutable or policy-withheld assertion is not
challenging anything, so `DerivationWriter._quarantine` stores the synthetic
sentinel ``quarantine:<reason>``.

That sentinel is never a `signal_objects.object_id`, so the orphan predicate
matched every quarantine row. Measured on the live node 2026-08-26: all 13
pending rows would have been deleted, and the sweep runs from the owner's
ordinary per-record exclusion flow.

What that queue holds is the human review path for third-party assertions —
including `health.condition` about people who never consented — so deleting it
silently discards the decisions a person is supposed to make. It also became
load-bearing the moment net-subject writes were switched to always-quarantine.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.lifecycle.derived_scrub import sweep_orphans
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "q.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _conflict(conn, conflict_id: str, incumbent: str) -> None:
    conn.execute(
        "INSERT INTO fact_conflicts (conflict_id, subject_entity_id, predicate, "
        "incumbent_object_id, challenger_value, challenger_confidence, status) "
        "VALUES (?, 'ent_owner', 'rel.relationship', ?, '{\"person\":\"x\"}', 0.9, 'pending')",
        (conflict_id, incumbent),
    )


def _live_fact(conn, object_id: str) -> None:
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, "
        "payload_json, valid_from, created_at, updated_at) "
        "VALUES (?, 'relationships', 'fact', ?, '{}', datetime('now'), datetime('now'), datetime('now'))",
        (object_id, f"fact:ent_owner:{object_id}"),
    )


def _ids(conn):
    return {r[0] for r in conn.execute("SELECT incumbent_object_id FROM fact_conflicts")}


def test_quarantine_rows_survive_the_sweep(conn):
    """The regression: every live quarantine shape, none of them deleted."""
    _conflict(conn, "c1", "quarantine:dossier_unresolved:Kim")
    _conflict(conn, "c2", "quarantine:about_unclear")
    _conflict(conn, "c3", "quarantine:net_subject_disabled:Nora")
    conn.commit()

    report = sweep_orphans(conn)

    assert report["fact_conflicts"] == 0, "quarantine rows are not orphans"
    assert _ids(conn) == {
        "quarantine:dossier_unresolved:Kim",
        "quarantine:about_unclear",
        "quarantine:net_subject_disabled:Nora",
    }


def test_a_genuinely_orphaned_conflict_is_still_deleted(conn):
    """The sweep must keep doing its actual job — this is not a blanket exemption."""
    _conflict(conn, "c1", "fact:ent_owner:vanished")
    conn.commit()

    report = sweep_orphans(conn)

    assert report["fact_conflicts"] == 1
    assert _ids(conn) == set()


def test_a_conflict_against_a_live_fact_is_kept(conn):
    _live_fact(conn, "fact_live_1")
    _conflict(conn, "c1", "fact_live_1")
    conn.commit()

    assert sweep_orphans(conn)["fact_conflicts"] == 0
    assert _ids(conn) == {"fact_live_1"}


def test_mixed_queue_keeps_quarantine_and_drops_the_orphan(conn):
    """The realistic shape: a review queue with one genuine orphan in it."""
    _live_fact(conn, "fact_live_1")
    _conflict(conn, "c1", "quarantine:dossier_unresolved:grandpa")
    _conflict(conn, "c2", "fact:ent_owner:vanished")
    _conflict(conn, "c3", "fact_live_1")
    conn.commit()

    report = sweep_orphans(conn)

    assert report["fact_conflicts"] == 1
    assert _ids(conn) == {"quarantine:dossier_unresolved:grandpa", "fact_live_1"}

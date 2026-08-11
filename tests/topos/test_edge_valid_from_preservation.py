"""A rebuild must not restart the belief clock on edges it rebuilds.

`valid_from`/`valid_to` are BELIEF validity — "when the edge started being
held" (update_edge in edges.py, and the FactStore successor chaining). They are
deliberately not event dates: an edge derived today from 2023 evidence began
being believed today, so `valid_from > last_event_at` is normal and expected.

The bug was narrower. `rebuild_evidence_edges` deletes and re-inserts the whole
co-occurrence set, and stamped a fresh `_now_iso()` on every row — restarting
the belief clock on every rebuild. On a live node 492 edges claimed to have
begun at whichever rebuild happened to run last, and the prior date was gone.

So the contract is: carry the prior belief date across the swap; only genuinely
new edges begin believing now. Never clamp to the evidence date — that asserts
a belief history that never happened.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.edges import EDGE_CO_OCCURRENCE, update_edge
from topos.features.entities.maintenance import rebuild_evidence_edges
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "e.db"), check_same_thread=False)
    apply_all_migrations(c)
    yield c
    c.close()


def _entity(conn, entity_id, name, is_self=0):
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self) "
        "VALUES (?, 'person', ?, ?, ?)",
        (entity_id, name, name.lower(), is_self),
    )


def _mention(conn, record_id, entity_id, event_at):
    conn.execute(
        "INSERT INTO entity_mentions (entity_id, record_id, source_id, canonical_table, event_at) "
        "VALUES (?, ?, 'src', 'conversation_messages', ?)",
        (entity_id, record_id, event_at),
    )


def _active(conn):
    return {
        (s, d, t): (vf, le)
        for s, d, t, vf, le in conn.execute(
            "SELECT src_entity_id, dst_entity_id, edge_type, valid_from, last_event_at "
            "FROM entity_edges WHERE valid_to IS NULL AND edge_type=?",
            (EDGE_CO_OCCURRENCE,),
        )
    }


def test_belief_date_survives_a_rebuild(conn):
    """The regression: every rebuild used to re-stamp valid_from with now."""
    _entity(conn, "e1", "Ada")
    _entity(conn, "e2", "Grace")
    _mention(conn, "r1", "e1", "2023-04-11T10:01:44Z")
    _mention(conn, "r1", "e2", "2023-04-11T10:01:44Z")
    conn.commit()

    rebuild_evidence_edges(conn)
    first = _active(conn)
    assert first, "expected a co-occurrence edge"
    original_valid_from = {k: v[0] for k, v in first.items()}

    rebuild_evidence_edges(conn)
    second = _active(conn)
    assert {k: v[0] for k, v in second.items()} == original_valid_from


def test_belief_may_legitimately_postdate_the_evidence(conn):
    """valid_from > last_event_at is CORRECT here — not corruption.

    The edge is derived now from 2023 evidence, so belief starts now. An earlier
    version of this fix clamped valid_from back to the event date, which asserts
    the owner believed something years before the derivation existed.
    """
    _entity(conn, "e1", "Ada")
    _entity(conn, "e2", "Grace")
    _mention(conn, "r1", "e1", "2023-04-11T10:01:44Z")
    _mention(conn, "r1", "e2", "2023-04-11T10:01:44Z")
    conn.commit()

    rebuild_evidence_edges(conn)
    for valid_from, last_event in _active(conn).values():
        assert last_event.startswith("2023-04-11")
        assert valid_from > last_event, "belief starts when derived, not when the event happened"


def test_a_genuinely_new_edge_starts_believing_now(conn):
    _entity(conn, "e1", "Ada")
    _entity(conn, "e2", "Grace")
    _entity(conn, "e3", "Alan")
    _mention(conn, "r1", "e1", "2023-04-11T10:01:44Z")
    _mention(conn, "r1", "e2", "2023-04-11T10:01:44Z")
    conn.commit()
    rebuild_evidence_edges(conn)
    before = _active(conn)

    # New evidence introduces a pair that was never believed before.
    _mention(conn, "r2", "e1", "2026-08-10T12:00:00Z")
    _mention(conn, "r2", "e3", "2026-08-10T12:00:00Z")
    conn.commit()
    rebuild_evidence_edges(conn)
    after = _active(conn)

    fresh = set(after) - set(before)
    assert fresh, "expected a new pair"
    for key in before:
        assert after[key][0] == before[key][0], "existing belief dates must not move"

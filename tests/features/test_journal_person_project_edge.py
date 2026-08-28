"""A journal entry that names a person AND a project must connect the two.

THE ACCEPTANCE TEST for the declared-column lane. The owner logs a work session
by typing who they were with (`people`) and what they were working on
(`category`) into two adjacent columns of one row. Before this lane, the graph
derived nothing from that pairing.

Measured on the live node 2026-08-28, before: 13 journal rows declare Rowan a
participant and 10 of those declare `category='Topos'` — the largest
person×project cell in the journal — and `entity_edges` held ZERO edges of any
type between him and any Topos entity. The entity spine held 2 mentions for him,
both scraped back out of prose.

Replayed over the same 370 real rows after: 13 mentions, and
`Topos (project) --co_occurrence ev=10--> Rowan (person)` as his strongest edge.

Two columns, one row, no model. This test pins that it stays that way.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.declared_mappings import extract_declared_entities
from topos.features.entities.edges import record_cooccurrence_pairs, update_edge
from topos.features.entities.resolver import EntityResolver
from topos.features.entities.structured_fields import record_structured_mentions

#: Shaped exactly like the live rows, including the pastime entries that must
#: NOT become projects and the sentinel that names nobody.
JOURNAL = [
    ("tl-1", "2026-05-16T09:26:00", "Topos", "Rowan", "We went through everything."),
    ("tl-2", "2026-05-31T10:58:00", "Topos", "Rowan", "We set Rowan up with Topos."),
    ("tl-3", "2026-06-20T11:00:00", "Topos", "Rowan", "Rowan came over."),
    ("tl-4", "2026-05-14T19:00:00", "Chill", "Rowan, Nadia", "Walked to the pizza place."),
    ("tl-5", "2026-07-06T13:15:00", "Topos", "Solo", "More website improvements."),
    ("tl-6", "2026-05-02T10:00:00", "Chill", "Yusra", "Sat with Yusra."),
]


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "journal.db"))
    apply_all_migrations(c)
    for entry_id, at, category, people, content in JOURNAL:
        c.execute(
            "INSERT INTO journal_entries (entry_id, entry_at, category, people,"
            " content, source_id) VALUES (?,?,?,?,?,?)",
            (entry_id, at, category, people, content, "grow_journal"),
        )
    c.commit()
    yield c
    c.close()


def _messages(conn):
    cur = conn.execute("SELECT * FROM journal_entries")
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        m = dict(zip(cols, row))
        m["_table"] = "journal_entries"
        m["record_id"] = m["entry_id"]
        m["event_at"] = m["entry_at"]
        out.append(m)
    return out


def _build(conn):
    """The entities_job fold, in the order that job runs it: declared rows, then
    structured columns, then ONE co-occurrence pass over the merged bucket."""
    resolver = EntityResolver(conn)
    msgs = _messages(conn)
    by_record: dict = {}

    for m in msgs:
        for rec in extract_declared_entities(
            m, record_id=m["record_id"], event_at=m["event_at"]
        ):
            entity_id, _ = resolver.resolve(
                rec["entity_text"],
                entity_type=rec["entity_type"],
                record_id=rec["record_id"],
                queue_review=False,
            )
            if not entity_id:
                continue
            resolver.record_mention(
                entity_id,
                record_id=rec["record_id"],
                surface_text=rec["entity_text"],
                source_id=rec["source_id"],
                canonical_table="journal_entries",
                confidence=1.0,
                event_at=rec["event_at"],
                authored_by_owner=1,
            )
            by_record.setdefault(rec["record_id"], []).append(entity_id)

    for record_id, ids in record_structured_mentions(conn, resolver, msgs).items():
        by_record.setdefault(record_id, []).extend(ids)
    conn.commit()

    at = {m["record_id"]: m["event_at"] for m in msgs}
    for record_id, ids in by_record.items():
        for src, dst in record_cooccurrence_pairs(ids):
            update_edge(
                conn,
                src_entity_id=src,
                dst_entity_id=dst,
                edge_type="co_occurrence",
                event_at=at.get(record_id),
            )
    conn.commit()
    return resolver


def _entity(conn, name, entity_type):
    row = conn.execute(
        "SELECT entity_id FROM entities WHERE canonical_name=? AND entity_type=?",
        (name, entity_type),
    ).fetchone()
    return row[0] if row else None


def _edge(conn, a, b):
    return conn.execute(
        "SELECT edge_type, evidence_count FROM entity_edges"
        " WHERE (src_entity_id=? AND dst_entity_id=?)"
        "    OR (src_entity_id=? AND dst_entity_id=?)",
        (a, b, b, a),
    ).fetchone()


def test_the_person_and_the_project_are_connected(conn):
    _build(conn)

    rowan = _entity(conn, "Rowan", "person")
    topos = _entity(conn, "Topos", "project")
    assert rowan and topos, "both declared columns must mint their entity"

    edge = _edge(conn, rowan, topos)
    assert edge is not None, "the two columns of one row must produce an edge"
    assert edge[0] == "co_occurrence"
    assert edge[1] == 3, "one per row that declares BOTH — not the Chill row"


def test_the_evidence_count_is_the_number_of_sessions(conn):
    """The edge weight has to mean something a person would recognise."""
    _build(conn)
    edge = _edge(conn, _entity(conn, "Rowan", "person"), _entity(conn, "Topos", "project"))
    declared = sum(
        1 for _, _, cat, people, _ in JOURNAL if cat == "Topos" and "Rowan" in people
    )
    assert edge[1] == declared


def test_a_pastime_never_becomes_a_project(conn):
    """`Owner --worked_on--> Chill` is a sentence nobody wrote."""
    _build(conn)
    assert _entity(conn, "Chill", "project") is None
    leaked = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_type='project'"
        " AND lower(canonical_name) IN ('chill','run','walk','fun')"
    ).fetchone()[0]
    assert leaked == 0


def test_a_solo_session_connects_nobody(conn):
    """tl-5 is Topos work with no participant. It must not invent one."""
    _build(conn)
    assert _entity(conn, "Solo", "person") is None


def test_every_declared_participant_is_mentioned(conn):
    """The count the live node got wrong: 13 declared, 2 recovered."""
    _build(conn)
    n = conn.execute(
        "SELECT COUNT(*) FROM entity_mentions m JOIN entities e"
        " ON e.entity_id=m.entity_id WHERE e.canonical_name='Rowan'"
    ).fetchone()[0]
    assert n == sum(1 for _, _, _, people, _ in JOURNAL if "Rowan" in people)


def test_a_second_participant_on_the_same_row_is_not_lost(conn):
    """tl-4 declares two people. Both must survive — the artifact lane has a
    collision bug on exactly this shape, and this lane must not repeat it."""
    _build(conn)
    rowan = _entity(conn, "Rowan", "person")
    claire = _entity(conn, "Nadia", "person")
    assert rowan and claire
    assert _edge(conn, rowan, claire) is not None

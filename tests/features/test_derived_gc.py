"""Derived-data GC gaps found during the 2026-07-09 live demo purge.

Two leaks:
  * entities anchored to a DELETED contact row persisted forever —
    _delete_orphan_entities checked contact_id IS NOT NULL but never whether
    the anchor still resolves;
  * facts whose every source_ref points at a deleted record stayed active
    (e.g. "certified_in AWS Solutions Architect" survived a full scrub of
    demo_resume_file via an unverifiable legacy ref).

Facts are CLOSED (valid_to stamped), not deleted — provenance may return.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.maintenance import rebuild_entity_graph
from topos.features.lifecycle.derived_scrub import (
    _delete_orphan_entities,
    close_dangling_facts,
)
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "g.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _contact(conn, contact_id: str, name: str) -> None:
    conn.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self) "
        "VALUES (?, 'ds', 'src', ?, 0)",
        (contact_id, name),
    )


def _anchored_entity(conn, name: str, contact_id: str) -> str:
    r = EntityResolver(conn)
    eid = r._create_entity(name, "person")
    conn.execute(
        "UPDATE entities SET contact_id=?, mention_count=0 WHERE entity_id=?",
        (contact_id, eid),
    )
    conn.commit()
    return eid


def _fact(conn, object_id: str, refs, *, predicate="works_at", value="X") -> None:
    payload = {"subject_entity_id": "ent_owner", "predicate": predicate, "object_value": value}
    conn.execute(
        """
        INSERT INTO signal_objects
            (object_id, signal_dimension, object_type, object_key, payload_json,
             confidence, source_refs_json, valid_from, valid_to, extractor_version,
             created_at, updated_at, created_by, updated_by)
        VALUES (?, 'profile', 'fact', ?, ?, 0.8, ?, '2026-01-01T00:00:00Z', NULL,
                'test', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'system', 'system')
        """,
        (object_id, f"fact:{object_id}", json.dumps(payload), json.dumps(refs)),
    )
    conn.commit()


# ------------------------------------------------- fix 1: dangling anchors


def test_entity_with_dangling_contact_anchor_is_pruned(conn):
    """Anchor row deleted → the anchored, mention-less entity must go too."""
    eid = _anchored_entity(conn, "Ghost Person", "contact-deleted")
    # no contacts row for 'contact-deleted'
    removed = _delete_orphan_entities(conn)
    assert eid in removed
    assert conn.execute("SELECT 1 FROM entities WHERE entity_id=?", (eid,)).fetchone() is None


def test_entity_with_live_contact_anchor_is_kept(conn):
    _contact(conn, "contact-real", "Real Person")
    eid = _anchored_entity(conn, "Real Person", "contact-real")
    removed = _delete_orphan_entities(conn)
    assert eid not in removed
    assert conn.execute("SELECT 1 FROM entities WHERE entity_id=?", (eid,)).fetchone() is not None


def test_self_entity_never_pruned_even_with_dangling_anchor(conn):
    eid = _anchored_entity(conn, "Me", "contact-gone")
    conn.execute("UPDATE entities SET is_self=1 WHERE entity_id=?", (eid,))
    conn.commit()
    removed = _delete_orphan_entities(conn)
    assert eid not in removed


# ------------------------------------------- fix 3: derivation-minted vertices


def _linked_entity(conn, name: str, etype: str = "topic") -> str:
    """A vertex minted by graph derivation: no mentions, but carrying an edge.

    fact_materializer and graph_enrichers mint theirs through
    EntityResolver.resolve, so they get ordinary ``ent_`` ids and ordinary
    types — neither the type nor the id-prefix exemption sees them.
    """
    other = EntityResolver(conn)._create_entity("Anchor Topic", etype)
    eid = EntityResolver(conn)._create_entity(name, etype)
    conn.execute(
        "INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type, weight)"
        " VALUES (?, ?, ?, 'discusses', 1.0)",
        (f"edge_{eid}", other, eid),
    )
    conn.execute("UPDATE entities SET mention_count=0 WHERE entity_id IN (?, ?)", (eid, other))
    conn.commit()
    return eid


def test_mentionless_vertex_with_an_edge_survives_the_scrub(conn):
    """Reaping these wiped their edges too, and the next derivation run rebuilt
    the same nodes and edges — the graph churned on every cycle."""
    eid = _linked_entity(conn, "Personal Intelligence Infrastructure Quadrant")
    removed = _delete_orphan_entities(conn)
    assert eid not in removed
    assert conn.execute("SELECT 1 FROM entities WHERE entity_id=?", (eid,)).fetchone() is not None
    assert conn.execute(
        "SELECT 1 FROM entity_edges WHERE dst_entity_id=?", (eid,)
    ).fetchone() is not None, "cascade took the edge with it"


def test_mentionless_vertex_without_an_edge_is_still_reaped(conn):
    """An edge is what makes a vertex load-bearing; with none it is junk."""
    eid = EntityResolver(conn)._create_entity("Nothing Points Here", "topic")
    conn.execute("UPDATE entities SET mention_count=0 WHERE entity_id=?", (eid,))
    conn.commit()
    removed = _delete_orphan_entities(conn)
    assert eid in removed


# --------------------------------------------- fix 2: dangling provenance


def test_fact_with_all_refs_dead_is_closed_not_deleted(conn):
    _fact(conn, "f_dead", [{"table": "profile_records", "record_id": "prof-gone"}])
    closed = close_dangling_facts(conn)
    assert closed == 1
    row = conn.execute(
        "SELECT valid_to FROM signal_objects WHERE object_id='f_dead'"
    ).fetchone()
    assert row is not None and row[0]  # closed, still present


def test_fact_with_one_live_ref_stays_active(conn):
    conn.execute(
        "INSERT INTO profile_records (record_id, record_type, source_id, title) "
        "VALUES ('prof-live', 'role', 'real_source', 'x')"
    )
    conn.commit()
    _fact(
        conn,
        "f_mixed",
        [
            {"table": "profile_records", "record_id": "prof-gone"},
            {"table": "profile_records", "record_id": "prof-live"},
        ],
    )
    assert close_dangling_facts(conn) == 0
    assert conn.execute(
        "SELECT valid_to FROM signal_objects WHERE object_id='f_mixed'"
    ).fetchone()[0] is None


def test_unverifiable_refs_are_conservative(conn):
    """Refs without table/record_id can't be checked — leave the fact alone."""
    _fact(conn, "f_vague", [{"source_id": "somewhere"}])
    assert close_dangling_facts(conn) == 0
    assert conn.execute(
        "SELECT valid_to FROM signal_objects WHERE object_id='f_vague'"
    ).fetchone()[0] is None


def test_refless_facts_untouched(conn):
    _fact(conn, "f_norefs", [])
    assert close_dangling_facts(conn) == 0


def test_aws_cert_leak_shape_closes_after_source_purge(conn):
    """The exact live leak: duplicate refs to the same deleted record — one
    attributed to the scrubbed source (trimmed by purge_facts_for_source), one
    legacy without source_id (kept the fact alive). The dangling sweep must
    close what survives."""
    from topos.features.lifecycle.derived_scrub import purge_facts_for_source

    _fact(
        conn,
        "f_aws",
        [
            {"table": "profile_records", "record_id": "prof-007"},
            {"table": "profile_records", "record_id": "prof-007", "source_id": "demo_resume_file"},
        ],
        predicate="certified_in",
        value="AWS Solutions Architect",
    )
    purge_facts_for_source(conn, "demo_resume_file")
    # the attributed ref is gone; the legacy ref kept the fact active
    assert conn.execute(
        "SELECT valid_to FROM signal_objects WHERE object_id='f_aws'"
    ).fetchone()[0] is None
    assert close_dangling_facts(conn) == 1
    assert conn.execute(
        "SELECT valid_to FROM signal_objects WHERE object_id='f_aws'"
    ).fetchone()[0]


# --------------------------------------------- exercised via rebuild


def test_rebuild_entity_graph_applies_both_gc_fixes(conn):
    eid = _anchored_entity(conn, "Ghost", "contact-gone")
    _fact(conn, "f_dead", [{"table": "profile_records", "record_id": "prof-gone"}])
    report = rebuild_entity_graph(conn)
    assert report["orphans_pruned"] >= 1
    assert report["facts_closed_dangling"] >= 1
    assert conn.execute("SELECT 1 FROM entities WHERE entity_id=?", (eid,)).fetchone() is None
    assert conn.execute(
        "SELECT valid_to FROM signal_objects WHERE object_id='f_dead'"
    ).fetchone()[0]

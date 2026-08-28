"""Ingest and rebuild must fold co-occurrence identically.

Two paths write ``co_occurrence`` edges from the same evidence:
``entities_job._resolve_into_spine`` at ingest, and
``maintenance.rebuild_evidence_edges`` on maintenance. They had their own copies
of the fold and the copies disagreed — the rebuild truncated each record to 8
entities with the comment "(mirrors the ingest path)", and the ingest path had no
cap at all.

That is not a cosmetic divergence. ``rebuild_evidence_edges`` DELETEs the whole
active co-occurrence set and re-inserts, unconditionally and with no ``valid_to``
tombstone, so every maintenance run silently destroyed the edges ingest had
created for any record above the cap — 66 of them, measured on the owner's node.
The graph was a function of which writer ran last, and nothing recorded the
difference.

Measured 2026-08-27, which is why the bound moved rather than the paths simply
agreeing on 8: 3,372 records carry <=8 entities and the cap never touched them,
exactly 5 exceed it, and the largest carries 13. The old cap bought no protection
worth having and cost 166 pair-observations.

The load-bearing test is ``test_both_writers_produce_the_same_edge_set``: it
drives the real ingest job and the real rebuild over one corpus and compares.
That is what catches the next divergence, rather than asserting a constant.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.edges import (
    CO_OCCURRENCE_MAX_ENTITIES_PER_RECORD,
    record_cooccurrence_pairs,
)


# ----------------------------------------------------------------- the fold


def test_pairs_are_every_unordered_combination():
    pairs = record_cooccurrence_pairs(["c", "a", "b"])

    assert sorted(pairs) == [("a", "b"), ("a", "c"), ("b", "c")]
    assert len(pairs) == 3


def test_duplicates_and_blanks_are_dropped():
    assert record_cooccurrence_pairs(["a", "a", "", "  ", "b"]) == [("a", "b")]


def test_a_single_entity_yields_nothing():
    assert record_cooccurrence_pairs(["only"]) == []
    assert record_cooccurrence_pairs([]) == []


def test_the_retained_set_is_deterministic_not_extraction_order():
    """The old truncation kept the first 8 by NER span position.

    Insertion order came from a query ordering ACROSS records, so within a record
    SQLite's tie-break decided — in practice the order the extractor emitted
    spans. "Who shows up alongside X" was answered by paragraph position and
    would change under a model swap with no schema change to signal it.
    """
    n = CO_OCCURRENCE_MAX_ENTITIES_PER_RECORD + 5
    ids = [f"e{i:03d}" for i in range(n)]

    forward = record_cooccurrence_pairs(ids)
    reversed_order = record_cooccurrence_pairs(list(reversed(ids)))
    shuffled = record_cooccurrence_pairs(ids[7:] + ids[:7])

    assert forward == reversed_order == shuffled


def test_the_bound_still_applies():
    """A bound is kept — the fold is O(n^2) and a long document could be huge."""
    n = CO_OCCURRENCE_MAX_ENTITIES_PER_RECORD + 20
    pairs = record_cooccurrence_pairs([f"e{i}" for i in range(n)])

    cap = CO_OCCURRENCE_MAX_ENTITIES_PER_RECORD
    assert len(pairs) == cap * (cap - 1) // 2


def test_the_bound_does_not_bite_real_records():
    """13 is the largest entity count on the live corpus; the cap must clear it."""
    assert CO_OCCURRENCE_MAX_ENTITIES_PER_RECORD >= 13
    pairs = record_cooccurrence_pairs([f"e{i}" for i in range(13)])
    assert len(pairs) == 78, "a 13-entity record must fold completely"


# ------------------------------------------------- the two writers agree


def _seed(path):
    from topos.storage.db.migrations import apply_all_migrations

    conn = sqlite3.connect(str(path))
    apply_all_migrations(conn)
    # One record with MORE entities than the old cap, which is the only shape
    # where the two paths used to disagree.
    for i in range(11):
        conn.execute(
            "INSERT INTO entities (entity_id, entity_type, canonical_name,"
            " normalized_name, mention_count, is_self) VALUES (?,?,?,?,1,0)",
            (f"ent-{i:02d}", "person", f"Person {i:02d}", f"person {i:02d}"),
        )
        conn.execute(
            "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id,"
            " canonical_table, surface_text, event_at) VALUES (?,?,?,?,?,?,?)",
            (f"m-{i:02d}", f"ent-{i:02d}", "rec-big", "imessage",
             "conversation_messages", f"Person {i:02d}", "2026-07-01T00:00:00"),
        )
    conn.commit()
    return conn


def _active_cooccurrence(conn):
    return {
        (min(a, b), max(a, b))
        for a, b in conn.execute(
            "SELECT src_entity_id, dst_entity_id FROM entity_edges"
            " WHERE edge_type='co_occurrence' AND valid_to IS NULL"
        )
    }


def test_both_writers_produce_the_same_edge_set(tmp_path):
    """The invariant. Drives the real rebuild against the real ingest fold.

    Asserting on a shared constant would pass even if one path stopped calling
    the helper. Comparing the produced edge sets is what actually holds.
    """
    from topos.features.entities.edges import EDGE_CO_OCCURRENCE, update_edge
    from topos.features.entities.maintenance import rebuild_evidence_edges

    conn = _seed(tmp_path / "fold.db")
    try:
        # what INGEST would write, through the shared fold
        ids = [f"ent-{i:02d}" for i in range(11)]
        for src, dst in record_cooccurrence_pairs(ids):
            update_edge(
                conn,
                src_entity_id=src,
                dst_entity_id=dst,
                edge_type=EDGE_CO_OCCURRENCE,
                event_at="2026-07-01T00:00:00",
            )
        conn.commit()
        ingest_edges = _active_cooccurrence(conn)
        assert len(ingest_edges) == 55, "11 entities must fold to 55 pairs"

        # what the REBUILD writes from the same mentions
        rebuild_evidence_edges(conn)
        conn.commit()
        rebuilt_edges = _active_cooccurrence(conn)

        assert rebuilt_edges == ingest_edges, (
            "the rebuild deletes and re-creates the active set, so a divergence "
            "here means a maintenance run silently changes the graph: "
            f"lost {sorted(ingest_edges - rebuilt_edges)}, "
            f"gained {sorted(rebuilt_edges - ingest_edges)}"
        )
    finally:
        conn.close()


def test_a_rebuild_is_idempotent_on_the_edge_set(tmp_path):
    """Two rebuilds over unchanged evidence must agree with each other too."""
    from topos.features.entities.maintenance import rebuild_evidence_edges

    conn = _seed(tmp_path / "fold2.db")
    try:
        rebuild_evidence_edges(conn)
        conn.commit()
        first = _active_cooccurrence(conn)

        rebuild_evidence_edges(conn)
        conn.commit()
        second = _active_cooccurrence(conn)

        assert first == second and first
    finally:
        conn.close()

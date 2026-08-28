"""A materialized edge must carry its real evidence count, and fold by identity.

Two defects in one place, both in the lane that recomputes edges from
aggregates (``_upsert_materialized_edge`` and its callers).

**The count was hardcoded.** ``evidence_count`` was literally ``1`` in the
INSERT and absent from the UPDATE, so on the owner's node 2026-08-27 *every one
of the 4,204 materialized edges claimed a single observation* — while the
evidence lane beside it (``co_occurrence``, ``communicates_with``) folded real
counts up to 2,784. Three callers already knew the true number and wrote it into
the human-readable statement — ``"visited X ×127"``, ``"mentioned in
conversation ×18"`` — while the column a query can actually rank on said 1. The
statement and the column disagreed by two orders of magnitude.

**Places folded by name, not by identity.** ``_materialize_places`` groups
``location_events`` by ``place_name``, but the upsert keys on
``(owner, place_id, 'located_at')`` and several names resolve to one place. Each
name overwrote the previous one's count. Measured on the same node: 69 distinct
names resolve to 65 entities, 4 entities receive two names apiece, 4 visits
vanish. Small today, and it scales with the corpus.

Two things this deliberately does NOT claim:

  * The visit counts are not fabricated. ``location_events`` is exactly one row
    per journal session at a place — 127 events across 127 distinct parent
    entries for the owner's most-visited place — so counting them is counting
    visits, not counting mentions.
  * Summing collided names is right given the resolution, but the resolution
    itself can be wrong: "Ashford Public Library" and "Kelvin Park- Ashford" share
    a node on the live data. That is a resolver defect, out of scope here, and
    folding does not hide it — it makes the merged node's count honest.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.fact_materializer import _upsert_materialized_edge


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "edges.db"))
    apply_all_migrations(c)
    for eid, etype, name in (
        ("owner", "person", "Owner"),
        ("place-a", "place", "Place A"),
        ("place-b", "place", "Place B"),
    ):
        c.execute(
            "INSERT INTO entities (entity_id, entity_type, canonical_name,"
            " normalized_name, mention_count, is_self) VALUES (?,?,?,?,1,?)",
            (eid, etype, name, name.lower(), 1 if eid == "owner" else 0),
        )
    c.commit()
    yield c
    c.close()


def _edge(conn, src="owner", dst="place-a", edge_type="located_at"):
    return conn.execute(
        "SELECT weight, evidence_count, metadata_json FROM entity_edges"
        " WHERE src_entity_id=? AND dst_entity_id=? AND edge_type=? AND valid_to IS NULL",
        (src, dst, edge_type),
    ).fetchone()


def _upsert(conn, **kw):
    kw.setdefault("src", "owner")
    kw.setdefault("dst", "place-a")
    kw.setdefault("edge_type", "located_at")
    kw.setdefault("weight", 3.0)
    kw.setdefault("valid_from", "2026-07-01")
    kw.setdefault("valid_to", None)
    kw.setdefault("statement", "visited Place A")
    kw.setdefault("source_object_id", "loc:a")
    return _upsert_materialized_edge(conn, **kw)


# --------------------------------------------------------- the count is carried


def test_a_supplied_count_reaches_the_column(conn):
    _upsert(conn, evidence_count=127)
    conn.commit()

    assert _edge(conn)[1] == 127


def test_a_caller_with_no_count_still_gets_one(conn):
    """`discusses` and the fact edges are honestly one observation each."""
    _upsert(conn)
    conn.commit()

    assert _edge(conn)[1] == 1


def test_the_count_is_SET_on_rebuild_not_incremented(conn):
    """The whole difference from ``fold_edge_observation``, and the trap.

    This lane recomputes every edge from a full aggregate on each rebuild. If
    the update added instead of assigning, one place visited 127 times would
    report 254 after the next rebuild and 381 after the one following — the
    derived-row multiplication this workstream keeps finding, in a column
    nobody would think to re-measure.
    """
    _upsert(conn, evidence_count=127)
    conn.commit()
    _upsert(conn, evidence_count=127)
    conn.commit()

    assert _edge(conn)[1] == 127


def test_a_falling_count_is_honoured(conn):
    """Evidence can be retracted. A max() would make counts a ratchet."""
    _upsert(conn, evidence_count=127)
    conn.commit()
    _upsert(conn, evidence_count=3)
    conn.commit()

    assert _edge(conn)[1] == 3


def test_a_nonsense_count_floors_at_one(conn):
    for bad in (0, -5, None):
        _upsert(conn, evidence_count=bad)
        conn.commit()
        assert _edge(conn)[1] == 1


def test_the_evidence_lane_still_folds(conn):
    """Control: streamed observations must keep incrementing.

    Setting is right for a recomputed aggregate and wrong for a stream. If this
    ever fails, the two lanes have been collapsed into one and
    ``communicates_with`` has stopped counting messages.
    """
    from topos.features.entities.edges import fold_edge_observation

    weight, count, _last = fold_edge_observation(
        weight=1.0, evidence_count=5, last_event_at="2026-07-01",
        event_at="2026-07-02", increment=1.0,
    )

    assert count == 6


# ------------------------------------------------- places fold by identity


def _seed_places(conn, rows):
    for i, (name, when) in enumerate(rows):
        conn.execute(
            "INSERT INTO location_events (event_id, place_name, event_at, source_id)"
            " VALUES (?,?,?,?)",
            (f"ev-{i}", name, when, "grow_journal"),
        )
    conn.commit()


def _materialize(conn, monkeypatch, resolution):
    """Run the enricher with resolution pinned, so the fold is what is tested."""
    from topos.features.entities import graph_enrichers

    class _Resolver:
        def __init__(self, _conn):
            pass

        def resolve(self, name, entity_type=None, queue_review=False):
            if name not in resolution:
                raise ValueError(name)
            return resolution[name], "exact"

    monkeypatch.setattr("topos.features.entities.resolver.EntityResolver", _Resolver)
    monkeypatch.setattr(graph_enrichers, "_ensure_node", lambda *a, **k: None, raising=False)
    return graph_enrichers._materialize_places(conn, "owner")


def test_two_names_on_one_place_accumulate(conn, monkeypatch):
    """Last-writer-wins lost the smaller name's visits entirely."""
    _seed_places(conn, [("Mill Pond", "2026-07-01")] * 15 + [("Mill Pond Trail", "2026-08-01")])

    _materialize(conn, monkeypatch, {"Mill Pond": "place-a", "Mill Pond Trail": "place-a"})
    conn.commit()

    assert _edge(conn)[1] == 16, "the second name's visit was overwritten, not added"


def test_the_merged_edge_spans_both_windows(conn, monkeypatch):
    """A fold that kept one name's dates would misreport when the owner was there."""
    _seed_places(conn, [("Mill Pond", "2026-07-01"), ("Mill Pond Trail", "2026-08-01")])

    _materialize(conn, monkeypatch, {"Mill Pond": "place-a", "Mill Pond Trail": "place-a"})
    conn.commit()

    row = conn.execute(
        "SELECT valid_from, last_event_at FROM entity_edges WHERE dst_entity_id='place-a'"
    ).fetchone()
    assert row == ("2026-07-01", "2026-08-01")


def test_the_merged_node_is_named_by_its_dominant_surface(conn, monkeypatch):
    """Reading "visited Mill Pond Trail ×16" for 15 Mill Pond visits is worse
    than the count being wrong — it attributes them to the wrong place."""
    _seed_places(conn, [("Mill Pond", "2026-07-01")] * 15 + [("Mill Pond Trail", "2026-08-01")])

    _materialize(conn, monkeypatch, {"Mill Pond": "place-a", "Mill Pond Trail": "place-a"})
    conn.commit()

    import json

    meta = json.loads(_edge(conn)[2])
    assert meta["statement"] == "visited Mill Pond ×16"


def test_the_statement_and_the_column_agree(conn, monkeypatch):
    """The signature of the original bug: ×N in the prose, 1 in the column."""
    _seed_places(conn, [("Mill Pond", "2026-07-01")] * 15 + [("Mill Pond Trail", "2026-08-01")])

    _materialize(conn, monkeypatch, {"Mill Pond": "place-a", "Mill Pond Trail": "place-a"})
    conn.commit()

    import json

    weight, count, meta_json = _edge(conn)
    assert json.loads(meta_json)["statement"].endswith(f"×{count}")


def test_distinct_places_stay_distinct(conn, monkeypatch):
    """Control: folding must not merge places that resolve apart."""
    _seed_places(conn, [("Mill Pond", "2026-07-01"), ("Metro Fitness", "2026-07-02")])

    _materialize(conn, monkeypatch, {"Mill Pond": "place-a", "Metro Fitness": "place-b"})
    conn.commit()

    assert _edge(conn, dst="place-a")[1] == 1
    assert _edge(conn, dst="place-b")[1] == 1


def test_the_visit_count_matches_the_rows_it_came_from(conn, monkeypatch):
    """`location_events` is one row per journal session at a place.

    Pins the semantics: the number published as "visited X ×N" is N logged
    sessions, so a reader comparing the edge against the table finds them equal.
    """
    _seed_places(conn, [("Mill Pond", f"2026-07-{d:02d}") for d in range(1, 13)])

    _materialize(conn, monkeypatch, {"Mill Pond": "place-a"})
    conn.commit()

    rows = conn.execute(
        "SELECT COUNT(*) FROM location_events WHERE place_name='Mill Pond'"
    ).fetchone()[0]
    assert _edge(conn)[1] == rows == 12


def test_a_places_rebuild_is_idempotent(conn, monkeypatch):
    """End to end: run the enricher twice, counts must not double."""
    _seed_places(conn, [("Mill Pond", "2026-07-01")] * 15 + [("Mill Pond Trail", "2026-08-01")])

    _materialize(conn, monkeypatch, {"Mill Pond": "place-a", "Mill Pond Trail": "place-a"})
    conn.commit()
    _materialize(conn, monkeypatch, {"Mill Pond": "place-a", "Mill Pond Trail": "place-a"})
    conn.commit()

    assert _edge(conn)[1] == 16
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_edges WHERE dst_entity_id='place-a' AND valid_to IS NULL"
    ).fetchone()[0] == 1


def test_no_materializer_call_site_hardcodes_a_count(conn):
    """The omission was invisible because the default is silent and plausible.

    A site that computes ``×{n}`` for a human but leaves ``evidence_count``
    defaulted reintroduces exactly the disagreement this closed, so the callers
    are checked for the pattern.
    """
    import re
    from pathlib import Path

    from topos.features.entities import fact_materializer, graph_enrichers

    offenders = []
    for mod in (graph_enrichers, fact_materializer):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for call in re.findall(r"_upsert_materialized_edge\(\n(.*?)\n\s*\)\)", src, re.S):
            if "×{" in call and "evidence_count" not in call:
                offenders.append(call.strip().splitlines()[0])
    assert offenders == [], (
        "these publish a count to a human but leave the queryable column at 1: "
        f"{offenders}"
    )

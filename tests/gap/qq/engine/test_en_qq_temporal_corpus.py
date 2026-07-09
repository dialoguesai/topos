"""Pure unit tests for the qq-seeded-4 temporal instrument cases (plan B1.1-B1.5).

No orchestrator, no LLM, no live DB: these verify the corpus itself — the
T1-T3 chain stays intact, T4's month computation is stable across month/year
edges, T5's canary cleanup is idempotent against a scratch DB, and no
wall-clock reads leak beyond the module's single _NOW anchor. Red/green
semantics of the new oracles are exercised through score_composition with
simulated responses, so the expected-red contracts are pinned without running
retrieval.
"""

from __future__ import annotations

import dataclasses
import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

import composition_seed_corpus as corpus
from composition_eval_cases import CompositionCase, Oracle, score_composition
from composition_seed_corpus import (
    FACT_CURRENT_VALID_FROM,
    FACT_SUPERSEDED_VALID_FROM,
    LIVE_TEMPORAL_CASES,
    NEEDLES,
    SEEDED_COMPOSITION_CASES,
    SEEDED_CORPUS_VERSION,
    T4_MONTH_LABEL,
    T5_CANARY_ENTITY_ID,
    T5_CURRENT_VALUE,
    T5_OBJECT_KEY_PREFIX,
    T5_SUPERSEDED_VALUE,
    T6_POISON_GROUPS,
    T7_DST_ENTITY_ID,
    T7_EDGE_TYPE,
    T7_SRC_ENTITY_ID,
    T8_DATE_NEEDLES,
    T8_STAT_FACT_ID,
    T8_STAT_PERIOD_END,
    _fmt,
    _t4_month_bounds,
    build_seeded_corpus,
    fact_chain_valid_from,
    oracle_t5_live_supersession,
    t5_cleanup,
    t5_seed_canary,
)

pytestmark = pytest.mark.gap

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _TS_FORMAT).replace(tzinfo=timezone.utc)


def _case(case_id: str) -> CompositionCase:
    for case in SEEDED_COMPOSITION_CASES + LIVE_TEMPORAL_CASES:
        if case.id == case_id:
            return case
    raise AssertionError(f"case {case_id} not registered")


def _resp(texts: List[str], source: str = "fact") -> Dict[str, Any]:
    """Minimal pipeline-shaped response: summaries with retrieval_source."""
    return {
        "public_result": {
            "summaries": [
                {"topic": t[:60], "summary_text": t, "retrieval_source": source}
                for t in texts
            ]
        }
    }


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory) -> sqlite3.Connection:
    db = build_seeded_corpus(tmp_path_factory.mktemp("qq-seeded4") / "seeded.db")
    conn = sqlite3.connect(str(db))
    yield conn
    conn.close()


# --- registration & version -----------------------------------------------------------


def test_version_bumped_and_cases_registered():
    assert SEEDED_CORPUS_VERSION == "qq-seeded-4"
    ids = [c.id for c in SEEDED_COMPOSITION_CASES]
    # T1-T3 still present, T4/T6/T7/T8 added, T5 lives in the live list.
    for required in ("T1", "T2", "T3", "T4", "T6", "T7", "T8"):
        assert required in ids
    assert "T5" not in ids
    assert [(c.id, c.lane) for c in LIVE_TEMPORAL_CASES] == [("T5", "live")]

    layers = {c.id: c.layer for c in SEEDED_COMPOSITION_CASES + LIVE_TEMPORAL_CASES}
    assert layers["T4"] == "facts:as_of"
    assert layers["T5"] == "facts:live_supersession"
    assert layers["T6"] == "facts:stale_suppression"
    assert layers["T7"] == "edges:validity"
    assert layers["T8"] == "artifacts:staleness_honesty"
    for case_id in ("T4", "T5", "T6", "T7", "T8"):
        assert _case(case_id).query_class in ("known_item", "aggregate")
    assert _case("T8").query_class == "aggregate"


def test_t1_t3_needles_unchanged():
    """The pre-existing chain is load-bearing for T1/T2 — byte-identical needles."""
    assert NEEDLES["fact_current"] == "the Foxglove Atelier"
    assert NEEDLES["fact_superseded"] == "the Larkspur Annex"
    assert NEEDLES["conversation_messages"] == (
        "the cobalt kayak invoice from Aurelio is ready for review"
    )
    assert NEEDLES["stat_insight"] == "Total kayak spend: 412.00 across 3 transactions."
    t1, t2 = _case("T1"), _case("T2")
    assert t1.oracle(None).needle_groups == [["the Foxglove Atelier"]]
    assert t2.oracle(None).needle_groups == [["the Larkspur Annex"], ["no longer current"]]


# --- corpus build: chain + new layers ---------------------------------------------------


def test_chain_intact_and_as_of_ground_truth(seeded_db):
    rows = seeded_db.execute(
        """SELECT valid_from, valid_to, json_extract(payload_json, '$.object_value')
           FROM signal_objects
           WHERE object_type='fact' AND object_key LIKE 'fact:seed-self-1:%'
           ORDER BY valid_from"""
    ).fetchall()
    assert len(rows) == 2
    (sup_from, sup_to, sup_value), (cur_from, cur_to, cur_value) = rows
    assert sup_value == NEEDLES["fact_superseded"]
    assert cur_value == NEEDLES["fact_current"]
    assert cur_to is None  # current fact active
    assert sup_to == cur_from  # supersession closed the incumbent at the new valid_from
    assert sup_from == FACT_SUPERSEDED_VALID_FROM
    assert cur_from == FACT_CURRENT_VALID_FROM

    # Ground truth IS derivable from the store (so T4's red is retrieval's gap,
    # not the corpus'): a mid-target-month as-of read returns the superseded value.
    from topos.features.facts.store import FactStore

    month_start, next_month_start = _t4_month_bounds(corpus._NOW)
    for as_of in (month_start, month_start + timedelta(days=14), next_month_start - timedelta(seconds=1)):
        facts = [
            f for f in FactStore(seeded_db).facts_for_subject("seed-self-1", as_of=_fmt(as_of))
            if f["payload"].get("predicate") == "studio_space"
        ]
        assert [f["payload"]["object_value"] for f in facts] == [NEEDLES["fact_superseded"]]


def test_t7_edge_and_t8_stat_seeded(seeded_db):
    edges = seeded_db.execute(
        "SELECT src_entity_id, dst_entity_id, edge_type FROM entity_edges"
    ).fetchall()
    assert (T7_SRC_ENTITY_ID, T7_DST_ENTITY_ID, T7_EDGE_TYPE) in edges

    from topos.features.entities.edges import top_edges

    # B2.2: the seeded collaboration is CLOSED (supersede_edge) — invisible to
    # the default active-only read, surfaced with validity via include_closed.
    assert top_edges(seeded_db, T7_SRC_ENTITY_ID) == []
    connected = top_edges(seeded_db, T7_SRC_ENTITY_ID, include_closed=True)
    assert [e["entity_name"] for e in connected] == [NEEDLES["edge_former_collaborator"]]
    assert connected[0]["valid_to"] is not None

    import json

    row = seeded_db.execute(
        "SELECT payload_json, created_at FROM signal_facts WHERE fact_id = ?",
        (T8_STAT_FACT_ID,),
    ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["tag"] == NEEDLES["stat_insight_dated"]
    assert payload["period_end"] == T8_STAT_PERIOD_END
    assert row[1] == T8_STAT_PERIOD_END  # created_at pinned to the artifact's own date
    assert T8_DATE_NEEDLES[0] == T8_STAT_PERIOD_END[:10]


# --- T4: month computation across window edges ------------------------------------------


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc),    # mid-window baseline
        datetime(2026, 2, 10, tzinfo=timezone.utc),          # anchor = Jan 1 (month start)
        datetime(2026, 2, 9, 23, 59, tzinfo=timezone.utc),   # anchor = Dec 31 (year edge)
        datetime(2026, 1, 3, tzinfo=timezone.utc),           # anchor in prior year
        datetime(2024, 4, 9, tzinfo=timezone.utc),           # anchor = Feb 29 (leap day)
        datetime(2026, 5, 10, tzinfo=timezone.utc),          # anchor = Mar 31 (31-day tail)
        datetime(2026, 5, 11, tzinfo=timezone.utc),          # anchor = Apr 1 (month start)
        datetime(2026, 3, 20, tzinfo=timezone.utc),          # anchor in 28-day February
        datetime(2026, 12, 11, tzinfo=timezone.utc),         # anchor = Nov 1
    ],
)
def test_t4_month_bounds_and_chain_cover_target_month(now):
    month_start, next_month_start = _t4_month_bounds(now)
    anchor = now - timedelta(days=40)
    assert month_start.day == 1 and next_month_start.day == 1
    assert month_start <= anchor < next_month_start
    assert 28 <= (next_month_start - month_start).days <= 31

    sup_from, cur_from = fact_chain_valid_from(now)
    sup_dt, cur_dt = _parse(sup_from), _parse(cur_from)
    # One-day buffers on both sides of the target month.
    assert sup_dt <= month_start - timedelta(days=1)
    assert cur_dt >= next_month_start + timedelta(days=1)
    # Chain stays temporally sane: strictly ordered, current already active.
    assert sup_dt < cur_dt
    assert cur_dt < now
    # Every instant of the target month resolves to the superseded fact under
    # as-of semantics (valid_from <= as_of AND valid_to > as_of; valid_to ==
    # the successor's valid_from).
    for as_of in (month_start, next_month_start - timedelta(seconds=1)):
        assert sup_dt <= as_of
        assert cur_dt > as_of
    # Deterministic given now.
    assert fact_chain_valid_from(now) == (sup_from, cur_from)


def test_t4_query_derived_from_module_now():
    assert T4_MONTH_LABEL == _t4_month_bounds(corpus._NOW)[0].strftime("%B %Y")
    query = _case("T4").query_text(None)
    assert T4_MONTH_LABEL in query
    assert query.startswith("What was my studio space in ")


def test_t4_red_green_semantics_via_scorer():
    case = _case("T4")
    oracle = case.oracle(None)
    # Today's behavior: the present-tense fact lane serves the CURRENT value.
    red = score_composition(
        case, _resp(["owner studio space the Foxglove Atelier"]), oracle
    )
    assert red["scores"]["correctness"] == 0.0
    # Post-B1.1 behavior: the point-in-time value surfaces.
    green = score_composition(
        case,
        _resp([
            "owner studio space the Larkspur Annex "
            "(no longer current — superseded 2026-06-29)"
        ]),
        oracle,
    )
    assert green["scores"]["correctness"] == 1.0


# --- T5: canary cleanup idempotency against a scratch DB --------------------------------


def _canary_rows(conn: sqlite3.Connection) -> List[tuple]:
    return conn.execute(
        """SELECT object_key, valid_from, valid_to, json_extract(payload_json, '$.object_value')
           FROM signal_objects WHERE object_key LIKE ? ORDER BY valid_from""",
        (T5_OBJECT_KEY_PREFIX + "%",),
    ).fetchall()


def test_t5_seed_and_cleanup_idempotent(tmp_path):
    db = build_seeded_corpus(tmp_path / "t5.db")
    rw = sqlite3.connect(str(db))
    try:
        baseline_objects = rw.execute("SELECT COUNT(*) FROM signal_objects").fetchone()[0]
        baseline_entities = rw.execute("SELECT COUNT(*) FROM entities").fetchone()[0]

        t5_cleanup(rw)  # virgin DB: no-op, no error
        assert rw.execute("SELECT COUNT(*) FROM signal_objects").fetchone()[0] == baseline_objects

        t5_seed_canary(rw)
        rows = _canary_rows(rw)
        assert len(rows) == 2
        assert all(key.startswith(T5_OBJECT_KEY_PREFIX) for key, *_ in rows)
        (_, sup_from, sup_to, sup_value), (_, cur_from, cur_to, cur_value) = rows
        assert sup_value == T5_SUPERSEDED_VALUE and cur_value == T5_CURRENT_VALUE
        assert cur_to is None and sup_to == cur_from  # real supersession ran
        # Equal confidence must supersede, never queue a conflict.
        assert rw.execute(
            "SELECT COUNT(*) FROM fact_conflicts WHERE subject_entity_id=?",
            (T5_CANARY_ENTITY_ID,),
        ).fetchone()[0] == 0

        # cleanup+seed twice -> same shape (idempotent re-plant, prior-run recovery)
        t5_cleanup(rw)
        t5_seed_canary(rw)
        assert len(_canary_rows(rw)) == 2

        # double cleanup -> zero canary rows both times, zero collateral damage
        t5_cleanup(rw)
        t5_cleanup(rw)
        assert _canary_rows(rw) == []
        assert rw.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_id=?", (T5_CANARY_ENTITY_ID,)
        ).fetchone()[0] == 0
        assert rw.execute("SELECT COUNT(*) FROM signal_objects").fetchone()[0] == baseline_objects
        assert rw.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == baseline_entities
    finally:
        rw.close()


def test_t5_oracle_plants_through_readonly_conn(tmp_path):
    """The runner hands oracles a mode=ro uri connection — the exact condition
    the write-around (PRAGMA database_list -> own connection) must survive."""
    db = build_seeded_corpus(tmp_path / "t5ro.db")
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        oracle = oracle_t5_live_supersession(ro)
        assert oracle.ok
        assert oracle.needle_groups == [[T5_SUPERSEDED_VALUE], ["no longer current"]]
    finally:
        ro.close()
    rw = sqlite3.connect(str(db))
    try:
        assert len(_canary_rows(rw)) == 2  # planted despite the read-only handle
        # Re-invocation (SqlOracle.from_cases re-runs oracles) stays stable.
        ro2 = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            assert oracle_t5_live_supersession(ro2).ok
        finally:
            ro2.close()
        assert len(_canary_rows(rw)) == 2
        t5_cleanup(rw)
        assert _canary_rows(rw) == []
    finally:
        rw.close()


def test_t5_oracle_vacuous_on_pathless_db():
    mem = sqlite3.connect(":memory:")
    try:
        oracle = oracle_t5_live_supersession(mem)
        assert not oracle.ok  # vacuous, never a false red/green
    finally:
        mem.close()


# --- T6: poison contract ----------------------------------------------------------------


def test_t6_declares_poison_contract():
    case = _case("T6")
    assert case.oracle(None).needle_groups == [[NEEDLES["fact_current"]]]
    assert T6_POISON_GROUPS == (("the Larkspur Annex",), ("no longer current",))
    field_names = {f.name for f in dataclasses.fields(CompositionCase)}
    if "poison_groups" in field_names:
        assert tuple(tuple(g) for g in case.poison_groups) == T6_POISON_GROUPS
    else:  # pre-merge fallback: shim must have dropped the kwarg silently
        assert not getattr(case, "poison_groups", ())


# --- T7/T8: red-by-construction semantics ------------------------------------------------


def test_t7_red_until_edge_validity_lands():
    case = _case("T7")
    oracle = case.oracle(None)
    # Today: the edge surfaces (top_edges branch) but nothing can mark it stale.
    red = score_composition(
        case,
        _resp(["Maren Oxbow: connected to Brindle Cassavetes"], source="entity_graph"),
        oracle,
    )
    assert red["scores"]["correctness"] == 0.5
    assert red["scores"]["attribution"] == 1.0
    # Post-B2.2: closed edge revision rendered with a staleness marker.
    green = score_composition(
        case,
        _resp(
            ["Maren Oxbow: worked with Brindle Cassavetes (no longer current — ended 2026-04)"],
            source="entity_graph",
        ),
        oracle,
    )
    assert green["scores"]["correctness"] == 1.0


def test_t8_red_without_artifact_date():
    case = _case("T8")
    oracle = case.oracle(None)
    red = score_composition(
        case, _resp([NEEDLES["stat_insight_dated"]], source="stat_insight"), oracle
    )
    assert red["scores"]["correctness"] == 0.5  # value renders, artifact date does not
    green = score_composition(
        case,
        _resp(
            [f"{NEEDLES['stat_insight_dated']} (as of {T8_STAT_PERIOD_END[:10]})"],
            source="stat_insight",
        ),
        oracle,
    )
    assert green["scores"]["correctness"] == 1.0


# --- wall-clock hygiene ------------------------------------------------------------------


def test_no_wall_clock_beyond_module_now_anchor():
    """All corpus timestamps must derive from the single _NOW read (the _iso
    scheme) — a second datetime.now() would decouple seeded data from the T4
    query/oracle and break determinism-relative-to-run."""
    src = inspect.getsource(corpus)
    assert src.count("datetime.now(") == 1  # the _NOW module anchor only
    # Derived constants stay in the _iso wire format (parseable, UTC, Z-suffixed).
    for ts in (
        FACT_SUPERSEDED_VALID_FROM,
        FACT_CURRENT_VALID_FROM,
        T8_STAT_PERIOD_END,
    ):
        assert _parse(ts).tzinfo is not None

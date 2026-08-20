"""The entity_thread lane has 14 tests. Not one of them asks whether it helps.

Severing `_load_entity_thread_items` (sweep probe r15) turns 14 tests red, and
every one of them is a presence or a privacy assertion: the lane emitted rows,
the rows carried the right source tag, the rows respected the manifest. None of
them notices if the rows it contributes make the answer WORSE. A lane can pass
all fourteen while quietly pushing better evidence out of the top of the list.

RANKING FLOOR — the ratchet idiom this repo already uses for the Wave A harness
(`QUALITY_FLOOR` in the justfile). The floor is the high-water mark on THIS
environment. It records where we are, not where we wish we were:

  * below the floor is a FAILURE — a ranking regression, visible at last;
  * above the floor is a NAG to raise it in the same commit that earned it;
  * it is never lowered to make a red go away. If a change genuinely trades
    one case for another, that is a conversation, not an edit to this table.

MEASURED 2026-08-19 by ablation. `topos.query.retrieval._load_entity_thread_items`
was replaced at RUNTIME with a function returning `[]` — the r15 severing with
no source edit — and both arms scored with `score_composition` unchanged. No
retrieval weight, fusion order, or lane scoring was touched:

    case  lane     lane OFF   lane ON     delta
    C11   live        0.725     0.725    +0.000
    C8    live        0.786     0.786    +0.000
    S4    seeded      1.000     0.917    -0.083
    T7    seeded      0.700     0.700    +0.000

    whole catalog (n=51): mean OFF 0.8344, mean ON 0.8327, aggregate -0.0016

These four are the ONLY cases in the catalog whose retrieval carries an
`entity_thread:*` source, so they are the whole of what this lane can be judged
on. No case gains from the lane. One loses: on S4 the lane adds a third item
(n_items 2 -> 3) that the oracle does not want, and specificity falls 1.000 ->
0.667. The floors below therefore pin the AS-SHIPPED numbers, dilution and all
— pinning the OFF numbers would be pinning a wish.

The per-case floors are deliberately not a single aggregate. An aggregate of
0.83 hides a lane that is flat on three cases and negative on one, which is
exactly the shape of the thing this file exists to make visible.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator
from topos.storage.adapters.factory import AdapterFactory

from composition_eval_cases import (
    LIVE_COMPOSITION_CASES,
    CompositionCase,
    score_composition,
)
from composition_seed_corpus import (
    LIVE_TEMPORAL_CASES,
    SEEDED_COMPOSITION_CASES,
    build_seeded_corpus,
)
from query_eval_cases import LIVE_DB_PATH, manifest_for_scope

pytestmark = [pytest.mark.gap, pytest.mark.qq_eval]


# The high-water mark on THIS environment, measured 2026-08-19 (see module
# docstring for the ablation that produced it). Raise, never lower.
RANKING_FLOOR: Dict[str, float] = {
    "C11": 0.725,
    "C8": 0.786,
    "S4": 0.917,
    "T7": 0.700,
}

SEEDED_FLOOR_CASES = ("S4", "T7")
LIVE_FLOOR_CASES = ("C11", "C8")

# score_composition rounds composites to three places, so an exact floor is a
# legitimate comparison; the epsilon is for float representation only.
_EPS = 1e-9


def _case(case_id: str) -> CompositionCase:
    for case in list(LIVE_COMPOSITION_CASES) + list(LIVE_TEMPORAL_CASES) + list(
        SEEDED_COMPOSITION_CASES
    ):
        if case.id == case_id:
            return case
    raise AssertionError(f"case {case_id} is pinned in RANKING_FLOOR but not registered")


async def _score(db_path: Path, case_id: str) -> Dict[str, Any]:
    case = _case(case_id)
    adapters = AdapterFactory.create("local_database", db_path=db_path)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        oracle = case.oracle(conn)
        query_text = case.query_text(conn)
    finally:
        conn.close()
    out = await orch.execute(
        query_text=query_text,
        scope_id=case.scope_id,
        access_mode=case.access_mode,
        manifest=manifest_for_scope(case.scope_id),
        query_session_id=f"qq-floor-{case_id}-{uuid.uuid4().hex[:8]}",
    )
    return score_composition(case, out, oracle)


def _assert_floor(case_id: str, scored: Dict[str, Any]) -> None:
    floor = RANKING_FLOOR[case_id]
    composite = float(scored.get("composite") or 0.0)
    sources = list(scored.get("sources") or [])

    # Non-vacuity. A floor held by a case the lane never touched proves nothing
    # about the lane, and this whole file is about the lane. If entity_thread
    # stops firing here, the pinning has silently moved to some other case's
    # behaviour and the number below is no longer evidence.
    assert any(str(s).startswith("entity_thread:") for s in sources), (
        f"{case_id} no longer retrieves anything through the entity_thread lane "
        f"(sources: {sources!r}). Its floor is now pinning a different case than "
        "the one that was measured, so it is not evidence about this lane any more."
    )

    assert composite + _EPS >= floor, (
        f"{case_id} ranking regression: composite {composite:.3f} is below the "
        f"floor {floor:.3f} measured 2026-08-19. Component scores "
        f"{scored.get('scores')!r}, sources {sources!r}. The floor is the "
        "high-water mark on this environment — do not lower it to make this "
        "green. Either the change that moved it is wrong, or the trade it makes "
        "is one the owner has to agree to."
    )

    if composite - _EPS > floor:
        print(
            f"\n[{case_id}] RAISE THE FLOOR: {composite:.3f} > {floor:.3f}. "
            "A ratchet that is never raised stops being a ratchet — pin this in "
            "the commit that earned it."
        )


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory) -> Path:
    return build_seeded_corpus(tmp_path_factory.mktemp("qq-entity-thread-floor") / "seeded.db")


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", SEEDED_FLOOR_CASES)
async def test_seeded_entity_thread_cases_hold_their_floor(seeded_db: Path, case_id: str) -> None:
    scored = await _score(seeded_db, case_id)
    print(f"\n[{case_id}] composite={scored.get('composite')} floor={RANKING_FLOOR[case_id]}")
    _assert_floor(case_id, scored)


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_DB_PATH.exists(), reason=f"live db missing: {LIVE_DB_PATH}")
@pytest.mark.parametrize("case_id", LIVE_FLOOR_CASES)
async def test_live_entity_thread_cases_hold_their_floor(case_id: str) -> None:
    scored = await _score(LIVE_DB_PATH, case_id)
    print(f"\n[{case_id}] composite={scored.get('composite')} floor={RANKING_FLOOR[case_id]}")
    _assert_floor(case_id, scored)


def test_every_pinned_case_is_registered_in_the_catalog() -> None:
    """A floor keyed on a case id that no longer exists is a dead guard.

    Cheap, offline, and it fails the moment someone renames or retires one of
    the four cases this lane is judged on, rather than at the next live run.
    """
    for case_id in RANKING_FLOOR:
        _case(case_id)
    assert set(RANKING_FLOOR) == set(SEEDED_FLOOR_CASES) | set(LIVE_FLOOR_CASES), (
        "RANKING_FLOOR and the two case lists disagree — a pinned case that is "
        "in neither list is never actually run"
    )

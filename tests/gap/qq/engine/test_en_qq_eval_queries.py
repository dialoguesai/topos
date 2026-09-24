"""
Live query eval: quality + latency against owner database.

Run against a snapshot of it (docs/testing/TEST_LANES.md), since every turn
writes a query_artifacts row. The conftest replaces a TOPOS_DATABASE_PATH that
points into ~/.topos with the session's throwaway database:
  snap=$(python scripts/snapshot_owner_db.py)
  TOPOS_DATABASE_PATH="$snap" pytest tests/gap/qq/engine/test_en_qq_eval_queries.py -m qq_eval -q -s
  rm -f "$snap" "$snap"-wal "$snap"-shm

Skips automatically when the database file is missing.
"""

from __future__ import annotations

import time
from typing import Any, Dict

import pytest

from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.storage.adapters.factory import AdapterFactory

from query_eval_cases import (
    LIVE_DB_PATH,
    PRIVACY_CASES,
    QUALITY_CASES,
    QueryQualityCase,
    manifest_for_scope,
)

pytestmark = [
    pytest.mark.gap,
    pytest.mark.qq_eval,
    pytest.mark.skipif(not LIVE_DB_PATH.exists(), reason=f"live db missing: {LIVE_DB_PATH}"),
]

# This lane is the owner asking their own node, so it asks as the owner's app.
# Since 860efe5f inference outside availability:read is owner-only: with no
# principal F1/F2/Q2/Q4 and P1 are refused before retrieval and grade the refusal.
OWNER = Principal(cls=OWNER_APP, channel="uds")


async def _execute_as_owner(orch: QueryPipelineOrchestrator, **kwargs: Any) -> Dict[str, Any]:
    token = set_principal(OWNER)
    try:
        return await orch.execute(**kwargs)
    finally:
        reset_principal(token)


@pytest.fixture(scope="module")
def live_orchestrator() -> QueryPipelineOrchestrator:
    adapters = AdapterFactory.create("local_database", db_path=LIVE_DB_PATH)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    # Warmup: first query pays one-time model loading (embeddings et al.), which
    # would otherwise be billed to whichever case runs first and blow its budget.
    import asyncio

    # The query must be realistic (multi-token, hits the vector path) — a bare
    # "warmup" token skips the semantic search that loads the embedding model —
    # and unique per run, or the query-memory cache serves it without touching
    # the vector path and the first real case pays the model load instead.
    import uuid

    asyncio.run(
        _execute_as_owner(
            orch,
            query_text=f"warmup pass over recent project notes {uuid.uuid4().hex[:8]}",
            scope_id="ai_conversations:read",
            access_mode="summary",
            manifest=manifest_for_scope("ai_conversations:read"),
            query_session_id="qq-eval-warmup",
        )
    )
    return orch


async def _run_case(orch: QueryPipelineOrchestrator, case: QueryQualityCase) -> tuple[Dict[str, Any], float]:
    import uuid

    manifest = manifest_for_scope(case.scope_id)
    t0 = time.perf_counter()
    # Unique per run (like the report runner): a stable session id keeps a
    # boundary from an OLD catalog version — re-scoping a case then classifies
    # the same session as EXPAND_BOUNDARY and the case never runs.
    out = await _execute_as_owner(
        orch,
        query_text=case.query,
        scope_id=case.scope_id,
        access_mode=case.access_mode,  # type: ignore[arg-type]
        manifest=manifest,
        query_session_id=f"qq-eval-{case.id}-{uuid.uuid4().hex[:8]}",
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return out, elapsed_ms


@pytest.mark.asyncio
@pytest.mark.parametrize("case", QUALITY_CASES, ids=[c.id for c in QUALITY_CASES])
async def test_quality_and_latency(live_orchestrator: QueryPipelineOrchestrator, case: QueryQualityCase) -> None:
    out, elapsed_ms = await _run_case(live_orchestrator, case)
    quality_ok, reason = case.evaluate(out)
    latency_ok = elapsed_ms <= case.max_latency_ms
    print(
        f"\n[{case.id}] {case.description}\n"
        f"  outcome={out.get('turn_outcome')} latency={elapsed_ms:.0f}ms (budget {case.max_latency_ms}ms)\n"
        f"  quality={'PASS' if quality_ok else 'FAIL'}: {reason}"
    )
    assert quality_ok, reason
    assert latency_ok, f"{case.id} took {elapsed_ms:.0f}ms > budget {case.max_latency_ms}ms"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", PRIVACY_CASES, ids=[c.id for c in PRIVACY_CASES])
async def test_inference_privacy(live_orchestrator: QueryPipelineOrchestrator, case: QueryQualityCase) -> None:
    out, elapsed_ms = await _run_case(live_orchestrator, case)
    quality_ok, reason = case.evaluate(out)
    print(f"\n[{case.id}] privacy latency={elapsed_ms:.0f}ms — {reason}")
    # The rubric reads a missing public_result as "nothing leaked", so a turn
    # refused before retrieval passed it without the inference lane running.
    assert isinstance(out.get("public_result"), dict), (
        f"{case.id}: nothing to check, outcome={out.get('turn_outcome')} "
        f"deny_reason={out.get('deny_reason')}"
    )
    assert quality_ok, reason


@pytest.mark.asyncio
async def test_q1_and_q5_return_different_top_summaries(live_orchestrator: QueryPipelineOrchestrator) -> None:
    q1, _ = await _run_case(live_orchestrator, QUALITY_CASES[0])
    q5, _ = await _run_case(live_orchestrator, QUALITY_CASES[4])
    s1 = q1.get("public_result") or {}
    s5 = q5.get("public_result") or {}
    top1 = (s1.get("summaries") or s1.get("scores") or [{}])[0]
    top5 = (s5.get("summaries") or s5.get("scores") or [{}])[0]
    assert _blob_key(top1) != _blob_key(top5), "Q1 and Q5 must rank different top clusters"


def _blob_key(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("topic") or item.get("summary_text") or item.get("label") or item)
    return str(item)


@pytest.mark.asyncio
async def test_vector_search_git_sanity(live_orchestrator: QueryPipelineOrchestrator) -> None:
    """Q7: vector search returns git-related hit with reasonable similarity."""
    from topos.features.signal.service import SignalService

    svc = SignalService(live_orchestrator._adapters)  # noqa: SLF001
    t0 = time.perf_counter()
    result = svc.search_vectors(query="git github", limit=5)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms <= 15000, f"vector search slow: {elapsed_ms:.0f}ms"
    items = result.get("items") or []
    if not items:
        pytest.skip("no vector embeddings in database")
    top = items[0]
    score = float(top.get("similarity") or top.get("score") or 0)
    # The field is `text_preview`; reading `preview` made the lexical half of
    # the assertion dead, so a hybrid top hit — no cosine, but the words right
    # there — failed on score alone. "git push to GitHub repository main
    # branch" was reported as a weak vector hit with an empty preview.
    preview = str(
        top.get("text_preview") or top.get("preview") or top.get("text") or top.get("source_text") or ""
    ).lower()
    print(f"\n[Q7] vector top similarity={score:.3f} preview={preview[:80]} latency={elapsed_ms:.0f}ms")
    assert score > 0.35 or "git" in preview, f"weak vector hit: score={score} preview={preview[:60]}"

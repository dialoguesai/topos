"""§F.6 — performance harness.

Measures per-path latency (wall-clock, with a warmup call to exclude model cold-start) and
reads the DDR `timings` block for the per-stage waterfall (retrieval / deterministic filter /
minimizer / game layer). The deny path is the tier-1 gate — it's fast, deterministic, and a
slow deny is a side-channel (ties to B.5/F.7). The other per-path numbers are reported for
trend/waterfall attribution; portable CI can't set a production budget dominated by hardware
and model cold-start, so those are sanity-checked, not strictly gated.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import replace
from typing import Any, Callable, Dict, List

from topos.query.manifest import ScopeResolutionManifest
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.storage.adapters.factory import AdapterBundle
from topos.storage.adapters.fakes import (
    InMemoryAuditLogStore,
    InMemoryCanonicalStore,
    InMemoryGraphEdgeStore,
    InMemoryQuerySessionStore,
    InMemorySignalFeatureStore,
    InMemoryVectorIndex,
)

SCOPE = "messages:read"


def _bundle() -> AdapterBundle:
    canonical = InMemoryCanonicalStore()
    for i in range(8):
        canonical.upsert(
            "conversation_messages",
            {"record_id": f"m{i}", "content": f"atlas note {i} about the launch", "content_disclosure": f"atlas note {i} about the launch"},
        )
    return AdapterBundle(
        canonical=canonical, signal=InMemorySignalFeatureStore(), vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(), audit=InMemoryAuditLogStore(), query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )


def _manifest(ceiling="raw") -> ScopeResolutionManifest:
    return ScopeResolutionManifest(
        scope_id=SCOPE, primary_dimensions=["Relationships"],
        canonical_tables=["conversation_messages"], access_mode_ceiling=ceiling,
    )


def percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round((pct / 100.0) * len(s) + 0.5)) - 1))
    return s[idx]


def time_calls(fn: Callable[[], Any], *, n: int = 12, warmup: int = 2) -> List[float]:
    """Return per-call wall-clock ms, discarding `warmup` cold calls (model load, imports)."""
    for _ in range(warmup):
        fn()
    out: List[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000.0)
    return out


def _exec(orch, **kw):
    return asyncio.run(orch.execute(**kw))


def deny_call() -> Dict[str, Any]:
    """A fast deny: raw requested against a summary ceiling (mode_ceiling_exceeded)."""
    orch = QueryPipelineOrchestrator(adapters=_bundle())
    return _exec(
        orch, query_text="anything", scope_id=SCOPE, access_mode="raw",
        manifest=_manifest("summary"), query_session_id=f"deny-{uuid.uuid4().hex[:8]}",
        requester_id="grantee-x", owner_id="owner-9", is_grantee_request=True,
    )


def grantee_summary_call() -> Dict[str, Any]:
    orch = QueryPipelineOrchestrator(adapters=_bundle())
    return _exec(
        orch, query_text="atlas launch", scope_id=SCOPE, access_mode="summary",
        manifest=_manifest("summary"), query_session_id=f"sum-{uuid.uuid4().hex[:8]}",
        requester_id="grantee-x", owner_id="owner-9", is_grantee_request=True,
    )


def owner_raw_call() -> Dict[str, Any]:
    orch = QueryPipelineOrchestrator(adapters=_bundle())
    return _exec(
        orch, query_text="atlas launch", scope_id=SCOPE, access_mode="raw",
        manifest=_manifest("raw"), query_session_id=f"own-{uuid.uuid4().hex[:8]}",
        requester_id="owner", owner_id="owner",
    )


def stage_waterfall(*, minimizer: bool = True) -> Dict[str, float]:
    """One grantee raw query with the DDR surfaced → the per-stage timings block."""
    prev_ddr = os.environ.get("TOPOS_QUERY_DDR")
    prev_min = os.environ.get("TOPOS_DISCLOSURE_MINIMIZER")
    os.environ["TOPOS_QUERY_DDR"] = "1"
    os.environ["TOPOS_DISCLOSURE_MINIMIZER"] = "1" if minimizer else "0"
    try:
        orch = QueryPipelineOrchestrator(adapters=_bundle())
        resp = _exec(
            orch, query_text="atlas launch", scope_id=SCOPE, access_mode="raw",
            manifest=_manifest("raw"), query_session_id=f"wf-{uuid.uuid4().hex[:8]}",
            requester_id="grantee-x", owner_id="owner-9", is_grantee_request=True,
        )
        return dict((resp.get("disclosure_decision_record") or {}).get("timings") or {})
    finally:
        for k, v in (("TOPOS_QUERY_DDR", prev_ddr), ("TOPOS_DISCLOSURE_MINIMIZER", prev_min)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def negotiation_resolution_wall_clock() -> Dict[str, float]:
    """Full-resolution wall-clock for arm C (rounds-to-resolution × per-round latency)."""
    from tests.evals.privacy.negotiation.ab_harness import run_arm_negotiated

    t0 = time.perf_counter()
    result = run_arm_negotiated()
    total = (time.perf_counter() - t0) * 1000.0
    return {"full_resolution_ms": round(total, 2), "rounds": result.rounds,
            "per_round_ms": round(total / max(1, result.rounds), 2)}


def build_perf_report(*, n: int = 12) -> Dict[str, Any]:
    def p(vals):
        return {"p50": round(percentile(vals, 50), 2), "p95": round(percentile(vals, 95), 2)}

    return {
        "paths": {
            "deny": p(time_calls(deny_call, n=n)),
            "grantee_summary": p(time_calls(grantee_summary_call, n=n)),
            "owner_raw": p(time_calls(owner_raw_call, n=n)),
        },
        "stage_waterfall_ms": stage_waterfall(minimizer=True),
        "negotiation": negotiation_resolution_wall_clock(),
    }

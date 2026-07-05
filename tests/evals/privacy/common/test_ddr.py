"""Tests for the Disclosure Decision Record (plan §A.3).

Unit-level: artifact counting and drop diffing. Integration-level: the DDR emitted by the
real query pipeline records per-stage timings and the artifacts a grantee request dropped.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from topos.query.ddr import (
    StageTimings,
    build_disclosure_decision_record,
    count_artifacts,
    diff_dropped,
)
from topos.query.pipeline import QueryPipelineOrchestrator

from tests.evals.privacy.common.corpus import build_canary_bundle

pytestmark = [pytest.mark.private]


def test_count_artifacts_counts_lists_and_graph():
    packet = {
        "rows": [1, 2, 3],
        "summaries": [{"a": 1}],
        "scores": [],
        "graph": {"nodes": [1, 2], "edges": [1]},
        "scope_id": "x",
    }
    counts = count_artifacts(packet)
    assert counts == {"rows": 3, "summaries": 1, "graph": 3}
    assert count_artifacts(None) == {}


def test_diff_dropped_reports_reductions_with_reasons():
    dropped = diff_dropped(
        {"rows": 10, "summaries": 2},
        {"rows": 3},
        reasons=["filter_manifest", "nsfw_exclusion"],
    )
    by_type = {d["artifact_type"]: d for d in dropped}
    assert by_type["rows"]["count"] == 7
    assert by_type["rows"]["reasons"] == ["filter_manifest", "nsfw_exclusion"]
    # summaries fully dropped (2 -> 0)
    assert by_type["summaries"]["count"] == 2


def test_build_record_shape():
    ddr = build_disclosure_decision_record(
        tier="default_disclosure",
        mode="summary",
        scope="messages:read",
        retrieval_packet={"rows": [1, 2], "summaries": [1]},
        filtered_packet={"summaries": [1]},
        filters_applied=["summary_mode_strip_raw"],
        timings=StageTimings(retrieval_ms=5.0, deterministic_filter_ms=1.0, total_ms=9.0),
    ).to_dict()
    assert ddr["tier"] == "default_disclosure"
    assert ddr["artifacts_in"] == {"rows": 2, "summaries": 1}
    assert ddr["artifacts_out"] == {"summaries": 1}
    assert any(d["artifact_type"] == "rows" and d["count"] == 2 for d in ddr["dropped"])
    assert ddr["timings"]["total_ms"] == 9.0
    assert ddr["minimizer"] == {"ran": False}


def test_pipeline_emits_ddr_on_audit_and_debug_result(monkeypatch):
    cb = build_canary_bundle()
    orch = QueryPipelineOrchestrator(adapters=cb.bundle)

    monkeypatch.setenv("TOPOS_QUERY_DDR", "1")
    resp = asyncio.run(
        orch.execute(
            query_text="recent messages",
            scope_id=cb.scope_id,
            access_mode="raw",
            manifest=cb.manifest,
            query_session_id=f"ddr-{uuid.uuid4().hex[:8]}",
            requester_id="grantee-x",
            owner_id="owner-9",
            is_grantee_request=True,
        )
    )
    # DDR present on both the internal audit and (debug flag on) the top-level result.
    ddr = resp["audit"]["disclosure_decision_record"]
    assert resp["disclosure_decision_record"] == ddr
    assert ddr["tier"] == "default_disclosure"
    assert ddr["mode"] == "raw"
    assert ddr["scope"] == cb.scope_id
    # Real per-stage timings recorded and ordered within the total.
    timings = ddr["timings"]
    assert timings["total_ms"] >= 0
    assert "retrieval_ms" in timings and "deterministic_filter_ms" in timings
    assert "game_layer_ms" in timings


def test_ddr_absent_from_result_without_debug_flag(monkeypatch):
    cb = build_canary_bundle()
    orch = QueryPipelineOrchestrator(adapters=cb.bundle)
    monkeypatch.delenv("TOPOS_QUERY_DDR", raising=False)
    resp = asyncio.run(
        orch.execute(
            query_text="recent messages",
            scope_id=cb.scope_id,
            access_mode="raw",
            manifest=cb.manifest,
            query_session_id=f"ddr-{uuid.uuid4().hex[:8]}",
            requester_id="grantee-x",
            owner_id="owner-9",
            is_grantee_request=True,
        )
    )
    # Internal audit always carries it; the grantee-facing result does not (no debug flag).
    assert "disclosure_decision_record" in resp["audit"]
    assert "disclosure_decision_record" not in resp

"""Runner for the time-signal request catalog (ts-1).

Executes every grantee request through the REAL query pipeline — the manifest
built from scope_registry.json, QueryPipelineOrchestrator with a sqlite adapter
bundle over the ts-1 corpus, grantee disclosure tier — and every fit case
through the owner-side evaluate_opportunity gate. Leak gates are hard failures.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict

import pytest

from time_signal_catalog import FIT_CASES, REQUEST_CASES, TS_CATALOG_VERSION
from time_signal_corpus import (
    TS_CORPUS_VERSION,
    build_empty_node,
    build_ts_corpus,
)

from topos.query.manifest import ScopeResolutionManifest
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.query.scope_registry_loader import get_scope_entry
from topos.storage.adapters.factory import AdapterFactory


@pytest.fixture(scope="module")
def corpus_conn(tmp_path_factory) -> sqlite3.Connection:
    db_path = build_ts_corpus(tmp_path_factory.mktemp("ts") / "ts.db")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def empty_conn(tmp_path_factory) -> sqlite3.Connection:
    db_path = build_empty_node(tmp_path_factory.mktemp("ts-empty") / "empty.db")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _manifest_for(scope_id: str) -> ScopeResolutionManifest:
    entry = get_scope_entry(scope_id)
    assert entry, f"scope {scope_id} missing from registry"
    return ScopeResolutionManifest.from_dict(entry)


async def _run_case(case: Dict[str, Any], conn: sqlite3.Connection) -> Dict[str, Any]:
    bundle = AdapterFactory.create("local_database", conn=conn)
    orchestrator = QueryPipelineOrchestrator(adapters=bundle)
    return await orchestrator.execute(
        query_text=case["query"],
        scope_id=case["scope_id"],
        access_mode=case["access_mode"],
        manifest=_manifest_for(case["scope_id"]),
        query_session_id=f"ts-{case['case_id']}",
        requester_id=case.get("persona") or "grantee",
        is_grantee_request=True,
    )


def _assert_expectations(case: Dict[str, Any], result: Dict[str, Any]) -> None:
    expect = case["expect"]
    blob = json.dumps(result, default=str).lower()
    assert result.get("turn_outcome") == expect["outcome"], (
        f"{case['case_id']}: outcome {result.get('turn_outcome')!r} != "
        f"{expect['outcome']!r} ({result.get('deny_reason') or result.get('reason')})"
    )
    if expect.get("deny_reason"):
        reason = str(
            result.get("deny_reason") or result.get("reason") or ""
        ).lower()
        assert reason == expect["deny_reason"], (
            f"{case['case_id']}: reason {reason!r} != {expect['deny_reason']!r}"
        )
    for group in expect.get("must_include_any") or []:
        assert any(token.lower() in blob for token in group), (
            f"{case['case_id']}: none of {group} found in response"
        )
    for token in expect.get("must_not_include") or []:
        assert token.lower() not in blob, (
            f"{case['case_id']}: LEAK — {token!r} crossed the scope boundary"
        )


def test_catalog_versions_pinned():
    assert TS_CATALOG_VERSION == "ts-1"
    assert TS_CORPUS_VERSION == "ts-1"


def test_catalog_covers_all_aspects():
    aspects = {c["aspect"] for c in REQUEST_CASES}
    assert {
        "availability",
        "negotiability",
        "rhythm",
        "load",
        "commitment",
        "leak",
        "abstention",
        "proportionality",
    } <= aspects
    assert {c["category"] for c in REQUEST_CASES} == {"usual", "targeted"}
    assert len({c["case_id"] for c in REQUEST_CASES + FIT_CASES}) == len(
        REQUEST_CASES + FIT_CASES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", REQUEST_CASES, ids=[c["case_id"] for c in REQUEST_CASES]
)
async def test_request_case(case, corpus_conn, empty_conn, monkeypatch):
    if case.get("negotiation"):
        monkeypatch.setenv("TOPOS_NEGOTIATION", "1")
    else:
        monkeypatch.delenv("TOPOS_NEGOTIATION", raising=False)
    conn = empty_conn if case.get("corpus") == "empty" else corpus_conn
    result = await _run_case(case, conn)
    _assert_expectations(case, result)


@pytest.mark.parametrize("case", FIT_CASES, ids=[c["case_id"] for c in FIT_CASES])
def test_fit_case(case, corpus_conn):
    from topos.features.fit.evaluator import evaluate_opportunity

    result = evaluate_opportunity(
        corpus_conn,
        case["opportunity_type"],
        context=case.get("context"),
    )
    expect = case["expect"]
    assert result["pass"] is expect["pass"], (
        f"{case['case_id']}: composite {result['composite_score']} "
        f"(threshold {result['pass_threshold']}) → pass={result['pass']}"
    )
    bands = {
        f["facet_id"]: f.get("public_band") for f in result["facet_results"]
    }
    for facet_id, band in (expect.get("facet_bands") or {}).items():
        assert bands.get(facet_id) == band, (
            f"{case['case_id']}: {facet_id} band {bands.get(facet_id)!r} != {band!r}"
        )


def test_scorecard_summary(corpus_conn):
    """Not an assertion lane — prints the catalog scorecard for humans."""
    by_aspect: Dict[str, int] = {}
    for case in REQUEST_CASES:
        by_aspect[case["aspect"]] = by_aspect.get(case["aspect"], 0) + 1
    total = len(REQUEST_CASES) + len(FIT_CASES)
    print(
        f"\ntime-signal catalog {TS_CATALOG_VERSION}: {total} cases "
        f"({len(REQUEST_CASES)} request + {len(FIT_CASES)} fit) — {by_aspect}"
    )

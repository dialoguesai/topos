"""Integration: the on-device minimizer wired into the pipeline (plan §D.3).

Driving the real orchestrator: off by default, grantee-only, reduces the disclosed rows to
intent-relevant facts when on, records its work in the DDR, and never touches the owner path.
Lives in the eval tree (not tests/query/) because it uses asyncio.run — see the p6 note.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace

import pytest

from topos.query.minimizer import DisclosureMinimizer, EngineSelector
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
from topos.query.manifest import ScopeResolutionManifest

pytestmark = [pytest.mark.private]

PHRASE = "quarterly launch"


def _bundle():
    canonical = InMemoryCanonicalStore()
    # Two relevant rows + one irrelevant, all retrievable by PHRASE, all pre-disclosed clean.
    for rid, content in [
        ("m0", f"{PHRASE}: logistics with Alex on Tuesday"),
        ("m1", f"{PHRASE}: sourdough recipe notes"),
        ("m2", f"{PHRASE}: budget for the launch"),
    ]:
        canonical.upsert("conversation_messages", {"record_id": rid, "content": content, "content_disclosure": content})
    return AdapterBundle(
        canonical=canonical,
        signal=InMemorySignalFeatureStore(),
        vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(),
        audit=InMemoryAuditLogStore(),
        query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )


def _manifest():
    return ScopeResolutionManifest(
        scope_id="messages:read",
        primary_dimensions=["Relationships"],
        canonical_tables=["conversation_messages"],
        access_mode_ceiling="raw",
    )


def _run(orch, *, intent, grantee=True, selector=None):
    if selector is not None:
        orch._minimizer = DisclosureMinimizer(selector=selector)
    kwargs = dict(
        query_text=intent,
        scope_id="messages:read",
        access_mode="raw",
        manifest=_manifest(),
        query_session_id=f"min-{uuid.uuid4().hex[:8]}",
    )
    if grantee:
        kwargs.update(requester_id="grantee-x", owner_id="owner-9", is_grantee_request=True)
    else:
        kwargs.update(requester_id="owner", owner_id="owner", is_grantee_request=False)
    return asyncio.run(orch.execute(**kwargs))


def _contents(resp):
    return {r.get("content") for r in (resp.get("public_result") or {}).get("rows") or []}


def test_minimizer_off_by_default_keeps_all_retrieved(monkeypatch):
    monkeypatch.delenv("TOPOS_DISCLOSURE_MINIMIZER", raising=False)
    # Query by the shared phrase so retrieval returns all three rows; with the minimizer OFF
    # they all survive (including the sourdough row) — the minimizer is what would drop it.
    resp = _run(QueryPipelineOrchestrator(adapters=_bundle()), intent=PHRASE)
    assert len(_contents(resp)) == 3
    assert any("sourdough" in c for c in _contents(resp))


def test_minimizer_on_reduces_to_relevant_facts(monkeypatch):
    monkeypatch.setenv("TOPOS_DISCLOSURE_MINIMIZER", "1")
    # Deterministic engine selector: keep only the logistics + budget rows.
    sel = EngineSelector(complete=lambda prompt: '["rows:0","rows:2"]')
    resp = _run(QueryPipelineOrchestrator(adapters=_bundle()), intent="launch logistics and budget", selector=sel)
    contents = _contents(resp)
    assert not any("sourdough" in c for c in contents), "irrelevant row should be minimized away"
    assert any("logistics" in c for c in contents)


def test_minimizer_records_ddr(monkeypatch):
    monkeypatch.setenv("TOPOS_DISCLOSURE_MINIMIZER", "1")
    monkeypatch.setenv("TOPOS_QUERY_DDR", "1")
    sel = EngineSelector(complete=lambda prompt: '["rows:0","rows:2"]')
    resp = _run(QueryPipelineOrchestrator(adapters=_bundle()), intent="launch logistics and budget", selector=sel)
    ddr = resp["disclosure_decision_record"]
    assert ddr["minimizer"]["ran"] is True
    assert ddr["minimizer"]["removed_facts"] >= 1
    assert "minimizer_ms" in ddr["timings"]


def test_minimizer_skips_owner(monkeypatch):
    monkeypatch.setenv("TOPOS_DISCLOSURE_MINIMIZER", "1")
    monkeypatch.setenv("TOPOS_QUERY_DDR", "1")
    sel = EngineSelector(complete=lambda prompt: "[]")  # would drop everything if it ran
    resp = _run(QueryPipelineOrchestrator(adapters=_bundle()), intent="anything", grantee=False, selector=sel)
    # Owner path: minimizer must not run; all rows remain.
    assert len(_contents(resp)) == 3
    ddr = resp["disclosure_decision_record"]
    # minimizer either absent from timings or not-ran on owner tier
    assert ddr["minimizer"].get("ran") in (False, None)

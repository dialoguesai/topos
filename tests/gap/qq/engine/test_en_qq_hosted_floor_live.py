"""SUITE-EXPOSE, engine leg: the hosted floor holds on the real corpus.

protects: the zero-exposure guarantee is enforced, not advisory.

Runs on the owner-snapshot lane because the guarantee is about VOLUME of real
prose, and a seeded database cannot produce it: `_bundle_is_global_db`
disables the vector and cluster layers on a non-global DB, so a seeded turn
returns `store_empty` and would assert nothing.

This is the case that found the defect. On the owner snapshot 2026-09-03, the
same query with a hosted binding returned `packet_resolution: scores_only`,
`packet_resolution_reason: hosted_binding` — and 60,151 characters of the
owner's conversation text in `public_result.summaries`, byte-identical to the
local run. Both legs run here so the comparison is the assertion: local
serves the owner their own words, hosted serves the same SHAPE with none of
them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from query_eval_cases import LIVE_DB_PATH, manifest_for_scope

pytestmark = [
    pytest.mark.gap,
    pytest.mark.qq_eval,
    pytest.mark.asyncio,
    pytest.mark.skipif(not LIVE_DB_PATH.exists(), reason=f"live db missing: {LIVE_DB_PATH}"),
]

QUERY = "What have I been working on lately?"
SCOPE = "ai_conversations:read"


async def _turn(db: Path):
    from topos.query.pipeline import QueryPipelineOrchestrator
    from topos.storage.adapters.factory import AdapterFactory

    adapters = AdapterFactory.create("local_database", db_path=db)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    return await orch.execute(
        query_text=QUERY,
        scope_id=SCOPE,
        access_mode="summary",
        manifest=manifest_for_scope(SCOPE),
        query_session_id="hosted-floor-live",
    )


def _summary_chars(public_result) -> int:
    return sum(
        len(str(s.get("summary_text") or ""))
        for s in (public_result.get("summaries") or [])
        if isinstance(s, dict)
    )


def _bind(monkeypatch, url):
    """`primary_binding_locality` reads the settings singleton, which is built
    at import time — an env var set inside the test process is never seen."""
    from topos.config.settings import settings

    monkeypatch.setattr(settings, "topos_engine_service_url", url, raising=False)


async def test_local_binding_serves_the_owner_their_own_words(monkeypatch):
    _bind(monkeypatch, None)
    out = await _turn(LIVE_DB_PATH)
    pr = out.get("public_result") or {}
    chars = _summary_chars(pr)
    assert pr.get("packet_resolution_reason") != "hosted_binding"
    assert chars > 1000, (
        f"only {chars} chars on the local leg — this corpus cannot demonstrate "
        "the guarantee, so the hosted leg below would assert nothing"
    )


async def test_hosted_binding_withholds_the_prose_on_the_real_corpus(monkeypatch):
    _bind(monkeypatch, "https://hosted.example/engine")
    out = await _turn(LIVE_DB_PATH)
    pr = out.get("public_result") or {}
    assert pr.get("packet_resolution") == "scores_only"
    assert pr.get("packet_resolution_reason") == "hosted_binding"
    chars = _summary_chars(pr)
    assert chars == 0, f"{chars} characters of owner prose survived the hosted floor"
    # The shape survives, so the turn is still legible and honest.
    assert pr.get("summaries"), "the floor emptied the result instead of withholding text"
    ledger = (out.get("narrowing") or {}).get("ledger") or []
    assert any(str(e.get("reason")) == "hosted_binding_text_withheld" for e in ledger)

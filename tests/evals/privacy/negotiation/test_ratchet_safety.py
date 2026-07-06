"""§C.4 ratchet safety — negotiation must not be a widening oracle.

Two invariants:
  1. A counter-offer (narrow_request) discloses NOTHING — no user data, no canary, ever.
  2. Cumulative disclosure across all negotiation rounds equals the disclosure of the final
     accepted query alone — the earlier rounds contribute zero, so a requester cannot
     accumulate more than a single proportional grant would give by iterating.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace

import pytest

from topos.query.negotiation import DEFAULT_MAX_ROUNDS
from topos.query.pipeline import QueryPipelineOrchestrator

from tests.evals.privacy.common.corpus import build_canary_bundle
from tests.evals.privacy.common.probe import response_discloses

pytestmark = [pytest.mark.private]


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("TOPOS_NEGOTIATION", "1")


def _execute(orch, cb, *, query_text, mode="summary", session_id):
    manifest = cb.manifest if mode == "raw" else replace(cb.manifest, access_mode_ceiling=mode)
    return asyncio.run(
        orch.execute(
            query_text=query_text,
            scope_id=cb.scope_id,
            access_mode=mode,
            manifest=manifest,
            query_session_id=session_id,
            requester_id="grantee-x",
            owner_id="owner-9",
            is_grantee_request=True,
        )
    )


def test_counter_offer_discloses_no_user_data():
    cb = build_canary_bundle()
    resp = _execute(cb_orch := QueryPipelineOrchestrator(adapters=cb.bundle), cb,
                    query_text="give me everything", session_id=f"r-{uuid.uuid4().hex[:8]}")
    assert resp["turn_outcome"] == "narrow_request"
    assert resp["public_result"] is None
    # The raw canary planted in the corpus must not appear anywhere in the offer response.
    assert not response_discloses(resp, [cb.raw_canary])
    assert cb.raw_canary not in json.dumps(resp, default=str)


def test_cumulative_disclosure_equals_final_query_alone():
    cb = build_canary_bundle()
    orch = QueryPipelineOrchestrator(adapters=cb.bundle)
    session_id = f"r-{uuid.uuid4().hex[:8]}"

    # Rounds 1-2: broad → narrow_request, zero disclosure.
    r1 = _execute(orch, cb, query_text="everything", session_id=session_id)
    r2 = _execute(orch, cb, query_text="all your data", session_id=session_id)
    assert r1["turn_outcome"] == "narrow_request" and r1["public_result"] is None
    assert r2["turn_outcome"] == "narrow_request" and r2["public_result"] is None

    # Round 3: specific + bounded → proceeds and discloses.
    r3 = _execute(
        orch, cb,
        query_text="Summarize messages with Alex about the Q3 launch in the last 30 days",
        session_id=session_id,
    )
    assert r3["turn_outcome"] != "narrow_request"

    # Cumulative disclosed payload across all rounds == round 3's payload alone.
    disclosed = [r for r in (r1, r2, r3) if r.get("public_result") is not None]
    assert disclosed == [r3], "only the accepted query disclosed anything"


def test_escalation_budget_bounds_total_offers():
    cb = build_canary_bundle()
    orch = QueryPipelineOrchestrator(adapters=cb.bundle)
    session_id = f"r-{uuid.uuid4().hex[:8]}"
    offers = 0
    denied = False
    for _ in range(DEFAULT_MAX_ROUNDS + 3):
        resp = _execute(orch, cb, query_text="everything", session_id=session_id)
        if resp["turn_outcome"] == "narrow_request":
            offers += 1
        elif resp["turn_outcome"] == "denied" and resp.get("deny_reason") == "negotiation_exhausted":
            denied = True
            break
    # The requester gets at most DEFAULT_MAX_ROUNDS offers before a hard deny — probing costs.
    assert offers <= DEFAULT_MAX_ROUNDS
    assert denied

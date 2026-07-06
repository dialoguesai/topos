"""Integration: negotiation wired into the query pipeline (plan §C.3).

Exercises the real QueryPipelineOrchestrator with an in-memory bundle: a grantee's broad
intent gets a narrow_request (no data touched), a specific intent proceeds, the owner is
never nagged, negotiation is off by default, and the round budget exhausts to a hard deny.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from topos.query.pipeline import QueryPipelineOrchestrator

from tests.evals.privacy.common.corpus import build_canary_bundle

pytestmark = [pytest.mark.private]


def _orch():
    return QueryPipelineOrchestrator(adapters=build_canary_bundle().bundle)


def _run(orch, cb, *, query_text, mode="summary", grantee=True, session_id=None):
    from dataclasses import replace

    manifest = cb.manifest if mode == "raw" else replace(cb.manifest, access_mode_ceiling=mode)
    kwargs = dict(
        query_text=query_text,
        scope_id=cb.scope_id,
        access_mode=mode,
        manifest=manifest,
        query_session_id=session_id or f"neg-{uuid.uuid4().hex[:8]}",
    )
    if grantee:
        kwargs.update(requester_id="grantee-x", owner_id="owner-9", is_grantee_request=True)
    else:
        kwargs.update(requester_id="owner", owner_id="owner", is_grantee_request=False)
    return asyncio.run(orch.execute(**kwargs))


def test_negotiation_off_by_default_broad_intent_proceeds(monkeypatch):
    monkeypatch.delenv("TOPOS_NEGOTIATION", raising=False)
    cb = build_canary_bundle()
    resp = _run(_orch(), cb, query_text="everything")
    assert resp["turn_outcome"] != "narrow_request"  # additive: default behavior unchanged


def test_grantee_broad_intent_gets_narrow_request(monkeypatch):
    monkeypatch.setenv("TOPOS_NEGOTIATION", "1")
    cb = build_canary_bundle()
    resp = _run(_orch(), cb, query_text="give me everything you have")
    assert resp["turn_outcome"] == "narrow_request"
    assert resp["public_result"] is None  # nothing disclosed
    assert resp["offer"]["suggested_intents"]
    assert resp["offer"]["round"] == 1


def test_grantee_specific_intent_proceeds(monkeypatch):
    monkeypatch.setenv("TOPOS_NEGOTIATION", "1")
    cb = build_canary_bundle()
    resp = _run(
        _orch(),
        cb,
        query_text="Summarize messages with Alex about the Q3 launch in the last 30 days",
    )
    assert resp["turn_outcome"] != "narrow_request"


def test_owner_is_never_nagged(monkeypatch):
    monkeypatch.setenv("TOPOS_NEGOTIATION", "1")
    cb = build_canary_bundle()
    resp = _run(_orch(), cb, query_text="everything", grantee=False)
    assert resp["turn_outcome"] != "narrow_request"


def test_mode_above_ceiling_becomes_offer_not_bare_deny(monkeypatch):
    monkeypatch.setenv("TOPOS_NEGOTIATION", "1")
    from dataclasses import replace

    cb = build_canary_bundle()
    # raw requested against a summary ceiling, but the intent is otherwise specific + bounded.
    manifest = replace(cb.manifest, access_mode_ceiling="summary")
    resp = asyncio.run(
        _orch().execute(
            query_text="messages with Alex about the launch last week",
            scope_id=cb.scope_id,
            access_mode="raw",
            manifest=manifest,
            query_session_id=f"neg-{uuid.uuid4().hex[:8]}",
            requester_id="grantee-x",
            owner_id="owner-9",
            is_grantee_request=True,
        )
    )
    assert resp["turn_outcome"] == "narrow_request"
    assert resp["offer"]["reason"] == "mode_above_ceiling"
    assert resp["offer"]["access_modes"]["raw"] == "requires_owner_approval"


def test_round_budget_exhausts_to_hard_deny(monkeypatch):
    monkeypatch.setenv("TOPOS_NEGOTIATION", "1")
    cb = build_canary_bundle()
    orch = _orch()
    session_id = f"neg-{uuid.uuid4().hex[:8]}"
    outcomes = []
    for _ in range(5):
        resp = _run(orch, cb, query_text="everything", session_id=session_id)
        outcomes.append((resp["turn_outcome"], resp.get("deny_reason")))
    # First DEFAULT_MAX_ROUNDS are narrow_request, then it hard-denies (budget spent).
    assert outcomes[0][0] == "narrow_request"
    assert any(o[0] == "denied" and o[1] == "negotiation_exhausted" for o in outcomes), outcomes

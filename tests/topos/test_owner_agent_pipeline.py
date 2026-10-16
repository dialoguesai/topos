"""The ``owner_agent`` disclosure tier through the REAL query pipeline (AI-tool on-ramp, CONTRACTS §1.3; lane L1,
verifier finding F2).

protects: the one line that hands the turn's scope to the tier (`pipeline.py`, `resolve_disclosure_tier(...,
scope_id=...)`). Without it the agent's tier is owner_raw for every scope, special-class ones included, and the
unit tests of `tier.py` cannot see that. So this drives `QueryPipelineOrchestrator.execute` on a scratch database
(the fixtures of `tests/query/test_query_multi_turn_cache.py`, invented calendar data) and reads the tier the turn
actually ran at, from the turn's own audit block and its top-level field.
"""
from __future__ import annotations

import pytest

from tests.query.test_query_multi_turn_cache import orchestrator, seeded_conn  # noqa: F401 (fixtures)
from topos.principal import Principal, reset_principal, set_principal
from topos.query.manifest_validation import resolve_scope_manifest

pytestmark = pytest.mark.public

AGENT = Principal(cls="owner_agent", channel="uds", client_id="agent-claude-code")


async def _turn(orchestrator, scope_id, query, session_id):
    token = set_principal(AGENT)
    try:
        return await orchestrator.execute(query_text=query, scope_id=scope_id, access_mode="raw",
                                          manifest=resolve_scope_manifest(scope_id), query_session_id=session_id)
    finally:
        reset_principal(token)


@pytest.mark.asyncio
async def test_a_special_class_scope_runs_at_default_disclosure_for_the_agent(orchestrator):
    turn = await _turn(orchestrator, "health:read", "How did I sleep on March 13 2026?", "qs-agent-health")
    assert turn["turn_outcome"] == "live_query", turn
    assert turn["audit"]["disclosure_tier"] == "default_disclosure"
    assert turn["disclosure_tier"] == "default_disclosure"
    assert turn["audit"]["client_id"] == "agent-claude-code"
    assert turn["audit"]["principal_cls"] == "owner_agent"


@pytest.mark.asyncio
async def test_a_normal_scope_runs_at_owner_raw_for_the_agent(orchestrator):
    turn = await _turn(orchestrator, "schedule:read", "What meetings does Jordan have on March 13 2026?", "qs-agent-schedule")
    assert turn["turn_outcome"] == "live_query", turn
    assert turn["audit"]["disclosure_tier"] == "owner_raw"
    assert turn["disclosure_tier"] == "owner_raw"

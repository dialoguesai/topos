"""BL-65: a turn that carries a principal is replayed from its session, and only for the same principal class.

The pipeline STORES a turn's artifact under a fingerprint and a cache key that fold in two disclosure dimensions:
the principal's class and the packet resolution (`compute_retrieval_fingerprint(principal_cls=, packet_resolution=)`,
`build_cache_key(packet_resolution=)`). The turn classifier built the EXPECTED fingerprint without them, so for any turn
with a principal the two never matched and the turn ran again (fails safe: more work, never a stale or wrong answer;
found by the N8 traced comparison). Now the classifier is given both.

protects: an owner-app turn and a third-party (his outside client) turn are each replayed for themselves; neither is
ever replayed for the other, in either order; a protection change still ends a replay. Invented calendar data.
"""
from __future__ import annotations

import pytest

from tests.query.test_query_multi_turn_cache import orchestrator, seeded_conn  # noqa: F401 (fixtures)
from topos.principal import OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.query.manifest_validation import resolve_scope_manifest

pytestmark = pytest.mark.public

APP = Principal(cls=OWNER_APP, channel="cp_relay", acting_user="owner-1")
CLIENT = Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="mcp", acting_user="owner-1")
QUESTION = "What meetings does Jordan have on March 13 2026?"


async def ask(orchestrator, principal, session_id, query=QUESTION):
    token = set_principal(principal)
    try:
        return await orchestrator.execute(query_text=query, scope_id="schedule:read", access_mode="raw",
                                          manifest=resolve_scope_manifest("schedule:read"), query_session_id=session_id)
    finally:
        reset_principal(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [APP, CLIENT], ids=["owner_app", "third_party"])
async def test_a_turn_with_a_principal_is_replayed_from_its_session(orchestrator, principal):
    """Rule: the classifier's expected fingerprint carries `principal_cls` and `packet_resolution`. Leave them out
    (as before BL-65) and the second turn queries live again."""
    first = await ask(orchestrator, principal, "qs-bl65-" + principal.cls)
    assert first["turn_outcome"] == "live_query" and orchestrator._retrieval.retrieve_call_count == 1
    second = await ask(orchestrator, principal, "qs-bl65-" + principal.cls)
    assert second["turn_outcome"] == "memory_hit"
    assert orchestrator._retrieval.retrieve_call_count == 1
    assert second["public_result"] == first["public_result"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_principal,then", [(APP, CLIENT), (CLIENT, APP)], ids=["app_then_client", "client_then_app"])
async def test_a_turn_is_never_replayed_for_another_principal_class(orchestrator, first_principal, then):
    session = "qs-bl65-cross"
    first = await ask(orchestrator, first_principal, session)
    assert first["turn_outcome"] == "live_query"
    other = await ask(orchestrator, then, session)
    assert other["turn_outcome"] == "live_query"
    assert orchestrator._retrieval.retrieve_call_count == 2


@pytest.mark.asyncio
async def test_a_protection_change_still_ends_a_replay(seeded_conn, orchestrator):
    session = "qs-bl65-protection"
    first = await ask(orchestrator, APP, session)
    assert first["turn_outcome"] == "live_query"
    seeded_conn.execute("INSERT INTO owner_only_records (canonical_table, record_id) VALUES ('calendar_events', 'zz')")
    seeded_conn.commit()
    second = await ask(orchestrator, APP, session)
    assert second["turn_outcome"] != "memory_hit"

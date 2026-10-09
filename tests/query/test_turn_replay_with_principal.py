"""BL-65: the owner's app's turn is replayed from its session; no other principal's turn is.

The pipeline STORES a turn's artifact under a fingerprint and a cache key that fold in two disclosure dimensions:
the principal's class and the packet resolution (`compute_retrieval_fingerprint(principal_cls=, packet_resolution=)`,
`build_cache_key(packet_resolution=)`). The turn classifier built the EXPECTED fingerprint without them, so for any turn
with a principal the two never matched and the turn ran again (fails safe: more work, never a stale or wrong answer;
found by the N8 traced comparison). Now the classifier is given both, and the owner's app's turns replay.

A turn of any other principal class (his outside client, a routine, any third party) is never replayed, as in 1.5.0
(WS0's ruling on review R-N1-151 M1): what ends a replay does not cover the protected closure (a contact, alias or
mention newly linked to an Off-limits person), so a replay could serve what the boundary would now cut.

protects: an owner-app turn is replayed; a third-party turn never is, in a session of its own or after an owner-app
turn, nor after a new Off-limits link; a protection change still ends a replay. Invented calendar data.
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
async def test_an_owner_app_turn_is_replayed_from_its_session(orchestrator):
    """Rule: the classifier's expected fingerprint carries `principal_cls` and `packet_resolution`. Leave them out
    (as before BL-65) and the second turn queries live again."""
    first = await ask(orchestrator, APP, "qs-bl65-app")
    assert first["turn_outcome"] == "live_query" and orchestrator._retrieval.retrieve_call_count == 1
    second = await ask(orchestrator, APP, "qs-bl65-app")
    assert second["turn_outcome"] == "memory_hit"
    assert orchestrator._retrieval.retrieve_call_count == 1
    assert second["public_result"] == first["public_result"]


@pytest.mark.asyncio
async def test_a_third_party_turn_is_never_replayed(orchestrator):
    """Rule: `TurnClassifierLite.classify` queries live for any principal class but the owner's app (R-N1-151 M1)."""
    for _ in range(3):
        turn = await ask(orchestrator, CLIENT, "qs-bl65-client")
        assert turn["turn_outcome"] == "live_query"
    assert orchestrator._retrieval.retrieve_call_count == 3


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
async def test_a_third_party_replay_after_a_new_off_limits_link_is_not_served(seeded_conn, orchestrator):
    """The case M1 names: an Off-limits entry exists, and a contact is then linked to that person. The entry ROWS do
    not move, so `protection_fingerprint` does not either; a third party's second turn still runs live."""
    from topos.features.lifecycle.blackhole import BlackholeStore
    BlackholeStore(seeded_conn).blackhole_entity(entity_ref="Perrin Ashgrove", processing_tier="secure", note=None)
    seeded_conn.commit()
    session = "qs-bl65-link"
    first = await ask(orchestrator, CLIENT, session)
    assert first["turn_outcome"] == "live_query"
    entries = {row[0] for row in seeded_conn.execute("SELECT blackhole_id FROM entity_blackholes")}
    # The person of the entry gains an alias that the first answer's attendee carries: a closure change only.
    seeded_conn.execute("INSERT INTO entities(entity_id, entity_type, canonical_name, normalized_name, aliases_json) "
                        "VALUES ('ent-linked', 'person', 'Perrin Ashgrove', 'perrin ashgrove', '[\"Jordan Lee\"]')")
    seeded_conn.commit()
    assert {row[0] for row in seeded_conn.execute("SELECT blackhole_id FROM entity_blackholes")} == entries
    second = await ask(orchestrator, CLIENT, session)
    assert second["turn_outcome"] == "live_query"


@pytest.mark.asyncio
async def test_a_protection_change_still_ends_a_replay(seeded_conn, orchestrator):
    session = "qs-bl65-protection"
    first = await ask(orchestrator, APP, session)
    assert first["turn_outcome"] == "live_query"
    seeded_conn.execute("INSERT INTO owner_only_records (canonical_table, record_id) VALUES ('calendar_events', 'zz')")
    seeded_conn.commit()
    second = await ask(orchestrator, APP, session)
    assert second["turn_outcome"] != "memory_hit"


def test_a_third_party_turn_back_to_an_earlier_scope_requalifies_as_in_150():
    """Review R-N1-151 R2 N4: the refusal for other classes sits where 1.5.0 refused a replay (an artifact under the
    same key), so a turn with no such artifact that returns to an earlier scope with a new intent still REQUALIFIES."""
    from topos.query.session import QueryArtifact, QuerySession, TurnOutcome
    from topos.query.turn_classifier import TurnClassifierLite
    from topos.query.types import QueryTurn

    session = QuerySession(session_id="qs-n4", requester_id="r", intent_hash="earlier-intent",
                           envelope_json={"scopes": ["schedule:read", "messages:read"], "access_modes": ["raw"],
                                          "last_scope_id": "messages:read"})
    turn = QueryTurn(query_text="What is on Friday?", scope_id="schedule:read", access_mode="raw")
    classify = TurnClassifierLite().classify
    assert classify(turn, session, principal_cls=CLIENT.cls).outcome == TurnOutcome.REQUALIFY
    stored = classify(turn, None).cache_key
    session.artifacts = [QueryArtifact(artifact_id="a1", session_id="qs-n4", cache_key=stored,
                                       retrieval_fingerprint="", public_result_json={})]
    assert classify(turn, session, principal_cls=CLIENT.cls).outcome == TurnOutcome.LIVE_QUERY
    assert classify(turn, session, principal_cls=APP.cls).outcome == TurnOutcome.MEMORY_HIT

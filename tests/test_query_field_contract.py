"""The engine's half of the query field contract.

Every seam between a client and this engine rebuilds its payload from a hand-written
allow-list. A field can be declared at one end, sent faithfully, and disappear in the
middle with nothing failing and nothing logged — twice in one day on 2026-08-17
(`sourceRefs` in the front end, `retrieval_text` in the control plane), and the second
was found only because someone went looking for it.

`protocol/query_field_contract.json` is the declaration those seams are tested against.
This file guards the engine end: a field the contract promises must actually be read
here, and one the contract promises to return must actually be emitted. The control
plane and the front end each guard their own seam against the same file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "topos" / "protocol" / "query_field_contract.json").read_text())
HANDLER = (ROOT / "topos" / "core" / "handlers" / "query.py").read_text()


def test_the_contract_is_well_formed() -> None:
    assert CONTRACT["version"] >= 1
    assert CONTRACT["request"]["required_forward"]
    assert CONTRACT["response"]["required_return"]


#: Addressing, not question: declared so every seam forwards them, resolved before the
#: handler rather than by it. Read off the contract instead of spelled here, so an
#: exception is a decision recorded in the protocol rather than a line added to a test.
BEFORE_HANDLER = set(CONTRACT["request"].get("consumed_before_handler") or {}) - {"$comment"}


def test_the_exemptions_are_declared_in_the_contract_not_invented_here() -> None:
    """The exemption list is only honest while it is small and justified in the contract.

    Each entry needs a stated reason there; an undocumented one is how "the handler does
    not read it" quietly becomes "nothing reads it anywhere".
    """
    declared = CONTRACT["request"].get("consumed_before_handler") or {}
    for field in BEFORE_HANDLER:
        assert field in CONTRACT["request"]["required_forward"], (
            f"{field!r} is exempted from the handler check but is not on the forward "
            "list — it is exempting nothing"
        )
        assert str(declared[field]).strip(), f"{field!r} is exempted with no reason given"


@pytest.mark.parametrize("field", CONTRACT["request"]["required_forward"])
def test_every_forwarded_request_field_is_read_by_the_handler(field: str) -> None:
    """A field nobody reads is a field the contract should not be promising."""
    if field in BEFORE_HANDLER:
        pytest.skip("declared consumed_before_handler in the contract, not by the handler")
    assert re.search(rf'payload\.get\("{re.escape(field)}"', HANDLER), (
        f"{field!r} is promised by the contract but never read in handlers/query.py — "
        "either the handler regressed or the contract is aspirational"
    )


def test_query_outranks_intent_and_retrieval_text_outranks_neither() -> None:
    """The precedence that keeps the classifier and planner on a sentence.

    `query` (the owner's words) must win over `intent` (a keyword digest); sending the
    digest as the query text was measured on 2026-08-16 to lose time windows and make
    the scope classifier abstain on keyword soup. `retrieval_text` is a companion for
    needle matching, never a substitute for either.
    """
    assert re.search(r'payload\.get\("query"\)\s*or\s*payload\.get\("intent"\)', HANDLER)
    assert "retrieval_text" in HANDLER
    assert not re.search(r'query_text\s*=\s*.*payload\.get\("retrieval_text"\)', HANDLER)


# ---------------------------------------------------------------------------------
# The response half.
#
# It used to be one parametrize over a hardcoded `["turn_outcome", "public_result"]` —
# 2 of the 8 fields the contract declares, checked by grepping the handler source for a
# quoted string. Adding a ninth field to `required_return` could never make it fail, and
# in the blind spot that left sat a live bug: `deny_reason` was declared, dropped by the
# control plane on every manifest-validation denial, and covered by nothing anywhere.
#
# So these run the real return paths and read the field list off the contract.
# ---------------------------------------------------------------------------------

#: Declared as returned, but ECHOED by the transport rather than emitted here — the
#: engine answers inside a scope it was told and at a tier it was told. Read off the
#: contract for the same reason `consumed_before_handler` is: an exemption argued in the
#: protocol is one somebody decided, and the next one has to be argued too.
TRANSPORT_SUPPLIED = set(CONTRACT["response"].get("originates") or {}) - {"$comment"}


def test_the_response_exemptions_are_declared_in_the_contract_not_invented_here() -> None:
    declared = CONTRACT["response"].get("originates") or {}
    for field in TRANSPORT_SUPPLIED:
        assert field in CONTRACT["response"]["required_return"], (
            f"{field!r} is marked transport-supplied but is not a declared return field "
            "— it is exempting nothing"
        )
        assert str(declared[field]).strip(), f"{field!r} is exempted with no reason given"


async def _manifest_denial_payload() -> dict:
    """The denial `handlers/query.py` builds itself, from the wire.

    An empty `scope_id` fails `resolve_scope_manifest` with `missing_scope`, which is the
    one return path in this engine that constructs a whole response from a literal
    allow-list instead of passing the orchestrator's through. It returns before an
    orchestrator or a database connection is ever needed.
    """
    from topos.core.handlers import handle_control_plane_request

    out = await handle_control_plane_request(
        {"id": "contract-denial", "type": "query", "payload": {"scope_id": "", "query": "hi"}}
    )
    assert out.get("status") == "ok", out
    return out["payload"]


def _in_memory_adapters():
    from topos.storage.adapters.factory import AdapterBundle
    from topos.storage.adapters.fakes import (
        InMemoryAuditLogStore,
        InMemoryCanonicalStore,
        InMemoryGraphEdgeStore,
        InMemoryQuerySessionStore,
        InMemorySignalFeatureStore,
        InMemoryVectorIndex,
    )

    signal = InMemorySignalFeatureStore()
    signal.put_summary(
        {"dimension": "relationship", "summary_text": "close friend", "topic": "relationship"}
    )
    return AdapterBundle(
        canonical=InMemoryCanonicalStore(),
        signal=signal,
        vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(),
        audit=InMemoryAuditLogStore(),
        query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )


async def _live_turn_payload() -> dict:
    """A turn that answers, through the real orchestrator on in-memory stores.

    The denial above cannot carry `public_result` or `game_layer_strategy` and this one
    cannot carry `deny_reason`; between them they cover every field the engine originates.
    A single fixture claiming to cover all of them is what `deny_reason: ""` was.
    """
    from topos.query.manifest import ScopeResolutionManifest
    from topos.query.pipeline import QueryPipelineOrchestrator

    orch = QueryPipelineOrchestrator(adapters=_in_memory_adapters())
    return await orch.execute(
        query_text="how is my relationship context this week",
        scope_id="relationship_context:read",
        access_mode="summary",
        manifest=ScopeResolutionManifest(
            scope_id="relationship_context:read",
            primary_dimensions=["relationship"],
            summary_objects=["relationship_summary"],
            access_mode_ceiling="summary",
        ),
        query_session_id="qs-contract",
    )


async def _engine_payloads() -> dict:
    return {
        "manifest_denied": await _manifest_denial_payload(),
        "live_query": await _live_turn_payload(),
    }


@pytest.mark.asyncio
async def test_every_returned_field_the_engine_originates_is_actually_emitted() -> None:
    """The check the hardcoded pair could not do: driven off the contract, on real payloads.

    Add a field to `required_return` and this goes red until some real engine path emits
    it — which is the whole point of declaring it.
    """
    payloads = await _engine_payloads()
    uncovered = [
        field
        for field in CONTRACT["response"]["required_return"]
        if field not in TRANSPORT_SUPPLIED
        and not any(field in payload for payload in payloads.values())
    ]
    assert not uncovered, (
        f"{uncovered} are declared as returned by this engine but no real return path "
        "emits them — either a path regressed or the contract is aspirational"
    )


@pytest.mark.asyncio
async def test_a_denial_says_why_it_denied() -> None:
    """`deny_reason` at TOP LEVEL, which the contract names as its canonical location.

    This path writes no `audit` block — it returns before the orchestrator that would
    build one — so a consumer that reads the reason only out of `audit` gets nothing,
    and the owner is handed a refusal with no why. That is what the control plane did.
    """
    payload = await _manifest_denial_payload()
    assert CONTRACT["response"]["deny_reason_location"]["canonical"] == "top_level"
    assert payload.get("turn_outcome") == "denied"
    assert payload.get("deny_reason") == "missing_scope"
    assert "audit" not in payload, (
        "this path grew an audit block; if the reason now travels there too, say so in "
        "the contract's deny_reason_location rather than leaving consumers to guess"
    )


@pytest.mark.asyncio
async def test_a_denial_carries_the_ledger_that_explains_it() -> None:
    """A denial with no ledger is the opacity the ledger was built to end.

    Every other denial in this engine goes through `_attach_narrowing`; this one returns
    above the orchestrator, so it has to write its own or carry none at all — which is
    what it did.
    """
    payload = await _manifest_denial_payload()
    narrowing = payload.get("narrowing") or {}
    assert narrowing.get("empty_cause") == "scope_denied"
    assert [e["reason"] for e in narrowing.get("ledger") or []] == ["missing_scope"]
    assert payload.get("query_session_id") == payload.get("session_id")


@pytest.mark.asyncio
async def test_the_ledger_on_a_denial_carries_no_owner_text() -> None:
    """The reason is a closed-set slug. `as_public` is what leaves the node."""
    from topos.core.handlers import handle_control_plane_request

    out = await handle_control_plane_request(
        {
            "id": "contract-denial-privacy",
            "type": "query",
            "payload": {"scope_id": "", "query": "did Sarah Chen email me about the divorce"},
        }
    )
    blob = json.dumps(out["payload"]["narrowing"]).lower()
    for word in ("sarah", "chen", "divorce", "email"):
        assert word not in blob, f"the denial ledger carried {word!r} off the node"

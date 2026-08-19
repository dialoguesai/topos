"""The last two hops of the second text field, driven from the wire.

`retrieval_text` and `retrieval_parts` cross five seams to do their job, and until now
CI could see four of them. `test_retrieval_parts_reach_the_engine.py` proves the wire
payload reaches the ORCHESTRATOR's keyword arguments; `test_retrieval_needle_text.py`
proves the retrieval module's internals read the right variable by grepping its source.
Between those two lies the hop that actually delivers the value — `pipeline.py`
constructing the `RetrievalRequest` — and nothing executed it.

That gap is not hypothetical. Severing `needle_parts=needle_parts or None` in
`pipeline.py` (writing `needle_parts=None`) left 592 query tests green: the field
arrives at the orchestrator, the orchestrator drops it on the floor, and the per-part
rare gate silently reverts to one flattened needle set — which is the exact production
behaviour the field was added to end. A source grep cannot see it because the source
still says `needle_parts`; a kwargs recorder cannot see it because the drop happens
after the kwargs.

So this file runs a REAL `QueryPipelineOrchestrator` on in-memory adapters, sends a
real `type: "query"` message through `handle_control_plane_request`, and watches the
two places the value has to arrive:

* the `RetrievalRequest` the pipeline hands to retrieval — `needle_text` / `needle_parts`
* the call to `_needle_token_groups`, which is where the rare gate turns them into the
  token groups it vetoes on

No database is opened: the adapters are the in-memory fakes and the handler's
`get_db_connection` is stubbed, so the live node is never touched.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

import topos.core.handlers as hub
import topos.query.retrieval as retrieval_module
import topos.query.runtime as runtime
from topos.core.handlers import handle_control_plane_request

SCOPE = "health:read"

#: A two-section ask. One section names something rare; the other is ordinary. Flattened
#: into a single needle set they gate each other — that is what the per-part gate exists
#: to stop, and it only works if both parts survive the hop this file watches.
PARTS = ["threnody rewrite", "sleep quality"]

#: The owner's actual sentence, instructional words and all. `_residual_content_tokens`
#: would turn "give", "me", "report" into needles the rows must contain.
SENTENCE = "give me a report on:\n1) how did the threnody rewrite go\n2) how did I sleep"

#: What the client distilled the SUBJECT down to. This, not the sentence, is what the
#: rare gate may tokenise.
NEEDLES = "threnody rewrite sleep quality"


class _Watched:
    """What the two watched hops saw."""

    def __init__(self) -> None:
        self.requests: List[Any] = []
        self.group_calls: List[Tuple[str, List[str], List[Any]]] = []

    @property
    def request(self) -> Any:
        assert self.requests, "retrieval was never called — the turn died before the hop"
        return self.requests[0]

    @property
    def group_call(self) -> Tuple[str, List[str], List[Any]]:
        assert self.group_calls, "the rare gate never tokenised anything"
        return self.group_calls[0]


def _in_memory_adapters():
    """The fakes, so a real retrieval can run without the live node."""
    from topos.storage.adapters.factory import AdapterBundle
    from topos.storage.adapters.fakes import (
        InMemoryAuditLogStore,
        InMemoryCanonicalStore,
        InMemoryGraphEdgeStore,
        InMemoryQuerySessionStore,
        InMemorySignalFeatureStore,
        InMemoryVectorIndex,
    )

    return AdapterBundle(
        canonical=InMemoryCanonicalStore(),
        signal=InMemorySignalFeatureStore(),
        vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(),
        audit=InMemoryAuditLogStore(),
        query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )


@pytest.fixture
def watched(monkeypatch: pytest.MonkeyPatch) -> _Watched:
    """A real orchestrator with two spies that delegate rather than replace.

    Both spies call through, so what is asserted is the value the REAL code received —
    a spy that returned a canned answer would only prove the pipeline can talk to a mock.
    """
    from topos.query.pipeline import QueryPipelineOrchestrator

    seen = _Watched()
    orch = QueryPipelineOrchestrator(adapters=_in_memory_adapters())

    real_retrieve = orch._retrieval.retrieve

    def retrieve_spy(request: Any) -> Any:
        seen.requests.append(request)
        return real_retrieve(request)

    monkeypatch.setattr(orch._retrieval, "retrieve", retrieve_spy)

    real_groups = retrieval_module._needle_token_groups

    def groups_spy(needle_text: str, needle_parts: Optional[List[str]] = None) -> Any:
        out = real_groups(needle_text, needle_parts)
        seen.group_calls.append((needle_text, list(needle_parts or []), out))
        return out

    monkeypatch.setattr(retrieval_module, "_needle_token_groups", groups_spy)

    # `handle_query` resolves both of these at call time, so patching the module
    # attribute is what a real request would land on.
    monkeypatch.setattr(runtime, "get_query_orchestrator", lambda **_: orch)
    monkeypatch.setattr(hub, "get_db_connection", lambda *_a, **_k: None)
    return seen


def _payload(**extra: Any) -> Dict[str, Any]:
    return {
        "scope_id": SCOPE,
        "access_mode": "summary",
        "query": SENTENCE,
        "query_session_id": "qs-needles",
        **extra,
    }


async def _send(**extra: Any) -> Dict[str, Any]:
    out = await handle_control_plane_request(
        {"id": "req-needles", "type": "query", "payload": _payload(**extra)}
    )
    assert out.get("status") == "ok", out
    return out["payload"]


# --- the field hop ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieval_text_from_the_wire_arrives_as_the_needle_text(
    watched: _Watched,
) -> None:
    """The hop the verification found uncovered: payload -> `RetrievalRequest`.

    Proven by hand at the time; nothing in CI executed it. Blank the assignment in
    `pipeline.py` and this goes red, where a source grep for the identifier would not.
    """
    await _send(retrieval_text=NEEDLES)
    assert watched.request.needle_text == NEEDLES, (
        "the client distilled the subject and the pipeline dropped it — every "
        "instructional word in the request is a needle the rows must contain again"
    )


@pytest.mark.asyncio
async def test_the_sentence_still_travels_beside_the_needles(watched: _Watched) -> None:
    """The two-field separation, at the hop that could collapse it.

    `query_text` is what the planner parses a window from, what the embedding is taken
    of, and what the scope classifier reads. Overwriting it with the digest was measured
    on 2026-08-16 to lose the window and make the classifier abstain.
    """
    await _send(retrieval_text=NEEDLES)
    assert watched.request.query_text == SENTENCE


@pytest.mark.asyncio
async def test_a_request_without_the_field_leaves_the_needles_on_the_sentence(
    watched: _Watched,
) -> None:
    """Every client that never sends it must be byte-identical to before."""
    await _send()
    assert watched.request.needle_text is None


@pytest.mark.asyncio
async def test_a_blank_retrieval_text_is_not_an_empty_needle_set(watched: _Watched) -> None:
    """Blank must mean absent. Kept as `""`, it would blank the needles entirely."""
    await _send(retrieval_text="   ")
    assert watched.request.needle_text is None


# --- the gate hop -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_rare_gate_tokenises_the_needles_not_the_sentence(
    watched: _Watched,
) -> None:
    """One hop further than the request: what the gate was actually handed.

    `retrieve` falls back to `query_text` when `needle_text` is absent, so asserting on
    the request alone leaves a pipeline that drops the field looking identical to one
    that never got it. This reads the value at the gate itself.
    """
    await _send(retrieval_text=NEEDLES)
    needle_text, _, _ = watched.group_call
    assert needle_text == NEEDLES
    assert "give" not in needle_text and "report" not in needle_text, (
        "the gate is tokenising the instruction; those words become needles the rows "
        "must contain and the lane empties on a request that merely describes itself"
    )


@pytest.mark.asyncio
async def test_parts_from_the_wire_become_one_token_group_per_part(
    watched: _Watched,
) -> None:
    """The hop severing which left 592 query tests green.

    `needle_parts=needle_parts or None` in `pipeline.py` is the entire delivery. Write
    `None` there and the request still carries `needle_text`, retrieval still runs, every
    existing test still passes — and the gate silently reverts to one flattened group,
    which is the production behaviour multi-needle exists to end.
    """
    await _send(retrieval_text=NEEDLES, retrieval_parts=PARTS)
    assert watched.request.needle_parts == PARTS, (
        "the sections reached the orchestrator and the pipeline dropped them before "
        "retrieval — the per-part gate is off and one section's needles can empty another"
    )
    _, parts_at_gate, groups = watched.group_call
    assert parts_at_gate == PARTS
    assert len(groups) == len(PARTS), (
        f"the gate built {len(groups)} token group(s) for {len(PARTS)} sections; a "
        "single flattened group is the companion-veto bug"
    )


@pytest.mark.asyncio
async def test_the_groups_are_per_part_not_the_same_tokens_twice(
    watched: _Watched,
) -> None:
    """Distinct groups, or `len(groups) == 2` is satisfied by copying one group twice."""
    await _send(retrieval_text=NEEDLES, retrieval_parts=PARTS)
    _, _, groups = watched.group_call
    as_sets = [frozenset(g) for g in groups]
    assert len(set(as_sets)) == len(PARTS), f"the groups are not distinct: {groups}"
    assert any("threnody" in g for g in as_sets)
    assert any("sleep" in g for g in as_sets)


@pytest.mark.asyncio
async def test_a_request_without_parts_gates_on_one_whole_request_group(
    watched: _Watched,
) -> None:
    """The unchanged path: no parts is one group, exactly as before multi-needle."""
    await _send(retrieval_text=NEEDLES)
    assert watched.request.needle_parts is None
    _, parts_at_gate, groups = watched.group_call
    assert parts_at_gate == []
    assert len(groups) == 1

"""The per-part rare gate, tested from the WIRE rather than from its own helper.

The gate at `retrieval.py::_rrf_fuse_summary_lists` is correct and `_needle_token_groups`
has its own tests. Neither fact says anything about whether a live turn can reach them,
and for the whole of this field's existence it could not: `retrieval_parts` had zero
senders, so every multi-part request arrived as one flattened needle set and the
companion veto was off in production on exactly the requests it was built for.

So these drive `handle_control_plane_request` — the function the control plane's
`send_request` actually lands on — with a `type: "query"` message, and assert on what
comes out the far side of the handler. A test that called `handle_query` directly would
skip the dispatcher; one that called the pipeline would skip the handler; and either
would have passed for the whole period the field was unreachable.

The orchestrator is stubbed, because the alternative is the live database and the public
lane must never open it. That stub is why `test_the_recorded_call_binds_to_the_real_pipeline`
exists: a stub accepts any keyword, so the recorded call is bound against the REAL
`QueryPipelineOrchestrator._execute_turn` signature — the turn body, not `execute`, which is
a `**kwargs` ledger wrapper that would take anything. Without it this file would prove only
that the handler can talk to a mock.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, Optional

import pytest

import topos.core.handlers as hub
import topos.query.runtime as runtime
from topos.core.handlers import handle_control_plane_request

SCOPE = "health:read"

#: A two-section ask: one section names something the store may not have, the other is
#: ordinary. Flattened into one needle set these gate each other; that is the bug.
PARTS = ["threnody rewrite", "sleep"]


class _RecordingOrchestrator:
    """Records the call instead of running it. Never touches a database."""

    def __init__(self) -> None:
        self.kwargs: Optional[Dict[str, Any]] = None

    async def execute(self, **kwargs: Any) -> Dict[str, Any]:
        self.kwargs = kwargs
        return {
            "turn_outcome": "live_query",
            "public_result": {"summaries": []},
            "session_id": kwargs.get("query_session_id"),
        }


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> _RecordingOrchestrator:
    recorder = _RecordingOrchestrator()
    # `handle_query` imports `get_query_orchestrator` at call time, so patching the
    # module attribute is what a real request would resolve.
    monkeypatch.setattr(runtime, "get_query_orchestrator", lambda **_: recorder)
    monkeypatch.setattr(hub, "get_db_connection", lambda *_a, **_k: None)
    return recorder


async def _send(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = await handle_control_plane_request({"id": "req-parts", "type": "query", "payload": payload})
    assert out.get("status") == "ok", out
    return out


def _payload(**extra: Any) -> Dict[str, Any]:
    return {
        "scope_id": SCOPE,
        "access_mode": "summary",
        "intent": "sleep threnody",
        "query": "1) how did the threnody rewrite go\n2) how did I sleep",
        "retrieval_text": "threnody rewrite sleep",
        "query_session_id": "qs-parts",
        **extra,
    }


@pytest.mark.asyncio
async def test_parts_sent_on_the_wire_reach_the_query_orchestrator(wire: _RecordingOrchestrator) -> None:
    """The wire this whole item exists to connect."""
    await _send(_payload(retrieval_parts=PARTS))
    assert wire.kwargs is not None
    assert wire.kwargs.get("retrieval_parts") == PARTS, (
        "a multi-part request reached the node with its sections and the handler did "
        "not forward them — the per-part gate cannot fire and the companion veto is "
        "back to emptying one section's lane with another section's needles"
    )


@pytest.mark.asyncio
async def test_the_recorded_call_binds_to_the_real_pipeline(wire: _RecordingOrchestrator) -> None:
    """Reachability, not just plumbing: the stub took the call, would the engine?

    A recorder accepts every keyword. Binding the same call against the real turn body
    is what makes the test above evidence about production — rename `retrieval_parts` on
    the pipeline and this goes red where the stub would happily keep passing.

    Bound against `_execute_turn` rather than `execute`, because `execute` is a
    `**kwargs` ledger wrapper: it accepts anything and would make this assertion
    vacuous. `_execute_turn` is where an unknown keyword actually raises.
    """
    await _send(_payload(retrieval_parts=PARTS))
    assert wire.kwargs is not None
    signature = inspect.signature(runtime.QueryPipelineOrchestrator._execute_turn)
    assert "kwargs" not in signature.parameters, (
        "the turn body grew a **kwargs catch-all; binding against it no longer proves "
        "the engine would accept these keywords"
    )
    bound = signature.bind_partial(object(), **wire.kwargs)
    assert bound.arguments.get("retrieval_parts") == PARTS


@pytest.mark.asyncio
async def test_the_parts_do_not_become_the_query(wire: _RecordingOrchestrator) -> None:
    """The two-field separation, asserted at the seam that could break it.

    Parts are needles. `query` stays the owner's sentence, because the planner's time
    window, the embedding and the Horos classifier all read it — handing them a keyword
    digest was measured on 2026-08-16 to lose the window and make the classifier abstain.
    """
    payload = _payload(retrieval_parts=PARTS)
    await _send(payload)
    assert wire.kwargs is not None
    assert wire.kwargs.get("query_text") == payload["query"]
    assert wire.kwargs.get("retrieval_text") == payload["retrieval_text"]


@pytest.mark.asyncio
async def test_a_request_without_parts_is_unchanged(wire: _RecordingOrchestrator) -> None:
    """Every client that never sends the field must be byte-identical to before."""
    await _send(_payload())
    assert wire.kwargs is not None
    assert wire.kwargs.get("retrieval_parts") is None


@pytest.mark.asyncio
async def test_blank_parts_are_dropped_rather_than_gating_on_nothing(
    wire: _RecordingOrchestrator,
) -> None:
    """An empty entry is not a section with no needles — it is a section that got lost.

    Kept, it would be a part that can never be vetoed, which quietly turns the whole-
    request veto off for every other section.
    """
    await _send(_payload(retrieval_parts=["  ", "sleep", "", None]))
    assert wire.kwargs is not None
    assert wire.kwargs.get("retrieval_parts") == ["sleep"]


@pytest.mark.asyncio
async def test_an_all_blank_list_is_the_same_as_no_parts(wire: _RecordingOrchestrator) -> None:
    await _send(_payload(retrieval_parts=["", "   "]))
    assert wire.kwargs is not None
    assert wire.kwargs.get("retrieval_parts") is None

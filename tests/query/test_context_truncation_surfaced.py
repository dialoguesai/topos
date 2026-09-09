"""SUITE-CONTEXT-TRUNCATION — an answer written from a cut packet says it was cut.

protects: SYS-query I1. `build_inference_context_packet` bounds the evidence it hands the model
and, when it has to cut, says so with `context_truncated`. That value has been computed since
2026-08-25 and dropped at every exit of `run_query_inference`, so an answer written from a packet
whose tail was removed was indistinguishable from one written from the whole of it — no field, no
ledger entry, nothing. A qualifier past the budget is exactly the kind of thing that changes an
answer, and the reader had no way to know it was missing.

Backlog option H-16. The flag now rides every return, including the failures: a deferred or errored
turn that was ALSO truncated is worth knowing about, and a flag that appears only on the happy path
is one a consumer learns to ignore.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from topos.query.inference import (
    DEFAULT_MAX_CONTEXT_CHARS,
    build_inference_context_packet,
    run_query_inference,
)

pytestmark = [pytest.mark.check("C-quality-context-truncation-surfaced")]


def _overflowing_packet(qualifier: str = "but only for the pilot cohort") -> Dict[str, Any]:
    """A packet whose LAST item carries the qualifier, past any sane budget. The shape that makes
    truncation matter: everything the model needs to answer confidently is near the front, and the
    thing that would have changed the answer is at the tail."""
    return {
        "summaries": [
            {"record_id": f"r{i}", "topic": f"project {i}",
             "summary_text": "shipped the installer work and reviewed the release notes " * 12}
            for i in range(60)
        ]
        + [{"record_id": "tail", "topic": "the caveat", "summary_text": qualifier}],
    }


def _small_packet() -> Dict[str, Any]:
    return {"summaries": [{"record_id": "r1", "topic": "installer", "summary_text": "shipped it"}]}


class TestTheBuilderStillComputesIt:
    def test_a_big_packet_is_cut_and_says_so(self) -> None:
        bounded = build_inference_context_packet(_overflowing_packet(), max_chars=2_000)
        assert bounded["context_truncated"] is True
        assert len(bounded["context"]) <= 2_000

    def test_a_small_packet_is_not(self) -> None:
        bounded = build_inference_context_packet(_small_packet(), max_chars=DEFAULT_MAX_CONTEXT_CHARS)
        assert bounded["context_truncated"] is False

    def test_the_qualifier_really_is_what_gets_lost(self) -> None:
        """Names the harm rather than asserting a boolean: the tail item is the one cut away."""
        bounded = build_inference_context_packet(_overflowing_packet(), max_chars=2_000)
        assert "but only for the pilot cohort" not in bounded["context"]


class _StubEngine:
    """A completed inference, so the test observes the plumbing rather than a model."""

    def __init__(self, status: str = "completed", output: Dict[str, Any] | None = None) -> None:
        self._status, self._output = status, output if output is not None else {"answer": "yes", "confidence": 0.9}

    def run(self, task, **kwargs):  # noqa: ANN001, ANN003
        status, output = self._status, self._output

        class _R:
            pass

        r = _R()
        r.status = status
        r.output = output
        r.error = "boom" if status not in ("completed", "deferred") else None
        return r


@pytest.fixture
def stub(monkeypatch):
    def _install(status: str = "completed", output: Dict[str, Any] | None = None):
        engine = _StubEngine(status, output)
        monkeypatch.setattr("topos.query.inference.get_engine_client_or_local", lambda *a, **k: engine)
        return engine
    return _install


class TestItReachesTheCaller:
    def test_a_truncated_answer_carries_the_flag(self, stub) -> None:
        stub()
        out = run_query_inference(
            query_text="did the pilot ship", context_packet=_overflowing_packet(),
            scope_id="work_context:read", max_chars=2_000,
        )
        assert out["context_truncated"] is True
        assert out["answer"] == "yes"

    def test_an_untruncated_answer_says_so_too(self, stub) -> None:
        """Present on both sides, so a consumer can trust its absence to mean something."""
        stub()
        out = run_query_inference(
            query_text="did the pilot ship", context_packet=_small_packet(),
            scope_id="work_context:read",
        )
        assert out["context_truncated"] is False

    @pytest.mark.parametrize("status", ["deferred", "failed"])
    def test_a_turn_that_did_not_complete_still_reports_truncation(self, stub, status: str) -> None:
        stub(status, {} if status == "deferred" else None)
        out = run_query_inference(
            query_text="did the pilot ship", context_packet=_overflowing_packet(),
            scope_id="work_context:read", max_chars=2_000,
        )
        assert out["context_truncated"] is True
        assert out["answer"] == "unknown"


class TestItIsNotAForbiddenField:
    def test_the_public_result_validator_admits_it(self) -> None:
        """The pipeline spreads this dict straight onto `public.payload`, which is validated before
        serialization. A field that cannot survive that check would be dropped at the last step."""
        from topos.query.session_utils import validate_public_result

        validate_public_result({"answer": "yes", "confidence": 0.9, "context_truncated": True})

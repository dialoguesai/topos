"""GT-EN-P3-S1-07: availability:read inference answers are free/busy only.

Live 2026-09-02 (deployed stack): "Who is on the invite list?" on
availability:read left the engine with a contact's full name in
public_result.answer at confidence 1. The game layer's who-guard had refused
(answer "unknown"), but its refusal carries answer_type "list", and the
pipeline's post-game-layer lanes (facts-direct, LLM inference) treated
anything that is not "band"/"facts" as theirs to overwrite. These tests pin
the contract at both layers: the lanes never run for availability inference
turns, and the closed-set egress scrub un-does any future lane that tries.
"""

import json

import pytest

from topos.query.game_layer import (
    DefaultGameLayer,
    enforce_availability_inference_contract,
)
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.query.session import TurnOutcome

from helpers import availability_manifest, make_adapter_bundle

pytestmark = pytest.mark.gap

_LEAKED_NAME = "Firstname Lastname"


@pytest.mark.asyncio
async def test_availability_who_ask_never_reaches_inference_lane(monkeypatch) -> None:
    calls = []

    def _leaking_inference(**kwargs):
        calls.append(kwargs)
        return {"answer": _LEAKED_NAME, "confidence": 1.0}

    monkeypatch.setattr(
        "topos.query.pipeline.run_query_inference", _leaking_inference
    )
    bundle = make_adapter_bundle()
    orch = QueryPipelineOrchestrator(adapters=bundle)
    turn = await orch.execute(
        query_text="Who is on the investor call invite list?",
        scope_id="availability:read",
        access_mode="inference",
        manifest=availability_manifest(),
        query_session_id="qs_avail_contract",
    )
    assert turn["turn_outcome"] == TurnOutcome.LIVE_QUERY.value
    assert calls == []
    public = turn["public_result"]
    assert public["answer"] == "unknown"
    assert public.get("items") in (None, [])
    assert _LEAKED_NAME not in json.dumps(public)


def test_scrub_reverts_free_text_answer_to_unknown() -> None:
    # The exact shape that crossed the wire live: the who-guard's refusal with
    # answer/confidence overwritten by the LLM lane.
    leaked = {
        "access_mode": "inference",
        "scope_id": "availability:read",
        "answer_type": "list",
        "items": [],
        "answer": _LEAKED_NAME,
        "confidence": 1.0,
    }
    out = enforce_availability_inference_contract(leaked)
    assert out["answer"] == "unknown"
    assert out["confidence"] == 0.0
    assert out["redaction"] == "availability_free_busy_only"
    assert _LEAKED_NAME not in json.dumps(out)


def test_scrub_drops_facts_lane_shape() -> None:
    # A facts-direct lane's payload: names in items, prose in answer, an
    # answer_type outside the availability vocabulary.
    leaked = {
        "access_mode": "inference",
        "scope_id": "availability:read",
        "answer_type": "facts",
        "answer": f"{_LEAKED_NAME} and 2 others",
        "facts": [{"fact": "meeting title here"}],
        "items": [_LEAKED_NAME],
        "facts_direct": True,
    }
    out = enforce_availability_inference_contract(leaked)
    assert out["answer_type"] == "band"
    assert out["answer"] == "unknown"
    assert out["items"] == []
    assert "facts" not in out and "facts_direct" not in out
    assert _LEAKED_NAME not in json.dumps(out)


def test_scrub_passes_band_answer_untouched() -> None:
    band = {
        "access_mode": "inference",
        "scope_id": "availability:read",
        "answer_type": "band",
        "band": "negotiable_overlap",
        "answer": "conditional",
        "confidence": 0.8,
    }
    assert enforce_availability_inference_contract(dict(band)) == band


def test_game_layer_who_guard_still_refuses() -> None:
    result = DefaultGameLayer().apply(
        context_packet={"scores": [{"entity_text": _LEAKED_NAME}]},
        access_mode="inference",
        scope_id="availability:read",
        query_text="Who is on the investor call invite list?",
    )
    assert result.payload["answer"] == "unknown"
    assert result.payload["items"] == []
    assert _LEAKED_NAME not in json.dumps(result.payload)

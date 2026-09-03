"""The hosted-binding floor must reach `public_result`, not just the packet.

protects: when the node reports that a hosted model binding floored the
turn, the owner's own text does not leave anyway.

Measured on an owner snapshot 2026-09-03 — the defect this closes. With a
hosted binding the turn reported, truthfully and prominently::

    packet_resolution        = "scores_only"
    packet_resolution_reason = "hosted_binding"

and `public_result.summaries` still carried **60,151 characters** of the
owner's conversation text — byte-identical to the same query on a local
binding. The floor was doing exactly what it was written to do: it gates the
INFERENCE PACKET, the thing the engine's own model reads. But summary text
does not stop at the packet. It rides `public_result` to whatever model
writes the answer, and on a hosted pack that model is the hosted one. So the
protection read as active on the turn that carried the content out.

The fix keeps the shape and withholds the words: identity, provenance,
relevance and dates survive (a caller can still see WHAT matched and rank
it), the prose does not, and the turn declares it with its own narrowing
reason — a silently emptied summary is indistinguishable from a corpus that
held nothing, which is the false-absence bug this plan exists to end.

**Lane note.** The end-to-end assertion lives in the owner-snapshot lane
(`test_hosted_floor_live.py`, `qq_eval`), not here: `_bundle_is_global_db`
disables the vector and cluster layers on a seeded non-global database, so a
seeded end-to-end turn returns `store_empty` and would assert nothing at all.
What is hermetic — and what actually carries the guarantee — is the cap
itself, exercised here against payload shapes taken from real turns.
"""

from __future__ import annotations

import pytest

from topos.query.narrowing import NarrowingLedger
from topos.query.pipeline import (
    _HOSTED_FLOOR_ITEM_KEYS,
    withhold_text_for_hosted_binding,
)

OWNER_PROSE = "the quarterly rollout slipped because staging was down"


def _payload():
    """The shape a real summary turn produces, trimmed to what matters here."""
    return {
        "access_mode": "summary",
        "answer_type": "summary",
        "scope_id": "ai_conversations:read",
        "summaries": [
            {
                "topic": "rollout",
                "summary_text": OWNER_PROSE,
                "record_id": "rec-1",
                "event_at": "2026-08-01T10:00:00",
                "relevance_score": 0.82,
                "retrieval_source": "vector",
            },
            {
                "topic": "staging",
                "summary_text": f"{OWNER_PROSE} again",
                "record_id": "rec-2",
                "relevance_score": 0.61,
                "retrieval_source": "derived:fact",
            },
        ],
        "scores": [{"label": OWNER_PROSE, "value": 0.4, "record_id": "rec-3"}],
        "facts": [{"predicate": "works_on", "text": OWNER_PROSE, "confidence": 0.9}],
    }


def test_prose_is_removed_from_every_declared_container():
    payload = _payload()
    removed = withhold_text_for_hosted_binding(payload)
    assert removed > 0
    assert OWNER_PROSE not in str(payload), "prose survived the cap"


def test_identity_rank_and_dates_survive():
    """The shape stays legible: a caller can see THAT something matched and
    how strongly. Emptying the lists would read as an empty corpus."""
    payload = _payload()
    withhold_text_for_hosted_binding(payload)
    summaries = payload["summaries"]
    assert len(summaries) == 2, "the cap dropped items instead of text"
    assert summaries[0]["record_id"] == "rec-1"
    assert summaries[0]["relevance_score"] == 0.82
    assert summaries[0]["event_at"] == "2026-08-01T10:00:00"
    assert summaries[0]["retrieval_source"] == "vector"


def test_the_withholding_is_declared_not_silent():
    payload = _payload()
    ledger = NarrowingLedger()
    withhold_text_for_hosted_binding(payload, ledger=ledger)
    entries = ledger.as_public()["ledger"]
    hit = [e for e in entries if e["reason"] == "hosted_binding_text_withheld"]
    assert hit, f"nothing declared the withholding: {entries}"
    assert hit[0]["stage"] == "disclosure"
    assert hit[0]["action"] == "excluded"
    assert hit[0]["dropped"] > 0


def test_a_free_text_answer_is_withheld_but_a_band_is_not():
    """A band or yes/no answer is a shape, not content — withholding it would
    destroy the answer while protecting nothing."""
    prose = {"answer_type": "summary", "answer": OWNER_PROSE}
    withhold_text_for_hosted_binding(prose)
    assert prose["answer"] == ""

    band = {"answer_type": "band", "answer": "mostly free"}
    withhold_text_for_hosted_binding(band)
    assert band["answer"] == "mostly free"


def test_every_declared_container_is_actually_walked():
    """Non-vacuity: each key in `_HOSTED_FLOOR_ITEM_KEYS` must be reachable by
    the cap. A container listed but not walked is the review passing while the
    policy does not apply — the `_GRANTEE_DICT_ARTIFACTS` lesson."""
    for container in _HOSTED_FLOOR_ITEM_KEYS:
        payload = {container: [{"summary_text": OWNER_PROSE, "record_id": "r"}]}
        removed = withhold_text_for_hosted_binding(payload)
        assert removed == 1, f"{container} declared but not walked"


def test_no_ledger_and_no_items_are_both_safe():
    assert withhold_text_for_hosted_binding({}) == 0
    assert withhold_text_for_hosted_binding({"summaries": None}) == 0
    assert withhold_text_for_hosted_binding({"summaries": ["not-a-dict"]}) == 0


def test_the_cap_is_wired_to_the_hosted_reason_only():
    """Source pin: the call site must fire on scores_only + hosted_binding and
    nothing else. A cap that also fired on `non_owner_floor` would blank a
    grantee turn the grantee scrubs already shape correctly, and one that
    fired on `active` would blank the owner's own answers."""
    import inspect

    from topos.query.pipeline import QueryPipelineOrchestrator

    src = inspect.getsource(QueryPipelineOrchestrator._execute_turn)
    assert 'if _pr["effective"] == "scores_only" and _pr["reason"] == "hosted_binding":' in src
    assert "withhold_text_for_hosted_binding(public_dict, ledger=ledger)" in src

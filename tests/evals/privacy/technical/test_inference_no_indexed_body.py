"""§F.8 — record text behind a semantic hit or topic cluster must not reach inference.

protects: an inference-mode turn carries the owner's messages, journal and
AI-chat text as signal (which record, how close, from where, when), never as
words a model can repeat or a caller can read.

History. 016002a (2026-09-03) put `search_text` on every semantic hit: the
indexed body, up to 2,000 characters of the record verbatim, which the SUMMARY
lane needs. The inference strip of the day named only the preview keys, so the
body reached the model. 860efe5f (shipped in 1.4.0) projects inference hits by
allow-list — `INFERENCE_SEMANTIC_FIELDS`, in retrieval and again in the context
builder — and the builder no longer copies unrecognised packet keys. What 1.4.0
still let through, and what this file pins:

  * the disclosure stage passed a hit's record text through in inference mode,
    so its output (read by the game layer, the minimizer's model and the
    context builder) was safe only because retrieval had projected first. The
    B4 battery's inference probe reads exactly this output and reported F8;
  * inference clusters kept `centroid_preview`, the first 120 characters of the
    cluster's most central member — a quote, not a label;
  * the grantee scrub (`_GRANTEE_TEXT_KEYS`) did not name `centroid_preview`.

One test per layer, each written against that layer alone, so neutering any one
of them turns its own test red while the others stay green:
retrieval, disclosure, the context builder, and the grantee scrub.

Why the end-to-end turn is an OWNER turn. Since 1.4.0 the pipeline refuses
non-owner inference on every scope but availability, before retrieval, and
availability never reaches the model (tests/query/test_beta_permission_boundaries.py).
A grantee leak test would run with no hit and no model call: vacuous. The last
test here asserts that refusal on this fixture, so the day the gate widens it
fails and says what to write instead.

Why the lanes are real: no vector index sits behind an in-memory or seeded
store, so without a fake the semantic lane is empty and every assertion below
passes vacuously. The vector service and the cluster loader are faked at the
seams retrieval calls, and every leak assertion is preceded by one showing the
hit's id reached the model.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Dict, List

import pytest

from topos.features.signal import service as signal_service
from topos.features.signal.derived_index import DERIVED_SOURCE_ID
from topos.principal import OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.query import retrieval as R
from topos.query.disclosure import DisclosureFilterPipeline
from topos.query.inference import build_inference_context_packet
from topos.query.manifest import ScopeResolutionManifest
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.query.types import RetrievalBundle, RetrievalRequest
from topos.storage.adapters.factory import AdapterBundle
from topos.storage.adapters.fakes import (
    InMemoryAuditLogStore,
    InMemoryCanonicalStore,
    InMemoryGraphEdgeStore,
    InMemoryQuerySessionStore,
    InMemorySignalFeatureStore,
    InMemoryVectorIndex,
)

from tests.evals.privacy.common.probe import response_discloses

pytestmark = [pytest.mark.private]

BODY_CANARY = "zx-canary-indexed-body-5528"
CLUSTER_CANARY = "zx-canary-centroid-7719"

# The shape 016002a was written for: an entry longer than its preview, with the
# telling part past character 200. The canary sits only in the body, so this
# pins `search_text` itself rather than the preview keys stripped long before.
_OPENING = "Long week. Mostly logistics for the move, and a lot of back and forth with the landlord about the deposit. "
BODY = (_OPENING * 2) + f"The spare key is under the blue pot and the alarm code is {BODY_CANARY}."
PREVIEW = BODY[:200]

QUERY = "did I mention the spare key"
SCOPE = "messages:read"

# What an inference hit may carry: which record, how close, from where, when.
_HIT_SIGNAL = {
    "record_id": "msg-42",
    "similarity": 0.83,
    "source_id": "imessage",
    "signal_dimension": "memory",
    "event_at": "2026-09-01T18:30:00Z",
    "record_type": "conversation_message",
}
_HIT = {**_HIT_SIGNAL, "text_preview": PREVIEW, "search_text": BODY}

_CLUSTER = {
    "cluster_id": "tc-move",
    "label": "Moving house",
    "dimension": "memory",
    "primary_dimension": "memory",
    "member_count": 12,
    "label_terms": ["move", "landlord", "deposit"],
    "centroid_preview": f"ok the alarm code is {CLUSTER_CANARY}, don't lose it",
    "relevance_score": 0.41,
    "object_type": "top_topics",
}
_CLUSTER_SIGNAL = {k: v for k, v in _CLUSTER.items() if k != "centroid_preview"}

# Every key that carries a record's own words, on either artifact. The first
# seven are the inference probe's own list (control-plane
# scripts/permissions_beta/boundary/inference_probe.py).
_RECORD_TEXT_KEYS = frozenset(
    {"text_preview", "search_text", "content", "text", "summary_text", "snippet", "body",
     "content_preview", "title", "centroid_preview"}
)


class _VectorServiceWithOneHit:
    """The vector service's seam, holding one raw message chunk."""

    def __init__(self) -> None:
        self.calls = 0

    def search_vectors(self, **kwargs: Any) -> Dict[str, Any]:
        if kwargs.get("source_id") == DERIVED_SOURCE_ID:
            return {"items": [], "total": 0}
        self.calls += 1
        return {"items": [dict(_HIT)], "total": 1}


class _ParrotModel:
    """Answers with the context it was given, verbatim, and records it.

    The worst case for a leak, and deterministic: a canary absent from the
    answer means the context never carried it, not that a model chose not to
    say it."""

    def __init__(self) -> None:
        self.contexts: List[str] = []

    def run(self, task: Any, **kwargs: Any) -> Any:
        context = str(task.input.get("context") or "")
        self.contexts.append(context)

        class _Result:
            status = "completed"
            output = {"answer": context, "confidence": 0.9}
            error = None

        return _Result()


def _bundle() -> AdapterBundle:
    return AdapterBundle(
        canonical=InMemoryCanonicalStore(),
        signal=InMemorySignalFeatureStore(),
        vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(),
        audit=InMemoryAuditLogStore(),
        query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )


def _manifest() -> ScopeResolutionManifest:
    return ScopeResolutionManifest(
        scope_id=SCOPE,
        primary_dimensions=["memory"],
        canonical_tables=["conversation_messages"],
        access_mode_ceiling="inference",
    )


def _hit_with_every_text_key() -> Dict[str, Any]:
    return {**_HIT_SIGNAL, **{key: f"{key}: {BODY_CANARY}" for key in _RECORD_TEXT_KEYS}}


@pytest.fixture
def vector_hit(monkeypatch) -> _VectorServiceWithOneHit:
    service = _VectorServiceWithOneHit()
    monkeypatch.setattr(signal_service, "get_signal_service", lambda *a, **k: service)
    # The loader under the black-hole wrapper, so the cluster policy still runs.
    monkeypatch.setattr(R, "_load_ranked_clusters_unfiltered", lambda *a, **k: [dict(_CLUSTER)])
    return service


def test_the_hit_builder_carries_the_body_so_the_strips_are_load_bearing(vector_hit):
    """Non-vacuity for everything below: retrieval really does hold the body.

    The summary lane needs it (016002a), so removing it where the hit is built
    is not the fix — which is why every inference layer has to leave it out."""
    hits, _ = R._semantic_hits(QUERY)
    assert hits and BODY_CANARY in str(hits[0].get("search_text"))
    assert BODY_CANARY not in PREVIEW, "fixture: the canary must sit past the preview"


def test_retrieval_inference_packet_carries_no_record_text(vector_hit):
    """Layer 1, retrieval alone: the packet it hands the disclosure stage."""
    result = R.DefaultSignalRetrievalAdapter(_bundle()).retrieve(
        RetrievalRequest(
            manifest=_manifest(),
            access_mode="inference",
            query_text=QUERY,
            disclosure_tier="default_disclosure",
        )
    )
    packet = result.context_packet
    blob = json.dumps(packet, default=str)
    assert BODY_CANARY not in blob, "the indexed body reached the inference packet"
    assert CLUSTER_CANARY not in blob, "a cluster's centroid text reached the inference packet"

    # The signal survives: the hit and the cluster are still there to rank on.
    assert packet.get("semantic_hits") == [_HIT_SIGNAL], packet
    assert packet.get("topic_clusters") == [_CLUSTER_SIGNAL], packet


@pytest.mark.parametrize("tier", ["owner_raw", "default_disclosure"])
def test_disclosure_keeps_only_signal_on_inference_hits_and_clusters(tier):
    """Layer 2, the disclosure stage alone, on a packet retrieval did NOT build.

    This is the output the inference probe reads, and the one the game layer,
    the minimizer and the context builder all read after it — so it must hold
    no record text whatever retrieval did. The bare string is a hit that is
    nothing but text; it has no signal to keep."""
    cluster = {**_CLUSTER, "centroid_preview": f"centroid_preview: {BODY_CANARY}"}
    packet = {
        "scope_id": SCOPE,
        "access_mode": "inference",
        "semantic_hits": [_hit_with_every_text_key(), f"bare: {BODY_CANARY}"],
        "topic_clusters": [cluster],
    }
    filtered = DisclosureFilterPipeline().apply(
        RetrievalBundle(context_packet=packet), access_mode="inference", disclosure_tier=tier
    )
    out = filtered.context_packet
    assert BODY_CANARY not in json.dumps(out, default=str), out
    assert out["semantic_hits"] == [_HIT_SIGNAL]
    assert out["topic_clusters"] == [_CLUSTER_SIGNAL]
    assert "inference_mode_strip_evidence" in filtered.filters_applied


def test_context_builder_copies_only_the_signal():
    """Layer 3, the context builder alone: whatever keys a hit or cluster
    arrives with, and whatever top-level keys the packet grows, the model is
    handed a hit's id, score, provenance and time and a cluster's label and
    score — nothing that could be record text."""
    hit = _hit_with_every_text_key()
    hit.update({key: f"{key}: {BODY_CANARY}" for key in ("person_name", "predicate", "a_text_key_added_later")})
    cluster = {**_CLUSTER, "centroid_preview": f"centroid_preview: {BODY_CANARY}"}
    bounded = build_inference_context_packet(
        {
            "semantic_hits": [hit, {"similarity": None}],
            "topic_clusters": [cluster],
            "rows": [{"content": BODY_CANARY}],
            "a_packet_key_added_later": {"body": BODY_CANARY},
        }
    )
    assert BODY_CANARY not in bounded["context"], bounded["context"]
    assert json.loads(bounded["context"]) == {
        "semantic_hits": [_HIT_SIGNAL],
        "topic_clusters": [{"label": "Moving house", "relevance_score": 0.41}],
    }


def test_grantee_scrub_redacts_the_cluster_quote_and_the_indexed_body():
    """Layer 4, the grantee text scrub: PII in a cluster's quote or a hit's
    indexed body is redacted for a grantee in every mode, like any other text
    key. Summary mode is where both keys legitimately reach this stage."""
    email, phone = "renter@example.com", "+1 (212) 555-0142"
    packet = {
        "topic_clusters": [{**_CLUSTER, "centroid_preview": f"write {email} or call {phone}"}],
        "semantic_hits": [{**_HIT, "search_text": f"forwarding to {email}, cell {phone}"}],
    }
    out = DisclosureFilterPipeline().apply(
        RetrievalBundle(context_packet=packet), access_mode="summary", disclosure_tier="default_disclosure"
    ).context_packet
    blob = json.dumps(out)
    assert email not in blob and "555-0142" not in blob, out
    assert out["topic_clusters"][0]["centroid_preview"] == "write [REDACTED_EMAIL] or call [REDACTED_PHONE]"
    assert out["semantic_hits"][0]["search_text"] == "forwarding to [REDACTED_EMAIL], cell [REDACTED_PHONE]"


def _run_turn(*, principal: Principal, is_grantee_request: bool, model: _ParrotModel, monkeypatch) -> Dict[str, Any]:
    monkeypatch.setattr("topos.query.inference.get_engine_client_or_local", lambda *a, **k: model)
    token = set_principal(principal)
    try:
        return asyncio.run(
            QueryPipelineOrchestrator(adapters=_bundle()).execute(
                query_text=QUERY,
                scope_id=SCOPE,
                access_mode="inference",
                manifest=_manifest(),
                query_session_id=f"inf-body-{uuid.uuid4().hex[:8]}",
                requester_id="owner-9" if not is_grantee_request else "grantee-x",
                owner_id="owner-9",
                is_grantee_request=is_grantee_request,
            )
        )
    finally:
        reset_principal(token)


def test_owner_inference_turn_hands_the_model_the_hit_but_not_its_text(vector_hit, monkeypatch):
    """End to end, every layer in place: the only turn that reaches the model."""
    model = _ParrotModel()
    resp = _run_turn(principal=Principal(OWNER_APP, "uds"), is_grantee_request=False, model=model,
                     monkeypatch=monkeypatch)
    assert resp.get("turn_outcome") == "live_query", resp

    # Non-vacuous: the lane produced the hit, the model was asked once, and the
    # hit's id and the cluster's label reached both the model and the answer —
    # so an absent canary means the text was withheld, not that nothing arrived.
    assert vector_hit.calls >= 1
    assert len(model.contexts) == 1, resp
    context = model.contexts[0]
    assert "msg-42" in context and "Moving house" in context, context
    assert "msg-42" in json.dumps(resp["public_result"], default=str)

    assert BODY_CANARY not in context, "the model was handed the indexed body"
    assert CLUSTER_CANARY not in context, "the model was handed a cluster's centroid text"
    leaked = response_discloses(resp, [BODY_CANARY, CLUSTER_CANARY])
    assert not leaked, f"an inference answer quoted record text: {leaked}"
    # Nor anywhere else on the response: the audit and its decision record leave the node.
    assert BODY_CANARY not in json.dumps(resp, default=str)
    assert CLUSTER_CANARY not in json.dumps(resp, default=str)


def test_grantee_inference_on_this_scope_is_refused_before_retrieval(vector_hit, monkeypatch):
    """Why the end-to-end test above is an owner turn: a grantee's inference
    turn on this scope never reads the vector index and never calls the model.
    If this starts failing, the gate has widened, and a grantee needs its own
    leak test: the hit's id reaching the model, then the canaries absent."""
    model = _ParrotModel()
    resp = _run_turn(principal=Principal(THIRD_PARTY, "cp_relay"), is_grantee_request=True, model=model,
                     monkeypatch=monkeypatch)
    assert resp.get("deny_reason") == "inference_view_unsupported", resp
    assert resp.get("public_result") is None
    assert vector_hit.calls == 0
    assert model.contexts == []

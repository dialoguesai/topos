"""§F.8 — Inference mode must not carry raw record content.

Inference answers yes/no + confidence from derived signal. Raw canonical text (message
bodies, journal entries) and raw semantic-chunk previews must never appear in the inference
context packet — otherwise "inference" becomes a raw-content side channel. Derived labels
(brief topics, fact tags, cluster labels) are allowed; raw record text is not.

Guards the leak where canonical rows contributed their raw content via topic/summary_text
(only content/text/body were stripped, not topic/summary_text) and where semantic hits
carried content_preview/text_preview into the packet.
"""

from __future__ import annotations

import json

import pytest

from topos.query.manifest import ScopeResolutionManifest
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterBundle
from topos.storage.adapters.fakes import (
    InMemoryAuditLogStore,
    InMemoryCanonicalStore,
    InMemoryGraphEdgeStore,
    InMemoryQuerySessionStore,
    InMemorySignalFeatureStore,
    InMemoryVectorIndex,
)

pytestmark = [pytest.mark.private]

RAW_BODY = "secret body zx-canary-inference-9931 meet at the safehouse"


def _bundle_with_message() -> AdapterBundle:
    canonical = InMemoryCanonicalStore()
    canonical.upsert(
        "ai_chat_messages",
        {
            "message_id": "m1",
            "conversation_id": "c",
            "sender_type": "human",
            "event_at": "2026-06-01T00:00:00Z",
            "content": RAW_BODY,
            "source_id": "chatgpt_file_ingestion",
        },
    )
    return AdapterBundle(
        canonical=canonical,
        signal=InMemorySignalFeatureStore(),
        vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(),
        audit=InMemoryAuditLogStore(),
        query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )


def _ai_conversations_manifest() -> ScopeResolutionManifest:
    return ScopeResolutionManifest(
        scope_id="ai_conversations:read",
        primary_dimensions=["Memory", "Work"],
        canonical_tables=["ai_chat_messages"],
        access_mode_ceiling="raw",
        default_source_id="chatgpt_file_ingestion",
    )


def test_inference_packet_has_no_raw_canonical_content():
    adapter = DefaultSignalRetrievalAdapter(_bundle_with_message())
    result = adapter.retrieve(
        RetrievalRequest(
            manifest=_ai_conversations_manifest(),
            access_mode="inference",
            query_text="work",
        )
    )
    blob = json.dumps(result.context_packet, default=str)
    assert RAW_BODY not in blob, "raw message body leaked into inference packet"
    assert "secret body" not in blob
    # The canonical row should still register as evidence (existence/relevance signal present).
    scores = result.context_packet.get("scores") or []
    assert scores, "canonical row should still contribute a score signal"
    for score in scores:
        assert "topic" not in score and "summary_text" not in score, (
            f"canonical inference score still carries raw text keys: {score}"
        )

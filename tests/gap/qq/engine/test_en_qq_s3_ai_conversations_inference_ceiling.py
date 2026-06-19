"""GT-EN-QQ-S3-06: ai_conversations:read allows inference mode."""

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest

from qq_helpers import make_adapter_bundle

pytestmark = pytest.mark.gap


def test_ai_conversations_inference_not_denied_by_ceiling() -> None:
    manifest = resolve_scope_manifest("ai_conversations:read")
    assert manifest.access_mode_ceiling == "inference"
    adapter = DefaultSignalRetrievalAdapter(make_adapter_bundle())
    bundle = adapter.retrieve(
        RetrievalRequest(manifest=manifest, access_mode="inference", query_text="keycloak")
    )
    assert bundle.error is None
    assert bundle.context_packet.get("access_mode") == "inference"

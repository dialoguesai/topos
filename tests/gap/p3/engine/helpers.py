"""Shared helpers for engine Phase 3 gap tests."""

from __future__ import annotations

from typing import Any, Dict

from topos.query.manifest import ScopeResolutionManifest
from topos.storage.adapters.factory import AdapterBundle
from topos.storage.adapters.fakes import (
    InMemoryAuditLogStore,
    InMemoryCanonicalStore,
    InMemoryGraphEdgeStore,
    InMemoryQuerySessionStore,
    InMemorySignalFeatureStore,
    InMemoryVectorIndex,
)


def _seed_adapter_stores(
    canonical: InMemoryCanonicalStore,
    signal: InMemorySignalFeatureStore,
    vector: InMemoryVectorIndex,
    graph: InMemoryGraphEdgeStore,
) -> None:
    canonical.upsert(
        "conversation_messages",
        {
            "record_id": "m1",
            "content": "Contact me at test@example.com nsfw content",
            "sender_id": "u1",
        },
    )
    signal.put_summary(
        {
            "dimension": "relationship",
            "summary_text": "Close friend, frequent contact",
            "topic": "relationship",
        }
    )
    signal.put_score(
        {
            "dimension": "time",
            "value": 0.85,
            "confidence": 0.85,
            "calendar_events.title": "Secret meeting",
            "content": "should not appear",
        }
    )
    vector.upsert(
        {"embedding_id": "emb1", "record_id": "m1", "signal_dimension": "messages"},
        vector=[0.1, 0.2, 0.3],
    )
    graph.upsert_node({"node_id": "n1", "dimension": "relationship"})
    graph.upsert_edge({"edge_id": "e1", "src_node_id": "n1", "dst_node_id": "n2"})


def make_adapter_bundle(*, seed: bool = True) -> AdapterBundle:
    canonical = InMemoryCanonicalStore()
    signal = InMemorySignalFeatureStore()
    vector = InMemoryVectorIndex()
    graph = InMemoryGraphEdgeStore()
    if seed:
        _seed_adapter_stores(canonical, signal, vector, graph)
    return AdapterBundle(
        canonical=canonical,
        signal=signal,
        vector=vector,
        graph=graph,
        audit=InMemoryAuditLogStore(),
        query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )


def _manifest(defaults: Dict[str, Any], **overrides: Any) -> ScopeResolutionManifest:
    return ScopeResolutionManifest(**{**defaults, **overrides})


def messages_manifest(**overrides: Any) -> ScopeResolutionManifest:
    return _manifest(
        {
            "scope_id": "messages:read",
            "primary_dimensions": ["messages"],
            "canonical_tables": ["conversation_messages"],
            "access_mode_ceiling": "raw",
        },
        **overrides,
    )


def availability_manifest(**overrides: Any) -> ScopeResolutionManifest:
    return _manifest(
        {
            "scope_id": "availability:read",
            "primary_dimensions": ["Time"],
            "canonical_tables": [],
            "access_mode_ceiling": "inference",
            "must_not_retrieve": ["calendar_events.title", "conversation_messages.content", "content"],
        },
        **overrides,
    )


def relationship_manifest(**overrides: Any) -> ScopeResolutionManifest:
    return _manifest(
        {
            "scope_id": "relationship_context:read",
            "primary_dimensions": ["relationship"],
            "summary_objects": ["relationship_summary"],
            "access_mode_ceiling": "summary",
        },
        **overrides,
    )

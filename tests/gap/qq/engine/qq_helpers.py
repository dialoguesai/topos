"""Shared helpers for query-quality gap tests."""

from __future__ import annotations

from typing import Any, Dict

from topos.query.manifest import ScopeResolutionManifest
from topos.query.manifest_validation import resolve_scope_manifest
from topos.storage.adapters.factory import AdapterBundle
from topos.storage.adapters.fakes import (
    InMemoryAuditLogStore,
    InMemoryCanonicalStore,
    InMemoryGraphEdgeStore,
    InMemoryQuerySessionStore,
    InMemorySignalFeatureStore,
    InMemoryVectorIndex,
)


def make_adapter_bundle(*, seed: bool = False) -> AdapterBundle:
    return AdapterBundle(
        canonical=InMemoryCanonicalStore(),
        signal=InMemorySignalFeatureStore(),
        vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(),
        audit=InMemoryAuditLogStore(),
        query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )


def ai_conversations_manifest(**overrides: Any) -> ScopeResolutionManifest:
    base = resolve_scope_manifest("ai_conversations:read")
    if overrides:
        fields = {f.name for f in ScopeResolutionManifest.__dataclass_fields__.values()}
        return ScopeResolutionManifest(**{**base.__dict__, **{k: v for k, v in overrides.items() if k in fields}})
    return base


def work_context_manifest(**overrides: Any) -> ScopeResolutionManifest:
    base = resolve_scope_manifest("work_context:read")
    if overrides:
        fields = {f.name for f in ScopeResolutionManifest.__dataclass_fields__.values()}
        return ScopeResolutionManifest(**{**base.__dict__, **{k: v for k, v in overrides.items() if k in fields}})
    return base


def relationship_manifest(**overrides: Any) -> ScopeResolutionManifest:
    base = resolve_scope_manifest("relationship_context:read")
    if overrides:
        fields = {f.name for f in ScopeResolutionManifest.__dataclass_fields__.values()}
        return ScopeResolutionManifest(**{**base.__dict__, **{k: v for k, v in overrides.items() if k in fields}})
    return base

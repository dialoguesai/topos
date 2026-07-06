"""§B.5/§F.7 — engine-level existence parity.

A grantee query where records exist but are all excluded (e.g. NSFW) must be indistinguishable
from one where no records exist: same turn_outcome, same empty result shape, no field that
reveals "there was something here you couldn't see".
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from topos.query.manifest import ScopeResolutionManifest
from topos.query.pipeline import QueryPipelineOrchestrator
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


def _bundle(rows):
    canonical = InMemoryCanonicalStore()
    for r in rows:
        canonical.upsert("conversation_messages", r)
    return AdapterBundle(
        canonical=canonical, signal=InMemorySignalFeatureStore(), vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(), audit=InMemoryAuditLogStore(), query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )


def _manifest():
    return ScopeResolutionManifest(
        scope_id="messages:read", primary_dimensions=["Relationships"],
        canonical_tables=["conversation_messages"], access_mode_ceiling="raw",
    )


def _grantee_query(bundle):
    orch = QueryPipelineOrchestrator(adapters=bundle)
    return asyncio.run(orch.execute(
        query_text="atlas note", scope_id="messages:read", access_mode="raw", manifest=_manifest(),
        query_session_id=f"ep-{uuid.uuid4().hex[:8]}",
        requester_id="grantee-x", owner_id="owner-9", is_grantee_request=True,
    ))


def _shape(resp):
    """Response shape stripped of session-specific / volatile fields."""
    pr = resp.get("public_result") or {}
    return {
        "turn_outcome": resp.get("turn_outcome"),
        "deny_reason": resp.get("deny_reason"),
        "rows": pr.get("rows"),
        "answer_type": pr.get("answer_type"),
    }


def test_all_excluded_looks_like_absent():
    # Bundle A: one NSFW record (excluded for grantee) → empty result.
    a = _grantee_query(_bundle([
        {"record_id": "n1", "content": "atlas note nsfw stuff", "content_disclosure": "atlas note [x]", "content_nsfw": 1},
    ]))
    # Bundle B: no records at all → empty result.
    b = _grantee_query(_bundle([]))

    assert _shape(a) == _shape(b), "exists-but-excluded must be indistinguishable from absent"
    # And neither leaks a hint that something was withheld in the public result.
    assert (a.get("public_result") or {}).get("rows") == []

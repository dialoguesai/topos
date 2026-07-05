"""§F.2 CER — the retrieval/scope/filter-mechanic classes.

off_scope        — a canary in a table the scope does not declare must never be retrieved.
blocklisted_col  — a canary in a must_not_retrieve field must be stripped.
out_of_window    — a canary in a record outside the grant's rolling window must be excluded.

Each has a non-vacuous control: remove the mechanism and the canary IS present, proving the
exclusion did the work (not query-filtering or an empty result).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List

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

from tests.evals.privacy.common.probe import response_discloses

pytestmark = [pytest.mark.cer, pytest.mark.private]

PHRASE = "project atlas"


def _bundle(canonical: InMemoryCanonicalStore) -> AdapterBundle:
    return AdapterBundle(
        canonical=canonical,
        signal=InMemorySignalFeatureStore(),
        vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(),
        audit=InMemoryAuditLogStore(),
        query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )


def _run(bundle, manifest, *, filter_manifest=None) -> Dict[str, Any]:
    orch = QueryPipelineOrchestrator(adapters=bundle)
    return asyncio.run(
        orch.execute(
            query_text=PHRASE,
            scope_id=manifest.scope_id,
            access_mode="raw",
            manifest=manifest,
            filter_manifest=filter_manifest,
            query_session_id=f"cerx-{uuid.uuid4().hex[:8]}",
            requester_id="owner",
            owner_id="owner",
        )
    )


def _msg(record_id: str, content: str, **extra) -> Dict[str, Any]:
    return {"record_id": record_id, "content": content, "content_disclosure": content, **extra}


# --- off_scope -----------------------------------------------------------------------------

def test_off_scope_record_not_retrieved():
    canary = "off-scope-health-secret-4410"
    canonical = InMemoryCanonicalStore()
    canonical.upsert("conversation_messages", _msg("m1", f"{PHRASE}: routine note"))
    canonical.upsert("journal_entries", _msg("j1", f"{PHRASE}: {canary}"))
    bundle = _bundle(canonical)

    messages_manifest = ScopeResolutionManifest(
        scope_id="messages:read", primary_dimensions=["Relationships"],
        canonical_tables=["conversation_messages"], access_mode_ceiling="raw",
    )
    resp = _run(bundle, messages_manifest)
    assert not response_discloses(resp, [canary]), "off-scope journal canary leaked into messages scope"

    # Non-vacuous: a scope that DOES declare journal_entries retrieves it.
    journal_manifest = ScopeResolutionManifest(
        scope_id="activity:read", primary_dimensions=["Memory"],
        canonical_tables=["journal_entries"], access_mode_ceiling="raw",
    )
    resp2 = _run(bundle, journal_manifest)
    assert response_discloses(resp2, [canary]), "journal canary should be visible in its own scope"


# --- blocklisted_column --------------------------------------------------------------------

def test_blocklisted_column_stripped():
    canary = "blocklisted-ssn-999-22-1111"
    canonical = InMemoryCanonicalStore()
    canonical.upsert("conversation_messages", _msg("m1", f"{PHRASE}: clean", ssn=canary))
    bundle = _bundle(canonical)

    base = ScopeResolutionManifest(
        scope_id="messages:read", primary_dimensions=["Relationships"],
        canonical_tables=["conversation_messages"], access_mode_ceiling="raw",
    )
    blocked = replace(base, must_not_retrieve=["ssn"])
    assert not response_discloses(_run(bundle, blocked), [canary]), "blocklisted ssn leaked"

    # Non-vacuous: without must_not_retrieve, the field is present.
    assert response_discloses(_run(bundle, base), [canary]), "ssn should be present without a blocklist"


# --- out_of_window -------------------------------------------------------------------------

def test_out_of_window_record_excluded():
    canary = "out-of-window-secret-8820"
    canonical = InMemoryCanonicalStore()
    now = datetime.now(timezone.utc).isoformat()
    canonical.upsert("conversation_messages", _msg("recent", f"{PHRASE}: recent note", event_at=now))
    canonical.upsert("conversation_messages", _msg("old", f"{PHRASE}: {canary}", event_at="2020-01-01T00:00:00Z"))
    bundle = _bundle(canonical)

    manifest = ScopeResolutionManifest(
        scope_id="messages:read", primary_dimensions=["Relationships"],
        canonical_tables=["conversation_messages"], access_mode_ceiling="raw",
    )
    window = {"manifest_version": 1, "filters": [{"filter_id": "rolling_window_days", "params": {"days": 30}}]}
    assert not response_discloses(_run(bundle, manifest, filter_manifest=window), [canary]), (
        "out-of-window canary leaked despite rolling_window_days=30"
    )

    # Non-vacuous: without the window filter, the old record's canary is present.
    assert response_discloses(_run(bundle, manifest), [canary]), "old record should be present without the window"

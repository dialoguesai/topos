"""
Query permission boundaries: fast engine-side checks (no live DB required for most cases).
"""

from __future__ import annotations

import time

import pytest

from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
from topos.query.manifest_validation import ManifestValidationError, resolve_scope_manifest
from topos.query.pipeline import QueryPipelineOrchestrator

from qq_helpers import make_adapter_bundle
from query_eval_cases import PERMISSION_CASES, PermissionBoundaryCase, manifest_for_scope

pytestmark = pytest.mark.gap

# Non-owner inference is availability:read only (860efe5f), so the owner's own
# path is the only one that still reaches a scope's mode ceiling on inference.
OWNER = Principal(cls=OWNER_APP, channel="uds")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [c for c in PERMISSION_CASES if c.id == "PB1"], ids=["PB1"])
async def test_mode_ceiling_denies_raw_on_availability(case: PermissionBoundaryCase) -> None:
    orch = QueryPipelineOrchestrator(adapters=make_adapter_bundle())
    manifest = manifest_for_scope(case.scope_id)
    # No principal: refused before the ceiling is read, as inference_view_unsupported,
    # which the case accepts alongside the ceiling.
    t0 = time.perf_counter()
    out = await orch.execute(
        query_text=case.query,
        scope_id=case.scope_id,
        access_mode=case.access_mode,  # type: ignore[arg-type]
        manifest=manifest,
        query_session_id="qq-pb1",
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert out.get("turn_outcome") == "denied", out
    deny = str(out.get("deny_reason") or "").lower()
    assert any(s in deny for s in case.deny_substrings), deny
    assert elapsed_ms <= case.max_latency_ms, f"deny path slow: {elapsed_ms:.0f}ms"

    # The ceiling itself, on the path that gate does not shadow: the owner's own
    # inference on a summary-ceiling scope is still refused.
    token = set_principal(OWNER)
    try:
        t0 = time.perf_counter()
        out = await orch.execute(
            query_text=case.query,
            scope_id=case.scope_id,
            access_mode=case.access_mode,  # type: ignore[arg-type]
            manifest=manifest,
            query_session_id="qq-pb1-owner",
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
    finally:
        reset_principal(token)
    assert out.get("turn_outcome") == "denied", out
    assert out.get("deny_reason") == "mode_ceiling_exceeded", out.get("deny_reason")
    assert elapsed_ms <= case.max_latency_ms, f"deny path slow: {elapsed_ms:.0f}ms"


def test_legacy_scope_rejected_at_manifest(case_id: str = "PB2") -> None:
    case = next(c for c in PERMISSION_CASES if c.id == case_id)
    with pytest.raises(ManifestValidationError) as exc:
        resolve_scope_manifest(case.scope_id)
    msg = str(exc.value.message).lower()
    assert any(s in msg for s in case.deny_substrings), msg


@pytest.mark.asyncio
async def test_session_requester_mismatch_denied() -> None:
    adapters = make_adapter_bundle()
    store = adapters.query_session
    store.put(
        {
            "session_id": "qq-session-lock",
            "requester_id": "user-a",
            "intent_hash": "h1",
            "envelope_json": {},
        }
    )
    orch = QueryPipelineOrchestrator(adapters=adapters)
    manifest = manifest_for_scope("messages:read")
    t0 = time.perf_counter()
    out = await orch.execute(
        query_text="hello",
        scope_id="messages:read",
        access_mode="raw",
        manifest=manifest,
        query_session_id="qq-session-lock",
        requester_id="user-b",
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert out.get("turn_outcome") == "denied"
    assert out.get("deny_reason") == "session_requester_mismatch"
    assert elapsed_ms <= 500


@pytest.mark.asyncio
async def test_granted_scope_not_denied() -> None:
    case = next(c for c in PERMISSION_CASES if c.id == "PB3")
    orch = QueryPipelineOrchestrator(adapters=make_adapter_bundle())
    manifest = manifest_for_scope(case.scope_id)
    # PB3 is the owner path, and inference outside availability:read is owner-only.
    token = set_principal(OWNER)
    try:
        out = await orch.execute(
            query_text=case.query,
            scope_id=case.scope_id,
            access_mode=case.access_mode,  # type: ignore[arg-type]
            manifest=manifest,
            query_session_id="qq-pb3",
        )
    finally:
        reset_principal(token)
    assert out.get("turn_outcome") != "denied", out.get("deny_reason")

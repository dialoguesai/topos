"""
Iteration 3b — Live engine handler grantee simulation (real DB, no MCP auth).
Exercises is_grantee_request + filter_manifest ceiling through engine handlers.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.adversarial, pytest.mark.gap]

LIVE_DB = Path(os.environ.get("TOPOS_DATABASE_PATH", Path.home() / ".topos" / "database.db"))


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_DB.exists(), reason=f"live db missing: {LIVE_DB}")
async def test_handler_grantee_raw_denied_with_summary_ceiling() -> None:
    from topos.core.handlers import handle_control_plane_request

    msg = {
        "id": str(uuid.uuid4()),
        "type": "query",
        "payload": {
            "scope_id": "messages:read",
            "access_mode": "raw",
            "intent": "grantee raw bypass",
            "query_session_id": "adv-iter3b-grantee-raw",
            "is_grantee_request": True,
            "filter_manifest": {"access_mode_ceiling": "summary"},
            "owner_user_id": "owner-adv",
            "requester_id": "grantee-adv",
        },
    }
    out = await handle_control_plane_request(msg)
    assert out.get("status") == "ok"
    payload = out.get("payload") or {}
    assert payload.get("turn_outcome") == "denied" or payload.get("deny_reason")
    assert "ceiling" in str(payload.get("deny_reason") or "").lower() or "mode" in str(payload.get("deny_reason") or "").lower()


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_DB.exists(), reason=f"live db missing: {LIVE_DB}")
async def test_handler_grantee_summary_allowed_with_summary_ceiling() -> None:
    from topos.core.handlers import handle_control_plane_request

    msg = {
        "id": str(uuid.uuid4()),
        "type": "query",
        "payload": {
            "scope_id": "messages:read",
            "access_mode": "summary",
            "intent": "grantee summary ok",
            "query_session_id": "adv-iter3b-grantee-summary",
            "is_grantee_request": True,
            "filter_manifest": {"access_mode_ceiling": "summary"},
            "owner_user_id": "owner-adv",
            "requester_id": "grantee-adv",
        },
    }
    out = await handle_control_plane_request(msg)
    payload = out.get("payload") or {}
    assert payload.get("turn_outcome") != "denied", payload.get("deny_reason")


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_DB.exists(), reason=f"live db missing: {LIVE_DB}")
async def test_handler_owner_raw_allowed_without_ceiling() -> None:
    from topos.core.handlers import handle_control_plane_request

    msg = {
        "id": str(uuid.uuid4()),
        "type": "query",
        "payload": {
            "scope_id": "messages:read",
            "access_mode": "raw",
            "intent": "owner raw ok",
            "query_session_id": "adv-iter3b-owner-raw",
            "is_grantee_request": False,
        },
    }
    out = await handle_control_plane_request(msg)
    payload = out.get("payload") or {}
    assert payload.get("turn_outcome") != "denied", payload.get("deny_reason")
    blob = json.dumps(payload.get("public_result") or {}, default=str).lower()
    assert payload.get("turn_outcome") == "live_query" or "message" in blob or "row" in blob


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_DB.exists(), reason=f"live db missing: {LIVE_DB}")
async def test_handler_activity_scope_raw_returns_data() -> None:
    from topos.core.handlers import handle_control_plane_request

    msg = {
        "id": str(uuid.uuid4()),
        "type": "query",
        "payload": {
            "scope_id": "activity:read",
            "access_mode": "raw",
            "intent": "activity rows",
            "query_session_id": "adv-iter3b-activity",
        },
    }
    out = await handle_control_plane_request(msg)
    payload = out.get("payload") or {}
    assert payload.get("turn_outcome") != "denied", payload.get("deny_reason")

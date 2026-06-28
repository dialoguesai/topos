"""HTTP API tests for POST /v1/source-scrub."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_source_scrub_api_requires_auth() -> None:
    from topos.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/source-scrub", json={"source_id": "grow_journal"})
    assert resp.status_code in (401, 403, 422)


@pytest.mark.asyncio
async def test_source_scrub_api_missing_source_id() -> None:
    from topos.app import app
    from topos.auth import require_api_key

    async def _fake_key():
        return "test-key"

    app.dependency_overrides[require_api_key] = _fake_key
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/source-scrub",
                headers={"Authorization": "Bearer test-key"},
                json={},
            )
    finally:
        app.dependency_overrides.pop(require_api_key, None)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_source_scrub_api_dry_run(monkeypatch) -> None:
    from topos.app import app
    from topos.auth import require_api_key

    async def _fake_key():
        return "test-key"

    app.dependency_overrides[require_api_key] = _fake_key
    async def _fake_core(payload):
        return {
            "status": "ok",
            "request_id": "req-1",
            "scrub_id": "scrub-1",
            "source_id": payload.get("source_id"),
            "scrub_status": "dry_run",
            "duration_ms": 1,
            "report": {"totals": {"rows_deleted": 3}},
        }

    monkeypatch.setattr("topos.api.source_scrub._scrub_source_core", _fake_core)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/source-scrub",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "source_id": "grow_journal",
                    "options": {"dry_run": True, "purge_attributed_rows": True},
                },
            )
    finally:
        app.dependency_overrides.pop(require_api_key, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["scrub_status"] == "dry_run"
    assert body["report"]["totals"]["rows_deleted"] == 3


@pytest.mark.asyncio
async def test_source_scrub_api_top_level_dry_run(monkeypatch) -> None:
    from topos.app import app
    from topos.auth import require_api_key

    async def _fake_key():
        return "test-key"

    captured: dict = {}

    async def _fake_scrub(*, source_id, scope=None, options=None):
        captured["options"] = options
        return {
            "status": "ok",
            "scrub_status": "dry_run",
            "report": {"totals": {}},
        }

    app.dependency_overrides[require_api_key] = _fake_key
    monkeypatch.setattr("topos.sources.scrub_service.scrub_source_async", _fake_scrub)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/source-scrub",
                headers={"Authorization": "Bearer test-key"},
                json={"source_id": "grow_journal", "dry_run": True},
            )
    finally:
        app.dependency_overrides.pop(require_api_key, None)

    assert resp.status_code == 200
    assert captured["options"] is not None
    assert captured["options"].dry_run is True


@pytest.mark.asyncio
async def test_source_scrub_api_conflicting_dry_run_returns_400() -> None:
    from topos.app import app
    from topos.auth import require_api_key

    async def _fake_key():
        return "test-key"

    app.dependency_overrides[require_api_key] = _fake_key
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/source-scrub",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "source_id": "grow_journal",
                    "dry_run": True,
                    "options": {"dry_run": False},
                },
            )
    finally:
        app.dependency_overrides.pop(require_api_key, None)

    assert resp.status_code == 400

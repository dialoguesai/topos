"""Privacy disclose HTTP API tests."""

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_privacy_disclose_api_happy_path(monkeypatch):
    from topos.app import app
    from topos.auth import require_api_key

    async def _fake_key():
        return "test-key"

    app.dependency_overrides[require_api_key] = _fake_key
    monkeypatch.setattr(
        "topos.api.privacy_disclose.redact_privacy_batch",
        lambda items, transform_id="pii_redaction": {
            "items": [{"id": items[0]["id"], "text": "[EMAIL]"}],
            "model": "openai/privacy-filter",
            "privacy_layer_version": "1",
            "status": "ok",
        },
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/privacy/disclose",
                headers={"Authorization": "Bearer test-key"},
                json={"items": [{"id": "1", "text": "a@b.com"}]},
            )
    finally:
        app.dependency_overrides.pop(require_api_key, None)
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["text"] == "[EMAIL]"


@pytest.mark.asyncio
async def test_privacy_disclose_api_requires_auth():
    from topos.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/v1/privacy/disclose", json={"items": [{"id": "1", "text": "x"}]})
    assert resp.status_code in (401, 403, 422)


@pytest.mark.asyncio
async def test_privacy_disclose_api_batch_limit(monkeypatch):
    from topos.app import app
    from topos.auth import require_api_key
    from topos.sanitization.privacy_filter import PRIVACY_DISCLOSE_MAX_BATCH

    async def _fake_key():
        return "test-key"

    app.dependency_overrides[require_api_key] = _fake_key
    items = [{"id": str(i), "text": "x"} for i in range(PRIVACY_DISCLOSE_MAX_BATCH + 1)]
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/privacy/disclose",
                headers={"Authorization": "Bearer test-key"},
                json={"items": items},
            )
    finally:
        app.dependency_overrides.pop(require_api_key, None)
    assert resp.status_code == 413

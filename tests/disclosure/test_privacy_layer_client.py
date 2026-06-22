"""PrivacyLayerClient tests."""

import pytest

from topos.disclosure.privacy_layer import PrivacyLayerClient


@pytest.mark.asyncio
async def test_privacy_layer_client_in_process_engine(monkeypatch):
    async def _fake_run_engine_task(engine, **kwargs):
        assert kwargs["subtype"] == "privacy_disclosure"

        class _Result:
            output = {
                "items": [{"id": "k1", "text": "[EMAIL]"}],
                "model": "openai/privacy-filter",
                "status": "ok",
            }

        return _Result()

    monkeypatch.setattr(
        "topos.enrichment.jobs.canonical._engine_runner.run_engine_task",
        _fake_run_engine_task,
    )
    client = PrivacyLayerClient(engine=object())
    out = await client.redact_batch([{"id": "k1", "text": "a@b.com"}])
    assert out["items"][0]["text"] == "[EMAIL]"


@pytest.mark.asyncio
async def test_privacy_layer_client_http(monkeypatch):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"items": [{"id": "k1", "text": "[EMAIL]"}], "model": "openai/privacy-filter", "status": "ok"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers):
            assert url.endswith("/v1/privacy/disclose")
            return _Resp()

    monkeypatch.setattr("topos.disclosure.privacy_layer.httpx.AsyncClient", lambda **kw: _Client())
    client = PrivacyLayerClient(engine_url="http://engine:8080", api_key="secret")
    out = await client.redact_batch([{"id": "k1", "text": "a@b.com"}])
    assert out["items"][0]["text"] == "[EMAIL]"

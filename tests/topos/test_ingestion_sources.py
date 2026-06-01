import os
import importlib
import sys
import tempfile

import pytest
from topos.testing.lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_ingest_source_file_route():
    os.environ["TOPOS_KEY"] = "test-key"

    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["TOPOS_INGESTION_BASE_PATH"] = temp_dir
        if "topos.app" in sys.modules:
            importlib.reload(sys.modules["topos.app"])
        from topos.app import app
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as temp_file:
            temp_file.write(b'{"id":"m1","thread_id":"t1","role":"user","content":"hi","created_at":1}\n')
            temp_file.flush()
            transport = ASGITransport(app=app)
            async with LifespanManager(app):
                async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                    resp = await client.post(
                        "/sources/chatgpt_file_ingestion/ingest",
                        params={"dataset_id": "user:default", "file_path": temp_file.name},
                        headers={"Authorization": "Bearer test-key"},
                    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["records_processed"] == 1


@pytest.mark.asyncio
async def test_ingest_source_ui_stream_requires_payload():
    os.environ["TOPOS_KEY"] = "test-key"
    from topos.app import app

    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/sources/chatgpt_ui_conversation/ingest",
                params={"dataset_id": "user:default"},
                headers={"Authorization": "Bearer test-key"},
            )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "error"


@pytest.mark.asyncio
async def test_ingest_source_ui_stream_processes_payload():
    os.environ["TOPOS_KEY"] = "test-key"
    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["TOPOS_INGESTION_BASE_PATH"] = temp_dir
        if "topos.app" in sys.modules:
            importlib.reload(sys.modules["topos.app"])
        from topos.app import app

        transport = ASGITransport(app=app)
        async with LifespanManager(app):
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/sources/chatgpt_ui_conversation/ingest",
                    params={"dataset_id": "user:default"},
                    headers={"Authorization": "Bearer test-key"},
                    json={"sender_type": "human", "content": "hello"},
                )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["records_processed"] >= 1


@pytest.mark.asyncio
async def test_sync_imessage_requires_dataset_id():
    os.environ["TOPOS_KEY"] = "test-key"
    from topos.app import app

    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/sources/imessage/sync",
                headers={"Authorization": "Bearer test-key"},
            )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "error"
    assert "dataset_id" in payload.get("error", "").lower()


@pytest.mark.asyncio
async def test_sync_imessage_returns_ok_or_error():
    """With dataset_id, sync returns 200; body is ok (0 or more processed) or error (e.g. chat.db not found)."""
    os.environ["TOPOS_KEY"] = "test-key"
    from topos.app import app

    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/sources/imessage/sync",
                params={"dataset_id": "test-dataset"},
                headers={"Authorization": "Bearer test-key"},
            )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] in ("ok", "error")
    if payload["status"] == "ok":
        assert "records_processed" in payload
    else:
        assert "error" in payload


@pytest.mark.asyncio
async def test_ingest_source_file_propagates_guard_denial(monkeypatch: pytest.MonkeyPatch):
    os.environ["TOPOS_KEY"] = "test-key"

    async def _deny_guard(**kwargs):
        return {
            "allowed": False,
            "source": "control_plane",
            "denial": {
                "reason_code": "LIMIT_EXCEEDED",
                "metric_key": "file_transfer_mb",
                "period_start": "2026-01-01T00:00:00+00:00",
                "period_end": "2026-02-01T00:00:00+00:00",
            },
        }

    monkeypatch.setattr("topos.api.ingestion_sources.submit_usage_guard_check", _deny_guard)

    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["TOPOS_INGESTION_BASE_PATH"] = temp_dir
        if "topos.app" in sys.modules:
            importlib.reload(sys.modules["topos.app"])
        from topos.app import app
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as temp_file:
            temp_file.write(b'{"id":"m1","thread_id":"t1","role":"user","content":"hi","created_at":1}\n')
            temp_file.flush()
            transport = ASGITransport(app=app)
            async with LifespanManager(app):
                async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                    resp = await client.post(
                        "/sources/chatgpt_file_ingestion/ingest",
                        params={"dataset_id": "user:default", "file_path": temp_file.name},
                        headers={"Authorization": "Bearer test-key"},
                    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "error"
    assert payload["error"] == "usage_guard_denied"
    denial = payload.get("denial") or {}
    assert denial.get("reason_code") == "LIMIT_EXCEEDED"
    assert denial.get("metric_key") == "file_transfer_mb"

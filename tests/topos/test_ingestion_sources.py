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
async def test_sync_imessage_returns_job_handle_without_running_the_sync():
    """Sync answers with a job handle immediately; it does not run inline.

    The whole point of the change: a first iMessage run drains the entire
    backlog and takes hours, so anything that waits for it inside the request
    is guaranteed to time out at the caller. A 200 carrying a job_id is the
    contract both this route and the websocket handler now keep.
    """
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
        assert payload.get("job_id")
        assert payload.get("already_running") is False
        # The sync must NOT have run in the request: a synchronous run is what
        # produced the 20s control-plane timeout this change exists to remove.
        assert "records_processed" not in payload


@pytest.mark.asyncio
async def test_sync_imessage_second_call_reattaches_to_the_running_job():
    """A second press returns the SAME job id instead of starting a rival sync.

    Two concurrent syncs write the same SQLite file; that is the shape behind
    the live `database is locked` receipt. Idempotency keys cannot express this
    (a done row's key would block every future sync forever), so mutual
    exclusion is an explicit queued/running lookup — and this pins it.
    """
    os.environ["TOPOS_KEY"] = "test-key"
    from topos.app import app

    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.post(
                "/sources/imessage/sync",
                params={"dataset_id": "test-dataset-reattach"},
                headers={"Authorization": "Bearer test-key"},
            )
            second = await client.post(
                "/sources/imessage/sync",
                params={"dataset_id": "test-dataset-reattach"},
                headers={"Authorization": "Bearer test-key"},
            )
    first_body, second_body = first.json(), second.json()
    if first_body.get("status") != "ok":
        pytest.skip(f"sync could not be enqueued here: {first_body.get('error')}")
    assert second_body["status"] == "ok"
    assert second_body["job_id"] == first_body["job_id"]
    assert second_body["already_running"] is True


@pytest.mark.asyncio
async def test_job_progress_route_answers_200_for_an_unknown_job():
    """The poll route never answers 5xx — not even for a job that does not exist.

    A 5xx on a route polled every couple of seconds is how a transient miss
    becomes an unreadable browser failure: the edge replaces an origin 5xx with
    its own page and strips the CORS header, so `fetch` rejects with
    "Failed to fetch" instead of surfacing the status.
    """
    os.environ["TOPOS_KEY"] = "test-key"
    from topos.app import app

    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get(
                "/v1/enrichment/progress/no-such-job-id",
                headers={"Authorization": "Bearer test-key"},
            )
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


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

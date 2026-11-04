"""Synthetic admission lifecycles; no model, sockets, or owner data."""
import asyncio
import contextvars
import json
import threading
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from topos.engine.admission import AdmissionRejected, EngineAdmission
from topos.engine.tasks import ProcessingResult

pytestmark = pytest.mark.public


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 2)


def blocked(release, calls, value):
    calls.append(value)
    assert release.wait(3), "test worker was not released"
    return value


@pytest.mark.asyncio
async def test_bounded_burst_fifo_overflow_and_recovery():
    admission = EngineAdmission(active=1, waiting=3)
    release, calls = threading.Event(), []
    work = []
    try:
        for index in range(4):
            work.append(asyncio.create_task(admission.execute(lambda i=index: blocked(release, calls, i))))
            await until(lambda: admission.snapshot()["totals"].get("admitted") == index + 1)
        rejected = await asyncio.gather(*(admission.execute(lambda: calls.append("unexpected")) for _ in range(20)), return_exceptions=True)
        assert all(isinstance(result, AdmissionRejected) and result.reason == "queue_full" for result in rejected)
        assert admission.snapshot()["active"] == 1
        assert admission.snapshot()["waiting"] == 3
        assert calls == [0]
        release.set()
        assert await asyncio.gather(*work) == [0, 1, 2, 3]
        assert calls == [0, 1, 2, 3]
        assert await admission.execute(lambda: "recovered") == "recovered"
        state = admission.snapshot()
        assert state["active"] == state["waiting"] == 0
        assert state["totals"]["offered"] == 25
        assert state["totals"]["dispatched"] == 5
        assert state["totals"]["rejected_full"] == 20
        assert state["backend_drain_known"] is False
    finally:
        release.set()
        admission.close()
        await asyncio.gather(*work, return_exceptions=True)


@pytest.mark.asyncio
async def test_queue_expiry_frees_waiting_capacity_and_never_executes():
    admission = EngineAdmission(active=1, waiting=1, queue_timeout=0.04)
    release, calls = threading.Event(), []
    first = asyncio.create_task(admission.execute(lambda: blocked(release, calls, "active")))
    try:
        await until(lambda: bool(calls))
        with pytest.raises(AdmissionRejected, match="busy") as raised:
            await admission.execute(lambda: calls.append("expired"))
        assert raised.value.reason == "queue_expired"
        assert admission.snapshot()["active"] == 1
        assert admission.snapshot()["waiting"] == 0
        release.set()
        await first
        assert await admission.execute(lambda: "new") == "new"
        assert calls == ["active"]
    finally:
        release.set()
        admission.close()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_active_caller_retains_slot_and_cancelled_queue_never_runs():
    admission = EngineAdmission(active=1, waiting=1)
    release, calls = threading.Event(), []
    first = asyncio.create_task(admission.execute(lambda: blocked(release, calls, "active")))
    second = None
    try:
        await until(lambda: bool(calls))
        second = asyncio.create_task(admission.execute(lambda: calls.append("cancelled")))
        await until(lambda: admission.snapshot()["waiting"] == 1)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        state = admission.snapshot()
        assert state["active"] == state["detached_active"] == 1
        assert state["waiting"] == 0
        third = asyncio.create_task(admission.execute(lambda: "recovered"))
        await until(lambda: admission.snapshot()["waiting"] == 1)
        with pytest.raises(AdmissionRejected):
            await admission.execute(lambda: "overflow")
        release.set()
        assert await third == "recovered"
        assert admission.snapshot()["active"] == admission.snapshot()["detached_active"] == 0
        assert calls == ["active"]
        assert admission.snapshot()["totals"]["cancelled_queued"] == 1
    finally:
        release.set()
        admission.close()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_rejects_waiters_and_reports_undrained_workers():
    admission = EngineAdmission(active=1, waiting=1)
    release, calls = threading.Event(), []
    first = asyncio.create_task(admission.execute(lambda: blocked(release, calls, "active")))
    try:
        await until(lambda: bool(calls))
        queued = asyncio.create_task(admission.execute(lambda: calls.append("stopped")))
        await until(lambda: admission.snapshot()["waiting"] == 1)
        assert admission.close(timeout=0) == 1
        with pytest.raises(AdmissionRejected) as raised:
            await queued
        assert raised.value.reason == "stopping"
        with pytest.raises(AdmissionRejected):
            await admission.execute(lambda: None)
        release.set()
        await first
        assert admission.close(timeout=0) == 0
        assert calls == ["active"]
    finally:
        release.set()
        admission.close()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_exception_releases_once_and_context_follows_request():
    admission = EngineAdmission(active=1, waiting=0)
    marker = contextvars.ContextVar("synthetic-parent", default="missing")
    token = marker.set("caller")
    try:
        assert await admission.execute(marker.get) == "caller"
        with pytest.raises(ValueError, match="synthetic failure"):
            await admission.execute(lambda: (_ for _ in ()).throw(ValueError("synthetic failure")))
        assert await admission.execute(lambda: "recovered") == "recovered"
        assert admission.snapshot()["active"] == 0
        assert admission.snapshot()["totals"]["worker_failed"] == 1
    finally:
        marker.reset(token)
        admission.close()


@asynccontextmanager
async def endpoint(monkeypatch, release, calls):
    from topos.api import engine_run
    from topos.auth import require_api_key
    from topos.config.settings import settings

    monkeypatch.setattr(settings, "topos_engine_http_active", 1)
    monkeypatch.setattr(settings, "topos_engine_http_waiting", 1)
    monkeypatch.setattr(settings, "topos_engine_http_shutdown_grace_sec", 0)
    class SyntheticEngine:
        def run(self, task):
            blocked(release, calls, task.id)
            return ProcessingResult(task_id=task.id, status="completed", output={"answer": "synthetic"})
    monkeypatch.setattr(engine_run, "Engine", SyntheticEngine)
    app = FastAPI()
    app.include_router(engine_run.router)
    app.dependency_overrides[require_api_key] = lambda: None
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            yield app, client


def body(task_id):
    return {"id": task_id, "type": "enrichment", "input": {"text": "synthetic"},
            "model_request": {"provider": "ollama", "model": "fixture"}}


@pytest.mark.asyncio
async def test_request_deadline_removes_queued_remote_work_before_dispatch(monkeypatch):
    from topos.engine.request_budget import TIMEOUT_HEADER
    release, calls = threading.Event(), []
    try:
        async with endpoint(monkeypatch, release, calls) as (app, client):
            active = asyncio.create_task(client.post('/v1/engine/run', json=body('active')))
            await until(lambda: calls == ['active'])
            expired = await client.post('/v1/engine/run', json=body('expired'), headers={TIMEOUT_HEADER:'50'})
            assert expired.status_code == 503
            assert expired.json()['result']['output']['availability']['reason'] == 'deadline_exceeded'
            assert app.state.engine_http_admission.snapshot()['waiting'] == 0
            assert app.state.engine_http_admission.snapshot()['active'] == 1
            release.set()
            assert (await active).status_code == 200
            assert (await client.post('/v1/engine/run', json=body('new'), headers={TIMEOUT_HEADER:'500'})).status_code == 200
            assert calls == ['active', 'new']
    finally:
        release.set()


@pytest.mark.asyncio
async def test_request_deadline_does_not_report_noncooperative_worker_as_drained(monkeypatch):
    from topos.engine.request_budget import TIMEOUT_HEADER
    release, calls = threading.Event(), []
    try:
        async with endpoint(monkeypatch, release, calls) as (app, client):
            response = await client.post('/v1/engine/run', json=body('slow'), headers={TIMEOUT_HEADER:'50'})
            assert response.status_code == 503
            snapshot = app.state.engine_http_admission.snapshot()
            assert snapshot['active'] == snapshot['detached_active'] == 1
            assert snapshot['backend_drain_known'] is False
            release.set()
            await until(lambda: app.state.engine_http_admission.snapshot()['active'] == 0)
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['nan', '-1', 'true', '120001', '0000001'])
async def test_invalid_remote_budget_never_dispatches(monkeypatch, value):
    from topos.engine.request_budget import TIMEOUT_HEADER
    release, calls = threading.Event(), []
    async with endpoint(monkeypatch, release, calls) as (_, client):
        response = await client.post('/v1/engine/run', json=body('invalid'), headers={TIMEOUT_HEADER:value})
        assert response.status_code == 422
        assert not calls


@pytest.mark.asyncio
async def test_http_overflow_control_responsiveness_and_recovery(monkeypatch):
    release, calls = threading.Event(), []
    try:
        async with endpoint(monkeypatch, release, calls) as (app, client):
            a = asyncio.create_task(client.post("/v1/engine/run", json=body("a")))
            await until(lambda: calls == ["a"])
            b = asyncio.create_task(client.post("/v1/engine/run", json=body("b")))
            await until(lambda: app.state.engine_http_admission.snapshot()["waiting"] == 1)
            health = await asyncio.wait_for(client.get("/v1/engine/admission"), 0.5)
            assert health.status_code == 200
            assert health.json()["active"] == health.json()["waiting"] == 1
            rejected = await asyncio.wait_for(client.post("/v1/engine/run", json=body("c")), 0.5)
            assert rejected.status_code == 503
            assert "Retry-After" not in rejected.headers
            result = rejected.json()["result"]
            assert result["task_id"] == "c" and result["status"] == "deferred"
            assert result["output"]["availability"]["code"] == "compute_busy"
            assert result["output"]["admission"]["reason"] == "queue_full"
            release.set()
            assert (await a).status_code == (await b).status_code == 200
            assert (await client.post("/v1/engine/run", json=body("d"))).status_code == 200
            assert calls == ["a", "b", "d"]
    finally:
        release.set()


@pytest.mark.asyncio
async def test_http_disconnect_retains_active_slot(monkeypatch):
    release, calls = threading.Event(), []
    try:
        async with endpoint(monkeypatch, release, calls) as (app, _):
            incoming, sent = asyncio.Queue(), []
            await incoming.put({"type": "http.request", "body": json.dumps(body("gone")).encode(), "more_body": False})
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                     "scheme": "http", "path": "/v1/engine/run", "raw_path": b"/v1/engine/run", "query_string": b"",
                     "root_path": "", "headers": [(b"content-type", b"application/json")],
                     "client": ("fixture", 1), "server": ("fixture", 80)}
            async def send(message):
                sent.append(message)
            request = asyncio.create_task(app(scope, incoming.get, send))
            await until(lambda: calls == ["gone"])
            await incoming.put({"type": "http.disconnect"})
            await asyncio.wait_for(request, 0.5)
            assert sent[0]["status"] == 499
            assert app.state.engine_http_admission.snapshot()["detached_active"] == 1
            assert app.state.engine_http_admission.snapshot()["active"] == 1
            release.set()
            await until(lambda: app.state.engine_http_admission.snapshot()["active"] == 0)
    finally:
        release.set()


@pytest.mark.parametrize("values", [{"active": 0}, {"active": 1.5}, {"active": True}, {"waiting": -1},
                                    {"waiting": 129}, {"queue_timeout": 0}, {"queue_timeout": float("nan")}])
def test_invalid_bounds_are_rejected(values):
    with pytest.raises(ValueError):
        EngineAdmission(**values)


@pytest.mark.asyncio
async def test_zero_waiting_and_multiple_worker_slots_are_bounded():
    admission = EngineAdmission(active=2, waiting=0)
    release, calls = threading.Event(), []
    running = [asyncio.create_task(admission.execute(lambda i=i: blocked(release, calls, i))) for i in range(2)]
    try:
        await until(lambda: len(calls) == 2)
        outcomes = await asyncio.gather(*(admission.execute(lambda: "unexpected") for _ in range(22)), return_exceptions=True)
        assert all(isinstance(value, AdmissionRejected) for value in outcomes)
        assert admission.snapshot()["active"] == 2
        assert admission.snapshot()["waiting"] == 0
        release.set()
        await asyncio.gather(*running)
        for _ in range(3):
            assert await admission.execute(lambda: "recovered") == "recovered"
        assert admission.snapshot()["totals"]["dispatched"] == 5
    finally:
        release.set()
        admission.close()
        await asyncio.gather(*running, return_exceptions=True)


@pytest.mark.asyncio
async def test_application_lifespan_cannot_replace_undrained_dispatcher(monkeypatch):
    from topos.api.engine_run import admission_lifespan
    from topos.config.settings import settings
    monkeypatch.setattr(settings, "topos_engine_http_shutdown_grace_sec", 0)
    app = FastAPI()
    release, calls = threading.Event(), []
    work = None
    try:
        async with admission_lifespan(app):
            prior = app.state.engine_http_admission
            work = asyncio.create_task(prior.execute(lambda: blocked(release, calls, "old")))
            await until(lambda: bool(calls))
        assert prior.snapshot()["active"] == 1
        with pytest.raises(RuntimeError, match="still draining"):
            async with admission_lifespan(app):
                pytest.fail("replaced a live worker budget")
        release.set()
        await work
        async with admission_lifespan(app):
            assert app.state.engine_http_admission is not prior
            assert await app.state.engine_http_admission.execute(lambda: "new") == "new"
    finally:
        release.set()
        if work:
            await asyncio.gather(work, return_exceptions=True)
        prior.close()


@pytest.mark.asyncio
async def test_auth_and_validation_precede_admission(monkeypatch):
    from topos.config.settings import settings
    from topos import uds
    release, calls = threading.Event(), []
    release.set()
    monkeypatch.setattr(settings, "topos_key", "synthetic-admission-key")
    monkeypatch.setattr(uds, "current_transport", lambda: "tcp")
    async with endpoint(monkeypatch, release, calls) as (app, client):
        app.dependency_overrides.clear()
        assert (await client.get("/v1/engine/admission")).status_code == 401
        assert (await client.post("/v1/engine/run", json=body("unauthorized"))).status_code == 401
        headers = {"Authorization": "Bearer synthetic-admission-key"}
        assert (await client.post("/v1/engine/run", json={"id": "invalid"}, headers=headers)).status_code == 422
        snapshot = await client.get("/v1/engine/admission", headers=headers)
        assert snapshot.status_code == 200
        assert snapshot.json()["totals"]["offered"] == 0
        assert all(value == 0 for value in snapshot.json()["totals"].values())
        assert not calls

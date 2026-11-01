"""Controlled provider ownership; no models, owner data or real sockets."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import uuid

import httpx
import pytest

from topos.compute.gateway import create_app, local_provider_url
from topos.compute.journal import OwnershipJournal
from topos.compute.provider import ProviderAuthority, Rejected

pytestmark = pytest.mark.public
TOKEN = "fixture-gateway-token-000000000000000"
BODY = b'{"model":"fixture","prompt":"Synthetic input","stream":false}'
DONE = b'{"response":"Synthetic result","done":true}\n'


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


class Stream(httpx.AsyncByteStream):
    def __init__(self, *, release=None, first=b'{"response":"First","done":false}\n', final=DONE, failure=False):
        self.release, self.first, self.final, self.failure = release, first, final, failure
        self.closed = False

    async def __aiter__(self):
        if self.first:
            yield self.first
        if self.release:
            await self.release.wait()
        if self.failure:
            raise httpx.ReadError("PRIVATE_PROVIDER_DIAGNOSTIC")
        if self.final:
            yield self.final

    async def aclose(self):
        self.closed = True


@asynccontextmanager
async def authority(tmp_path, handler, **kwargs):
    journal = OwnershipJournal(tmp_path / "authority", "fixture-resource")
    async with httpx.AsyncClient(base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(handler)) as client:
        gate = ProviderAuthority(journal, client, **kwargs)
        try:
            yield gate
        finally:
            await gate.close(0)


async def consume(job):
    status, _ = await asyncio.shield(job.headers)
    chunks = [chunk async for chunk in job.chunks()]
    return status, b"".join(chunks)


@pytest.mark.asyncio
async def test_mixed_chat_generate_share_budget_fifo_overflow_recovery(tmp_path):
    release = asyncio.Event()
    calls = []
    async def provider(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(200, stream=Stream(release=release))
        return httpx.Response(200, content=DONE)
    async with authority(tmp_path, provider, active=1, waiting=3) as gate:
        jobs = [gate.submit("/api/generate", BODY, True)]
        await until(lambda: len(calls) == 1)
        jobs += [gate.submit(path, BODY, False) for path in ("/api/chat", "/api/generate", "/api/chat")]
        for _ in range(20):
            with pytest.raises(Rejected) as failure:
                gate.submit("/api/chat", BODY, False)
            assert failure.value.reason == "queue_full"
        assert gate.snapshot()["active"] == 1 and gate.snapshot()["waiting"] == 3
        release.set()
        assert all(status == 200 for status, _ in await asyncio.gather(*(consume(job) for job in jobs)))
        await until(lambda: not gate.running)
        assert calls == ["/api/generate", "/api/chat", "/api/generate", "/api/chat"]
        assert (await consume(gate.submit("/api/chat", BODY, False)))[0] == 200
        await until(lambda: not gate.running)
        assert gate.snapshot()["totals"]["terminal_observed"] == 5
        assert gate.snapshot()["totals"]["rejected_full"] == 20
        assert gate.snapshot()["uncertain"] == 0


@pytest.mark.asyncio
async def test_stream_delivers_first_frame_before_provider_finishes(tmp_path):
    release = asyncio.Event()
    async with authority(tmp_path, lambda _: httpx.Response(200, stream=Stream(release=release))) as gate:
        job = gate.submit("/api/generate", BODY, True)
        await job.headers
        iterator = job.chunks()
        first = await asyncio.wait_for(anext(iterator), 1)
        assert b"First" in first and not release.is_set()
        gate.detach(job)
        assert gate.snapshot()["detached_active"] == 1
        assert gate.snapshot()["active"] == 1
        release.set()
        await until(lambda: not gate.running)
        assert gate.snapshot()["totals"]["terminal_observed"] == 1
        await iterator.aclose()


@pytest.mark.asyncio
async def test_cancel_queued_and_expire_never_dispatch(tmp_path):
    release, calls = asyncio.Event(), []
    def provider(request):
        calls.append(request.url.path)
        return httpx.Response(200, stream=Stream(release=release))
    async with authority(tmp_path, provider, queue_seconds=0.03) as gate:
        active = gate.submit("/api/generate", BODY, True)
        await active.headers
        cancelled = gate.submit("/api/chat", BODY, False)
        gate.detach(cancelled)
        expired = gate.submit("/api/chat", BODY, False)
        with pytest.raises(Rejected) as failure:
            await expired.headers
        assert failure.value.reason == "queue_expired"
        assert calls == ["/api/generate"]
        assert gate.snapshot()["totals"]["cancelled_queued"] == 1
        release.set()
        await consume(active)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["eof", "broken", "timeout", "error_status", "invalid_json", "error_frame", "extra_after_done"])
async def test_uncertain_provider_blocks_pending_and_new_work_after_restart(tmp_path, mode):
    release = asyncio.Event()
    calls = []
    def provider(request):
        calls.append(request.url.path)
        if mode == "error_status":
            return httpx.Response(503, text="PRIVATE_PROVIDER_DIAGNOSTIC")
        final = {"eof": b"", "invalid_json": b"invalid\n", "error_frame": b'{"error":"PRIVATE_PROVIDER_DIAGNOSTIC"}\n',
                 "extra_after_done": DONE + DONE}.get(mode, DONE)
        return httpx.Response(200, stream=Stream(release=release, final=final, failure=mode == "broken"))
    async with authority(tmp_path, provider, execution_seconds=0.03 if mode == "timeout" else 10) as gate:
        job = gate.submit("/api/generate", BODY, True)
        waiting = gate.submit("/api/chat", BODY, False)
        if mode != "timeout":
            release.set()
        await until(lambda: not gate.running)
        assert gate.snapshot()["uncertain"] == 1
        assert gate.snapshot()["active"] == gate.snapshot()["waiting"] == 0
        assert gate.snapshot()["totals"]["aborted_queued"] == 1
        with pytest.raises(Rejected):
            await waiting.headers
        with pytest.raises(Rejected):
            gate.submit("/api/chat", BODY, False)
        assert len(calls) == 1
        assert "PRIVATE" not in str(gate.snapshot())
    async with authority(tmp_path, provider) as restarted:
        assert restarted.snapshot()["uncertain"] == 1
        with pytest.raises(Rejected):
            restarted.submit("/api/chat", BODY, False)


@pytest.mark.asyncio
async def test_shutdown_active_retains_durable_uncertainty(tmp_path):
    release = asyncio.Event()
    async with authority(tmp_path, lambda _: httpx.Response(200, stream=Stream(release=release))) as gate:
        job = gate.submit("/api/generate", BODY, True)
        await job.headers
    async with authority(tmp_path, lambda _: pytest.fail("must not dispatch")) as restarted:
        assert restarted.uncertain == 1
        with pytest.raises(Rejected):
            restarted.submit("/api/chat", BODY, False)


@pytest.mark.asyncio
async def test_slow_client_bounded_buffer_does_not_block_provider_drain(tmp_path):
    release = asyncio.Event()
    first = b'{"response":"' + b'x' * 900 + b'","done":false}\n'
    async with authority(tmp_path, lambda _: httpx.Response(200, stream=Stream(first=first*4, release=release)), buffer_bytes=1024) as gate:
        job = gate.submit("/api/generate", BODY, True)
        await until(lambda: job.detached)
        assert job.buffered <= 1024 and gate.snapshot()["active"] == 1
        assert gate.snapshot()["totals"]["slow_consumer"] == 1
        release.set()
        await until(lambda: not gate.running)
        assert gate.uncertain == 0
        assert gate.snapshot()["totals"]["terminal_observed"] == 1


@pytest.mark.asyncio
async def test_durable_intent_precedes_dispatch_and_journal_is_content_free(tmp_path):
    def provider(request):
        with sqlite3.connect(tmp_path / "authority/ownership.sqlite3") as conn:
            assert conn.execute("SELECT state FROM operations").fetchall() == [("executing",)]
        return httpx.Response(200, content=DONE)
    async with authority(tmp_path, provider) as gate:
        await consume(gate.submit("/api/generate", BODY, False))
        await until(lambda: not gate.running)
    raw = (tmp_path / "authority/ownership.sqlite3").read_bytes()
    assert b"Synthetic input" not in raw and b"Synthetic result" not in raw


def test_duplicate_authority_and_resource_mismatch_fail_closed(tmp_path):
    journal = OwnershipJournal(tmp_path / "authority", "one")
    try:
        with pytest.raises(BlockingIOError):
            OwnershipJournal(tmp_path / "authority", "one")
    finally:
        journal.close()
    with pytest.raises(ValueError, match="different resource"):
        OwnershipJournal(tmp_path / "authority", "two")


def test_restart_after_simulated_crash_intent_is_uncertain(tmp_path):
    journal = OwnershipJournal(tmp_path / "authority", "one")
    journal.begin("synthetic-operation")
    journal.close()  # No terminal record: same persisted state as process death.
    restarted = OwnershipJournal(tmp_path / "authority", "one")
    try:
        assert restarted.unresolved == 1
        assert restarted.epoch != journal.epoch
    finally:
        restarted.close()


@pytest.mark.parametrize("url", ["https://remote.invalid", "http://localhost:11434", "http://remote.invalid", "http://127.0.0.1:11434/path", "http://user:pass@127.0.0.1", "http://127.0.0.1?forward=remote"])
def test_pilot_cannot_forward_local_privacy_to_remote_or_dynamic_host(url):
    with pytest.raises(ValueError):
        local_provider_url(url)


@pytest.mark.asyncio
async def test_http_auth_json_stream_overflow_and_control_responsiveness(tmp_path):
    release = asyncio.Event()
    calls = []
    def provider(request):
        calls.append(request.url.path)
        data = json.loads(request.content)
        if data.get("stream"):
            return httpx.Response(200, stream=Stream(release=release))
        return httpx.Response(200, content=DONE)
    app = create_app(upstream="http://127.0.0.1:11434", state_dir=tmp_path / "gateway",
                     token=TOKEN, waiting=0, transport=httpx.MockTransport(provider), shutdown_grace=0)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway") as client:
            assert (await client.get("/admission")).status_code == 401
            assert (await client.post("/api/generate", content=BODY)).status_code == 401
            headers = {"Authorization": f"Bearer {TOKEN}"}
            response = await client.post("/api/chat", content=BODY, headers=headers)
            assert response.status_code == 200 and response.json()["done"] is True
            assert uuid.UUID(response.headers["X-Topos-Operation-Id"])
            blocked = asyncio.create_task(client.post("/api/generate", json={"model":"fixture", "stream":True}, headers=headers))
            await until(lambda: len(calls) == 2)
            async with asyncio.timeout(0.5):
                assert (await client.get("/admission", headers=headers)).status_code == 200
                rejected = await client.post("/api/chat", content=BODY, headers=headers)
            assert rejected.status_code == 503
            assert rejected.json()["detail"]["code"] == "compute_busy"
            release.set()
            response = await blocked
            assert response.status_code == 200 and b'"done":true' in response.content
            assert calls == ["/api/chat", "/api/generate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_real_asgi_disconnect_retains_active_or_removes_queued(tmp_path, queued):
    release = asyncio.Event()
    calls = []
    def provider(request):
        calls.append(request.url.path)
        return httpx.Response(200, stream=Stream(release=release))
    app = create_app(upstream="http://127.0.0.1:11434", state_dir=tmp_path / "gateway",
                     token=TOKEN, transport=httpx.MockTransport(provider), shutdown_grace=0)
    async with app.router.lifespan_context(app):
        gate = app.state.provider_authority
        if queued:
            existing = gate.submit("/api/chat", BODY, True)
            await existing.headers
        inbound = asyncio.Queue()
        await inbound.put({"type":"http.request", "body":b'{"model":"fixture","stream":true}', "more_body":False})
        sent = []
        async def send(message): sent.append(message)
        scope = {"type":"http", "asgi":{"version":"3.0", "spec_version":"2.3"}, "http_version":"1.1",
                 "method":"POST", "scheme":"http", "path":"/api/generate", "raw_path":b"/api/generate",
                 "query_string":b"", "root_path":"", "headers":[(b"authorization", f"Bearer {TOKEN}".encode())],
                 "client":("127.0.0.1", 1), "server":("127.0.0.1", 11435)}
        request = asyncio.create_task(app(scope, inbound.get, send))
        if queued:
            await until(lambda: len(gate.waiting) == 1)
        else:
            await until(lambda: any(m["type"] == "http.response.body" for m in sent))
        await inbound.put({"type":"http.disconnect"})
        await asyncio.wait_for(request, 1)
        assert gate.snapshot()["active"] == 1
        assert gate.snapshot()["waiting"] == 0
        assert len(calls) == 1
        if queued:
            assert gate.snapshot()["totals"]["cancelled_queued"] == 1
        else:
            assert gate.snapshot()["detached_active"] == 1
        release.set()
        await until(lambda: not gate.running)
        assert gate.uncertain == 0


def test_process_exit_preserves_intent_and_releases_authority_lock(tmp_path):
    directory = tmp_path / "crash"
    code = "from pathlib import Path; import os,sys; from topos.compute.journal import OwnershipJournal; j=OwnershipJournal(Path(sys.argv[1]), 'fixture'); j.begin('interrupted'); os._exit(0)"
    subprocess.run([sys.executable, "-c", code, str(directory)], check=True, timeout=10)
    journal = OwnershipJournal(directory, "fixture")
    try:
        assert journal.unresolved == 1
    finally:
        journal.close()


@pytest.mark.asyncio
async def test_storage_failure_never_dispatches_or_opens_next_slot(tmp_path, monkeypatch):
    async with authority(tmp_path, lambda _: pytest.fail("must not dispatch")) as gate:
        def fail(*args): raise OSError("PRIVATE_STORAGE_DETAIL")
        monkeypatch.setattr(gate.journal, "begin", fail)
        monkeypatch.setattr(gate.journal, "finish", fail)
        job = gate.submit("/api/chat", BODY, False)
        with pytest.raises(Rejected): await job.headers
        await until(lambda: not gate.running)
        assert gate.storage_failed
        assert gate.snapshot()["totals"]["dispatched"] == 0
        with pytest.raises(Rejected): gate.submit("/api/chat", BODY, False)


@pytest.mark.asyncio
async def test_shutdown_race_with_terminal_commit_finishes_cleanup(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    async with authority(tmp_path, lambda _: httpx.Response(200, content=DONE)) as gate:
        finish = gate.journal.finish
        def paused(*args):
            started.set()
            assert release.wait(3)
            finish(*args)
        monkeypatch.setattr(gate.journal, "finish", paused)
        job = gate.submit("/api/chat", BODY, False)
        await until(started.is_set)
        task = gate.running[job]
        task.cancel()
        release.set()
        await task
        assert not gate.running and gate.uncertain == 0 and job.response_done
        assert job.state == "terminal"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_oversized_response_quarantines_without_unbounded_buffer(tmp_path, stream):
    content = b'{"response":"' + b'x' * 3000 + b'","done":true}\n'
    async with authority(tmp_path, lambda _: httpx.Response(200, content=content), frame_bytes=1024) as gate:
        job = gate.submit("/api/chat", BODY, stream)
        await until(lambda: not gate.running)
        assert gate.uncertain == 1 and job.buffered == 0


@pytest.mark.asyncio
async def test_http_rejects_bad_input_management_and_redirects(tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(307, headers={"location":"http://remote.invalid"})
    app = create_app(upstream="http://127.0.0.1:11434", state_dir=tmp_path / "gateway", token=TOKEN,
                     transport=httpx.MockTransport(provider))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway",
                                     headers={"Authorization":f"Bearer {TOKEN}"}) as client:
            for content in (b"invalid", b"[]", b'{"model":"fixture","stream":"false"}'):
                assert (await client.post("/api/chat", content=content)).status_code == 400
            assert (await client.post("/api/chat", content=b'x'*1048577)).status_code == 413
            assert (await client.post("/api/pull", content=BODY)).status_code == 404
            assert not calls
            response = await client.post("/api/chat", content=BODY)
            assert response.status_code == 503
            assert len(calls) == 1
            assert app.state.provider_authority.uncertain == 1

"""Secrets never land in pipeline_jobs.payload_json.

What this guards: a job row outlives the request that created it. Before this
fix a Signal sync persisted the SQLCipher key the caller supplied
(``sync_options.signal_hex_key``), and every file import persisted
``progress_api_key`` — which defaults to the node's shared engine key — in
plain JSON, in a table the legacy inspection handlers served to non-owner
callers. A finished row kept both forever.

The executor still needs them, so they are held in process memory keyed by job
id and merged back just before the executor runs; the progress key falls back to
the node's own key, so a restart costs nothing there. A caller-supplied Signal
key does not survive a restart, and the failure says so.

Everything here runs on synthetic databases under ``tmp_path``.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.pipeline import job_runner, job_secrets
from topos.pipeline.job_store import claim_next_job, enqueue_job, get_job
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

# Synthetic canaries, not credentials: each is searched for in stored rows.
SIGNAL_KEY = "ab" * 32
ENGINE_KEY = "engine-key-canary-7f3a"
CP_SENT_KEY = "cp-sent-key-canary-91c2"  # gitleaks:allow — synthetic canary, never a real key


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "pipeline.db"
    seed = sqlite3.connect(str(path))
    apply_pipeline_jobs_v1_up(seed)
    seed.close()
    return path


@pytest.fixture
def conn_factory(db_path):
    conns: list[sqlite3.Connection] = []

    def _factory() -> sqlite3.Connection:
        c = sqlite3.connect(str(db_path), check_same_thread=False)
        c.row_factory = None
        conns.append(c)
        return c

    yield _factory
    for c in conns:
        c.close()


@pytest.fixture(autouse=True)
def empty_vault():
    job_secrets.clear()
    yield
    job_secrets.clear()


def _raw_rows(db_path) -> list[str]:
    c = sqlite3.connect(str(db_path))
    try:
        return [str(r[0]) for r in c.execute("SELECT payload_json FROM pipeline_jobs")]
    finally:
        c.close()


def _assert_no_secret_at_rest(db_path, *secrets: str) -> None:
    rows = _raw_rows(db_path)
    assert rows, "expected at least one job row"
    for raw in rows:
        for secret in secrets:
            assert secret not in raw, raw


# ---- the two doors that used to persist secrets ----------------------------


@pytest.mark.asyncio
async def test_a_signal_sync_key_is_not_written_to_the_job_row(monkeypatch, db_path, conn_factory):
    from topos.ingestion.local_sync_jobs import enqueue_local_sync

    monkeypatch.setattr(job_runner, "start_pipeline_worker", lambda *_a, **_k: None)
    out = await enqueue_local_sync(
        source_id="signal",
        dataset_id="ds-1",
        sync_options={"signal_hex_key": SIGNAL_KEY, "start_date": "2026-01-01"},
        conn_factory=conn_factory,
    )
    assert out["status"] == "ok"
    _assert_no_secret_at_rest(db_path, SIGNAL_KEY)
    stored = json.loads(_raw_rows(db_path)[0])
    # Non-secret options survive; the row records THAT a key was withheld.
    assert stored["sync_options"] == {"start_date": "2026-01-01"}
    assert stored["withheld_secrets"] == ["sync_options.signal_hex_key"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sent_key", [None, CP_SENT_KEY])
async def test_start_ingestion_does_not_write_the_progress_key(monkeypatch, db_path, conn_factory, sent_key):
    import topos.core.handlers as hub
    from topos.core.handlers import ingest as ingest_handlers
    from topos.config.settings import settings

    monkeypatch.setattr(settings, "topos_key", ENGINE_KEY, raising=False)
    monkeypatch.setattr(hub, "get_db_connection", conn_factory)
    monkeypatch.setattr(job_runner, "start_pipeline_worker", lambda *_a, **_k: None)
    payload = {"job_id": "job-1", "dataset_id": "ds-1", "schema_id": "chatgpt.conversation.v1", "file_path": "/nonexistent/x.jsonl"}
    if sent_key:
        payload["progress_api_key"] = sent_key
    result = await ingest_handlers.handle_start_ingestion({"id": "r1", "type": "start_ingestion", "payload": payload})
    assert result["status"] == "ok", result
    _assert_no_secret_at_rest(db_path, ENGINE_KEY, CP_SENT_KEY)


# ---- the store is the choke point ------------------------------------------


def test_enqueue_strips_secrets_for_any_caller(db_path, conn_factory):
    enqueue_job(
        conn_factory(),
        kind="file_ingestion",
        payload={"dataset_id": "ds", "progress_api_key": CP_SENT_KEY,
                 "sync_options": {"signal_hex_key": SIGNAL_KEY}},
        job_id="j1",
    )
    _assert_no_secret_at_rest(db_path, SIGNAL_KEY, CP_SENT_KEY)
    assert "progress_api_key" not in (get_job(conn_factory(), "j1") or {}).get("payload", {})


def test_a_failed_job_requeued_with_a_fresh_payload_stays_clean(db_path, conn_factory):
    c = conn_factory()
    enqueue_job(c, kind="file_ingestion", payload={"dataset_id": "ds"}, job_id="j1", idempotency_key="k")
    c.execute("UPDATE pipeline_jobs SET status='failed' WHERE job_id='j1'")
    c.commit()
    enqueue_job(c, kind="file_ingestion", payload={"dataset_id": "ds", "progress_api_key": CP_SENT_KEY},
                job_id="j2", idempotency_key="k")
    _assert_no_secret_at_rest(db_path, CP_SENT_KEY)
    assert job_secrets.peek("j1") == {"progress_api_key": CP_SENT_KEY}


# ---- the executor still gets what it needs ---------------------------------


@pytest.mark.asyncio
async def test_the_executor_receives_the_withheld_secrets(monkeypatch, db_path, conn_factory):
    seen: list[dict] = []

    async def _capture(payload):
        seen.append(dict(payload))
        return {"status": "ok"}

    monkeypatch.setitem(job_runner.EXECUTORS, "file_ingestion", _capture)
    enqueue_job(conn_factory(), kind="file_ingestion", job_id="j1",
                payload={"dataset_id": "ds", "progress_api_key": CP_SENT_KEY,
                         "sync_options": {"signal_hex_key": SIGNAL_KEY}})
    job = claim_next_job(conn_factory(), lease_owner="t", kinds=["file_ingestion"])
    await job_runner.process_job(conn_factory, job)

    assert seen[0]["progress_api_key"] == CP_SENT_KEY
    assert seen[0]["sync_options"]["signal_hex_key"] == SIGNAL_KEY
    # A finished job's secrets are dropped from memory too.
    assert job_secrets.peek("j1") == {}
    _assert_no_secret_at_rest(db_path, SIGNAL_KEY, CP_SENT_KEY)


@pytest.mark.asyncio
async def test_after_a_restart_the_progress_key_is_the_nodes_own(monkeypatch, conn_factory):
    from topos.config.settings import settings

    seen: list[dict] = []

    async def _capture(payload):
        seen.append(dict(payload))
        return {"status": "ok"}

    monkeypatch.setattr(settings, "topos_key", ENGINE_KEY, raising=False)
    monkeypatch.setitem(job_runner.EXECUTORS, "file_ingestion", _capture)
    enqueue_job(conn_factory(), kind="file_ingestion", job_id="j1",
                payload={"dataset_id": "ds", "progress_api_url": "https://cp.example",
                         "progress_api_key": CP_SENT_KEY})
    job_secrets.clear()  # the process that held it is gone
    job = claim_next_job(conn_factory(), lease_owner="t", kinds=["file_ingestion"])
    await job_runner.process_job(conn_factory, job)
    assert seen[0]["progress_api_key"] == ENGINE_KEY


@pytest.mark.asyncio
async def test_a_requeued_job_keeps_its_secrets_for_the_next_attempt(monkeypatch, conn_factory):
    async def _decline(_payload):
        return {"status": "requeue"}

    monkeypatch.setitem(job_runner.EXECUTORS, "file_ingestion", _decline)
    enqueue_job(conn_factory(), kind="file_ingestion", job_id="j1",
                payload={"dataset_id": "ds", "sync_options": {"signal_hex_key": SIGNAL_KEY}})
    job = claim_next_job(conn_factory(), lease_owner="t", kinds=["file_ingestion"])
    await job_runner.process_job(conn_factory, job)
    assert job_secrets.peek("j1") == {"sync_options.signal_hex_key": SIGNAL_KEY}


@pytest.mark.asyncio
async def test_a_lost_signal_key_is_named_in_the_failure(monkeypatch):
    """A restart drops a caller-supplied key. The sync may still succeed from
    Signal's own config or the keychain; when it does not, the error has to say
    the key must be supplied again rather than leave a bare decrypt error."""
    import topos.ingestion.local_sync as local_sync

    monkeypatch.setattr(local_sync, "run_signal_sync",
                        lambda *_a, **_k: {"status": "error", "error": "file is not a database"})
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: sqlite3.connect(":memory:"))
    monkeypatch.setattr("topos.storage.source_settings.update_sync_result", lambda *_a, **_k: None)
    result = await job_runner._execute_local_sync({
        "source_id": "signal", "dataset_id": "ds",
        "sync_options": {}, "withheld_secrets": ["sync_options.signal_hex_key"],
    })
    assert result["status"] == "error"
    assert "file is not a database" in result["error"]
    assert "Signal key" in result["error"] and "again" in result["error"]


# ---- rows written before this fix ------------------------------------------


def test_startup_lifts_secrets_out_of_rows_already_on_disk(db_path, conn_factory):
    c = conn_factory()
    legacy = [
        ("done-1", "done", {"dataset_id": "ds", "progress_api_key": ENGINE_KEY}),
        ("queued-1", "queued", {"dataset_id": "ds", "sync_options": {"signal_hex_key": SIGNAL_KEY, "x": 1}}),
        ("clean-1", "done", {"dataset_id": "ds"}),
    ]
    for job_id, status, payload in legacy:
        c.execute(
            "INSERT INTO pipeline_jobs (job_id, kind, status, payload_json, created_at, updated_at)"
            " VALUES (?, 'local_sync', ?, ?, datetime('now'), datetime('now'))",
            (job_id, status, json.dumps(payload)),
        )
    c.commit()

    job_runner.recover_pipeline_jobs(c)

    _assert_no_secret_at_rest(db_path, SIGNAL_KEY, ENGINE_KEY)
    # A job that will still run in THIS process keeps its key in memory;
    # a finished one's is simply dropped.
    assert job_secrets.peek("queued-1") == {"sync_options.signal_hex_key": SIGNAL_KEY}
    assert job_secrets.peek("done-1") == {}
    assert json.loads(_raw_rows(db_path)[1])["sync_options"] == {"x": 1}

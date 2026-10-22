"""The local-sync embeddings catch-up and the general lane never wait on each other.

Seen on one node on 22 Oct 2026: after the owner approved `embed-local-sync-messages`, its first 500-row batch
sat queued for over three hours. The general lane is serial and claims oldest first, and about 26 coding-session
derive jobs (local model, minutes each) were queued ahead of it. Each finished batch then queued the next at
the back, behind every job the hourly syncs had added meanwhile, so the ~196 batches would have taken days.

These run the production worker loops (``start_pipeline_worker``) over a synthetic node: real catch-up
discovery, real batches with a stubbed embedder, and derive jobs that finish only when the test says so.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from topos.ingestion import local_sync_catchup
from topos.ingestion.local_sync_catchup import KIND as CATCHUP, enqueue_catchup
from topos.pipeline import job_runner
from topos.pipeline.job_store import enqueue_job, get_job
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

GENERAL = "inbox_deferred_enrichment"
#: About 26 sat ahead of the first batch on that node; three show the shape.
AHEAD = 3
BATCHES = 4
BATCH_SIZE = 2
#: A coding-session derive job carries a full batch, so no two are coalesced into one turn of the lane.
FULL_BATCH = [{"message_id": f"r{i}"} for i in range(job_runner._COALESCE_MAX_RECORDS)]


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A node database with BATCHES * BATCH_SIZE unembedded iMessage rows; one connection per thread."""
    path = str(tmp_path / "node.db")
    setup = sqlite3.connect(path)
    setup.execute("PRAGMA journal_mode=WAL")
    apply_all_migrations(setup)
    ConversationsTablesManager(setup).ensure_tables()
    for i in range(BATCHES * BATCH_SIZE):
        setup.execute(
            "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_type, "
            "content, event_at, source_id) VALUES (?, 'thread-1', 'owner:default', 'user', ?, ?, 'imessage')",
            (f"m{i}", f"synthetic message {i}", f"2026-09-{i + 1:02d}T12:00:00Z"),
        )
    setup.commit()
    setup.close()

    local = threading.local()

    def connection() -> sqlite3.Connection:
        conn = getattr(local, "conn", None)
        if conn is None:
            conn = local.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        return conn

    async def embed(_engine, *, input_payload, **_kw):
        texts = input_payload["texts"]
        return SimpleNamespace(
            status="completed",
            output={"vectors": [[0.1, 0.2, 0.3] for _ in texts], "dims": 3, "model": "test-model"},
        )

    monkeypatch.setattr("topos.core.state.get_db_connection", connection)
    monkeypatch.setattr("topos.enrichment.jobs.canonical.embeddings_job.run_engine_task", embed)
    monkeypatch.setattr(local_sync_catchup, "BATCH_SIZE", BATCH_SIZE)
    monkeypatch.delenv("TOPOS_LOCAL_SYNC_CATCHUP", raising=False)
    monkeypatch.setenv("TOPOS_PIPELINE_WORKER", "on")
    for task in ("_worker_task", "_long_worker_task", "_catchup_worker_task"):
        monkeypatch.setattr(job_runner, task, None, raising=False)
    return connection


class Lanes:
    """The production lanes. A derive job runs until the test hands it a tick, then one more arrives."""

    def __init__(self, connection) -> None:
        self.connection = connection
        self.ticks: asyncio.Queue = asyncio.Queue()
        self.derives_started: list[str] = []
        self.arrivals = True

    def queue_derive(self) -> str:
        return enqueue_job(self.connection(), kind=GENERAL, source_id="codex_sessions",
                           payload={"canonical_records": FULL_BATCH})

    async def derive(self, payload: dict) -> dict:
        self.derives_started.append(str(len(self.derives_started)))
        await self.ticks.get()
        if self.arrivals:
            # The hourly coding syncs keep adding: one job arrives for every one that finishes.
            await asyncio.to_thread(self.queue_derive)
        return {"status": "ok", "messages_processed": len(payload["canonical_records"]), "records_created": {}}

    def catchup_jobs(self, status: str) -> int:
        return self.connection().execute(
            "SELECT COUNT(*) FROM pipeline_jobs WHERE kind=? AND status=?", (CATCHUP, status)).fetchone()[0]

    async def run(self, body, executors: dict | None = None):
        with patch.dict("topos.pipeline.job_runner.EXECUTORS", {GENERAL: self.derive, **(executors or {})}):
            job_runner.start_pipeline_worker(self.connection)
            try:
                return await body()
            finally:
                self.arrivals = False
                for _ in range(100):
                    self.ticks.put_nowait(None)
                await job_runner.stop_pipeline_worker()


async def until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(predicate):
            return True
        await asyncio.sleep(0.02)
    return bool(await asyncio.to_thread(predicate))


@pytest.mark.asyncio
async def test_catchup_runs_while_the_derive_jobs_queued_ahead_of_it_still_wait(node):
    lanes = Lanes(node)
    ahead = [lanes.queue_derive() for _ in range(AHEAD)]
    assert len(enqueue_catchup(node(), start=True)) == 1

    async def body():
        # The general lane holds the oldest derive job on the model and will not let it go.
        assert await until(lambda: lanes.derives_started == ["0"])
        finished = await until(lambda: lanes.catchup_jobs("done") == BATCHES)
        assert finished, (
            f"{lanes.catchup_jobs('done')} of {BATCHES} catch-up batches ran while {AHEAD} derive jobs "
            "queued ahead of the first one were still waiting"
        )
        # Not one derive job was let finish: the first is still on the model, the rest still queued.
        assert [get_job(node(), jid)["status"] for jid in ahead] == ["running", "queued", "queued"]
        assert lanes.derives_started == ["0"]

    await lanes.run(body)


@pytest.mark.asyncio
async def test_the_next_batch_never_queues_behind_jobs_that_arrived_during_the_last(node):
    lanes = Lanes(node)
    for _ in range(AHEAD):
        lanes.queue_derive()
    enqueue_catchup(node(), start=True)

    async def body():
        # Let the general lane finish exactly the jobs that were ahead of the first batch. Each one that
        # finishes is replaced by an arrival, so the lane is never empty, as on a node with hourly syncs.
        for tick in range(AHEAD):
            lanes.ticks.put_nowait(None)
            assert await until(lambda: len(lanes.derives_started) == tick + 2)
        # The lane now waits on a job that arrived meanwhile; nothing it holds can let a batch through.
        await until(lambda: lanes.catchup_jobs("done") == BATCHES)
        done = lanes.catchup_jobs("done")
        assert done == BATCHES, (
            f"{done} of {BATCHES} catch-up batches after the {AHEAD} jobs ahead of the first finished: "
            f"batch 2 was queued behind the {AHEAD} that arrived while batch 1 waited"
        )

    await lanes.run(body)


@pytest.mark.asyncio
async def test_a_slow_catchup_batch_never_holds_up_the_general_lane(node):
    lanes = Lanes(node)
    lanes.arrivals = False
    first = enqueue_catchup(node(), start=True)[0]
    behind = [lanes.queue_derive() for _ in range(AHEAD)]
    batch_started = asyncio.Event()
    release = asyncio.Event()

    async def slow_batch(_payload: dict) -> dict:
        # A batch on a loaded node: a cold model load and 500 rows on two CPU threads.
        batch_started.set()
        await release.wait()
        return {"status": "ok", "messages_processed": 0}

    async def body():
        try:
            await asyncio.wait_for(batch_started.wait(), timeout=10)
            for _ in range(AHEAD):
                lanes.ticks.put_nowait(None)
            drained = await until(lambda: all(get_job(node(), jid)["status"] == "done" for jid in behind))
            assert drained, "the general lane waited on an embeddings batch"
            assert get_job(node(), first)["status"] == "running"
        finally:
            release.set()

    await lanes.run(body, {CATCHUP: slow_batch})

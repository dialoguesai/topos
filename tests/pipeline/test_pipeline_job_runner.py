"""Tests for durable pipeline job queue."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from topos.pipeline.job_runner import process_pending_jobs_once, recover_pipeline_jobs
from topos.pipeline.job_store import (
    claim_matching_queued_jobs,
    claim_next_job,
    enqueue_job,
    get_job,
    is_derivation_complete,
    recover_stale_jobs,
)
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "pipeline.db"))
    apply_pipeline_jobs_v1_up(db)
    yield db
    db.close()


def test_enqueue_is_idempotent_by_key(conn: sqlite3.Connection) -> None:
    first = enqueue_job(
        conn,
        kind="file_ingestion",
        payload={"job_id": "j1"},
        job_id="j1",
        idempotency_key="file_ingestion:j1",
    )
    second = enqueue_job(
        conn,
        kind="file_ingestion",
        payload={"job_id": "j1"},
        job_id="j1",
        idempotency_key="file_ingestion:j1",
    )
    assert first == second
    rows = conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0]
    assert rows == 1


def test_interrupted_running_job_is_retried(conn: sqlite3.Connection) -> None:
    enqueue_job(conn, kind="file_ingestion", payload={"job_id": "j2"}, job_id="j2")
    claimed = claim_next_job(conn, lease_owner="worker-a")
    assert claimed is not None
    assert claimed["status"] == "running"

    conn.execute(
        "UPDATE pipeline_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id='j2'"
    )
    conn.commit()

    assert recover_stale_jobs(conn) == 1
    job = get_job(conn, "j2")
    assert job is not None
    assert job["status"] == "queued"


def _enqueue_inbox_job(conn: sqlite3.Connection, *, idx: int, source_id: str) -> str:
    return enqueue_job(
        conn,
        kind="inbox_deferred_enrichment",
        payload={
            "source_id": source_id,
            "sync_batch_id": f"batch-{idx}",
            "canonical_records": [{"record_id": f"r{idx}", "source_id": source_id}],
            "write_id": f"w{idx}",
        },
        job_id=f"inbox-{idx}",
        source_id=source_id,
        write_id=f"w{idx}",
        sync_batch_id=f"batch-{idx}",
        idempotency_key=f"inbox_derivation:w{idx}",
    )


def test_claim_matching_queued_jobs_scopes_by_source(conn: sqlite3.Connection) -> None:
    for idx in range(3):
        _enqueue_inbox_job(conn, idx=idx, source_id="browser_visits")
    _enqueue_inbox_job(conn, idx=9, source_id="imessage")

    claimed = claim_matching_queued_jobs(
        conn,
        lease_owner="worker-a",
        kind="inbox_deferred_enrichment",
        source_id="browser_visits",
    )
    assert {j["job_id"] for j in claimed} == {"inbox-0", "inbox-1", "inbox-2"}
    assert all(j["status"] == "running" for j in claimed)
    assert get_job(conn, "inbox-9")["status"] == "queued"


@pytest.mark.asyncio
async def test_inbox_backlog_coalesces_into_one_batch(conn: sqlite3.Connection) -> None:
    calls: list[dict] = []

    async def _exec(payload: dict) -> dict:
        calls.append(payload)
        return {
            "status": "ok",
            "messages_processed": len(payload.get("canonical_records") or []),
            "records_created": {},
        }

    for idx in range(3):
        _enqueue_inbox_job(conn, idx=idx, source_id="browser_visits")
    _enqueue_inbox_job(conn, idx=9, source_id="imessage")

    with patch.dict(
        "topos.pipeline.job_runner.EXECUTORS",
        {"inbox_deferred_enrichment": _exec},
    ):
        processed = await process_pending_jobs_once(lambda: conn, limit=10)

    # Two claims total: the browser backlog coalesced into one executor call,
    # the imessage job ran on its own.
    assert processed == 2
    assert len(calls) == 2
    by_size = sorted(calls, key=lambda p: len(p["canonical_records"]), reverse=True)
    assert sorted(r["record_id"] for r in by_size[0]["canonical_records"]) == ["r0", "r1", "r2"]
    assert [r["record_id"] for r in by_size[1]["canonical_records"]] == ["r9"]

    for job_id in ("inbox-0", "inbox-1", "inbox-2", "inbox-9"):
        assert get_job(conn, job_id)["status"] == "done"
    # Every coalesced delivery gets its derivation-completion receipt.
    for write_id in ("w0", "w1", "w2", "w9"):
        assert is_derivation_complete(conn, write_id)


@pytest.mark.asyncio
async def test_coalesced_jobs_all_fail_together(conn: sqlite3.Connection) -> None:
    async def _exec(_payload: dict) -> dict:
        return {"status": "error", "error": "boom"}

    for idx in range(2):
        _enqueue_inbox_job(conn, idx=idx, source_id="browser_visits")

    with patch.dict(
        "topos.pipeline.job_runner.EXECUTORS",
        {"inbox_deferred_enrichment": _exec},
    ):
        processed = await process_pending_jobs_once(lambda: conn, limit=10)

    assert processed == 1
    for job_id in ("inbox-0", "inbox-1"):
        assert get_job(conn, job_id)["status"] == "failed"
    assert not is_derivation_complete(conn, "w0")
    assert not is_derivation_complete(conn, "w1")


@pytest.mark.asyncio
async def test_process_pending_jobs_once(conn: sqlite3.Connection) -> None:
    seen: list[str] = []

    async def _exec(payload: dict) -> dict:
        seen.append(str(payload.get("marker")))
        return {"status": "ok", "messages_processed": 1, "records_created": {}}

    import topos.pipeline.job_runner as runner

    runner.EXECUTORS["file_ingestion"] = _exec
    enqueue_job(
        conn,
        kind="file_ingestion",
        payload={"marker": "done"},
        job_id="job-process",
    )
    processed = await process_pending_jobs_once(lambda: conn, limit=5)
    assert processed == 1
    assert seen == ["done"]
    job = get_job(conn, "job-process")
    assert job is not None
    assert job["status"] == "done"

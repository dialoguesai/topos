"""R-N154 L3 (the reviewer's test): a park that lands while the worker's claim is in its thread hands the job back."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

from topos import node_stop
from topos.pipeline import job_runner, job_store
from topos.pipeline.job_store import enqueue_job, get_job, recover_stale_jobs
from topos.runtime_shutdown import begin_runtime, current_generation
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up


@pytest.fixture(autouse=True)
def _fresh_run(monkeypatch):
    from topos.features.entities import rebuild_subprocess
    rebuild_subprocess.allow_rebuild_children()
    monkeypatch.setenv("TOPOS_SHUTDOWN_EXIT_DEADLINE_SECONDS", "0")
    monkeypatch.setattr(node_stop, "_stop_begun", threading.Event())
    begin_runtime("test")
    yield
    begin_runtime("test")


@pytest.mark.asyncio
async def test_park_during_the_claim_leaves_the_job_running_on_its_lease(tmp_path):
    db = sqlite3.connect(str(tmp_path / "pipeline.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(db)
    ran = []

    async def _exec(payload):
        ran.append(1)
        return {"status": "ok", "messages_processed": 1, "records_created": {}}

    real_claim = job_runner.claim_next_job
    in_claim = threading.Event()

    def slow_claim(*a, **kw):
        if "file_ingestion" not in (kw.get("kinds") or []):
            return None
        in_claim.set()
        time.sleep(0.5)                       # the claim is in its thread when the park cancels the loop
        return real_claim(*a, **kw)

    enqueue_job(db, kind="file_ingestion", payload={}, job_id="mid-claim")
    with patch.dict("topos.pipeline.job_runner.EXECUTORS", {"file_ingestion": _exec}), \
            patch.object(job_runner, "_long_running_kinds", lambda: ["no-such-kind"]), \
            patch.object(job_runner, "claim_next_job", slow_claim):
        job_runner.start_pipeline_worker(lambda: db)
        while not in_claim.is_set():
            await asyncio.sleep(0.01)
        await job_runner.park_pipeline_worker(current_generation())
        await asyncio.sleep(0.8)              # the claim thread lands its UPDATE after the cancel
    job = get_job(db, "mid-claim")
    assert job["status"] == "queued" and job["lease_owner"] is None, "the park left the claimed job on its lease"
    begin_runtime("next start")
    recover_stale_jobs(db)
    job = get_job(db, "mid-claim")
    assert ran == []
    assert job["status"] == "queued", "a job claimed as the park landed waits out its lease (300 s / 1800 s)"


@pytest.mark.asyncio
async def test_a_claim_that_finished_before_the_cancel_reached_the_loop_is_handed_back(tmp_path, monkeypatch):
    """The other order: the claim's thread returned the row, and the cancel lands before the loop resumes."""
    db = sqlite3.connect(str(tmp_path / "pipeline.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(db)
    real_to_thread = asyncio.to_thread
    claimed = asyncio.Event()

    async def to_thread(func, *a, **kw):
        if getattr(func, "__name__", "") == "_claim":
            result = func(*a, **kw)              # the row is claimed...
            if result is None:
                await asyncio.sleep(0.05)
                return None
            claimed.set()
            await asyncio.Event().wait()         # ...and the cancel arrives before the loop takes the result
            return result  # pragma: no cover
        return await real_to_thread(func, *a, **kw)
    monkeypatch.setattr(asyncio, "to_thread", to_thread)

    enqueue_job(db, kind="file_ingestion", payload={}, job_id="after-claim")
    with patch.dict("topos.pipeline.job_runner.EXECUTORS", {"file_ingestion": lambda p: None}), \
            patch.object(job_runner, "_long_running_kinds", lambda: ["no-such-kind"]):
        job_runner.start_pipeline_worker(lambda: db)
        await asyncio.wait_for(claimed.wait(), timeout=10)
        assert get_job(db, "after-claim")["status"] == "running"
        await job_runner.park_pipeline_worker(current_generation())
    job = get_job(db, "after-claim")
    assert job["status"] == "queued" and job["lease_owner"] is None, job

"""A coding-session backlog must reach the models in bounded, resumable batches.

On 2026-10-19 a full Claude Code backfill queued 17 deferred-enrichment jobs of
~870 records each. The runner coalesced every one of them into a single
14,823-record derive batch: one executor call that commits nothing until it
ends. It ran ~19 hours, a node restart threw it away, and the next start
coalesced the same 17 again. Zero records were processed in a day, and the
1.5.13 recovery path never ran because it waits for those jobs to finish.

The guards below pin three rules:
1. coalescing stops at a record cap, not only a job count;
2. a coding sync queues its post-canonical work in cap-sized jobs;
3. an oversized coding job already in the queue (written by 1.5.10–1.5.13) is
   split into cap-sized jobs before any model work, so an upgraded node heals
   without a migration.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from topos.pipeline.job_runner import process_pending_jobs_once
from topos.pipeline.job_store import enqueue_job, get_job
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

CAP = 128  # the literal, so a silent change of the constant fails here


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "pipeline.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(db)
    yield db


def _records(prefix: str, n: int, source_id: str) -> list[dict]:
    return [{"record_id": f"{prefix}-{i}", "message_id": f"{prefix}-{i}", "source_id": source_id}
            for i in range(n)]


def _enqueue(conn, *, job_id: str, source_id: str, n: int, write_id: str | None = None) -> str:
    return enqueue_job(
        conn,
        kind="inbox_deferred_enrichment",
        payload={"source_id": source_id, "sync_batch_id": f"{job_id}-batch",
                 "canonical_records": _records(job_id, n, source_id)},
        job_id=job_id,
        source_id=source_id,
        write_id=write_id,
        sync_batch_id=f"{job_id}-batch",
        idempotency_key=f"test:{job_id}",
    )


async def _drain(conn) -> list[dict]:
    calls: list[dict] = []

    async def _exec(payload: dict) -> dict:
        calls.append(payload)
        return {"status": "ok", "messages_processed": len(payload.get("canonical_records") or []),
                "records_created": {}}

    with patch.dict("topos.pipeline.job_runner.EXECUTORS", {"inbox_deferred_enrichment": _exec}), \
         patch("topos.pipeline.job_runner._enqueue_ai_tool_catchup", lambda *a, **k: None):
        for _ in range(50):
            if not await process_pending_jobs_once(lambda: conn, limit=50):
                break
    return calls


def _ids(calls: list[dict]) -> list[str]:
    return [r["record_id"] for call in calls for r in call.get("canonical_records") or []]


@pytest.mark.asyncio
async def test_coalescing_stops_at_the_record_cap(conn) -> None:
    for idx in range(5):
        _enqueue(conn, job_id=f"c{idx}", source_id="claude_code_sessions", n=60)

    calls = await _drain(conn)

    assert max(len(c["canonical_records"]) for c in calls) <= CAP
    assert sorted(_ids(calls)) == sorted(f"c{i}-{j}" for i in range(5) for j in range(60))
    assert len(_ids(calls)) == 300  # every record exactly once
    for idx in range(5):
        assert get_job(conn, f"c{idx}")["status"] == "done"


@pytest.mark.asyncio
async def test_small_deliveries_still_coalesce_into_one_batch(conn) -> None:
    # Pin: the cap must not undo the reason coalescing exists (many one-record
    # inbox deliveries become one derive batch).
    for idx in range(40):
        _enqueue(conn, job_id=f"b{idx}", source_id="browser_visits", n=1)

    calls = await _drain(conn)

    assert len(calls) == 1
    assert len(calls[0]["canonical_records"]) == 40


@pytest.mark.asyncio
async def test_oversized_coding_job_is_split_before_any_model_work(conn) -> None:
    _enqueue(conn, job_id="legacy", source_id="claude_code_sessions", n=300)

    calls = await _drain(conn)

    assert [len(c["canonical_records"]) for c in calls] == [128, 128, 44]
    assert sorted(_ids(calls)) == sorted(f"legacy-{j}" for j in range(300))
    parent = get_job(conn, "legacy")
    assert parent["status"] == "done"
    assert parent["detail"]["split_into"] and len(parent["detail"]["split_into"]) == 3
    children = conn.execute(
        "SELECT status, payload_json FROM pipeline_jobs WHERE job_id != 'legacy'"
    ).fetchall()
    assert len(children) == 3
    assert {status for status, _ in children} == {"done"}
    assert all(json.loads(p)["source_id"] == "claude_code_sessions" for _, p in children)


@pytest.mark.asyncio
async def test_split_is_idempotent_when_the_parent_is_claimed_again(conn) -> None:
    # A node stopping right after the split must not queue the children twice.
    from topos.pipeline.job_runner import _split_oversized_inbox_job

    _enqueue(conn, job_id="legacy", source_id="codex_sessions", n=200)
    job = get_job(conn, "legacy")
    first = _split_oversized_inbox_job(conn, job, dict(job["payload"]))
    second = _split_oversized_inbox_job(conn, job, dict(job["payload"]))

    assert first == second
    assert len(first) == 2
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_non_coding_oversized_job_is_not_split(conn) -> None:
    # Pin: the split is scoped to the coding-session sources. Other sources keep
    # their batch as queued; only the merge cap applies to them.
    _enqueue(conn, job_id="big", source_id="chatgpt_ui_conversation", n=300)

    calls = await _drain(conn)

    assert [len(c["canonical_records"]) for c in calls] == [300]
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_job_with_a_write_receipt_is_not_split(conn) -> None:
    # A write_id carries a derivation-completion receipt for exactly one job;
    # splitting it would complete the receipt before its records ran.
    _enqueue(conn, job_id="receipt", source_id="claude_code_sessions", n=300, write_id="w-1")

    calls = await _drain(conn)

    assert [len(c["canonical_records"]) for c in calls] == [300]


def test_coding_sync_queues_cap_sized_jobs(conn) -> None:
    from topos.ingestion.ai_tool_sync import _post_canonical
    from topos.sources.registry import REGISTRY

    records = _records("s", 300, "claude_code_sessions")
    first = _post_canonical(source_def=REGISTRY["claude_code_sessions"], canonical_records=records,
                            sync_batch_id="claude_code_sessions:abc", db_conn=conn)
    again = _post_canonical(source_def=REGISTRY["claude_code_sessions"], canonical_records=records,
                            sync_batch_id="claude_code_sessions:abc", db_conn=conn)

    rows = conn.execute(
        "SELECT payload_json FROM pipeline_jobs WHERE kind='inbox_deferred_enrichment' ORDER BY created_at, job_id"
    ).fetchall()
    sizes = sorted((len(json.loads(p)["canonical_records"]) for (p,) in rows), reverse=True)
    assert sizes == [128, 128, 44]
    assert first["status"] == "queued" and len(first["job_ids"]) == 3
    assert again["job_ids"] == first["job_ids"]  # re-delivery queues nothing new

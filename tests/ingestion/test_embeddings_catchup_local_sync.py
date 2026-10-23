"""Messages a local sync stored before it embedded them are drained in bounded, resumable batches."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from topos.enrichment.models.mvp_defaults import job_spec_version
from topos.enrichment.record_progress import mark_records_processed
from topos.ingestion import embeddings_catchup
from topos.ingestion.embeddings_catchup import enqueue_catchup, execute_catchup, processing_status
from topos.pipeline.job_store import claim_next_job, enqueue_job, get_job
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

SPEC = job_spec_version("embeddings")
KIND = embeddings_catchup.LOCAL_SYNC.kind


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "catchup.db"), check_same_thread=False)
    apply_all_migrations(c)
    ConversationsTablesManager(c).ensure_tables()
    for i in range(5):
        c.execute(
            "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_type, "
            "content, event_at, source_id) VALUES (?, 'thread-1', 'owner:default', 'user', ?, ?, 'imessage')",
            (f"m{i}", f"synthetic message {i}", f"2026-09-0{i + 1}T12:00:00Z"),
        )
    c.commit()
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: c)
    monkeypatch.delenv("TOPOS_LOCAL_SYNC_CATCHUP", raising=False)
    yield c
    c.close()


@pytest.fixture
def model_calls(monkeypatch):
    calls = []

    async def run(_engine, *, input_payload, record_ids, **_kw):
        calls.append(list(record_ids))
        texts = input_payload["texts"]
        return SimpleNamespace(
            status="completed",
            output={"vectors": [[0.1, 0.2, 0.3] for _ in texts], "dims": 3, "model": "test-model"},
        )

    monkeypatch.setattr("topos.enrichment.jobs.canonical.embeddings_job.run_engine_task", run)
    return calls


def payload(conn, job_id):
    return json.loads(conn.execute("SELECT payload_json FROM pipeline_jobs WHERE job_id=?", (job_id,)).fetchone()[0])


def mark(conn, ids, spec=SPEC):
    mark_records_processed(conn, "imessage", "embeddings", [{"message_id": rid} for rid in ids], spec)


def test_owed_rows_are_queued_once_newest_first_as_ids_only(conn, monkeypatch):
    monkeypatch.setattr(embeddings_catchup, "BATCH_SIZE", 2)
    mark(conn, ["m4"])
    ids = enqueue_catchup(conn, start="local_sync_embeddings")
    assert len(ids) == 1
    body = payload(conn, ids[0])
    assert body["record_ids"] == ["m3", "m2"]
    assert "synthetic" not in json.dumps(body)
    # One batch in flight per source.
    assert enqueue_catchup(conn) == []


def test_a_marker_from_an_older_spec_is_still_owed(conn):
    mark(conn, ["m0", "m1", "m2", "m3", "m4"], spec=SPEC - 1)
    assert processing_status(conn, "imessage")["pending_records"] == 5
    mark(conn, ["m0", "m1", "m2", "m3", "m4"])
    assert processing_status(conn, "imessage") == {
        "source_id": "imessage", "job_id": "embeddings", "eligible_records": 5,
        "processed_records": 5, "pending_records": 0}
    assert enqueue_catchup(conn, start="local_sync_embeddings") == []


def test_a_running_sync_goes_first(conn):
    enqueue_job(conn, kind="local_sync", source_id="imessage", payload={})
    assert enqueue_catchup(conn, start="local_sync_embeddings") == []


def test_installing_the_build_alone_queues_nothing(conn):
    # Worker start and finished syncs continue a catch-up; only the owner starts one.
    assert enqueue_catchup(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 0


def test_owner_detach_and_the_switch_both_stop_discovery(conn, monkeypatch):
    monkeypatch.setenv("TOPOS_LOCAL_SYNC_CATCHUP", "off")
    assert enqueue_catchup(conn, start="local_sync_embeddings") == []
    monkeypatch.delenv("TOPOS_LOCAL_SYNC_CATCHUP")
    from topos.enrichment.source_overrides import set_source_enrichment_override

    set_source_enrichment_override("imessage", "embeddings", False, conn=conn)
    assert enqueue_catchup(conn, start="local_sync_embeddings") == []


@pytest.mark.asyncio
async def test_batches_embed_mark_and_resume_until_nothing_is_owed(conn, model_calls, monkeypatch):
    monkeypatch.setattr(embeddings_catchup, "BATCH_SIZE", 2)
    seen = []
    while True:
        # Started once; every later batch is queued by discovery alone.
        queued = enqueue_catchup(conn, start="local_sync_embeddings" if not seen else None)
        if not queued:
            break
        result = await execute_catchup(payload(conn, queued[0]))
        assert result["status"] == "ok", result
        conn.execute("UPDATE pipeline_jobs SET status='done' WHERE job_id=?", (queued[0],))
        conn.commit()
        seen.append(payload(conn, queued[0])["record_ids"])
    assert seen == [["m4", "m3"], ["m2", "m1"], ["m0"]]
    embedded = {r[0] for r in conn.execute(
        "SELECT record_id FROM signal_embeddings WHERE source_id='imessage' AND record_type='conversation_message'")}
    assert embedded == {"m0", "m1", "m2", "m3", "m4"}
    assert processing_status(conn, "imessage")["pending_records"] == 0


@pytest.mark.asyncio
async def test_a_resumed_batch_skips_rows_marked_before_the_restart(conn, model_calls):
    queued = enqueue_catchup(conn, start="local_sync_embeddings")
    body = payload(conn, queued[0])
    mark(conn, ["m4", "m3"])  # committed by the run a stop interrupted
    result = await execute_catchup(body)
    assert result == {**result, "status": "ok", "messages_processed": 3}
    assert sorted(rid for call in model_calls for rid in call) == ["m0", "m1", "m2"]


@pytest.mark.asyncio
async def test_detached_after_queueing_reads_nothing(conn, model_calls):
    from topos.enrichment.source_overrides import set_source_enrichment_override

    body = payload(conn, enqueue_catchup(conn, start="local_sync_embeddings")[0])
    set_source_enrichment_override("imessage", "embeddings", False, conn=conn)
    result = await execute_catchup(body)
    assert result["status"] == "ok" and "skipped" in result
    assert model_calls == []


@pytest.mark.asyncio
async def test_a_failed_model_batch_fails_the_job_and_stays_owed(conn, monkeypatch):
    async def failed(_engine, **_kw):
        return SimpleNamespace(status="failed", output={})

    monkeypatch.setattr("topos.enrichment.jobs.canonical.embeddings_job.run_engine_task", failed)
    result = await execute_catchup(payload(conn, enqueue_catchup(conn, start="local_sync_embeddings")[0]))
    assert result["status"] == "error"
    assert processing_status(conn, "imessage")["pending_records"] == 5


@pytest.mark.asyncio
async def test_done_means_marked(conn, model_calls, monkeypatch):
    monkeypatch.setattr("topos.enrichment.orchestrator.EnrichmentOrchestrator._mark_processed",
                        lambda self, job_name, messages: None)
    result = await execute_catchup(payload(conn, enqueue_catchup(conn, start="local_sync_embeddings")[0]))
    assert result["status"] == "error"
    assert "without a progress marker" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"source_id": "codex_sessions", "record_ids": ["m0"]},
                                 {"source_id": "imessage", "record_ids": []}])
async def test_refuses_a_payload_it_did_not_write(conn, model_calls, bad):
    assert (await execute_catchup(bad))["status"] == "error"
    assert model_calls == []


@pytest.mark.asyncio
async def test_the_switch_pauses_a_queued_batch_without_work(conn, model_calls, monkeypatch):
    body = payload(conn, enqueue_catchup(conn, start="local_sync_embeddings")[0])
    monkeypatch.setenv("TOPOS_LOCAL_SYNC_CATCHUP", "off")
    result = await execute_catchup(body)
    assert result["status"] == "ok" and "skipped" in result
    assert model_calls == []


def test_the_upgrade_target_starts_it_and_reports_counts_only(conn):
    from topos.upgrades.runner import _exec_derived_rebuild

    detail = _exec_derived_rebuild({"params": {"targets": ["local_sync_embeddings"]}}, conn)
    assert detail["targets"]["local_sync_embeddings"] == {
        "queued_batches": 1, "pending_records": {"imessage": 5, "signal": 0}}
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs WHERE kind=?", (KIND,)).fetchone()[0] == 1


async def _finish(conn, kind, job_id):
    from topos.pipeline.job_runner import process_job

    async def _exec(_payload):
        return {"status": "ok"}

    enqueue_job(conn, kind=kind, source_id="imessage", payload={}, job_id=job_id)
    claimed = claim_next_job(conn, lease_owner="worker-a", kinds=[kind])
    with patch.dict("topos.pipeline.job_runner.EXECUTORS", {kind: _exec}):
        await process_job(lambda: conn, claimed)
    assert get_job(conn, job_id)["status"] == "done"
    return [row[0] for row in conn.execute(
        "SELECT job_id FROM pipeline_jobs WHERE kind=? AND status='queued'", (KIND,))]


@pytest.mark.asyncio
async def test_a_finished_batch_queues_the_next(conn):
    nxt = await _finish(conn, KIND, "first")
    assert len(nxt) == 1
    assert payload(conn, nxt[0])["record_ids"] == ["m4", "m3", "m2", "m1", "m0"]


@pytest.mark.asyncio
async def test_a_finished_sync_continues_only_a_started_catchup(conn):
    assert await _finish(conn, "local_sync", "sync-1") == []
    enqueue_job(conn, kind=KIND, source_id="imessage", payload={}, job_id="started")
    conn.execute("UPDATE pipeline_jobs SET status='done' WHERE job_id='started'")
    conn.commit()
    nxt = await _finish(conn, "local_sync", "sync-2")
    assert len(nxt) == 1


def test_a_release_declares_the_step_and_asks_the_owner_first():
    """The target is only reachable if a release declares it. Searched across releases:
    cut_release.py moves the staging entry's steps into the stamped version."""
    from pathlib import Path

    import topos

    data = json.loads((Path(topos.__file__).parent / "upgrades" / "manifests.json").read_text())
    steps = [s for release in data["releases"] for s in release.get("steps", [])
             if s.get("id") == "embed-local-sync-messages"]
    assert len(steps) == 1
    assert steps[0]["kind"] == "derived_rebuild"
    assert steps[0]["params"]["targets"] == ["local_sync_embeddings"]
    assert steps[0]["consent"] == "prompt", "a ~100k-row embedding pass of the owner's messages is the owner's call"

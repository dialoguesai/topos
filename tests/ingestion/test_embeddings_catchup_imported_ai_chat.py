"""Imported AI-chat rows an import stored but never embedded are drained in bounded, resumable batches.

An export import the owner stopped mid-run keeps the canonical rows it wrote; their derivation never ran, so they
have no vector and no ``embeddings`` marker. Rows already in the vector index are left alone even when their only
witness is an older-spec marker (or none): re-embedding them would change nothing in the index.
"""
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
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.db.migrations import apply_all_migrations

SPEC = job_spec_version("embeddings")
EXPORT = "chatgpt_file_ingestion"
CAPTURE = "chatgpt_ui_conversation"
KIND = "imported_ai_chat_embeddings_catchup"
TARGET = "imported_ai_chat_embeddings"
SWITCH = "TOPOS_IMPORTED_AI_CHAT_CATCHUP"


def _row(c, rid, source, day, *, sender="human", excluded=0):
    c.execute(
        "INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, source_id, "
        "share_excluded) VALUES (?, 'chat-1', ?, ?, ?, ?, ?)",
        (rid, sender, f"2026-09-{day:02d}T12:00:00Z", f"synthetic imported text {rid}", source, excluded),
    )


def _vector(c, rid, source):
    c.execute(
        "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, model, dims, vector_blob, chunk_index) "
        "VALUES (?, ?, ?, 'test-model', 3, X'00', 0)", (f"emb-{rid}", rid, source),
    )


def mark(c, ids, source=EXPORT, spec=SPEC):
    mark_records_processed(c, source, "embeddings", [{"message_id": rid} for rid in ids], spec)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "imported.db"), check_same_thread=False)
    apply_all_migrations(c)
    CanonicalTablesManager(c)
    for day in range(1, 5):
        _row(c, f"f{day}", EXPORT, day, sender="human" if day % 2 else "assistant")
    _row(c, "kept", EXPORT, 5, excluded=1)            # the owner keeps it from every share
    _row(c, "indexed", EXPORT, 6)                     # vector from the import, spec-1 witness
    _vector(c, "indexed", EXPORT)
    _row(c, "indexed-unmarked", EXPORT, 7)            # vector, no witness at all
    _vector(c, "indexed-unmarked", EXPORT)
    _row(c, "ran-empty", EXPORT, 8)                   # ran at the current spec, legitimately no vector
    _row(c, "u1", CAPTURE, 3)
    _row(c, "u-shared", CAPTURE, 4)                   # its vector was written under the export's source id
    _vector(c, "u-shared", EXPORT)
    c.commit()
    mark(c, ["indexed"], spec=SPEC - 1)
    mark(c, ["ran-empty"])
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: c)
    monkeypatch.delenv(SWITCH, raising=False)
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


def queued_by_source(conn):
    rows = conn.execute("SELECT source_id, job_id FROM pipeline_jobs WHERE kind=? AND status='queued'", (KIND,))
    return {sid: payload(conn, jid)["record_ids"] for sid, jid in rows.fetchall()}


def test_owed_means_shareable_with_no_vector_and_no_current_marker(conn):
    assert len(enqueue_catchup(conn, start=TARGET)) == 2
    assert queued_by_source(conn) == {EXPORT: ["f4", "f3", "f2", "f1"], CAPTURE: ["u1"]}
    assert "synthetic" not in json.dumps(queued_by_source(conn))
    # One batch in flight per source.
    assert enqueue_catchup(conn) == []


def test_status_is_counts_of_shareable_rows_only(conn):
    assert processing_status(conn, EXPORT) == {
        "source_id": EXPORT, "job_id": "embeddings", "eligible_records": 7,
        "processed_records": 3, "pending_records": 4}
    assert processing_status(conn, CAPTURE)["pending_records"] == 1


def test_installing_the_build_alone_queues_nothing(conn):
    assert enqueue_catchup(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 0


def test_starting_one_lane_never_starts_the_other(conn):
    enqueue_catchup(conn, start="local_sync_embeddings")
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 0
    enqueue_catchup(conn, start=TARGET)
    kinds = {row[0] for row in conn.execute("SELECT kind FROM pipeline_jobs")}
    assert kinds == {KIND}


@pytest.mark.parametrize("ingest", ["file_ingestion", "enrichment_process_source", "inbox_deferred_enrichment"])
def test_an_import_in_flight_goes_first(conn, ingest):
    enqueue_job(conn, kind=ingest, source_id=EXPORT, payload={})
    enqueue_catchup(conn, start=TARGET)
    assert set(queued_by_source(conn)) == {CAPTURE}


def test_owner_detach_and_the_switch_both_stop_discovery(conn, monkeypatch):
    monkeypatch.setenv(SWITCH, "off")
    assert enqueue_catchup(conn, start=TARGET) == []
    monkeypatch.delenv(SWITCH)
    from topos.enrichment.source_overrides import set_source_enrichment_override

    set_source_enrichment_override(EXPORT, "embeddings", False, conn=conn)
    enqueue_catchup(conn, start=TARGET)
    assert set(queued_by_source(conn)) == {CAPTURE}


@pytest.mark.asyncio
async def test_batches_embed_mark_and_resume_until_nothing_is_owed(conn, model_calls, monkeypatch):
    monkeypatch.setattr(embeddings_catchup, "BATCH_SIZE", 2)
    seen = []
    while True:
        queued = enqueue_catchup(conn, EXPORT, start=TARGET if not seen else None)
        if not queued:
            break
        result = await execute_catchup(payload(conn, queued[0]))
        assert result["status"] == "ok", result
        conn.execute("UPDATE pipeline_jobs SET status='done' WHERE job_id=?", (queued[0],))
        conn.commit()
        seen.append(payload(conn, queued[0])["record_ids"])
    assert seen == [["f4", "f3"], ["f2", "f1"]]
    embedded = {r[0] for r in conn.execute(
        "SELECT record_id FROM signal_embeddings WHERE source_id=? AND record_type='ai_chat_message'", (EXPORT,))}
    assert embedded == {"f1", "f2", "f3", "f4"}
    assert sorted(rid for call in model_calls for rid in call) == ["f1", "f2", "f3", "f4"]
    assert processing_status(conn, EXPORT)["pending_records"] == 0


@pytest.mark.asyncio
async def test_a_resumed_batch_skips_rows_marked_or_embedded_before_the_restart(conn, model_calls):
    body = payload(conn, enqueue_catchup(conn, EXPORT, start=TARGET)[0])
    mark(conn, ["f4"])          # committed by the run a stop interrupted
    _vector(conn, "f3", EXPORT)  # embedded by an import that ran meanwhile
    conn.commit()
    result = await execute_catchup(body)
    assert result == {**result, "status": "ok", "messages_processed": 2}
    assert sorted(rid for call in model_calls for rid in call) == ["f1", "f2"]


@pytest.mark.asyncio
async def test_a_row_flagged_after_queueing_is_never_read(conn, model_calls):
    body = payload(conn, enqueue_catchup(conn, EXPORT, start=TARGET)[0])
    conn.execute("UPDATE ai_chat_messages SET share_excluded=1 WHERE message_id='f2'")
    conn.commit()
    result = await execute_catchup(body)
    assert result["status"] == "ok", result
    assert sorted(rid for call in model_calls for rid in call) == ["f1", "f3", "f4"]


@pytest.mark.asyncio
async def test_detached_after_queueing_reads_nothing(conn, model_calls):
    from topos.enrichment.source_overrides import set_source_enrichment_override

    body = payload(conn, enqueue_catchup(conn, EXPORT, start=TARGET)[0])
    set_source_enrichment_override(EXPORT, "embeddings", False, conn=conn)
    result = await execute_catchup(body)
    assert result["status"] == "ok" and "skipped" in result
    assert model_calls == []


@pytest.mark.asyncio
async def test_the_switch_pauses_a_queued_batch_without_work(conn, model_calls, monkeypatch):
    body = payload(conn, enqueue_catchup(conn, EXPORT, start=TARGET)[0])
    monkeypatch.setenv(SWITCH, "off")
    result = await execute_catchup(body)
    assert result["status"] == "ok" and "skipped" in result
    assert model_calls == []


@pytest.mark.asyncio
async def test_a_failed_model_batch_fails_the_job_and_stays_owed(conn, monkeypatch):
    async def failed(_engine, **_kw):
        return SimpleNamespace(status="failed", output={})

    monkeypatch.setattr("topos.enrichment.jobs.canonical.embeddings_job.run_engine_task", failed)
    result = await execute_catchup(payload(conn, enqueue_catchup(conn, EXPORT, start=TARGET)[0]))
    assert result["status"] == "error"
    assert processing_status(conn, EXPORT)["pending_records"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"source_id": "codex_sessions", "record_ids": ["f1"]},
                                 {"source_id": EXPORT, "record_ids": []}])
async def test_refuses_a_payload_it_did_not_write(conn, model_calls, bad):
    assert (await execute_catchup(bad))["status"] == "error"
    assert model_calls == []


def test_a_node_that_never_imported_a_chat_owes_nothing(tmp_path):
    from topos.ingestion.embeddings_catchup import start_catchup

    fresh = sqlite3.connect(str(tmp_path / "fresh.db"))
    apply_all_migrations(fresh)  # ai_chat_messages is created by the first import, not by migrations
    assert start_catchup(fresh, TARGET) == {"queued_batches": 0, "pending_records": {EXPORT: 0, CAPTURE: 0}}
    fresh.close()


def test_the_upgrade_target_starts_it_and_reports_counts_only(conn):
    from topos.upgrades.runner import _exec_derived_rebuild

    detail = _exec_derived_rebuild({"params": {"targets": [TARGET]}}, conn)
    assert detail["targets"][TARGET] == {"queued_batches": 2, "pending_records": {EXPORT: 4, CAPTURE: 1}}
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs WHERE kind=?", (KIND,)).fetchone()[0] == 2


async def _finish(conn, kind, job_id, source_id=EXPORT):
    from topos.pipeline.job_runner import process_job

    async def _exec(_payload):
        return {"status": "ok"}

    enqueue_job(conn, kind=kind, source_id=source_id, payload={}, job_id=job_id)
    claimed = claim_next_job(conn, lease_owner="worker-a", kinds=[kind])
    with patch.dict("topos.pipeline.job_runner.EXECUTORS", {kind: _exec}):
        await process_job(lambda: conn, claimed)
    assert get_job(conn, job_id)["status"] == "done"
    return queued_by_source(conn)


@pytest.mark.asyncio
async def test_a_finished_batch_queues_the_next(conn):
    assert await _finish(conn, KIND, "first") == {EXPORT: ["f4", "f3", "f2", "f1"]}


@pytest.mark.asyncio
async def test_a_finished_import_continues_only_a_started_catchup(conn):
    assert await _finish(conn, "file_ingestion", "import-1") == {}
    enqueue_job(conn, kind=KIND, source_id=EXPORT, payload={}, job_id="started")
    conn.execute("UPDATE pipeline_jobs SET status='done' WHERE job_id='started'")
    conn.commit()
    assert await _finish(conn, "file_ingestion", "import-2") == {EXPORT: ["f4", "f3", "f2", "f1"]}


def test_the_worker_runs_this_kind():
    from topos.pipeline.job_runner import EXECUTORS

    assert KIND in EXECUTORS


def test_a_release_declares_the_step_and_asks_the_owner_first():
    from pathlib import Path

    import topos

    data = json.loads((Path(topos.__file__).parent / "upgrades" / "manifests.json").read_text())
    steps = [s for release in data["releases"] for s in release.get("steps", [])
             if s.get("id") == "embed-imported-ai-chats"]
    assert len(steps) == 1
    assert steps[0]["kind"] == "derived_rebuild"
    assert steps[0]["params"]["targets"] == [TARGET]
    assert steps[0]["consent"] == "prompt", "minutes of local model work over the owner's chats is the owner's call"

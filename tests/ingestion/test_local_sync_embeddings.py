"""A local-sync batch (iMessage, Signal) must land in the vector index.

The local-sync lane ran only the source's canonical jobs. ``embeddings`` is a
signal-lane job for every source that maps to a canonical table, and nothing on
that lane ever ran the signal lane, so no synced message was embedded. Measured
on one node 21 Oct 2026: 93,271 of 97,262 iMessage rows had no vector and no
``embeddings`` progress marker, while entities and emo_27 had run on the same
rows.
"""
import sqlite3
from types import SimpleNamespace

import pytest

from topos.enrichment.jobs.canonical.emo_27_job import Emo27Job
from topos.enrichment.jobs.canonical.entities_job import EntitiesJob
from topos.enrichment.models.mvp_defaults import job_spec_version
from topos.ingestion.local_sync import _run_local_sync_enrichment_if_enabled
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "local-sync-embeddings.db"), check_same_thread=False)
    apply_all_migrations(c)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: c)
    yield c
    c.close()


@pytest.fixture
def model_calls(monkeypatch):
    """Stub every model: vectors for the embedding call, no output from the rest."""
    calls = []

    async def run(_engine, *, input_payload, **_kw):
        texts = input_payload["texts"]
        calls.append(len(texts))
        return SimpleNamespace(
            status="completed",
            output={"vectors": [[0.1, 0.2, 0.3] for _ in texts], "dims": 3, "model": "test-model"},
        )

    async def no_output(self, messages, progress_callback=None):
        return []

    monkeypatch.setattr("topos.enrichment.jobs.canonical.embeddings_job.run_engine_task", run)
    monkeypatch.setattr(Emo27Job, "enrich", no_output)
    monkeypatch.setattr(EntitiesJob, "enrich", no_output)
    return calls


def _messages(source_id, n=3):
    return [
        {
            "message_id": f"{source_id}-m{i}",
            "conversation_id": "thread-1",
            "sender_type": "user",
            "ts": f"2026-09-0{i + 1}T12:00:00Z",
            "content": f"synthetic message number {i}",
            "source_id": source_id,
        }
        for i in range(n)
    ]


def _embedded(conn, source_id):
    return {r[0] for r in conn.execute(
        "SELECT record_id FROM signal_embeddings WHERE source_id=?", (source_id,))}


def _marked(conn, source_id):
    return {r[0] for r in conn.execute(
        "SELECT record_id FROM enrichment_record_progress "
        "WHERE source_id=? AND job_id='embeddings' AND spec_version>=?",
        (source_id, job_spec_version("embeddings")))}


@pytest.mark.parametrize("source_id", ["imessage", "signal"])
def test_local_sync_batch_is_embedded_and_marked(conn, model_calls, source_id):
    messages = _messages(source_id)
    _run_local_sync_enrichment_if_enabled(db_conn=conn, source_id=source_id, canonical_messages=messages)

    ids = {m["message_id"] for m in messages}
    assert _embedded(conn, source_id) == ids
    assert _marked(conn, source_id) == ids
    record_types = {r[0] for r in conn.execute(
        "SELECT DISTINCT record_type FROM signal_embeddings WHERE source_id=?", (source_id,))}
    assert record_types == {"conversation_message"}


def test_owner_detached_embeddings_stays_detached(conn, model_calls):
    from topos.enrichment.source_overrides import set_source_enrichment_override

    set_source_enrichment_override("imessage", "embeddings", False, conn=conn)
    _run_local_sync_enrichment_if_enabled(
        db_conn=conn, source_id="imessage", canonical_messages=_messages("imessage"))

    assert model_calls == []
    assert _embedded(conn, "imessage") == set()


def test_failed_embedding_leaves_the_batch_owed(conn, model_calls, monkeypatch):
    async def failed(_engine, **_kw):
        return SimpleNamespace(status="failed", output={})

    monkeypatch.setattr("topos.enrichment.jobs.canonical.embeddings_job.run_engine_task", failed)
    _run_local_sync_enrichment_if_enabled(
        db_conn=conn, source_id="imessage", canonical_messages=_messages("imessage"))

    # Non-fatal to the sync, as every enrichment failure on this lane is, and
    # no marker: the catch-up finds these rows again.
    assert _marked(conn, "imessage") == set()

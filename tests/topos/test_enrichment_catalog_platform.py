"""WS-A: unified enrichment catalog, coverage, lifecycle delete, and trigger gating."""

from __future__ import annotations

import sqlite3

import pytest

from topos.enrichment.catalog import (
    all_jobs_for_source,
    get_catalog_entry,
    get_enrichment_catalog,
    jobs_configured_for_source,
    list_catalog_entries,
)
from topos.sources.registry import BROWSER_VISITS, CHATGPT_FILE


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_catalog_covers_all_registered_jobs():
    catalog = get_enrichment_catalog()
    # Canonical + signal + raw lanes all present.
    assert "emo_27" in catalog
    assert "embeddings" in catalog
    assert "topic_clusters" in catalog
    assert "attachments" in catalog

    embeddings = catalog["embeddings"]
    assert embeddings.baseline is True
    assert "signal" in embeddings.lanes
    assert embeddings.default_provider == "huggingface"
    assert embeddings.output_tables == ("signal_embeddings",)

    # Raw stubs are marked and not backfillable.
    attachments = catalog["attachments"]
    assert attachments.stub is True
    assert attachments.supports_backfill is False


def test_catalog_entries_serialize():
    entries = list_catalog_entries()
    assert entries
    sample = entries[0]
    for key in ("job_id", "title", "description", "lanes", "output_tables", "cost_tier"):
        assert key in sample


def test_jobs_configured_for_source_merges_lanes():
    lanes = jobs_configured_for_source(CHATGPT_FILE)
    assert "emo_27" in lanes["canonical"]
    # Canonical-mapped source receives baseline signal jobs.
    assert "embeddings" in lanes["signal"]
    all_jobs = all_jobs_for_source(CHATGPT_FILE)
    assert "emo_27" in all_jobs
    assert "topic_clusters" in all_jobs
    assert len(all_jobs) == len(set(all_jobs))


def test_backfill_allowed_for_signal_lane_jobs():
    # url_classification is configured on browser_visits (canonical + signal lanes)
    assert "url_classification" in all_jobs_for_source(BROWSER_VISITS)
    entry = get_catalog_entry("url_classification")
    assert entry is not None and entry.supports_backfill


# ---------------------------------------------------------------------------
# Coverage + delete cores (SQLite fixtures)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE ai_chat_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            sender_type TEXT,
            sender_id TEXT,
            event_at TEXT,
            content TEXT,
            content_rendered TEXT,
            metadata_json TEXT,
            sequence INTEGER,
            source_id TEXT
        );
        CREATE TABLE message_emotions (
            message_id TEXT,
            source_id TEXT,
            emotion_label TEXT,
            confidence REAL,
            model_name TEXT,
            all_emotions_json TEXT,
            created_at TEXT
        );
        CREATE TABLE signal_embeddings (
            embedding_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            model TEXT,
            content_hash TEXT,
            chunk_index INTEGER
        );
        CREATE TABLE signal_facts (
            fact_id TEXT PRIMARY KEY,
            dimension TEXT,
            source_id TEXT,
            record_id TEXT,
            model TEXT,
            provider TEXT,
            payload_json TEXT
        );
        """
    )
    for i in range(4):
        conn.execute(
            "INSERT INTO ai_chat_messages (message_id, source_id, content) VALUES (?, ?, ?)",
            (f"m{i}", "chatgpt_file_ingestion", f"hello {i}"),
        )
    # Two of four messages enriched with emotions.
    for i in range(2):
        conn.execute(
            "INSERT INTO message_emotions (message_id, source_id, emotion_label) VALUES (?, ?, ?)",
            (f"m{i}", "chatgpt_file_ingestion", "joy"),
        )
    # One embedding row for chatgpt_file, one for another source.
    conn.execute(
        "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, model) VALUES ('e1','m0','chatgpt_file_ingestion','mini')"
    )
    conn.execute(
        "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, model) VALUES ('e2','x0','other_source','mini')"
    )
    # signal_facts rows: interests (url_classification) + stats (statistics).
    conn.execute(
        "INSERT INTO signal_facts (fact_id, dimension, source_id, record_id, payload_json) VALUES ('f1','interests','chatgpt_file_ingestion','m0','{}')"
    )
    conn.execute(
        "INSERT INTO signal_facts (fact_id, dimension, source_id, record_id, payload_json) VALUES ('stat:1','memory','chatgpt_file_ingestion','m1','{}')"
    )
    conn.commit()

    import topos.api.enrichment as api_mod

    monkeypatch.setattr(api_mod, "get_db_connection", lambda: conn)
    yield conn
    conn.close()


def test_enrichment_coverage_core(db_conn):
    from topos.api.enrichment import _enrichment_coverage_core

    result = _enrichment_coverage_core("chatgpt_file_ingestion")
    assert result["status"] == "ok"
    assert result["total_records"] == 4
    by_job = {j["job_id"]: j for j in result["jobs"]}

    emo = by_job["emo_27"]
    assert emo["enriched_records"] == 2
    assert emo["coverage_percent"] == 50.0

    emb = by_job["embeddings"]
    assert emb["enriched_records"] == 1
    assert emb["output_rows"]["signal_embeddings"] == 1


def test_enrichment_coverage_unknown_source(db_conn):
    from topos.api.enrichment import _enrichment_coverage_core

    with pytest.raises(ValueError):
        _enrichment_coverage_core("no_such_source")


def test_delete_enrichment_data_scoped_to_source(db_conn):
    from topos.api.enrichment import _delete_enrichment_data_core

    result = _delete_enrichment_data_core("chatgpt_file_ingestion", "embeddings")
    assert result["deleted_total"] == 1
    remaining = db_conn.execute("SELECT source_id FROM signal_embeddings").fetchall()
    assert [row[0] for row in remaining] == ["other_source"]


def test_delete_statistics_only_touches_stat_facts(db_conn):
    from topos.api.enrichment import _delete_enrichment_data_core

    result = _delete_enrichment_data_core("chatgpt_file_ingestion", "statistics")
    assert result["deleted_rows"].get("signal_facts") == 1
    remaining = {row[0] for row in db_conn.execute("SELECT fact_id FROM signal_facts").fetchall()}
    assert remaining == {"f1"}


def test_delete_unknown_job_rejected(db_conn):
    from topos.api.enrichment import _delete_enrichment_data_core

    with pytest.raises(ValueError):
        _delete_enrichment_data_core("chatgpt_file_ingestion", "not_a_job")


def test_enrichment_preview_attaches_chips(db_conn):
    from topos.api.enrichment import _enrichment_preview_core

    db_conn.execute(
        """
        CREATE TABLE ai_chat_conversations (
            conversation_id TEXT PRIMARY KEY,
            owner_user_id TEXT
        )
        """
    )
    result = _enrichment_preview_core("chatgpt_file_ingestion", limit=10)
    assert result["status"] == "ok"
    by_id = {r["record_id"]: r for r in result["records"]}
    assert by_id, "expected preview records"
    # m0 and m1 have emotion rows in the fixture.
    assert "joy" in (by_id.get("m0", {}).get("enrichments", {}).get("emotions") or [])


def test_catalog_core_annotates_source():
    from topos.api.enrichment import _enrichment_catalog_core

    result = _enrichment_catalog_core(source_id="chatgpt_file_ingestion")
    assert result["status"] == "ok"
    by_id = {e["job_id"]: e for e in result["enrichments"]}
    assert by_id["emo_27"]["enabled"] is True
    assert "canonical" in by_id["emo_27"]["enabled_lanes"]
    # availability_scores is signal-lane only and not baseline; chatgpt does not enable it.
    assert by_id["availability_scores"]["enabled"] is False


# ---------------------------------------------------------------------------
# Trigger gating: manual sources skip signal derivation on the ingest path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_derivation_gated_by_manual_trigger(monkeypatch):
    from dataclasses import replace

    from topos.ingestion.canonical_pipeline import run_post_canonical_pipeline

    calls = []

    async def fake_signal_run(self, canonical_messages, source_id, job_names=None, sync_batch_id=None, progress_callback=None):
        calls.append({"source_id": source_id, "count": len(canonical_messages)})
        return {"jobs_run": 1, "records_created": {}, "errors": [], "deferred_jobs": [], "envelopes": []}

    from topos.enrichment.orchestrator import SignalDerivationOrchestrator

    monkeypatch.setattr(SignalDerivationOrchestrator, "run_signal_derivation", fake_signal_run)

    manual_source = replace(CHATGPT_FILE, enrichment_trigger="manual")
    records = [{"message_id": "m1", "content": "hello", "source_id": manual_source.source_id}]

    # Automatic ingest path: gated for manual sources.
    outcome = await run_post_canonical_pipeline(
        source_def=manual_source,
        canonical_records=list(records),
        sync_batch_id="batch1",
        run_enrichment=False,
    )
    assert calls == []
    assert (outcome.get("signal_derivation") or {}).get("skipped") == "manual_trigger"

    # Deliberate invocation: force_signal opens the gate.
    outcome = await run_post_canonical_pipeline(
        source_def=manual_source,
        canonical_records=list(records),
        sync_batch_id="batch2",
        run_enrichment=False,
        force_signal=True,
    )
    assert len(calls) == 1

    # Automatic-trigger sources still run signal derivation on ingest.
    outcome = await run_post_canonical_pipeline(
        source_def=CHATGPT_FILE,
        canonical_records=list(records),
        sync_batch_id="batch3",
        run_enrichment=False,
    )
    assert len(calls) == 2

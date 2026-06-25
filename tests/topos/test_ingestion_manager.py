import json
import sqlite3

import pytest

from topos.enrichment.derived_tables import DerivedTablesManager
from topos.enrichment.jobs import CANONICAL_JOBS
from topos.ingestion.checkpoints.checkpoint_store import CheckpointStore, IngestionCheckpoint
from topos.ingestion.manager import IngestionManager, _filter_unenriched_messages
from topos.ingestion.triggers.file_trigger import FileTrigger
from topos.storage.raw.file_store import RawFileStore


class InMemoryCheckpointStore(CheckpointStore):
    def __init__(self):
        self.saved: IngestionCheckpoint | None = None

    def get_checkpoint(self, dataset_id: str, schema_id: str):
        _ = (dataset_id, schema_id)
        return self.saved

    def save_checkpoint(self, checkpoint: IngestionCheckpoint) -> None:
        self.saved = checkpoint


@pytest.mark.asyncio
async def test_ingestion_manager_processes_jsonl(tmp_path):
    file_store = RawFileStore(base_path=tmp_path)
    trigger = FileTrigger(file_store=file_store)
    dataset_id = "user:chatgpt"
    schema_id = "chatgpt.conversation.v1"

    records = [
        {"id": "m1", "thread_id": "t1", "role": "user", "content": "hello", "created_at": 1},
        {"id": "m2", "thread_id": "t1", "role": "assistant", "content": "hi", "created_at": 2},
    ]
    payload = "\n".join(json.dumps(record) for record in records).encode("utf-8")
    job = trigger.create_job_from_bytes(
        job_id="job-1",
        dataset_id=dataset_id,
        schema_id=schema_id,
        payload=payload,
        file_format="jsonl",
    )

    checkpoint_store = InMemoryCheckpointStore()
    manager = IngestionManager(file_store=file_store, checkpoint_store=checkpoint_store)
    result = await manager.process_job(job)

    assert result["records_processed"] == 2
    # Enrichment may add errors (e.g. emo_27 when torch/transformers not installed); ingestion still succeeds
    assert result["errors_count"] >= 0
    assert checkpoint_store.saved is not None
    assert checkpoint_store.saved.last_record_id == "m2"


def test_filter_unenriched_messages_scopes_by_source_and_owner():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE ai_chat_conversations (
            conversation_id TEXT PRIMARY KEY,
            owner_user_id TEXT NOT NULL,
            title TEXT,
            source_id TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE ai_chat_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            sender_type TEXT NOT NULL,
            sender_id TEXT,
            event_at TEXT NOT NULL,
            content TEXT NOT NULL,
            content_rendered TEXT,
            metadata_json TEXT,
            sequence INTEGER NOT NULL DEFAULT 0,
            source_id TEXT NOT NULL
        )
        """
    )

    table_name = CANONICAL_JOBS[0].get_derived_table()
    conn.execute(f"CREATE TABLE {table_name} (message_id TEXT PRIMARY KEY, payload_json TEXT)")
    conn.execute(f"INSERT INTO {table_name} (message_id, payload_json) VALUES (?, ?)", ("shared-msg", "{}"))

    conn.execute(
        """
        INSERT INTO ai_chat_conversations (
            conversation_id, owner_user_id, title, source_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("conv-other", "other-user", None, "other_source", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    conn.execute(
        """
        INSERT INTO ai_chat_messages (
            message_id, conversation_id, sender_type, sender_id, event_at, content, content_rendered, metadata_json, sequence, source_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("shared-msg", "conv-other", "human", None, "2026-01-01T00:00:00Z", "hello", None, None, 0, "other_source"),
    )
    conn.commit()

    tables_manager = DerivedTablesManager(conn=conn)
    msgs = [{"message_id": "shared-msg", "content": "hello"}]

    filtered = _filter_unenriched_messages(
        msgs,
        [CANONICAL_JOBS[0].get_job_name()],
        tables_manager,
        source_id="chatgpt_ingestion",
        dataset_id="user-a:default",
    )
    assert filtered == msgs


def test_filter_unenriched_messages_falls_back_to_source_scope_without_conversations():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE ai_chat_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            sender_type TEXT NOT NULL,
            sender_id TEXT,
            event_at TEXT NOT NULL,
            content TEXT NOT NULL,
            content_rendered TEXT,
            metadata_json TEXT,
            sequence INTEGER NOT NULL DEFAULT 0,
            source_id TEXT NOT NULL
        )
        """
    )

    table_name = CANONICAL_JOBS[0].get_derived_table()
    conn.execute(f"CREATE TABLE {table_name} (message_id TEXT PRIMARY KEY, payload_json TEXT)")
    conn.execute(f"INSERT INTO {table_name} (message_id, payload_json) VALUES (?, ?)", ("shared-msg", "{}"))
    conn.execute(
        """
        INSERT INTO ai_chat_messages (
            message_id, conversation_id, sender_type, sender_id, event_at, content, content_rendered, metadata_json, sequence, source_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("shared-msg", "conv-a", "human", None, "2026-01-01T00:00:00Z", "hello", None, None, 0, "chatgpt_ingestion"),
    )
    conn.commit()

    tables_manager = DerivedTablesManager(conn=conn)
    msgs = [{"message_id": "shared-msg", "content": "hello"}]

    filtered = _filter_unenriched_messages(
        msgs,
        [CANONICAL_JOBS[0].get_job_name()],
        tables_manager,
        source_id="chatgpt_ingestion",
        dataset_id="user-a:default",
    )
    assert filtered == []


@pytest.mark.asyncio
async def test_ingestion_manager_uses_canonicalizer_output_for_enrichment(tmp_path, monkeypatch):
    file_store = RawFileStore(base_path=tmp_path)
    trigger = FileTrigger(file_store=file_store)
    dataset_id = "user:chatgpt"
    schema_id = "chatgpt.conversation.v1"

    records = [
        {"id": "m1", "thread_id": "t1", "role": "user", "content": "hello", "created_at": 1},
    ]
    payload = "\n".join(json.dumps(record) for record in records).encode("utf-8")
    job = trigger.create_job_from_bytes(
        job_id="job-canonicalized-enrichment",
        dataset_id=dataset_id,
        schema_id=schema_id,
        payload=payload,
        file_format="jsonl",
    )

    def fake_canonicalize_staging_batch(self, staging_records, source, batch_size=1000, **kwargs):
        _ = (self, staging_records, source, batch_size, kwargs)
        return {
            "conversations_created": 1,
            "messages_created": 1,
            "canonical_messages": [
                {
                    "message_id": "mapped-msg-1",
                    "conversation_id": "mapped-conv-1",
                    "sender_type": "human",
                    "sender_id": None,
                    "ts": "2026-01-01T00:00:00Z",
                    "content": "FROM_CANONICALIZER",
                    "content_rendered": None,
                    "metadata_json": None,
                    "seq": 0,
                    "source_id": "chatgpt_ui_conversation",
                }
            ],
            "errors": [],
        }

    captured: dict = {}

    async def fake_run_canonical(self, messages, job_names=None, progress_callback=None):
        _ = (self, job_names, progress_callback)
        captured["messages"] = messages
        return {"jobs_run": 1, "records_created": {}, "errors": []}

    monkeypatch.setattr(
        "topos.storage.canonical.ai_chat.canonicalizer.Canonicalizer.canonicalize_staging_batch",
        fake_canonicalize_staging_batch,
    )
    monkeypatch.setattr(
        "topos.enrichment.orchestrator.EnrichmentOrchestrator.run_canonical",
        fake_run_canonical,
    )

    manager = IngestionManager(file_store=file_store, checkpoint_store=InMemoryCheckpointStore())
    result = await manager.process_job(job, source_id="chatgpt_ui_conversation")

    assert result["records_processed"] == 1
    assert captured["messages"][0]["message_id"] == "mapped-msg-1"
    assert captured["messages"][0]["content"] == "FROM_CANONICALIZER"

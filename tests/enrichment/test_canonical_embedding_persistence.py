"""Canonical embedding success means vectors actually exist, not a legacy no-op."""
import sqlite3
from types import SimpleNamespace

import pytest

from topos.enrichment.jobs.canonical.embeddings_job import EmbeddingsJob
from topos.enrichment.derived_tables import DerivedTablesManager
from topos.storage.db.migrations import apply_all_migrations


def test_canonical_embedding_hook_writes_searchable_store_and_is_idempotent(tmp_path):
    c = sqlite3.connect(str(tmp_path / 'vectors.db'))
    apply_all_migrations(c)
    manager = DerivedTablesManager(c)
    rows = [{'record_id': 'm1', 'message_id': 'm1', 'source_id': 'codex_sessions',
             'model': 'test-model', 'provider': 'huggingface', 'dims': 3,
             'vector': [0.1, 0.2, 0.3], 'content_hash': 'same', 'chunk_index': 0,
             'record_type': 'ai_chat_message', 'text_preview': 'Synthetic project'}]
    job = EmbeddingsJob(engine=object())
    job.write_derived(rows, [], tables_manager=manager)
    assert c.execute("SELECT count(*) FROM signal_embeddings WHERE record_id='m1'").fetchone()[0] == 1
    job.write_derived(rows, [], tables_manager=manager)
    assert c.execute("SELECT count(*) FROM signal_embeddings WHERE record_id='m1'").fetchone()[0] == 1
    c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['failed', 'short'])
async def test_failed_or_incomplete_model_batches_cannot_be_marked_done(monkeypatch, mode):
    async def run(*a, **kw):
        return SimpleNamespace(status='failed' if mode == 'failed' else 'completed', output={'vectors': []})
    monkeypatch.setattr('topos.enrichment.jobs.canonical.embeddings_job.run_engine_task', run)
    with pytest.raises(RuntimeError):
        await EmbeddingsJob(engine=object())._embed_batch(['synthetic'], [{'message_id': 'm1'}])

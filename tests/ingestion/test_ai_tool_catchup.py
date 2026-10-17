"""A committed transcript cursor must not hide unfinished derivation."""
import json
import sqlite3

import pytest

from topos.ingestion.ai_tool_catchup import enqueue_catchup, KIND
from topos.enrichment.models.mvp_defaults import job_spec_version
from topos.enrichment.record_progress import mark_records_processed
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / 'catchup.db'), check_same_thread=False)
    apply_all_migrations(c)
    c.execute("CREATE TABLE ai_chat_messages(message_id TEXT PRIMARY KEY,conversation_id TEXT,sender_type TEXT,source_id TEXT,content TEXT,share_excluded INTEGER)")
    monkeypatch.setattr('topos.ingestion.ai_tool_sync.source_state',
                        lambda conn, sid: {'enabled': sid == 'codex_sessions', 'paused': False})
    for rid, role, excluded in [('old-human', 'human', 0), ('old-ai', 'assistant', 0),
                                 ('private', 'human', 1)]:
        c.execute('INSERT INTO ai_chat_messages (message_id,conversation_id,sender_type,source_id,content,share_excluded) VALUES (?, ?, ?, ?, ?, ?)',
                  (rid, 'chat', role, 'codex_sessions', 'synthetic text', excluded))
    c.commit()
    yield c
    c.close()


def payload(conn, jid):
    return json.loads(conn.execute('SELECT payload_json FROM pipeline_jobs WHERE job_id=?', (jid,)).fetchone()[0])


def test_cursor_independent_debt_is_queued_once_and_respects_authorship(conn):
    ids = enqueue_catchup(conn)
    assert len(ids) == 1
    missing = payload(conn, ids[0])['missing']
    assert missing['entities'] == ['old-human']
    assert missing['topics'] == ['old-human']
    assert set(missing['embeddings']) == {'old-human', 'old-ai'}
    assert enqueue_catchup(conn) == []
    assert 'synthetic text' not in json.dumps(payload(conn, ids[0]))


def test_successful_marks_clear_debt_and_do_not_require_output_rows(conn):
    for job in ['entities', 'topics', 'embeddings', 'ai_tool_pipeline']:
        mark_records_processed(conn, 'codex_sessions', job,
                               [{'message_id': rid} for rid in ['old-human', 'old-ai']], job_spec_version(job))
    assert enqueue_catchup(conn) == []


def test_paused_source_is_not_queued(conn, monkeypatch):
    monkeypatch.setattr('topos.ingestion.ai_tool_sync.source_state', lambda *a: {'enabled': True, 'paused': True})
    assert enqueue_catchup(conn) == []


def test_pending_normal_ingestion_finishes_before_catchup(conn):
    from topos.pipeline.job_store import enqueue_job
    enqueue_job(conn, kind='inbox_deferred_enrichment', source_id='codex_sessions', payload={})
    assert enqueue_catchup(conn) == []


def test_batches_are_bounded_and_resume_after_completion(conn, monkeypatch):
    monkeypatch.setattr('topos.ingestion.ai_tool_catchup.BATCH_SIZE', 1)
    first = enqueue_catchup(conn)[0]
    batch = payload(conn, first)['missing']
    assert len({rid for ids in batch.values() for rid in ids}) == 1
    for job, ids in batch.items():
        mark_records_processed(conn, 'codex_sessions', job,
                               [{'message_id': rid} for rid in ids], job_spec_version(job))
    conn.execute("UPDATE pipeline_jobs SET status='done' WHERE job_id=?", (first,))
    conn.commit()
    second = enqueue_catchup(conn)[0]
    assert second != first
    assert set(r for ids in payload(conn, second)['missing'].values() for r in ids).isdisjoint(
        set(r for ids in batch.values() for r in ids))


@pytest.mark.asyncio
async def test_execution_rechecks_pause_and_never_reads_rows_when_paused(conn, monkeypatch):
    from topos.ingestion.ai_tool_catchup import execute_catchup
    monkeypatch.setattr('topos.core.state.get_db_connection', lambda: conn)
    monkeypatch.setattr('topos.ingestion.ai_tool_sync.source_state', lambda *a: {'enabled': True, 'paused': True})
    out = await execute_catchup({'source_id': 'codex_sessions', 'missing': {'entities': ['old-human']}})
    assert out['status'] == 'ok' and 'skipped' in out

@pytest.mark.asyncio
async def test_executor_reloads_current_eligible_records_and_runs_only_owed_jobs(conn, monkeypatch):
    from topos.ingestion.ai_tool_catchup import execute_catchup
    monkeypatch.setattr('topos.core.state.get_db_connection', lambda: conn)
    called = []
    async def canonical(self, records, job_names, **kwargs):
        called.append((job_names, [r['message_id'] for r in records]))
        return {'errors': []}
    async def pipeline(**kw):
        assert 'private' not in [r['message_id'] for r in kw['canonical_records']]
        assert kw['run_enrichment'] is False
        assert not set(kw['job_names']) & {'entities', 'topics', 'embeddings'}
        return {'signal_derivation': {'errors': []}, 'privacy_disclosure_layer': {'errors': []}}
    monkeypatch.setattr('topos.enrichment.orchestrator.EnrichmentOrchestrator.run_canonical', canonical)
    monkeypatch.setattr('topos.ingestion.canonical_pipeline.run_post_canonical_pipeline', pipeline)
    p = {'source_id': 'codex_sessions', 'missing': {'entities': ['old-human', 'private'],
            'embeddings': ['old-ai'], 'ai_tool_pipeline': ['old-human', 'old-ai']}}
    result = await execute_catchup(p)
    assert result['status'] == 'ok'
    assert called == [(['entities'], ['old-human']), (['embeddings'], ['old-ai'])]
    assert conn.execute("SELECT count(*) FROM enrichment_record_progress WHERE job_id='ai_tool_pipeline'").fetchone()[0] == 2

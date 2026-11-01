"""Content-free stage attribution must preserve verification and Engine routing."""
import json
from types import SimpleNamespace
import uuid

import httpx
import pytest

from topos.engine import client
from topos.engine.tasks import ExecutionMeta, ProcessingResult
from topos.engine.usage_observation import derive_usage_idempotency_key
from topos.query import verify


@pytest.mark.parametrize('remote', [False, True])
def test_distinct_calls_keep_usage_identity_and_phase_data_across_clients(monkeypatch, remote):
    from topos.config.settings import settings
    seen = []

    def execute(task):
        seen.append(task)
        return ProcessingResult(task_id=task.id, status='completed', execution_meta=ExecutionMeta(duration_ms=2100),
            output={'lanes': {'self': {'stance': 'supports', 'confidence': .9}},
                    'usage': {'timings': {'total_ms': 2000, 'load_ms': 1500, 'prefill_ms': 300,
                                         'decode_ms': 200, 'prompt': 'must never enter audit',
                                         'provider_secret': 'must never enter audit'}}})

    monkeypatch.setattr(settings, 'topos_engine_service_url', 'https://compute.invalid' if remote else '')
    if remote:
        from topos.engine.tasks import ProcessingTask
        real_client = httpx.Client

        def dispatch(request):
            assert request.url.path == '/v1/engine/run'
            task = ProcessingTask.model_validate(json.loads(request.content))
            return httpx.Response(200, json={'result': execute(task).model_dump(mode='json')})

        monkeypatch.setattr(client.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(dispatch), **kwargs))
    else:
        monkeypatch.setattr(client, 'Engine', lambda: SimpleNamespace(run=execute))

    audits = [{}, {}]
    for audit in audits:
        assert verify._llm_lane_stances('private claim', {'self': ['private evidence']}, audit=audit) == {'self': {'stance': 'supports', 'confidence': .9}}
        assert audit['provider_timings_ms'] == {'total_ms': 2000, 'load_ms': 1500, 'prefill_ms': 300, 'decode_ms': 200}
        assert audit['timings_ms']['engine_adapter'] == 2100
        assert 'private' not in json.dumps(audit) and 'must never' not in json.dumps(audit)
        uuid.UUID(audit['task_id'].removeprefix('truth_verdict:'))
    assert seen[0].id != seen[1].id
    keys = [derive_usage_idempotency_key(action='llm.generate', metric_key='llm_tokens', producer='engine.truth_verdict',
        canonical_action_identity={'task_id': task.id}) for task in seen]
    assert len(set(keys)) == 2


def test_bad_or_missing_timing_values_do_not_change_answer(monkeypatch):
    result = ProcessingResult(task_id='stub', status='completed', output={
        'lanes': {'self': {'stance': 'supports', 'confidence': .9}},
        'usage': {'timings': {'load_ms': float('nan'), 'prefill_ms': -1, 'decode_ms': True, 'total_ms': '123'}}})
    monkeypatch.setattr(client, 'get_engine_client_or_local', lambda _: SimpleNamespace(run=lambda task: result))
    audit = {}
    assert verify._llm_lane_stances('claim', {'self': ['evidence']}, audit=audit)['self']['stance'] == 'supports'
    assert 'provider_timings_ms' not in audit and 'timings_ms' not in audit


def test_no_evidence_has_no_invented_inference_time(monkeypatch, tmp_path):
    import sqlite3
    from topos.storage.db.migrations import apply_all_migrations
    conn = sqlite3.connect(tmp_path / 'synthetic.db')
    apply_all_migrations(conn)
    monkeypatch.setattr(verify, '_llm_lane_stances', lambda *a, **kw: pytest.fail('No evidence must not invoke inference'))
    result = verify.verify_claim(conn, statement='An invented unrecorded trip', caller_app_id='test')
    assert result['abstained']
    timings = result['_audit']['timings_ms']
    assert timings['verify_total'] >= timings['retrieval_and_filter'] >= 0
    assert 'engine_roundtrip' not in timings and 'provider_timings_ms' not in result['_audit']
    conn.close()

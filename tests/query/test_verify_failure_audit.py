"""Every verify_claim exit leaves one content-free audit line (1-2 Nov 2026 stress: 18 failures, 0 audited)."""
import ast
import asyncio
import logging
import time

import pytest

import topos.core.handlers as hub
from topos.core.handlers import query
from topos.engine.overload import ComputeUnavailable
from topos.engine.request_budget import current_budget
from topos.features.lifecycle import blackhole_guard
from topos.principal import OWNER_APP, Principal, set_principal, reset_principal
from topos.query import verify

STATEMENT = 'A private invented statement'
MARKER = 'truth verify_claim audit: '


def _audits(caplog):
    return [ast.literal_eval(r.getMessage().split(MARKER, 1)[1]) for r in caplog.records if MARKER in r.getMessage()]


def _call(payload):
    async def run():
        token = set_principal(Principal(cls=OWNER_APP, channel='uds'))
        try:
            return await query.handle_verify_claim({'id': 'synthetic', 'payload': payload})
        finally:
            reset_principal(token)
    return asyncio.run(run())


def _stub(monkeypatch, fake):
    monkeypatch.setattr(hub, 'get_db_connection', lambda: object())
    monkeypatch.setattr(blackhole_guard, 'guard_from_message', lambda *args: object())
    monkeypatch.setattr(query, '_record_verify_claim_after_answer', lambda *args: None)
    monkeypatch.setattr(verify, 'verify_claim', fake)


def test_deadline_in_the_model_call_is_audited_with_its_phase(monkeypatch, caplog):
    def stuck_at_model(*args, audit_sink, **kwargs):
        audit_sink.update(mode='standard', retrieval_touched=True, evidence_counts={'self': 2},
                          timings_ms={'retrieval_and_filter': 1.5}, phase='model_call', phase_started=time.perf_counter())
        while True:
            current_budget().check()
            time.sleep(.005)

    _stub(monkeypatch, stuck_at_model)
    with caplog.at_level(logging.INFO):
        result = _call({'statement': STATEMENT, 'caller_app_id': 'test', 'deadline_s': .08})
    assert result['error_type'] == 'compute_unavailable'
    [audit] = _audits(caplog)
    assert audit['outcome'] == 'deadline_exceeded' and audit['phase'] == 'model_call'
    assert audit['evidence_counts'] == {'self': 2} and audit['caller_app_id'] == 'test'
    timings = audit['timings_ms']
    assert timings['retrieval_and_filter'] == 1.5
    assert 0 < timings['phase_elapsed'] <= timings['dispatch_to_result']
    assert 'phase_started' not in audit
    assert STATEMENT not in caplog.text


def test_compute_unavailable_is_audited(monkeypatch, caplog):
    def unavailable(*args, audit_sink, **kwargs):
        audit_sink.update(mode='standard', phase='model_call', phase_started=time.perf_counter())
        raise ComputeUnavailable('compute_unavailable')

    _stub(monkeypatch, unavailable)
    with caplog.at_level(logging.INFO):
        result = _call({'statement': STATEMENT, 'caller_app_id': 'test'})
    assert result['error_type'] == 'compute_unavailable'
    [audit] = _audits(caplog)
    assert audit['outcome'] == 'compute_unavailable' and audit['phase'] == 'model_call'


def test_failure_before_verify_starts_still_leaves_a_line(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError('private detail ' + STATEMENT)

    _stub(monkeypatch, broken)
    with caplog.at_level(logging.INFO):
        result = _call({'statement': STATEMENT, 'caller_app_id': 'test'})
    assert result['status'] == 'error'
    [audit] = _audits(caplog)
    assert audit['outcome'] == 'error' and audit['phase'] == 'dispatch'
    assert 'private detail' not in str(audit)


def test_success_keeps_one_line_without_progress_markers(monkeypatch, caplog):
    def ok(*args, audit_sink, **kwargs):
        audit_sink.update(mode='standard', judge='llm', timings_ms={'verify_total': 3.0},
                          phase='model_call', phase_started=time.perf_counter())
        return {'mode': 'standard', 'lanes': {'self': {'stance': 'supports'}}, '_audit': audit_sink}

    _stub(monkeypatch, ok)
    with caplog.at_level(logging.INFO):
        result = _call({'statement': STATEMENT, 'caller_app_id': 'test'})
    assert result['status'] == 'ok'
    [audit] = _audits(caplog)
    assert audit['outcome'] == 'ok' and 'phase' not in audit and 'phase_started' not in audit


def test_verify_claim_fills_the_sink_it_was_given(tmp_path, monkeypatch):
    import sqlite3
    from topos.storage.db.migrations import apply_all_migrations
    conn = sqlite3.connect(tmp_path / 'synthetic.db')
    apply_all_migrations(conn)
    sink = {}
    result = verify.verify_claim(conn, statement='An invented unrecorded trip', caller_app_id='test', audit_sink=sink)
    assert result['_audit'] is sink
    assert sink['phase'] == 'retrieval' and sink['reason'] == 'no_evidence'
    conn.close()


# --- every overload outcome, through the real verify_claim -----------------------------------------------------------
EVIDENCE = 'the quarterly budget spreadsheet'
CLAIM = 'I work on ' + EVIDENCE


def _node_with_one_fact(tmp_path, monkeypatch, availability):
    """A migrated temporary database holding one invented fact, and a model host that answers `availability`."""
    import sqlite3
    from types import SimpleNamespace
    from topos.engine import client
    from topos.engine.tasks import ProcessingResult
    from topos.features.facts.store import FactStore
    from topos.storage.db.migrations import apply_all_migrations
    conn = sqlite3.connect(tmp_path / 'synthetic.db', check_same_thread=False)
    apply_all_migrations(conn)
    FactStore(conn).assert_fact(subject_entity_id='synthetic-owner', predicate='works_on', object_value=EVIDENCE,
                                dimension='activity', confidence=0.8, source_refs=[], disclosure='scoped')
    conn.commit()
    monkeypatch.setenv('TOPOS_TRUTH_LLM', 'on')
    monkeypatch.setattr(hub, 'get_db_connection', lambda: conn)
    monkeypatch.setattr(blackhole_guard, 'guard_from_message', lambda *args: None)
    monkeypatch.setattr(query, '_record_verify_claim_after_answer', lambda *args: None)
    monkeypatch.setattr(client, 'get_engine_client_or_local', lambda _: SimpleNamespace(run=lambda task: ProcessingResult(
        task_id=task.id, status='deferred', output={'availability': availability, 'text': 'model output ' + EVIDENCE})))
    return conn


@pytest.mark.parametrize('code, status, retry_after', [
    ('compute_busy', 503, 4), ('compute_unavailable', 503, None), ('compute_rate_limited', 429, 30)])
def test_each_overload_outcome_leaves_one_line_naming_it(tmp_path, monkeypatch, caplog, code, status, retry_after):
    conn = _node_with_one_fact(tmp_path, monkeypatch, ComputeUnavailable(code, retry_after).detail())
    with caplog.at_level(logging.INFO):
        result = _call({'statement': CLAIM, 'caller_app_id': 'test'})
    conn.close()
    assert result['status'] == 'error' and result['error_type'] == code and result['error_code'] == status
    [audit] = _audits(caplog)
    assert audit['outcome'] == code and audit['phase'] == 'model_call'
    assert audit.get('retry_after_seconds') == retry_after
    assert audit['mode'] == 'standard' and audit['retrieval_touched'] is True and audit['evidence_counts']['self'] == 1
    assert audit['task_id'].startswith('truth_verdict:') and 'judge' not in audit
    timings = audit['timings_ms']
    assert 0 <= timings['retrieval_and_filter'] and 0 <= timings['phase_elapsed'] <= timings['dispatch_to_result']
    assert 0 <= timings['worker_ready'] <= timings['dispatch_to_result']
    assert set(audit) <= {'outcome', 'caller_app_id', 'retry_after_seconds', 'timings_ms', *query._VERIFY_FAILURE_AUDIT_KEYS}
    assert EVIDENCE not in caplog.text and 'model output' not in caplog.text


@pytest.mark.parametrize('reason', ['deadline_exceeded', 'cancelled'])
def test_a_model_host_that_stopped_the_request_is_audited_as_that(tmp_path, monkeypatch, caplog, reason):
    conn = _node_with_one_fact(tmp_path, monkeypatch, {**ComputeUnavailable('compute_unavailable').detail(), 'reason': reason})
    with caplog.at_level(logging.INFO):
        result = _call({'statement': CLAIM, 'caller_app_id': 'test'})
    conn.close()
    assert result['termination_reason'] == reason
    [audit] = _audits(caplog)
    assert audit['outcome'] == reason and audit['phase'] == 'model_call'


@pytest.mark.parametrize('reason', ['because ' + STATEMENT, {'because': STATEMENT}])
def test_an_outcome_word_the_node_does_not_know_is_logged_as_error(monkeypatch, caplog, reason):
    from topos.engine.request_budget import RequestStopped

    def stopped(*args, **kwargs):
        raise RequestStopped(reason)

    _stub(monkeypatch, stopped)
    with caplog.at_level(logging.INFO):
        _call({'statement': STATEMENT, 'caller_app_id': 'test'})
    [audit] = _audits(caplog)
    assert audit['outcome'] == 'error' and STATEMENT not in str(audit)


def test_a_cancelled_request_is_audited(monkeypatch, caplog):
    import threading
    started = threading.Event()

    def blocked(*args, audit_sink, **kwargs):
        audit_sink.update(mode='standard', phase='retrieval', phase_started=time.perf_counter())
        started.set()
        while True:
            current_budget().check()
            time.sleep(.005)

    _stub(monkeypatch, blocked)

    async def run():
        token = set_principal(Principal(cls=OWNER_APP, channel='uds'))
        try:
            task = asyncio.create_task(query.handle_verify_claim({'id': 'synthetic', 'payload': {
                'statement': STATEMENT, 'caller_app_id': 'test'}}))
            assert await asyncio.to_thread(started.wait, .5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            reset_principal(token)

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    [audit] = _audits(caplog)
    assert audit['outcome'] == 'cancelled' and audit['phase'] == 'retrieval'
    assert STATEMENT not in caplog.text


# --- a request refused before any work is an exit too ----------------------------------------------------------------
@pytest.mark.parametrize('payload, reason', [
    ({'statement': STATEMENT, 'caller_app_id': 'test', 'mode': 'private-mode-word'}, 'unknown_mode'),
    ({'statement': STATEMENT}, 'caller_app_id_required'),
    ({'statement': STATEMENT, 'caller_app_id': 'test', 'deadline_s': 'private-deadline-word'}, 'invalid_deadline_s'),
    ({'statement': STATEMENT, 'caller_app_id': 'test', 'deadline_s': 0}, 'invalid_deadline_s'),
])
def test_a_request_refused_before_any_work_leaves_one_line(tmp_path, monkeypatch, caplog, payload, reason):
    import sqlite3
    conn = sqlite3.connect(tmp_path / 'synthetic.db', check_same_thread=False)
    monkeypatch.setattr(hub, 'get_db_connection', lambda: conn)
    monkeypatch.setattr(blackhole_guard, 'guard_from_message', lambda *args: None)
    monkeypatch.setattr(query, '_record_verify_claim_after_answer', lambda *args: pytest.fail('a refusal is not usage'))
    with caplog.at_level(logging.INFO):
        result = _call(payload)
    conn.close()
    assert result['status'] == 'error' and result['error'] == reason
    [audit] = _audits(caplog)
    assert audit['outcome'] == 'error' and audit['reason'] == reason and audit['phase'] == 'dispatch'
    assert audit['timings_ms']['dispatch_to_result'] >= 0
    assert 'private' not in str(audit) and 'mode' not in audit


def test_an_error_code_the_node_does_not_know_is_not_copied(monkeypatch, caplog):
    _stub(monkeypatch, lambda *args, **kwargs: {'error': 'because ' + STATEMENT})
    with caplog.at_level(logging.INFO):
        result = _call({'statement': STATEMENT, 'caller_app_id': 'test'})
    assert result['status'] == 'error'
    [audit] = _audits(caplog)
    assert audit['outcome'] == 'error' and 'reason' not in audit and STATEMENT not in str(audit)

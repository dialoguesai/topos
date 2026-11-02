"""Every verify_claim exit leaves one content-free audit line (1-2 Nov 2026 stress: 18 failures, 0 audited)."""
import ast
import asyncio
import logging
import time

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

"""Synthetic history contracts: snapshots, owner testimony, and erasure."""
import json
import pytest
from tests.query.test_truth_owner_doors import node, week
from tests.truth_lane_support import add_journal
from topos.query import truth_checks as store
from topos.ingestion import truth_inbound as lane


def attempt(key, verdict='unable', **kw):
    return dict(record_table='journal_entries', record_id=key, source='journal', speaker_kind='owner',
                claim_text='PRIVATE CANARY', verdict=verdict, reason='model_unavailable:TimeoutError',
                model='local:test', latency_ms=20000, read_timeout_s=20,
                engine_sha16='test-build', timings_ms={'read': 20000, 'total': 20001}, **kw)


def events(conn, key):
    return [json.loads(r[0]) for r in conn.execute(
        'SELECT payload_json FROM truth_events WHERE check_id=? ORDER BY sequence', (key,))]


def test_attempts_survive_recheck_and_duplicate_is_not_event(node):
    row = attempt('a')
    key = store.record_check(node.conn, row)
    assert store.record_check(node.conn, row) is None
    assert store.recheck(node.conn, attempt('a', 'no_evidence'), 2) == key
    history = events(node.conn, key)
    assert [e['verdict'] for e in history] == ['unable', 'no_evidence']
    assert [e['attempt'] for e in history] == [1, 2]
    assert history[0]['failure_class'] == 'model_unavailable:TimeoutError'
    assert history[0]['timings_ms']['read'] == 20000
    assert 'PRIVATE CANARY' not in json.dumps(history)


def test_label_revisions_and_dismissal_keep_judgement_not_truth(node, week):
    store.label_claim(node.conn, week.them, 'true', 'PRIVATE CANARY')
    store.label_claim(node.conn, week.them, 'false')
    store.dismiss_claim(node.conn, week.them, 'not_a_claim')
    history = events(node.conn, week.them)
    assert [e.get('label') for e in history[1:]] == ['true', 'false', None]
    assert [e.get('previous_label') for e in history[1:]] == [None, 'true', 'false']
    assert history[1]['acceptance'] == 'owner_testimony'
    assert history[1]['verdict'] == 'no_evidence'
    assert history[2]['acceptance'] == 'not_accepted'
    assert 'PRIVATE CANARY' not in json.dumps(history)


def test_citation_snapshots_preserve_parent_state_and_are_erased(node):
    add_journal(node.conn, 'a', 'PRIVATE CANARY')
    node.conn.commit()
    parent = store.record_check(node.conn, attempt('a'))
    store.label_claim(node.conn, parent, 'true')
    obj = node.conn.execute("SELECT object_id FROM signal_objects WHERE object_key=?", ('truth_check:' + parent,)).fetchone()[0]
    child = store.record_check(node.conn, attempt('b', 'supported', cited_refs=[
        {'table':'journal_entries', 'record_id':'a'}, {'table':'verified_claim','record_id':obj}]))
    edges = node.conn.execute('SELECT edge_kind, parent_verdict, parent_label FROM truth_evidence_edges').fetchall()
    assert sorted(edges) == [('owner_label_citation', 'unable', 'true'), ('source_record_reuse', 'unable', 'true')]
    store.label_claim(node.conn, parent, 'false')
    assert node.conn.execute('SELECT DISTINCT parent_label FROM truth_evidence_edges').fetchall() == [('true',)]
    store.forget_records(node.conn, [('journal_entries', 'a')])
    assert events(node.conn, parent) == []
    assert node.conn.execute('SELECT COUNT(*) FROM truth_evidence_edges').fetchone()[0] == 0
    assert len(events(node.conn, child)) == 1


def test_existing_timings_are_forwarded_without_untrusted_fields():
    row = lane.check_row({'table':'journal_entries','record_id':'a','source':'journal','owner':True},
                         {'verdict':'unable','timings_ms':{'total':20000,'read':19900,'secret':'PRIVATE CANARY'}}, model='local:test')
    assert row['timings_ms'] == {'total':20000,'read':19900}


def test_history_failure_rolls_back_mutation(node, monkeypatch):
    store.ensure_tables(node.conn)
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic audit failure')
    monkeypatch.setattr(store.truth_audit, 'append', fail)
    with pytest.raises(RuntimeError):
        store.record_check(node.conn, attempt('a'))
    assert node.conn.execute('SELECT COUNT(*) FROM truth_checks').fetchone()[0] == 0
    assert not node.conn.in_transaction


def test_existing_installation_is_not_backfilled(node, week):
    node.conn.execute('DROP TABLE truth_events')
    node.conn.execute('DROP TABLE truth_evidence_edges')
    node.conn.commit()
    store.ensure_tables(node.conn)
    assert events(node.conn, week.me) == []
    store.skip_claim(node.conn, week.me, 'need_context', True)
    assert len(events(node.conn, week.me)) == 1
    assert events(node.conn, week.me)[0]['acceptance'] == 'unchanged'


def test_self_citations_are_visible_and_unchecked_sources_not_relabelled(node):
    key = store.record_check(node.conn, attempt('a', cited_refs=[
        {'table':'journal_entries','record_id':'a'}, {'table':'journal_entries','record_id':'unverified'}]))
    assert node.conn.execute('SELECT check_id,parent_check_id FROM truth_evidence_edges').fetchall() == [(key,key)]


def test_private_and_invalid_timing_fields_are_omitted():
    from topos.query.truth_audit import timings, failure_class
    assert timings({'read':float('nan'),'judge':float('inf'),'total':-1,'gate':True,'check':3}) == {'check':3}
    assert failure_class('model_unavailable:PRIVATE CANARY', 'unable') == 'model_unavailable:other'

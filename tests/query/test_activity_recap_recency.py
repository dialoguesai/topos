"""Dated activity recall: source clocks, strict windows and bounded fallback."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import sqlite3
from types import SimpleNamespace
import pytest

from topos.query.planner import _relative_time_range
from topos.query.retrieval import _rrf_fuse_summary_lists, _fact_source_event_at, _activity_in_window

NOW = datetime.fromisoformat('2026-10-11T09:16:00-05:00')

def test_past_seven_days_is_rolling_and_not_eight_calendar_days():
    window = _relative_time_range('What have I been working on over the past 7 days?', NOW)
    assert window == ('2026-10-04T09:16:00-05:00', '2026-10-11T09:16:00-05:00')

def test_today_preserves_chicago_local_midnight():
    window = _relative_time_range('What have I been up to today?', NOW)
    assert window == ('2026-10-11T00:00:00-05:00', '2026-10-11T23:59:59-05:00')

def test_today_respects_dst_offset_change():
    now = datetime(2026, 11, 1, 16, tzinfo=ZoneInfo('America/Chicago'))
    start, end = map(datetime.fromisoformat, _relative_time_range('today', now))
    assert start.astimezone(timezone.utc).hour == 5
    assert end.astimezone(timezone.utc).hour == 5  # 23:59 local after fall-back

def test_strict_fusion_filters_every_lane_before_capping():
    old = [{'record_id':f'old-{i}', 'summary_text':'Completed today', 'event_at':'2026-05-15T12:00:00Z'} for i in range(40)]
    new = [{'record_id':'current', 'summary_text':'Reviewed code', 'event_at':'2026-10-10T12:00:00Z'}]
    unknown = [{'record_id':'extracted-now', 'created_at':NOW.isoformat(), 'summary_text':'Old undated claim'}]
    fused = _rrf_fuse_summary_lists([('facts_store',3,old),('briefs',2,unknown),('recent',1,new)],
        context_sources=frozenset({'recent','briefs'}), cap=12, now=NOW,
        activity_time_range=_relative_time_range('past 7 days', NOW))
    assert [i['record_id'] for i in fused] == ['current']
    assert fused[0]['in_time_window'] is True

def test_empty_strict_window_cannot_fall_back_to_months_old_items():
    assert _activity_in_window([{'event_at':'2026-05-15T12:00:00Z'}], _relative_time_range('past 7 days', NOW)) == []

def test_fact_uses_source_event_not_extraction_or_belief_validity():
    conn = sqlite3.connect(':memory:')
    conn.execute('create table journal_entries(entry_id text, entry_at text)')
    conn.execute("insert into journal_entries values ('entry','2026-05-15T10:10:00')")
    assert _fact_source_event_at(conn, {'valid_from':NOW.isoformat(), 'created_at':NOW.isoformat(),
        'source_refs':[{'table':'journal_entries','record_id':'entry'}]}) == '2026-05-15T10:10:00+00:00'
    assert _fact_source_event_at(conn, {'valid_from':NOW.isoformat()}) is None
    conn.close()

def test_today_expands_only_to_a_declared_recent_window(monkeypatch):
    from topos.storage.adapters.factory import AdapterFactory
    from topos.query.retrieval import DefaultSignalRetrievalAdapter
    from topos.query.types import RetrievalRequest
    from topos.query.manifest_validation import resolve_scope_manifest
    calls=[]
    def summary(**args):
        bounds=args['plan'].time_range
        calls.append(bounds)
        if bounds[0].startswith('2026-10-04'):
            return [{'summary_text':'Reviewed the code six days ago','event_at':'2026-10-05T12:00:00Z','in_time_window':True}]
        return []
    monkeypatch.setattr('topos.query.retrieval._build_summary_items',summary)
    monkeypatch.setattr('topos.core.state.get_db_connection',lambda:None)
    adapter=DefaultSignalRetrievalAdapter(AdapterFactory.create('memory'))
    packet=adapter.retrieve(RetrievalRequest(manifest=resolve_scope_manifest('work_context:read'),access_mode='summary',
        query_text='What have I been up to today?',owner_mode=True,now=NOW)).context_packet
    assert len(calls)==3
    assert packet['time_window']['expanded'] is True
    assert packet['time_window']['requested_from'].startswith('2026-10-11')
    assert packet['time_window']['from'].startswith('2026-10-04')
    calls.clear()
    packet=adapter.retrieve(RetrievalRequest(manifest=resolve_scope_manifest('work_context:read'),access_mode='summary',
        query_text='What have I been working on over the past 7 days? Report only work from those days.',owner_mode=True,now=NOW)).context_packet
    assert len(calls)==1
    assert 'expanded' not in packet['time_window']


def test_past_seven_days_is_168_hours_across_dst():
    now=datetime(2026,11,3,10,tzinfo=ZoneInfo('America/Chicago'))
    start,end=map(datetime.fromisoformat,_relative_time_range('past 7 days',now))
    assert (end.astimezone(timezone.utc)-start.astimezone(timezone.utc)).total_seconds()==168*3600


def test_recent_journal_uses_source_clock_before_window_and_cap():
    from topos.query.retrieval import _load_recent_summary_items
    conn=sqlite3.connect(':memory:')
    conn.executescript('''
      CREATE TABLE journal_entries(entry_id TEXT PRIMARY KEY, entry_at TEXT, starts_at TEXT);
      CREATE TABLE signal_embeddings(record_id TEXT, source_id TEXT, signal_dimension TEXT,
        text_preview TEXT, event_at TEXT, record_type TEXT, chunk_index INTEGER);
    ''')
    # Yesterday's event was entered today. A real event today was indexed late
    # under an old clock. Both must be filtered on source time, before LIMIT 1.
    conn.executemany('INSERT INTO journal_entries VALUES (?,?,?)',[
      ('old','2026-10-10T12:00:00Z','2026-10-10T12:00:00Z'),
      ('today','2026-10-11T14:00:00Z','2026-10-11T14:00:00Z')])
    conn.executemany('INSERT INTO signal_embeddings VALUES (?,?,?,?,?,?,0)',[
      ('old','journal','work','old event','2026-10-11T14:10:00Z','journal_entry'),
      ('today','journal','work','today event','2026-09-01T14:00:00Z','journal_entry')])
    items=_load_recent_summary_items(conn,time_range=('2026-10-11T00:00:00-05:00',NOW.isoformat()),now=NOW,limit=1)
    assert [item['record_id'] for item in items]==['today']
    assert items[0]['event_at']=='2026-10-11T14:00:00Z'


def test_embedding_clock_prefers_canonical_event_over_submission():
    from topos.enrichment.jobs.canonical.embeddings_job import _event_at
    assert _event_at({'_table':'journal_entries','entry_at':'2026-10-10T12:00:00Z',
       'starts_at':'2026-10-09T12:00:00Z','ts':NOW.isoformat(),'event_at':NOW.isoformat()})=='2026-10-09T12:00:00Z'
    assert _event_at({'_table':'activity_events','occurred_at':'2026-10-10T12:00:00Z',
       'event_at':NOW.isoformat()})=='2026-10-10T12:00:00Z'
    assert _event_at({'_table':'ai_chat_messages','event_at':NOW.isoformat()})==NOW.isoformat()


def test_inference_activity_window_excludes_old_and_undated_contributors(monkeypatch):
    from topos.storage.adapters.factory import AdapterFactory
    from topos.query.retrieval import DefaultSignalRetrievalAdapter
    from topos.query.types import RetrievalRequest
    from topos.query.manifest_validation import resolve_scope_manifest
    from topos.query.game_layer import DefaultGameLayer
    from topos.query.inference import build_inference_context_packet
    monkeypatch.setattr('topos.core.state.get_db_connection',lambda:None)
    monkeypatch.setattr('topos.query.retrieval._load_canonical_summary_items',lambda **kwargs:[
       {'record_id':'new','topic':'Current contributor','event_at':'2026-10-10T12:00:00Z'},
       {'record_id':'old','topic':'Old project','event_at':'2026-03-10T12:00:00Z'},
       {'record_id':'unknown','topic':'Undated relationship','created_at':NOW.isoformat()},
    ])
    adapter=DefaultSignalRetrievalAdapter(AdapterFactory.create('memory'))
    packet=adapter.retrieve(RetrievalRequest(manifest=resolve_scope_manifest('relationship_context:read'),
        access_mode='inference',query_text='Who have I been working alongside over the past 7 days?',
        owner_mode=True,now=NOW)).context_packet
    assert [i['record_id'] for i in packet['scores']]==['new']
    assert 'topic_clusters' not in packet
    public=DefaultGameLayer().apply(context_packet=packet,access_mode='inference',scope_id='relationship_context:read',query_text='Who?')
    assert public.payload['time_window']==packet['time_window']
    prompt=build_inference_context_packet(packet)['context']
    assert 'time_window' in prompt
    assert 'Old project' not in prompt and 'Undated relationship' not in prompt


def test_semantic_journal_cannot_reintroduce_submission_clock():
    from topos.query.retrieval import _restore_index_event_clocks
    conn=sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE journal_entries(entry_id TEXT PRIMARY KEY, entry_at TEXT, starts_at TEXT)')
    conn.execute("INSERT INTO journal_entries VALUES ('late', '2026-10-11T12:00:00Z', '2026-10-01T12:00:00Z')")
    hits=_restore_index_event_clocks([{'record_id':'late','record_type':'journal_entry','event_at':NOW.isoformat()}],conn)
    assert not _activity_in_window(hits,_relative_time_range('past 7 days',NOW))


def test_journal_fact_uses_session_time_not_later_entry_time():
    conn=sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE journal_entries(entry_id TEXT, entry_at TEXT, starts_at TEXT)')
    conn.execute("INSERT INTO journal_entries VALUES ('late', '2026-10-11T12:00:00Z', '2026-10-01T12:00:00Z')")
    assert _fact_source_event_at(conn, {'source_refs':[{'table':'journal_entries','record_id':'late'}]}) == '2026-10-01T12:00:00+00:00'


def test_pipeline_does_not_replace_windowed_inference_with_all_time_people(monkeypatch):
    import asyncio
    from topos.principal import OWNER_APP, Principal, set_principal, reset_principal
    from topos.query.pipeline import QueryPipelineOrchestrator
    from topos.storage.adapters.factory import AdapterFactory
    from topos.query.manifest_validation import resolve_scope_manifest
    monkeypatch.setattr('topos.core.state.get_db_connection',lambda:None)
    monkeypatch.setattr('topos.query.retrieval._load_canonical_summary_items',lambda **kwargs:[])
    calls=[]
    def stale_direct(*args,**kwargs):
        calls.append('direct')
        return {'answer':'Old collaborator','answer_type':'facts'}
    for module,fn in [('facts_direct','try_facts_direct'),('closeness','try_close_circle'),('collaborators','try_collaborators')]:
        monkeypatch.setattr('topos.query.'+module+'.'+fn,stale_direct)
    def inference(**kwargs):
        calls.append('inference')
        assert 'time_window' in kwargs['context_packet']
        return {'answer':'unknown','confidence':0}
    monkeypatch.setattr('topos.query.pipeline.run_query_inference',inference)
    orch=QueryPipelineOrchestrator(adapters=AdapterFactory.create('memory'))
    token=set_principal(Principal(OWNER_APP,'uds'))
    try:
        result=asyncio.run(orch.execute(query_text='Who have I been working alongside over the past 7 days?',
            scope_id='relationship_context:read',access_mode='inference',
            manifest=resolve_scope_manifest('relationship_context:read'),now=NOW))
    finally:
        reset_principal(token)
    assert result['turn_outcome']=='live_query'
    assert result['public_result']['answer']=='unknown'
    assert calls==['inference']


def test_empty_activity_window_does_not_claim_the_store_is_empty():
    from topos.query.retrieval import _rrf_fuse_summary_lists
    from topos.query.narrowing import NarrowingLedger
    ledger=NarrowingLedger()
    assert _rrf_fuse_summary_lists([('canonical',1.0,[{'record_id':'older',
        'event_at':'2026-09-01T12:00:00Z','summary_text':'A real stored message'}])],
        activity_time_range=_relative_time_range('past 7 days',NOW),ledger=ledger)==[]
    assert ledger.empty_cause is None


@pytest.mark.parametrize("broken_vector, expected", [(False, "no_match"), (True, "engine_failed")])
def test_empty_window_is_no_match_when_source_history_exists(monkeypatch, broken_vector, expected):
    from topos.query.retrieval import DefaultSignalRetrievalAdapter, _rrf_fuse_summary_lists
    from topos.storage.adapters.factory import AdapterFactory
    from topos.query.manifest_validation import resolve_scope_manifest
    from topos.query.types import RetrievalRequest
    from topos.query.narrowing import NarrowingLedger
    conn=sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE conversation_messages(message_id TEXT)')
    conn.execute("INSERT INTO conversation_messages VALUES ('older')")
    monkeypatch.setattr('topos.core.state.get_db_connection',lambda:None)
    adapters=AdapterFactory.create('memory')
    adapters.signal._conn=conn
    def empty_summary(**kwargs):
        return _rrf_fuse_summary_lists([],ledger=kwargs.get('ledger'))
    monkeypatch.setattr('topos.query.retrieval._build_summary_items',empty_summary)
    # This test is about a completed empty search, not local model availability.
    # Exercise the real semantic-lane error handling with a deterministic service.
    def search_vectors(**kwargs):
        if broken_vector:
            raise RuntimeError('synthetic vector outage')
        return {'items': []}
    monkeypatch.setattr('topos.features.signal.service.get_signal_service',
                        lambda: SimpleNamespace(search_vectors=search_vectors))
    ledger=NarrowingLedger()
    DefaultSignalRetrievalAdapter(adapters).retrieve(RetrievalRequest(
        manifest=resolve_scope_manifest('messages:read'),access_mode='summary',
        query_text='What messages did I send today?',owner_mode=True,now=NOW,ledger=ledger))
    assert ledger.empty_cause == expected

def test_everyday_activity_paraphrases_use_event_window():
    from topos.query.planner import is_dated_activity_query
    for query in ['What was I doing yesterday?', 'What was I up to yesterday?',
                  'What have I been doing this week?', 'What am I doing today?']:
        assert is_dated_activity_query(query), query
    assert not is_dated_activity_query('What did Peter say about doing the interview?')


def test_yesterday_recap_passes_strict_window_to_fusion(monkeypatch):
    from types import SimpleNamespace
    from topos.query import retrieval
    from topos.query.manifest import ScopeResolutionManifest
    from topos.query.planner import _relative_time_range
    from topos.storage.adapters.factory import AdapterFactory
    window = _relative_time_range('yesterday', NOW)
    captured = {}
    def fuse(lanes, **kwargs):
        captured.update(kwargs)
        return []
    monkeypatch.setattr(retrieval, '_rrf_fuse_summary_lists', fuse)
    monkeypatch.setattr(retrieval, '_default_conn', lambda: None)
    monkeypatch.setattr('topos.core.state.get_db_connection', lambda: None)
    monkeypatch.setattr('topos.features.signal.vector_settings.fusion_rrf_enabled', lambda: True)
    retrieval._build_summary_items_unfiltered(
        manifest=ScopeResolutionManifest(scope_id='activity:read', primary_dimensions=[]),
        adapters=AdapterFactory.create('memory'), query_text='What was I doing yesterday?',
        semantic_hits=[], ranked_clusters=[],
        plan=SimpleNamespace(time_range=window), now=NOW)
    assert captured['activity_time_range'] == window

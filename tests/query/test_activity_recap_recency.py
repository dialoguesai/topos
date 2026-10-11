"""Dated activity recall: source clocks, strict windows and bounded fallback."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import sqlite3

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

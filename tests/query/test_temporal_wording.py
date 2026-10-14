from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from topos.query.planner import build_query_plan, comparison_windows, is_dated_activity_query
NOW=datetime(2026,10,12,20,47,tzinfo=ZoneInfo('America/Chicago'))

@pytest.mark.parametrize('a,b', [('past seven days','past 7 days'),('past week','past 7 days'),('last seven days','last 7 days'),('past two weeks','past 14 days'),('previous month','last month')])
def test_equivalent_wording_has_identical_windows(a,b):
    assert build_query_plan(None,'my activity '+a,now=NOW).time_range == build_query_plan(None,'my activity '+b,now=NOW).time_range
    assert build_query_plan(None,'my activity '+a,now=NOW).time_range


def test_hours_are_elapsed_even_across_dst():
    now=datetime(2026,11,1,12,tzinfo=ZoneInfo('America/Chicago'))
    start,end=build_query_plan(None,'What did I do in the last 24 hours?',now=now).time_range
    assert datetime.fromisoformat(end).timestamp()-datetime.fromisoformat(start).timestamp()==24*3600


def test_past_month_uses_calendar_boundary_with_clamped_day():
    start,end=build_query_plan(None,'my activity in the past month',now=NOW.replace(month=3,day=31)).time_range
    assert start.startswith('2026-02-28') and end.startswith('2026-03-31')


def test_comparison_previous_month_keeps_both_windows():
    assert len(comparison_windows('What changed this month compared with the previous month?',NOW))==2


def test_narrative_recall_is_activity_but_identity_is_not():
    assert is_dated_activity_query('Give me a recap of yesterday and how it relates to my goals')
    assert not is_dated_activity_query('What are my long term goals?')

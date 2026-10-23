"""A sentence that names several relative windows, without asking for their difference,
is searched over their union.

`_relative_time_range` used to return the first window in a fixed precedence (hours, past
month, days, then named periods) and stop. A weekly recap that said "the past 7 days ...
cover the past 24 hours separately" resolved to the last 24 hours wherever each phrase
sat, so every scope retrieved one day of a seven-day window (measured 2026-10-22 on
1.5.15).

A single window and a differenced ask ("compare this week vs last week") keep exactly the
windows they had: the values below are the ones the first-match resolver produced.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from topos.query.planner import _relative_time_range, build_query_plan, comparison_windows


# A Thursday morning in a zone at UTC-5 on these dates.
NOW = datetime(2026, 10, 22, 7, 0, tzinfo=ZoneInfo("America/Chicago"))

PAST_7_DAYS = ("2026-10-15T07:00:00-05:00", "2026-10-22T07:00:00-05:00")
PAST_24_HOURS = ("2026-10-21T07:00:00-05:00", "2026-10-22T07:00:00-05:00")
THIS_WEEK = ("2026-10-19T00:00:00-05:00", "2026-10-22T23:59:59-05:00")
LAST_WEEK = ("2026-10-12T00:00:00-05:00", "2026-10-18T23:59:59-05:00")


def plan(query: str):
    return build_query_plan(None, query, now=NOW)


def test_days_and_hours_in_one_ask_search_the_seven_days() -> None:
    q = "Summarize my work over the past 7 days. Cover the past 24 hours separately."
    assert _relative_time_range(q, NOW) == PAST_7_DAYS, (
        "the hours phrase won, so the week was searched as its last day")
    p = plan(q)
    assert p.time_range == PAST_7_DAYS
    assert p.time_windows == [] and p.comparison_intent is False


@pytest.mark.parametrize(
    "query",
    [
        "Summarize my work over the past 7 days. Cover the past 24 hours separately.",
        "Cover the past 24 hours separately. Summarize my work over the past 7 days.",
        "Cover the past 24 hours separately. Summarize my work over the past week.",
        "Summarize my work over the past seven days, then the past 24 hours.",
    ],
)
def test_the_order_of_the_phrases_does_not_matter(query: str) -> None:
    assert plan(query).time_range == PAST_7_DAYS


def test_a_two_period_recall_covers_both_periods() -> None:
    # "this week" came first in the precedence, so last week was never searched.
    p = plan("my work this week and last week")
    assert p.time_range == (LAST_WEEK[0], THIS_WEEK[1])
    assert p.comparison_intent is False


@pytest.mark.parametrize(
    "query",
    [
        "What did I do in the past 24 hours?",
        "What did I do in the past 24 hours? List the past 24 hours by source.",
        "What did I do in the last 24 hours, i.e. the past 24 hours?",  # one window, twice
    ],
)
def test_hours_only_is_unchanged(query: str) -> None:
    assert _relative_time_range(query, NOW) == PAST_24_HOURS
    assert plan(query).time_range == PAST_24_HOURS


@pytest.mark.parametrize(
    "query, first_match, windows, span",
    [
        ("compare this week vs last week", THIS_WEEK, [LAST_WEEK, THIS_WEEK],
         (LAST_WEEK[0], THIS_WEEK[1])),
        ("How did the past 24 hours differ from the past 7 days?", PAST_24_HOURS,
         [PAST_7_DAYS, PAST_24_HOURS], PAST_7_DAYS),
    ],
)
def test_a_differenced_ask_keeps_its_windows(query: str, first_match, windows, span) -> None:
    # The first-match window is what direct callers (the quality harness) saw before;
    # the plan's pair and span come from `_comparison_windows`, as before.
    assert _relative_time_range(query, NOW) == first_match
    assert comparison_windows(query, NOW) == windows
    p = plan(query)
    assert p.comparison_intent is True
    assert p.time_windows == windows
    assert p.time_range == span

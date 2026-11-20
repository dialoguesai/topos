"""Plain time phrases resolve to the window the owner means.

Measured 19 Nov 2026 with `build_query_plan` on "What did I talk about with AI <phrase>?":
"yesterday", "today", "this week", "last week", "this month", "last month" and the
"last/past N days|hours" forms produced a window. These did not, so the ask ran with no
date scope at all and was answered from whatever ranked highest:

    last night, this morning, tonight, two days ago, 3 days ago, a week ago, on Monday,
    last Friday, last weekend, this year, last year, last 3 months, the past few days,
    in the last hour, in October, since Monday

"the day before yesterday" was worse than absent: "before" was read as a boundary word
governing "yesterday", so the window was 1970 through two days ago. And a written date
("on November 18") was cut at UTC midnight while "yesterday" was cut at the owner's own
midnight, so the same day asked two ways returned two different sets of rows.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from topos.query.planner import build_query_plan

#: A Thursday, early afternoon, US Central (UTC-6).
NOW = datetime.fromisoformat("2026-11-19T13:25:00-06:00")


def window(phrase: str, now: datetime = NOW, template: str = "What did I talk about with AI {}?"):
    return build_query_plan(None, template.format(phrase), now=now).time_range


def day(start: str, end: str | None = None, offset: str = "-06:00"):
    return (f"{start}T00:00:00{offset}", f"{end or start}T23:59:59{offset}")


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("the day before yesterday", day("2026-11-17")),
        ("day before yesterday", day("2026-11-17")),
        ("two days ago", day("2026-11-17")),
        ("2 days ago", day("2026-11-17")),
        ("3 days ago", day("2026-11-16")),
        ("a couple of days ago", day("2026-11-17")),
        ("a day ago", day("2026-11-18")),
    ],
)
def test_a_counted_day_is_that_single_day(phrase, expected):
    assert window(phrase) == expected


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("last night", ("2026-11-18T17:00:00-06:00", "2026-11-19T04:59:59-06:00")),
        ("this morning", ("2026-11-19T00:00:00-06:00", "2026-11-19T11:59:59-06:00")),
        ("this afternoon", ("2026-11-19T12:00:00-06:00", "2026-11-19T17:59:59-06:00")),
        ("yesterday morning", ("2026-11-18T00:00:00-06:00", "2026-11-18T11:59:59-06:00")),
        ("yesterday afternoon", ("2026-11-18T12:00:00-06:00", "2026-11-18T17:59:59-06:00")),
        ("yesterday evening", ("2026-11-18T17:00:00-06:00", "2026-11-18T23:59:59-06:00")),
    ],
)
def test_a_part_of_a_day(phrase, expected):
    assert window(phrase) == expected


def test_this_evening_once_it_has_begun():
    late = datetime.fromisoformat("2026-11-19T20:10:00-06:00")
    expected = ("2026-11-19T17:00:00-06:00", "2026-11-19T23:59:59-06:00")
    assert window("this evening", late) == expected
    assert window("tonight", late) == expected


@pytest.mark.parametrize(
    "phrase",
    ["tonight", "this evening", "this weekend", "this Saturday", "in December 2027"],
)
def test_a_period_that_has_not_started_gets_no_window(phrase):
    """Unchanged from before these phrases were read: a future window belongs to the
    calendar, and the dated-recall path would clamp it into one that holds nothing."""
    assert window(phrase) is None


def test_this_weekend_on_the_weekend_is_the_one_under_way():
    sunday = datetime.fromisoformat("2026-11-22T10:00:00-06:00")
    assert window("this weekend", sunday) == day("2026-11-21", "2026-11-22")
    assert window("last weekend", sunday) == day("2026-11-14", "2026-11-15")


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("on Monday", day("2026-11-16")),
        ("last Friday", day("2026-11-13")),
        ("on Tuesday night", day("2026-11-17")),
        ("Monday morning", day("2026-11-16")),
        ("this past Wednesday", day("2026-11-18")),
        # Asked on a Thursday: the most recent Thursday that is already over.
        ("last Thursday", day("2026-11-12")),
    ],
)
def test_a_weekday_is_its_most_recent_past_occurrence(phrase, expected):
    assert window(phrase) == expected


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("last weekend", day("2026-11-14", "2026-11-15")),
        ("over the weekend", day("2026-11-14", "2026-11-15")),
        ("a week ago", day("2026-11-09", "2026-11-15")),
        ("2 weeks ago", day("2026-11-02", "2026-11-08")),
        ("a month ago", day("2026-10-01", "2026-10-31")),
        ("this year", day("2026-01-01", "2026-11-19")),
        ("last year", day("2025-01-01", "2025-12-31")),
        ("in October", day("2026-10-01", "2026-10-31")),
        ("last October", day("2026-10-01", "2026-10-31")),
        # December has not happened yet this year: the one that has.
        ("in December", day("2025-12-01", "2025-12-31")),
        ("in October 2025", day("2025-10-01", "2025-10-31")),
    ],
)
def test_a_named_period(phrase, expected):
    assert window(phrase) == expected


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("in the last 3 months", day("2026-08-19", "2026-11-19")),
        ("in the past few days", ("2026-11-16T13:25:00-06:00", "2026-11-19T13:25:00-06:00")),
        ("the last few days", day("2026-11-16", "2026-11-19")),
        ("in the past couple of weeks", ("2026-11-05T13:25:00-06:00", "2026-11-19T13:25:00-06:00")),
        ("in the last hour", ("2026-11-19T12:25:00-06:00", "2026-11-19T13:25:00-06:00")),
        ("in the past year", ("2025-11-19T13:25:00-06:00", "2026-11-19T13:25:00-06:00")),
    ],
)
def test_a_counted_span_reaches_back_from_now(phrase, expected):
    assert window(phrase) == expected


def test_since_a_weekday_runs_from_that_day_to_the_end_of_today():
    assert window("since Monday") == ("2026-11-16T00:00:00-06:00", "2026-11-19T23:59:59-06:00")


def test_the_boundary_words_keep_the_callers_own_midnight():
    # "since last week" used to end at 23:59:59 UTC of the local date, which is 17:59 in
    # Chicago, so an evening's rows fell outside a window that ends "today".
    assert window("since last week") == ("2026-11-09T00:00:00-06:00", "2026-11-19T23:59:59-06:00")
    assert window("before last week")[1] == "2026-11-08T23:59:59-06:00"


@pytest.mark.parametrize(
    "phrase",
    ["on November 18", "on Nov 18th", "on November 18, 2026", "on 2026-11-18"],
)
def test_a_written_date_is_the_callers_calendar_day_like_yesterday_is(phrase):
    assert window(phrase) == window("yesterday") == day("2026-11-18")


def test_a_written_date_without_a_caller_clock_stays_utc():
    plan = build_query_plan(None, "What did I talk about with AI on 2026-11-18?")
    assert plan.time_range == ("2026-11-18T00:00:00+00:00", "2026-11-18T23:59:59+00:00")


def test_a_named_zone_keeps_its_own_offset_across_a_dst_change():
    # 1 Nov 2026 is the fall-back day in Chicago: it starts at UTC-5 and ends at UTC-6.
    now = datetime(2026, 11, 3, 10, tzinfo=ZoneInfo("America/Chicago"))
    start, end = map(datetime.fromisoformat, window("on Sunday", now))
    assert (start.date().isoformat(), end.date().isoformat()) == ("2026-11-01", "2026-11-01")
    assert start.utcoffset().total_seconds() == -5 * 3600
    assert end.utcoffset().total_seconds() == -6 * 3600


@pytest.mark.parametrize(
    "question",
    [
        "What is Monday.com used for?",
        "What did I buy on Black Friday deals pages?",
        "What may happen if I skip the meeting?",
        "Should we march ahead with the plan?",
        "What did I decide about the second option?",
        "How do I sort a list in Python?",
        "Who won the 18th hole?",
        # A day number with no month stays unread: an existing planner rule.
        "Can we meet on the 5th?",
    ],
)
def test_words_that_only_look_like_time_get_no_window(question):
    assert build_query_plan(None, question, now=NOW).time_range is None


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("yesterday", day("2026-11-18")),
        ("today", day("2026-11-19")),
        ("this week", day("2026-11-16", "2026-11-19")),
        ("last week", day("2026-11-09", "2026-11-15")),
        ("last month", day("2026-10-01", "2026-10-31")),
        ("in the last 24 hours", ("2026-11-18T13:25:00-06:00", "2026-11-19T13:25:00-06:00")),
        ("in the past 7 days", ("2026-11-12T13:25:00-06:00", "2026-11-19T13:25:00-06:00")),
        ("in the past month", ("2026-10-19T13:25:00-06:00", "2026-11-19T13:25:00-06:00")),
    ],
)
def test_the_phrases_that_already_worked_still_do(phrase, expected):
    assert window(phrase) == expected


def test_two_named_windows_are_still_one_span():
    plan = build_query_plan(
        None, "What did I work on last weekend and on Monday?", now=NOW
    )
    assert plan.time_range == ("2026-11-14T00:00:00-06:00", "2026-11-16T23:59:59-06:00")


def test_the_time_words_do_not_reach_the_semantic_query():
    plan = build_query_plan(None, "What did I say about the roadmap last Friday night?", now=NOW)
    assert "friday" not in plan.semantic_residual.lower()
    assert "roadmap" in plan.semantic_residual.lower()

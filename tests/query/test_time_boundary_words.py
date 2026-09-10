"""SUITE-TIME-BOUNDARY — "before", "after" and "since" move the window, not just a flag.

protects: SYS-query I1. Measured 2026-09-09 (`test_temporal_cardinality_twins.py`): "what did I work
on before last week" and "... after last week" produced the identical window, last week, so the
first question returned exactly the interval it excludes. The planner READ the word (it set
`temporal_shift = "past"`) and nothing carried it to the window the lanes filter on.

Graded on the plan, because that is where the window is decided, plus two checks through the real
window filter so the new bounds are known to be usable: `_prefer_time_window` treats an
unparseable bound as no window at all, which is why an open end is a real earliest instant.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from topos.query.planner import build_query_plan
from topos.query.retrieval import _prefer_time_window

pytestmark = [pytest.mark.check("C-quality-temporal-cardinality-twins")]

#: A Wednesday, so "last week" is Monday 31 August to Sunday 6 September.
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
EARLIEST = "1970-01-01T00:00:00+00:00"
LAST_WEEK = ("2026-08-31T00:00:00+00:00", "2026-09-06T23:59:59+00:00")


def _window(query: str):
    return build_query_plan(None, query, now=NOW).time_range


class TestTheBaseline:
    def test_last_week_is_monday_to_sunday(self) -> None:
        assert _window("what did I work on last week") == LAST_WEEK


class TestBefore:
    @pytest.mark.parametrize("phrase", ["before last week", "prior to last week"])
    def test_before_last_week_is_everything_up_to_it(self, phrase: str) -> None:
        assert _window(f"what did I work on {phrase}") == (EARLIEST, "2026-08-30T23:59:59+00:00")

    def test_before_yesterday(self) -> None:
        assert _window("what did I do before yesterday") == (EARLIEST, "2026-09-07T23:59:59+00:00")

    def test_before_last_month(self) -> None:
        assert _window("what did I do before last month") == (EARLIEST, "2026-07-31T23:59:59+00:00")

    def test_before_the_last_few_days(self) -> None:
        assert _window("what did I do before last 3 days") == (EARLIEST, "2026-09-05T23:59:59+00:00")

    def test_the_read_is_still_marked_past(self) -> None:
        """The flag that WAS read keeps working; the fix adds the window, it replaces nothing."""
        plan = build_query_plan(None, "what did I work on before last week", now=NOW)
        assert plan.temporal_shift == "past"


class TestAfter:
    def test_after_last_week_runs_from_its_end_to_today(self) -> None:
        assert _window("what did I work on after last week") == (
            "2026-09-07T00:00:00+00:00", "2026-09-09T23:59:59+00:00",
        )

    def test_after_today_is_not_inverted_into_a_window_that_matches_nothing(self) -> None:
        """A period that has not happened yet. Left as the plain window rather than turned into
        one that starts after it ends."""
        assert _window("what did I do after today") == _window("what did I do today")


class TestSince:
    def test_since_last_week_includes_it_and_runs_to_today(self) -> None:
        assert _window("what have I done since last week") == (
            "2026-08-31T00:00:00+00:00", "2026-09-09T23:59:59+00:00",
        )

    def test_since_last_month(self) -> None:
        assert _window("what have I done since last month") == (
            "2026-08-01T00:00:00+00:00", "2026-09-09T23:59:59+00:00",
        )


class TestOnlyAGoverningWordCounts:
    def test_a_before_that_belongs_to_another_phrase_leaves_the_window(self) -> None:
        """"Before the meeting last week" is about last week; the "before" is the meeting's."""
        assert _window("what did I do before the meeting last week") == LAST_WEEK

    def test_no_boundary_word_no_change(self) -> None:
        assert _window("what did I work on last week") == LAST_WEEK

    def test_an_undated_ask_still_has_no_window(self) -> None:
        assert _window("what did I do before the meeting") is None


class TestWhatIsLeftAlone:
    def test_a_differenced_ask_keeps_its_union_window(self) -> None:
        plan = build_query_plan(None, "compare last week and this week", now=NOW)
        assert plan.comparison_intent
        assert plan.time_range == (plan.time_windows[0][0], plan.time_windows[-1][1])

    @pytest.mark.xfail(
        strict=True,
        reason="boundary words apply to relative windows only; an explicit date keeps its own "
               "reading. Recorded 2026-09-10, not fixed.",
    )
    def test_before_an_explicit_date(self) -> None:
        window = _window("what did I do before 2026-08-15")
        assert window and window[1] < "2026-08-15"


class TestTheWindowFilterCanUseIt:
    def test_before_last_week_keeps_older_rows_and_drops_last_weeks(self) -> None:
        items = [{"record_id": "older", "event_at": "2026-08-20T10:00:00+00:00"},
                 {"record_id": "last_week", "event_at": "2026-09-02T10:00:00+00:00"}]
        kept = _prefer_time_window(items, _window("what did I work on before last week"))
        assert [i["record_id"] for i in kept] == ["older"]

    def test_after_last_week_keeps_this_weeks_rows(self) -> None:
        items = [{"record_id": "last_week", "event_at": "2026-09-02T10:00:00+00:00"},
                 {"record_id": "this_week", "event_at": "2026-09-08T10:00:00+00:00"}]
        kept = _prefer_time_window(items, _window("what did I work on after last week"))
        assert [i["record_id"] for i in kept] == ["this_week"]

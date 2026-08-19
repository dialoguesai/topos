"""One plan, two windows, differenced — the shape behind "what changed between last
week and this week".

`_relative_time_range` returns the FIRST window it matches and stops. That is right for
a single-window ask and is exactly why a differenced ask lost its other half: the plan
searched "last week" and the answer differenced one period against nothing, which reads
as a confident "no change" rather than an admission that half the question was never
retrieved.

Deliberately NOT a temporal algebra. Two windows, both named, both relative, and only
when the sentence asks for the difference. "my work this week and last week" is a
two-period recall, not a difference, and stays on the single-window path it has always
been on.

The labels (`baseline` / `current`) are a closed set and are always earlier-then-later,
whatever order the sentence used — synthesis differencing "current minus baseline" must
not invert because the owner wrote "this week vs last week".
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from topos.query.planner import (
    WINDOW_BASELINE,
    WINDOW_CURRENT,
    build_query_plan,
    comparison_windows,
)
from topos.query.retrieval import _label_time_windows


# A Wednesday, so "this week" is a partial week and cannot coincide with "last week".
NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)


def plan(query: str):
    return build_query_plan(None, query, now=NOW)


class TestADifferencedAskKeepsBothWindows:
    def test_the_example_query_plans_two_windows(self) -> None:
        p = plan("what changed between last week and this week")
        assert p.comparison_intent is True
        assert len(p.time_windows) == 2
        assert p.time_windows[0][0].startswith("2026-08-10")  # last week
        assert p.time_windows[1][0].startswith("2026-08-17")  # this week

    def test_time_range_widens_to_the_union_so_both_sides_are_retrieved(self) -> None:
        p = plan("what changed between last week and this week")
        assert p.time_range == (p.time_windows[0][0], p.time_windows[1][1])

    def test_baseline_is_the_earlier_window_whatever_order_the_sentence_used(self) -> None:
        forward = plan("what changed between last week and this week")
        reversed_ = plan("how is this week different from last week")
        assert forward.time_windows == reversed_.time_windows, (
            "window order followed the sentence, so 'current minus baseline' would "
            "silently invert on half of the phrasings"
        )

    def test_months_difference_too(self) -> None:
        p = plan("what changed this month vs last month")
        assert p.comparison_intent is True
        assert p.time_windows[0][0].startswith("2026-07-01")
        assert p.time_windows[1][0].startswith("2026-08-01")

    @pytest.mark.parametrize(
        "query",
        [
            "what changed between last week and this week",
            "how is this week different from last week",
            "compare this week to last week",
            "did I do more or less this week versus last week",
        ],
    )
    def test_the_differenced_phrasings_all_land(self, query: str) -> None:
        assert len(plan(query).time_windows) == 2


class TestOrdinaryQueriesAreUntouched:
    @pytest.mark.parametrize(
        "query",
        [
            "what did I do last week",
            "my work this week",
            "my work this week and last week",  # recall of two periods, not a difference
            "what changed",  # difference verb, no windows named
            "what changed last week",  # difference verb, ONE window
        ],
    )
    def test_no_comparison_plan(self, query: str) -> None:
        p = plan(query)
        assert p.time_windows == []
        assert p.comparison_intent is False

    def test_a_single_window_query_keeps_the_window_it_always_had(self) -> None:
        assert plan("what did I do last week").time_range[0].startswith("2026-08-10")

    def test_to_meta_stays_the_same_shape_when_there_is_no_comparison(self) -> None:
        meta = plan("what did I do last week").to_meta()
        assert "time_windows" not in meta and "comparison_intent" not in meta

    def test_to_meta_carries_the_windows_when_there_is(self) -> None:
        meta = plan("what changed between last week and this week").to_meta()
        assert len(meta["time_windows"]) == 2
        assert meta["comparison_intent"] is True


class TestTheHashHelperAgreesWithThePlan:
    """The intent hash is computed before retrieval opens a connection, so it resolves
    the windows itself. If the two disagreed, the hash would key on a window the plan
    never searched."""

    def test_same_windows_without_a_connection(self) -> None:
        q = "what changed between last week and this week"
        assert comparison_windows(q, NOW) == plan(q).time_windows

    def test_empty_for_an_ordinary_query(self) -> None:
        assert comparison_windows("what did I do last week", NOW) == []


class TestItemsSayWhichSideTheyEvidence:
    def _items(self):
        return [
            {"record_id": "a", "event_at": "2026-08-12T09:00:00+00:00"},  # last week
            {"record_id": "b", "event_at": "2026-08-18T09:00:00+00:00"},  # this week
            {"record_id": "c", "event_at": "2026-07-01T09:00:00+00:00"},  # before both
            {"record_id": "d"},                                           # undated
        ]

    def test_each_item_is_labelled_with_its_window(self) -> None:
        windows = plan("what changed between last week and this week").time_windows
        items = self._items()
        assert _label_time_windows(items, windows) == 2
        assert items[0]["time_window_label"] == WINDOW_BASELINE
        assert items[1]["time_window_label"] == WINDOW_CURRENT

    def test_items_outside_both_windows_are_left_unlabelled(self) -> None:
        """They evidence neither side of the difference, and a label would let
        synthesis count them into one."""
        windows = plan("what changed between last week and this week").time_windows
        items = self._items()
        _label_time_windows(items, windows)
        assert "time_window_label" not in items[2]
        assert "time_window_label" not in items[3]

    def test_labels_are_a_closed_set_never_the_owners_words(self) -> None:
        windows = plan("what changed between last week and this week").time_windows
        items = self._items()
        _label_time_windows(items, windows)
        labels = {i["time_window_label"] for i in items if "time_window_label" in i}
        assert labels <= {WINDOW_BASELINE, WINDOW_CURRENT}

    def test_no_windows_labels_nothing(self) -> None:
        items = self._items()
        assert _label_time_windows(items, []) == 0
        assert _label_time_windows(items, None) == 0
        assert all("time_window_label" not in i for i in items)

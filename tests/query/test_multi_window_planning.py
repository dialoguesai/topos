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

from datetime import datetime, timedelta, timezone

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


# --- nested and overlapping windows ---------------------------------------------------
# A Saturday at 16:00 in a UTC-7 zone, as in the PNH holdout clock. "This week" is
# Mon 08-17 .. Sat 08-22 and contains "yesterday" (Fri 08-21).
SAT = datetime(2026, 8, 22, 16, 0, 0, tzinfo=timezone(timedelta(hours=-7)))


def splan(query: str):
    return build_query_plan(None, query, now=SAT)


def _week_items():
    return [
        {"record_id": "last_week", "event_at": "2026-08-13T17:00:00+00:00"},
        {"record_id": "monday", "event_at": "2026-08-17T17:00:00+00:00"},
        {"record_id": "saturday_before_window", "event_at": "2026-08-16T05:00:00+00:00"},
        {"record_id": "yesterday", "event_at": "2026-08-21T17:00:00+00:00"},
        {"record_id": "today", "event_at": "2026-08-22T17:00:00+00:00"},
    ]


def _labels(query: str):
    items = _week_items()
    _label_time_windows(items, splan(query).time_windows)
    return {i["record_id"]: i.get("time_window_label") for i in items}


class TestNestedWindows:
    @pytest.mark.parametrize("query", ["What changed between yesterday and this week?",
                                       "What changed between this week and yesterday?"])
    def test_union_runs_to_the_end_of_the_wider_window(self, query: str) -> None:
        p = splan(query)
        assert p.comparison_intent is True
        assert p.time_range == ("2026-08-17T00:00:00-07:00", "2026-08-22T23:59:59-07:00"), (
            "the span stopped at the end of yesterday and dropped today from retrieval")

    @pytest.mark.parametrize("query", ["What changed between yesterday and this week?",
                                       "What changed between this week and yesterday?"])
    def test_the_narrower_window_claims_its_items_and_both_labels_are_used(self, query: str) -> None:
        p = splan(query)
        assert p.time_windows[1] == ("2026-08-21T00:00:00-07:00", "2026-08-21T23:59:59-07:00")
        labels = _labels(query)
        assert labels["yesterday"] == WINDOW_CURRENT
        assert labels["monday"] == labels["today"] == WINDOW_BASELINE
        assert labels["last_week"] is None and labels["saturday_before_window"] is None

    def test_month_containing_week(self) -> None:
        labels = _labels("How is this month different from this week?")
        assert labels["last_week"] == WINDOW_BASELINE  # in the month, outside the week
        assert labels["monday"] == labels["yesterday"] == labels["today"] == WINDOW_CURRENT


class TestOverlappingAndDisjointWindows:
    @pytest.mark.parametrize("query", ["Compare the past 7 days with this week.",
                                       "Compare this week with the past 7 days."])
    def test_overlap_goes_to_the_narrower_window(self, query: str) -> None:
        p = splan(query)
        # past 7 days = 168 h ending now; this week = Mon 00:00 .. Sat 23:59 (~144 h).
        assert p.time_range == ("2026-08-15T16:00:00-07:00", "2026-08-22T23:59:59-07:00")
        labels = _labels(query)
        assert labels["monday"] == labels["yesterday"] == labels["today"] == WINDOW_CURRENT
        assert labels["saturday_before_window"] == WINDOW_BASELINE  # only in the past 7 days

    @pytest.mark.parametrize("query", ["Compare last week and this week.", "Compare this week and last week."])
    def test_disjoint_windows_are_unchanged(self, query: str) -> None:
        p = splan(query)
        assert p.time_range == ("2026-08-10T00:00:00-07:00", "2026-08-22T23:59:59-07:00")
        labels = _labels(query)
        assert labels["last_week"] == labels["saturday_before_window"] == WINDOW_BASELINE
        assert labels["monday"] == labels["yesterday"] == labels["today"] == WINDOW_CURRENT


def test_union_compares_instants_not_strings() -> None:
    from topos.query.planner import _union_span
    late_local = ("2026-08-21T00:00:00-07:00", "2026-08-21T23:59:59-07:00")  # ends 08-22 06:59Z
    early_utc = ("2026-08-22T05:00:00+00:00", "2026-08-22T06:00:00+00:00")
    assert _union_span([late_local, early_utc]) == ("2026-08-21T00:00:00-07:00", "2026-08-21T23:59:59-07:00")


def test_summary_packet_publishes_the_full_span_and_labels_both_sides(monkeypatch) -> None:
    from topos.query.manifest_validation import resolve_scope_manifest
    from topos.query.retrieval import DefaultSignalRetrievalAdapter
    from topos.query.types import RetrievalRequest
    from topos.storage.adapters.factory import AdapterFactory

    seen = []

    def summary(**args):
        seen.append(args["plan"].time_range)
        return [dict(item, summary_text=item["record_id"]) for item in _week_items()[1:]]

    monkeypatch.setattr("topos.query.retrieval._build_summary_items", summary)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: None)
    adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("memory"))
    packet = adapter.retrieve(RetrievalRequest(manifest=resolve_scope_manifest("work_context:read"), access_mode="summary",
        query_text="What changed between yesterday and this week?", owner_mode=True, now=SAT)).context_packet
    assert seen and seen[0][1] == "2026-08-22T23:59:59-07:00"
    window = packet["time_window"]
    assert (window["from"], window["to"]) == ("2026-08-17T00:00:00-07:00", "2026-08-22T23:59:59-07:00")
    assert [w["label"] for w in window["windows"]] == [WINDOW_BASELINE, WINDOW_CURRENT]
    labels = {s.get("summary_text"): s.get("time_window_label") for s in packet["summaries"]}
    assert labels.get("yesterday") == WINDOW_CURRENT
    assert labels.get("monday") == WINDOW_BASELINE and labels.get("today") == WINDOW_BASELINE

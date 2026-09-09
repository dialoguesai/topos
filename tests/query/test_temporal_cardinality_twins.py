"""SUITE-TEMPORAL-TWINS — words that move the window, or count the answer, must be read.

protects: SYS-query I1 and the operator half of the catalogue. This is a SENSITIVITY family, the
counterpart to the transform family's invariance: "before June" and "after June" are different
questions, "my three most recent projects" asks for three, and a plan that reads none of that
answers a question nobody asked.

Graded on `build_query_plan`, which is where these words become decisions, rather than on a
retrieval trace. The plan is the seam: if the distinction is not in it, no lane downstream can
recover it, and a test against retrieved rows would blame the wrong stage.

Backlog option H-04. What it found on 2026-09-09 is written into the assertions rather than
smoothed over — two of the four pairs are read, one is read only partly, and one is not read at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from topos.query.planner import build_query_plan, derive_as_of

pytestmark = [pytest.mark.check("C-quality-temporal-cardinality-twins")]

#: Fixed so the windows below are arithmetic rather than a function of the day the suite runs.
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _plan(query: str):
    """A plan with no database.  takes a connection for the lanes that consult
    the node; every distinction under test here is parsed from the sentence, so None is the honest
    fixture — and if one of them ever starts needing the node, this fails rather than passing on a
    silent default."""
    return build_query_plan(None, query, now=NOW)


class TestTheWindowMoves:
    """Pairs where the pipeline does read the distinction."""

    def test_last_week_and_this_month_are_different_windows(self) -> None:
        a, b = _plan("what did I work on last week"), _plan("what did I work on this month")
        assert a.time_range and b.time_range and a.time_range != b.time_range

    def test_a_relative_window_is_bounded_not_open(self) -> None:
        """A window that silently ran to now would make every dated ask a recency ask."""
        start, end = _plan("what did I work on last week").time_range
        assert start < end < NOW.isoformat()

    def test_an_undated_ask_has_no_window(self) -> None:
        """The floor for the pairs above: a plan that always produced a window would make every
        comparison meaningless."""
        assert _plan("what have I been working on").time_range is None


class TestAsOfMonths:
    """A bare month anchors a point in time, not a range — the fact-chain read."""

    def test_a_month_resolves_to_its_last_day(self) -> None:
        assert derive_as_of("what did I do in june", NOW) == "2026-06-30"

    def test_a_bare_future_month_reads_as_last_year(self) -> None:
        """December has not happened in 2026, so "in december" is the one that has."""
        assert derive_as_of("what did I do in december", NOW) == "2025-12-31"

    def test_a_month_with_a_day_is_a_date_not_an_as_of(self) -> None:
        assert derive_as_of("what did I do in March 13", NOW) is None

    def test_two_different_months_do_not_collide(self) -> None:
        assert derive_as_of("in june", NOW) != derive_as_of("in july", NOW)


class TestBeforeVersusAfter:
    """The pair the option names first, and the one that is only half read."""

    def test_before_marks_the_read_as_past_and_after_does_not(self) -> None:
        assert _plan("what did I work on before last week").temporal_shift == "past"
        assert _plan("what did I work on after last week").temporal_shift != "past"

    def test_but_the_window_itself_is_identical(self) -> None:
        """Measured, and stated as a fact rather than a verdict: the boundary word changes a flag
        and not the window, so both asks retrieve the same seven days. "Before last week" returns
        last week — which is the one interval it excludes."""
        before = _plan("what did I work on before last week").time_range
        after = _plan("what did I work on after last week").time_range
        assert before == after and before is not None

    @pytest.mark.xfail(
        strict=True,
        reason="the boundary word moves a flag, not the window; measured 2026-09-09 and filed "
               "against H-04 rather than fixed — moving a window is a retrieval change with its "
               "own two-sided cost",
    )
    def test_before_and_after_should_not_share_a_window(self) -> None:
        b, a = _plan("what did I work on before last week"), _plan("what did I work on after last week")
        assert b.time_range != a.time_range


class TestCardinality:
    """"my three most recent projects" and "my recent projects" ask for different amounts."""

    def test_neither_form_carries_a_count_today(self) -> None:
        """Recorded, not demanded. The plan has no cardinality field at all, so a count in the ask
        has nowhere to land — the number reaches retrieval only as a token, where the gate's
        integer exemption then ignores it. Naming it here means the day a count is parsed, the
        change is deliberate."""
        three = _plan("my three most recent projects")
        plain = _plan("my recent projects")
        assert not hasattr(three, "cardinality") or getattr(three, "cardinality", None) is None
        assert not hasattr(plain, "cardinality") or getattr(plain, "cardinality", None) is None

    def test_the_two_asks_are_indistinguishable_to_the_plan(self) -> None:
        three, plain = _plan("my three most recent projects"), _plan("my recent projects")
        for field in ("time_range", "aggregate_intent", "temporal_shift", "as_of"):
            assert getattr(three, field, None) == getattr(plain, field, None), field

    def test_an_aggregate_ask_is_at_least_recognised_as_one(self) -> None:
        """The one counting distinction the plan does make: "how many" is an aggregate, and a
        browse is not. Cardinality of the ANSWER is not the same as asking for a count, but it is
        the nearest thing the plan currently has."""
        assert _plan("how many messages did I send last week").aggregate_intent is True
        assert _plan("show me my messages from last week").aggregate_intent is False

"""Absolute dates are time framing, not content the rows must contain.

`_residual_content_tokens` returns the discriminative needles a retrieved row has to
match; when a query carries needles and nothing matches them, the rare gate returns an
honest empty lane rather than filler. That is correct behaviour — but `_RECENCY_TERMS`
only covered RELATIVE time ("yesterday", "lately"), so an absolute date leaked into the
needles.

Live 2026-08-17: `work_context:read` answered a dateless "what am I working on" with 25
items, including the exact goals the owner asked for ("Keep running install tests for the
new multi Topos installer…", event_at 2026-08-16). Scoped to Aug 11-16 — a window that
CONTAINS that goal — it returned nothing: `aug` and `2026` were needles, no goal text
contains "aug", and the lane was gated to empty. Naming the window destroyed the lane it
was meant to narrow, and the failure looked like missing data.

Dates belong to `plan.time_range` / `_prefer_time_window`, which filters and annotates
`in_time_window`. They must not also act as content.
"""

from __future__ import annotations

import pytest

from topos.query.retrieval import _query_tokens, _residual_content_tokens


def needles(query: str) -> list[str]:
    return _residual_content_tokens(_query_tokens(query))


class TestDatesAreNotContentNeedles:
    def test_the_live_failure_keeps_only_real_content(self) -> None:
        got = needles(
            "work goals and progress Aug 11 through Aug 16 2026, Dialogues and TOPOS, "
            "achievements and adjustments"
        )
        assert "aug" not in got
        assert "2026" not in got
        assert "11" not in got and "16" not in got
        assert "through" not in got
        # The genuinely discriminative words survive.
        assert {"dialogues", "topos"} <= set(got)

    def test_a_pure_date_scoped_ask_carries_no_needles_at_all(self) -> None:
        """No needles means "no specific ask" — the window alone narrows it."""
        assert needles("my work goals Aug 11-16, 2026") == []

    @pytest.mark.parametrize(
        "query,absent",
        [
            ("what did I spend on dining in March 2026", ("march", "2026")),
            ("commits between Aug 11 and Aug 16 2026", ("aug", "between", "11")),
            ("my journal for september 3", ("september", "3")),
            ("browsing on 2026-08-14", ("2026",)),
        ],
    )
    def test_month_and_year_tokens_never_survive(self, query: str, absent: tuple) -> None:
        got = needles(query)
        for token in absent:
            assert token not in got, f"{token!r} leaked into needles for {query!r}"

    def test_content_words_are_kept_alongside_the_date(self) -> None:
        assert "dining" in needles("what did I spend on dining in March 2026")
        assert "commits" in needles("commits between Aug 11 and Aug 16 2026")


class TestTheStripIsContextSensitive:
    """Only a nearby month or year turns numbers and range words into framing.

    The guard matters because `_query_tokens` does keep small integers in some
    phrasings ("issue 42 in March 2026" tokenises 42; "Aug 11 16 2026" keeps both day
    numbers). An unconditional strip would delete content from undated queries.
    """

    def test_day_numbers_are_stripped_only_in_a_date_scoped_ask(self) -> None:
        # Dated: 11 and 16 are the window the planner already consumed.
        assert needles("Aug 11 16 2026") == []
        # Undated: nothing here is date framing, so the content words remain.
        # ("sites" is a surface-intent term for the activity lane and is stripped for
        # that separate, pre-existing reason — "visited" is the content needle.)
        assert "visited" in needles("top sites I visited")

    def test_through_is_content_when_no_date_is_present(self) -> None:
        assert "through" in needles("did I see that project through")

    def test_through_becomes_framing_once_a_date_appears(self) -> None:
        assert "through" not in needles("see that project through in March 2026")

    def test_large_numbers_are_never_day_numbers(self) -> None:
        # Amounts and ids must survive even in a date-scoped ask.
        assert "12000" in needles("did I spend 12000 in March 2026")

    def test_day_numbers_out_of_calendar_range_stay_content(self) -> None:
        assert "42" in needles("what happened to issue 42 in March 2026")

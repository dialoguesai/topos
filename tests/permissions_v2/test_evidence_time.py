"""OD-53: a journal entry's zone-less timestamp is what it states, never a guessed instant.

protects: the grant window accepts only explicit UTC text, and the journal writer stores naive
local text, so every journal row was "undated: withhold" (0 of 501 parsed on the node this was
measured on). Two tempting fixes leak: reading naive text as UTC moves an entry across a window
edge by up to 14 hours, and releasing it as an instant tells a recipient a time of day the row
never proved. The rules here keep both out, on the node's own time-point grammar: a row is inside
a window only when every instant it can denote is, and a row without a recorded offset is released
as its stated day or not at all.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from topos.permissions_v2.evidence_time import CANONICAL, STATED_DAY, event_bounds, released_time, within_window

HOUR = 3600 * 1_000_000
ALL_RULES = (CANONICAL, STATED_DAY)


def _us(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()) * 1_000_000


DAY_START = _us("2026-09-10T00:00:00")
WRITTEN = _us("2026-09-10T08:30:00")
# A day began no earlier than 00:00 at UTC+14 and has ended everywhere by 12:00 UTC the next day.
DAY_SPAN = (DAY_START - 14 * HOUR, DAY_START + 36 * HOUR - 1)


@pytest.mark.parametrize("semantics", ALL_RULES)
@pytest.mark.parametrize("text", ["2026-09-10T08:30:00Z", "2026-09-10T08:30:00+00:00"])
def test_explicit_utc_text_is_an_instant_under_every_rule(semantics, text):
    assert event_bounds(text, semantics=semantics) == (WRITTEN, WRITTEN)


def test_the_journal_rule_reads_a_fraction_of_any_length_on_every_interpreter():
    """datetime.fromisoformat accepts only 0, 3 or 6 fractional digits on Python 3.10 and any length from
    3.11; the node's time-point grammar does not use it, so this rule decides the same on both. (The message
    rule keeps its own parser, ``canonical_utc_microseconds``, and is not changed here.)"""
    assert event_bounds("2026-09-10T08:30:00.25", semantics=STATED_DAY) == DAY_SPAN
    assert event_bounds("2026-09-10T08:30:00.5-05:00", semantics=STATED_DAY) == (
        WRITTEN + 500_000 + 5 * HOUR, WRITTEN + 500_000 + 5 * HOUR)


@pytest.mark.parametrize("text", ["2026-09-10T08:30:00", "2026-09-10T23:59:59.999999", "2026-09-10"])
def test_naive_text_is_undated_under_the_message_rule_and_a_stated_day_under_the_journal_rule(text):
    assert event_bounds(text, semantics=CANONICAL) is None
    assert event_bounds(text, semantics=STATED_DAY) == DAY_SPAN


def test_the_written_time_of_day_is_not_trusted():
    """Two entries of one stated day have one span, wherever in the day they say they were written."""
    assert event_bounds("2026-09-10T00:00:01", semantics=STATED_DAY) == event_bounds(
        "2026-09-10T23:59:59", semantics=STATED_DAY) == DAY_SPAN


def test_a_recorded_offset_is_an_exact_instant_under_the_journal_rule_only():
    semantics = STATED_DAY
    text = "2026-09-10T08:30:00-05:00"
    assert event_bounds(text, semantics=semantics) == (WRITTEN + 5 * HOUR, WRITTEN + 5 * HOUR)
    assert event_bounds(text, semantics=CANONICAL) is None  # the message rule is unchanged: explicit UTC only
    assert released_time(text, semantics=semantics, precision="second") == (WRITTEN + 5 * HOUR) // 1_000_000


@pytest.mark.parametrize("text", [
    "2026-02-30", "2026-13-01T00:00:00", "2026-09-10T25:00:00", "2026-09-10T08:30:60",   # not a real day or time
    "2026-09-10T08:30:00+15:00",                                                           # no such civil offset
    "2026-09", "2026",                                                                     # too coarse for a window
    " 2026-09-10", "2026-09-10 ", "2026-09-10 08:30:00", "20260910", "10 Sep 2026", "", None, 20260910, 1.5,
    b"2026-09-10"])
@pytest.mark.parametrize("semantics", ALL_RULES)
def test_anything_else_stays_unknown(text, semantics):
    assert event_bounds(text, semantics=semantics) is None
    assert not within_window(text, semantics=semantics, lower_us=-2**62, upper_us=2**62)
    assert released_time(text, semantics=semantics, precision="day") is None


def test_a_stated_day_is_inside_a_window_only_when_the_whole_day_is():
    inside = dict(semantics=STATED_DAY, lower_us=DAY_SPAN[0], upper_us=DAY_SPAN[1])
    assert within_window("2026-09-10T08:30:00", **inside)
    # The newest entries wait: until the day has ended everywhere it may still be "now", or later, somewhere.
    assert not within_window("2026-09-10T08:30:00", **{**inside, "upper_us": DAY_SPAN[1] - 1})
    assert not within_window("2026-09-10T08:30:00", **{**inside, "upper_us": DAY_START + 24 * HOUR})
    # An entry at the old edge leaves early: its day may have begun before the window did.
    assert not within_window("2026-09-10T08:30:00", **{**inside, "lower_us": DAY_SPAN[0] + 1})
    assert not within_window("2026-09-10T08:30:00", **{**inside, "lower_us": DAY_START})
    # Reading the naive text as UTC would have admitted it in all four.
    assert DAY_START <= WRITTEN <= DAY_START + 24 * HOUR


@pytest.mark.parametrize("semantics", ALL_RULES)
def test_an_instant_is_held_to_the_window_exactly(semantics):
    text = "2026-09-10T08:30:00Z"
    assert within_window(text, semantics=semantics, lower_us=WRITTEN, upper_us=WRITTEN)
    assert not within_window(text, semantics=semantics, lower_us=WRITTEN + 1, upper_us=WRITTEN + HOUR)
    assert not within_window(text, semantics=semantics, lower_us=WRITTEN - HOUR, upper_us=WRITTEN - 1)


@pytest.mark.parametrize("text", ["2026-09-10T08:30:00", "2026-09-10T23:59:59", "2026-09-10"])
def test_a_row_without_a_recorded_offset_releases_as_its_stated_day_or_not_at_all(text):
    semantics = STATED_DAY
    assert released_time(text, semantics=semantics, precision="day") == DAY_START // 1_000_000
    assert released_time(text, semantics=semantics, precision="second") is None  # it never proved an instant
    assert released_time(text, semantics=semantics, precision="none") is None
    assert released_time(text, semantics=CANONICAL, precision="day") is None


@pytest.mark.parametrize("semantics", ALL_RULES)
def test_an_instant_releases_as_the_message_rule_releases_it(semantics):
    text = "2026-09-10T08:30:00Z"
    assert released_time(text, semantics=semantics, precision="second") == WRITTEN // 1_000_000
    assert released_time(text, semantics=semantics, precision="day") == DAY_START // 1_000_000
    assert released_time(text, semantics=semantics, precision="none") is None


def test_an_unknown_rule_or_precision_is_an_error_not_a_default():
    with pytest.raises(ValueError):
        event_bounds("2026-09-10", semantics="local_time_v1")
    with pytest.raises(ValueError):
        within_window("2026-09-10", semantics="", lower_us=0, upper_us=1)
    with pytest.raises(ValueError):
        released_time("2026-09-10", semantics=STATED_DAY, precision="hour")

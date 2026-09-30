"""OD-53: a journal entry's zone-less timestamp is its stated day, never a guessed instant.

protects: the grant window accepts only explicit UTC text, and the journal writer stores naive
local text, so every journal row was "undated: withhold" (0 of 501 parsed on the node this was
measured on). Two tempting fixes leak: reading naive text as UTC moves an entry across a window
edge by up to 14 hours, and releasing it as an instant tells a recipient a time of day the row
never proved. ``stated_day_v1`` keeps both out: the row is its calendar day, inside a window only
when the whole day is under every offset, and released as a day or not at all.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from topos.permissions_v2.evidence_time import (CANONICAL, STATED_DAY, event_bounds, released_time, within_window)

HOUR = 3600 * 1_000_000


def _us(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()) * 1_000_000


DAY_START = _us("2026-09-10T00:00:00")


@pytest.mark.parametrize("semantics", [CANONICAL, STATED_DAY])
@pytest.mark.parametrize("text", ["2026-09-10T08:30:00Z", "2026-09-10T08:30:00+00:00", "2026-09-10T08:30:00.250000Z"])
def test_explicit_utc_text_is_an_instant_under_either_rule(semantics, text):
    earliest, latest = event_bounds(text, semantics=semantics)
    assert earliest == latest and earliest // 1_000_000 == _us("2026-09-10T08:30:00") // 1_000_000


@pytest.mark.parametrize("text", ["2026-09-10T08:30:00", "2026-09-10T23:59:59.999999", "2026-09-10"])
def test_naive_text_is_undated_under_the_message_rule_and_a_stated_day_under_the_journal_rule(text):
    assert event_bounds(text, semantics=CANONICAL) is None
    # The day began no earlier than 00:00 at UTC+14 and has ended everywhere by 12:00 UTC the next day.
    assert event_bounds(text, semantics=STATED_DAY) == (DAY_START - 14 * HOUR, DAY_START + 36 * HOUR)


@pytest.mark.parametrize("text", [
    "2026-09-10T08:30:00-05:00", "2026-09-10T08:30:00+05:30",   # another offset: not this node's convention
    "2026-02-30", "2026-13-01T00:00:00", "2026-09-10T25:00:00",  # not a real day or time
    " 2026-09-10", "2026-09-10 ", "2026-09-10 08:30:00", "20260910", "10 Sep 2026", "", None, 20260910, 1.5, b"2026-09-10"])
@pytest.mark.parametrize("semantics", [CANONICAL, STATED_DAY])
def test_anything_else_stays_unknown(text, semantics):
    assert event_bounds(text, semantics=semantics) is None
    assert not within_window(text, semantics=semantics, lower_us=0, upper_us=2**62)
    assert released_time(text, semantics=semantics, precision="day") is None


def test_a_stated_day_is_inside_a_window_only_when_the_whole_day_is():
    inside = dict(semantics=STATED_DAY, lower_us=DAY_START - 14 * HOUR, upper_us=DAY_START + 36 * HOUR)
    assert within_window("2026-09-10T08:30:00", **inside)
    # The newest entries wait: until the day has ended everywhere, it may still be "now" or later somewhere.
    assert not within_window("2026-09-10T08:30:00", **{**inside, "upper_us": DAY_START + 36 * HOUR - 1})
    assert not within_window("2026-09-10T08:30:00", **{**inside, "upper_us": DAY_START + 24 * HOUR})
    # An entry at the old edge leaves early: its day may have begun before the window did.
    assert not within_window("2026-09-10T08:30:00", **{**inside, "lower_us": DAY_START - 14 * HOUR + 1})
    assert not within_window("2026-09-10T08:30:00", **{**inside, "lower_us": DAY_START})
    # Reading the naive text as UTC would have admitted it in both cases.
    naive_as_utc = _us("2026-09-10T08:30:00")
    assert DAY_START <= naive_as_utc <= DAY_START + 24 * HOUR


def test_an_instant_is_held_to_the_window_exactly():
    text, at = "2026-09-10T08:30:00Z", _us("2026-09-10T08:30:00")
    for semantics in (CANONICAL, STATED_DAY):
        assert within_window(text, semantics=semantics, lower_us=at, upper_us=at)
        assert not within_window(text, semantics=semantics, lower_us=at + 1, upper_us=at + HOUR)
        assert not within_window(text, semantics=semantics, lower_us=at - HOUR, upper_us=at - 1)


def test_a_stated_day_releases_as_its_day_or_not_at_all():
    naive = "2026-09-10T08:30:00"
    assert released_time(naive, semantics=STATED_DAY, precision="day") == DAY_START // 1_000_000
    assert released_time(naive, semantics=STATED_DAY, precision="second") is None  # it never stated an instant
    assert released_time(naive, semantics=STATED_DAY, precision="none") is None
    assert released_time(naive, semantics=CANONICAL, precision="day") is None


def test_an_instant_releases_as_the_message_rule_releases_it():
    text, at = "2026-09-10T08:30:00Z", _us("2026-09-10T08:30:00") // 1_000_000
    for semantics in (CANONICAL, STATED_DAY):
        assert released_time(text, semantics=semantics, precision="second") == at
        assert released_time(text, semantics=semantics, precision="day") == DAY_START // 1_000_000
        assert released_time(text, semantics=semantics, precision="none") is None


def test_an_unknown_rule_or_precision_is_an_error_not_a_default():
    with pytest.raises(ValueError):
        event_bounds("2026-09-10", semantics="local_time_v1")
    with pytest.raises(ValueError):
        released_time("2026-09-10", semantics=STATED_DAY, precision="hour")

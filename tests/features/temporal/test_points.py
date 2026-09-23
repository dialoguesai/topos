"""The temporal regression set for the pure point module.

Organised by the categories the plan names: precision, unknown, timezone
boundaries, future values, and the comparison rule itself. Producer and store
behaviour (re-extraction, corrections) is tested where those writers live.
"""
from datetime import datetime, timezone
import random

import pytest

from topos.features.temporal.points import (MICROSECONDS_PER_DAY, TimePoint, days_from_civil, occurred_by,
    order, parse_point, span, unknown, within)
from topos.features.temporal.records import (EventTime, FactTemporal, evidence_from_row, event_time, fact_temporal,
    producer_clock, read_fact_temporal)

H = 3_600 * 1_000_000


def utc_us(*parts):
    return int(datetime(*parts, tzinfo=timezone.utc).timestamp()) * 1_000_000


def p(text, provenance="stated_in_content"):
    return parse_point(text, provenance=provenance)


# --- precision is never promoted ----------------------------------------------

@pytest.mark.parametrize("text,precision,basis", [
    ("2026-09-15T21:11:24Z", "instant", "utc"),
    ("2026-09-15T21:11:24.5Z", "instant", "utc"),
    ("2026-09-15T21:11:24.123456+00:00", "instant", "utc"),
    ("2026-09-15T21:11:24-00:00", "instant", "utc"),
    ("2026-09-15T21:11:24+05:30", "instant", "fixed_offset"),
    ("2026-09-15T21:11:24", "instant", "unrecorded"),
    ("2026-09-15", "day", "unrecorded"),
    ("2026-09", "month", "unrecorded"),
    ("2026", "year", "unrecorded"),
])
def test_each_form_keeps_its_own_precision_and_basis(text, precision, basis):
    point = p(text)
    assert (point.precision, point.basis, point.text) == (precision, basis, text)


def test_a_year_is_never_an_instant_at_midnight():
    """The profile extractor's `YYYY-01-01T00:00:00+00:00` is exactly the bug.

    The point for "2021" must cover the whole year across every offset, so it
    has not occurred by the instant its old encoding claimed.
    """
    year = p("2021")
    assert year.precision == "year"
    invented_midnight = utc_us(2021, 1, 1)
    assert occurred_by(year, invented_midnight) is False
    assert occurred_by(year, utc_us(2022, 1, 1) + 12 * H) is True
    assert order(p("2021-01-01T00:00:00Z"), year) == "overlapping"


# --- unknown stays unknown ----------------------------------------------------

@pytest.mark.parametrize("raw", [
    None, "", " ", "2026-09-15 ", " 2026-09-15", "1789506684", 1789506684, "2026-13-01", "2026-02-30",
    "2023-02-29", "0000-01-01", "2026-9-15", "2026-09-15T24:00:00Z", "2026-09-15T21:60:00Z",
    "2026-09-15T21:11:60Z", "2026-09-15T21:11:24.1234567Z", "2026-09-15T21:11:24+15:00",
    "2026-09-15T21:11:24-12:30", "2026-09-15T21:11:24+05:60", "2026-09-15T21:11:24z", "last year", "college",
    "2026-W37", "2026-258", "20260915", "2026/09/15", "2026-09-15T21:11Z",
])
def test_everything_outside_the_grammar_is_unknown(raw):
    point = parse_point(raw, provenance="unverified_producer")
    assert not point.known and point.text is None and point.basis == "unknown"
    assert span(point) is None
    assert order(point, p("2026")) is None and order(p("2026"), point) is None
    assert occurred_by(point, utc_us(2030, 1, 1)) is None
    assert within(point, 0, utc_us(2030, 1, 1)) is None


def test_an_unknown_point_keeps_its_provenance():
    assert parse_point("", provenance="ingestion_clock_substitute").provenance == "ingestion_clock_substitute"


def test_a_point_cannot_be_constructed_inconsistently():
    with pytest.raises(ValueError):
        TimePoint("2026", "instant", "utc", None, "stated_in_content")
    with pytest.raises(ValueError):
        TimePoint(None, "day", "unrecorded", None, "unknown")
    with pytest.raises(ValueError):
        TimePoint("2026-09-15T21:11:24+05:30", "instant", "fixed_offset", 60, "stated_in_content")
    with pytest.raises(ValueError):
        TimePoint("2026", "year", "unrecorded", None, "made_up")


# --- timezone boundaries ------------------------------------------------------

def test_a_stated_day_spans_every_civil_offset():
    day = span(p("2026-09-15"))
    # Begins at 00:00 in UTC+14 and ends at 24:00 in UTC-12.
    assert day.lo == utc_us(2026, 9, 14, 10) and day.hi == utc_us(2026, 9, 16, 12) and not day.hi_inclusive


def test_a_naive_clock_reading_spans_every_civil_offset():
    reading = span(p("2026-09-15T00:00:00"))
    assert reading.lo == utc_us(2026, 9, 14, 10) and reading.hi == utc_us(2026, 9, 15, 12) and reading.hi_inclusive


def test_an_explicit_offset_is_exact_and_can_cross_a_utc_date():
    tokyo = span(p("2026-09-16T01:00:00+09:00"))
    assert tokyo.lo == tokyo.hi == utc_us(2026, 9, 15, 16)
    honolulu = span(p("2026-09-15T20:00:00-10:00"))
    assert honolulu.lo == utc_us(2026, 9, 16, 6)
    assert order(p("2026-09-16T01:00:00+09:00"), p("2026-09-15T20:00:00-10:00")) == "before"


def test_the_civil_offset_extremes_are_accepted_and_nothing_beyond():
    assert p("2026-09-15T00:00:00+14:00").known and p("2026-09-15T00:00:00-12:00").known
    assert not p("2026-09-15T00:00:00+14:01").known and not p("2026-09-15T00:00:00-12:01").known


def test_leap_days_and_month_lengths():
    assert p("2024-02-29").known and not p("2025-02-29").known and not p("1900-02-29").known and p("2000-02-29").known
    february = span(p("2024-02"))
    assert february.hi - february.lo == 29 * MICROSECONDS_PER_DAY + 26 * H


def test_days_from_civil_matches_the_standard_library_across_the_whole_range():
    rng = random.Random(20260916)
    for _ in range(5000):
        year, month = rng.randint(1, 9999), rng.randint(1, 12)
        day = rng.randint(1, 28)
        expected = (datetime(year, month, day) - datetime(1970, 1, 1)).days
        assert days_from_civil(year, month, day) == expected


# --- ordering -----------------------------------------------------------------

def test_order_is_before_only_when_nothing_could_overlap():
    """A stated day with an unrecorded timezone covers 50 hours of UTC.

    So adjacent days overlap, and so do days two apart: the 13th ends at 12:00Z
    on the 14th (UTC-12) while the 15th begins at 10:00Z on the 14th (UTC+14).
    Only three days apart can a stated day be known to come first.
    """
    assert order(p("2026-09-14"), p("2026-09-15")) == "overlapping"
    assert order(p("2026-09-13"), p("2026-09-15")) == "overlapping"
    assert order(p("2026-09-13"), p("2026-09-16")) == "before"
    assert order(p("2026-09-15T21:00:00Z"), p("2026-09-15T21:00:00Z")) == "overlapping"
    assert order(p("2026-09-15T21:00:00Z"), p("2026-09-15T21:00:00.000001Z")) == "before"
    assert order(p("2026-09-15T21:00:00.000001Z"), p("2026-09-15T21:00:00Z")) == "after"
    assert order(p("2019"), p("2026-09-15T21:00:00Z")) == "before"


# --- future values ------------------------------------------------------------

def test_a_future_stated_applicability_has_not_occurred():
    now = utc_us(2026, 9, 16, 12)
    assert occurred_by(p("2027"), now) is False
    assert occurred_by(p("2026-09-17"), now) is False
    # The 17th has already begun in UTC+14 (at 10:00Z on the 16th), so it is
    # neither inside nor outside a window ending now. The 18th has not begun
    # anywhere yet.
    assert within(p("2026-09-17"), now - 30 * 24 * H, now) is None
    assert within(p("2026-09-18"), now - 30 * 24 * H, now) is False


def test_within_is_true_only_when_the_whole_point_is_inside():
    now = utc_us(2026, 9, 16, 12)
    assert within(p("2026-09-16T11:59:59Z"), now - H, now) is True
    assert within(p("2026-09-16T12:00:00Z"), now - H, now) is True
    assert within(p("2026-09-16T12:00:00.000001Z"), now - H, now) is False
    # A day that straddles the window edge can be neither accepted nor refused.
    assert within(p("2026-09-16"), now - 12 * H, now) is None


# --- records ------------------------------------------------------------------

def test_a_fact_record_round_trips_canonically_and_rejects_anything_else():
    record = fact_temporal(asserted=producer_clock(datetime(2026, 9, 16, 12, tzinfo=timezone.utc)),
                           applies_start=p("2021"), evidence=p("2026-09-15T21:11:24Z", "native_source_clock"))
    raw = record.to_json()
    assert read_fact_temporal(raw) == record
    assert read_fact_temporal(None) is None
    with pytest.raises(ValueError):
        read_fact_temporal(raw.replace('"applies"', ' "applies"'))
    with pytest.raises(ValueError):
        read_fact_temporal(raw.replace("topos-fact-temporal/v1", "topos-fact-temporal/v2"))


def test_the_assertion_time_is_always_an_explicit_utc_instant_from_the_asserting_clock():
    with pytest.raises(ValueError):
        FactTemporal(p("2026-09-16"), unknown(), unknown(), unknown())
    with pytest.raises(ValueError):
        FactTemporal(p("2026-09-16T12:00:00Z", "native_source_clock"), unknown(), unknown(), unknown())
    with pytest.raises(ValueError):
        producer_clock(datetime(2026, 9, 16, 12))
    assert fact_temporal().asserted.provenance == "producer_clock"


def test_a_row_without_a_record_contributes_an_unverified_event_time():
    point = evidence_from_row({"event_at": "2026-09-15T21:11:24+00:00"})
    assert point.known and point.provenance == "unverified_producer"
    recorded = event_time("2026-09-15T21:11:24.000000+00:00", provenance="native_source_clock")
    assert evidence_from_row({"event_at": "whatever", "event_time_json": recorded.to_json()}).provenance == "native_source_clock"
    with pytest.raises(ValueError):
        evidence_from_row({"event_time_json": '{"version":"topos-event-time/v1"}'})
    assert EventTime.from_json(recorded.to_json()) == recorded

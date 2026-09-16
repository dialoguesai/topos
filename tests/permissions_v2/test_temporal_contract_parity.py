"""The separated-time rule agrees with the permission time contracts already signed.

A future fact-validity or event-time contract will be built on
``topos.features.temporal.points``. These properties pin that it starts from the
behaviour of ``exact_instant_v1``, ``stated_day_v1`` and
``canonical_event_time_v1`` rather than beside it, so a grant does not change
meaning merely because its evaluator moved to the new module.

One divergence is deliberate and asserted: the signed parser accepts fractional
seconds through ``datetime.fromisoformat``, which on Python 3.10 accepts only 0,
3 or 6 digits although the contract text says 1-6. The new grammar accepts 1-6
on every interpreter. Parity is therefore asserted over the forms the signed
parser accepts on the interpreter running the tests, and the divergence itself
is pinned so it cannot change unnoticed.
"""
import random
import sys

from topos.features.temporal.points import event_window, occurred_by, parse_point, within
from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds, stated_day_elapsed_microseconds

RNG_SEED = 20260916


def _instants(rng, count):
    for _ in range(count):
        year = rng.randint(1, 9999)
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        text = f"{year:04d}-{month:02d}-{day:02d}T{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"
        fraction = rng.choice(["", f".{rng.randint(0, 999):03d}", f".{rng.randint(0, 999999):06d}"])
        yield text + fraction + rng.choice(["Z", "+00:00"])


def test_an_explicit_utc_instant_occurs_exactly_when_exact_instant_v1_says_it_is_current():
    rng = random.Random(RNG_SEED)
    for text in _instants(rng, 4000):
        start = canonical_utc_microseconds(text)
        assert start is not None, text
        point = parse_point(text, provenance="stated_in_content")
        for anchor in (start - 1, start, start + 1, start + rng.randint(-10**12, 10**12)):
            assert occurred_by(point, anchor) is (start <= anchor), (text, anchor)


def test_a_stated_day_occurs_exactly_when_stated_day_v1_says_it_is_current():
    rng = random.Random(RNG_SEED + 1)
    days = [f"{rng.randint(1, 9999):04d}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}" for _ in range(4000)]
    days += ["2024-02-29", "2000-02-29", "0001-01-01", "9999-12-31", "1970-01-01", "1969-12-31"]
    for text in days:
        current_from = stated_day_elapsed_microseconds(text)
        assert current_from is not None, text
        point = parse_point(text, provenance="stated_in_content")
        for anchor in (current_from - 1, current_from, current_from + 1):
            assert occurred_by(point, anchor) is (current_from <= anchor), (text, anchor)


def test_both_reject_the_same_malformed_days():
    for text in ("2023-02-29", "0000-01-01", "2026-9-15", "2026-13-01", "2026-09-15 ", "20260915"):
        assert stated_day_elapsed_microseconds(text) is None
        assert not parse_point(text, provenance="stated_in_content").known


def test_an_explicit_utc_instant_matches_the_signed_event_window_in_all_three_values():
    """Not only on `True`. A deny clause ORs its time match, so a future event
    must stay unknown: `False` there would let a matching deny silently not match.
    """
    rng = random.Random(RNG_SEED + 2)
    for text in _instants(rng, 4000):
        event = canonical_utc_microseconds(text)
        point = parse_point(text, provenance="native_source_clock")
        anchor = event + rng.choice([-5, -1, 0, 1, 5, 86_400_000_000])
        max_age = rng.choice([0, 1, 86_400_000_000])
        signed = None if event > anchor else anchor - max_age <= event
        assert event_window(point, anchor, max_age) is signed, (text, anchor, max_age)


def test_a_future_event_leaves_a_deny_clause_unknown_not_unmatched():
    from topos.permissions_v2.fact_policy import _and, _or
    point = parse_point("2026-09-16T12:00:00.000001Z", provenance="native_source_clock")
    anchor = canonical_utc_microseconds("2026-09-16T12:00:00Z")
    time_match = event_window(point, anchor, 86_400_000_000)
    assert time_match is None
    assert _or([_and([time_match, True])]) is None
    assert within(point, anchor - 86_400_000_000, anchor) is False  # which is why within() is not the contract


def test_the_fraction_divergence_from_the_signed_parser_is_exactly_what_the_interpreter_implies():
    lengths = {}
    for digits in range(1, 7):
        text = "2026-09-15T21:11:24." + "1" * digits + "Z"
        lengths[digits] = (canonical_utc_microseconds(text) is not None, parse_point(text, provenance="unknown").known)
    assert all(new for _signed, new in lengths.values())
    if sys.version_info < (3, 11):
        assert {digits for digits, (signed, _new) in lengths.items() if signed} == {3, 6}
    else:
        assert all(signed for signed, _new in lengths.values())

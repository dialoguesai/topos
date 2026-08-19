"""P3's two new retrieval-identity inputs must be in the hash, for the same reason
`retrieval_text` had to be.

Per-part needles and a differenced ask's two windows both change WHAT COMES BACK while
leaving scope + mode + query + retrieval_text identical. Two report sections that differ
only in their parts, or the same weekly comparison asked in two different weeks, would
otherwise collide in `compute_intent_hash`; `_load_cached_artifact` would then return the
first call's `public_result` verbatim as the second's, stamped `turn_outcome: memory_hit`.

That is not a stale answer. It is one section's findings presented as another section's,
looking healthy — strictly worse than an honest empty, which is the property the whole
hash exists to protect.
"""

from __future__ import annotations

import hashlib

from topos.query.intent import compute_intent_hash


BASE = dict(
    scope_id="health:read",
    access_mode="summary",
    query_text="How was my week? 1) goals 2) surprises",
)


class TestPartsAreRetrievalIdentity:
    def test_different_parts_are_different_turns(self) -> None:
        a = compute_intent_hash(**BASE, needle_parts=["goals progress", "surprises"])
        b = compute_intent_hash(**BASE, needle_parts=["goals progress", "sleep"])
        assert a != b, (
            "two requests whose parts gate different lanes hashed identically — the "
            "artifact cache will serve one's answer as the other's"
        )

    def test_part_order_is_part_of_the_identity(self) -> None:
        a = compute_intent_hash(**BASE, needle_parts=["goals", "surprises"])
        b = compute_intent_hash(**BASE, needle_parts=["surprises", "goals"])
        assert a != b

    def test_a_part_count_change_is_a_different_turn(self) -> None:
        a = compute_intent_hash(**BASE, needle_parts=["goals", "surprises"])
        b = compute_intent_hash(**BASE, needle_parts=["goals"])
        assert a != b

    def test_parts_normalize_like_the_query_does(self) -> None:
        a = compute_intent_hash(**BASE, needle_parts=["Goals   Progress", " Surprises "])
        b = compute_intent_hash(**BASE, needle_parts=["goals progress", "surprises"])
        assert a == b

    def test_blank_parts_are_not_identity(self) -> None:
        a = compute_intent_hash(**BASE, needle_parts=["goals", "", "   "])
        b = compute_intent_hash(**BASE, needle_parts=["goals"])
        assert a == b

    def test_punctuation_cannot_forge_a_collision_across_parts(self) -> None:
        """`normalize_query` only lowercases and collapses whitespace, so an owner
        sentence can contain the separator characters the payload uses. Two genuinely
        different part lists must not be able to serialize to the same string."""
        a = compute_intent_hash(**BASE, needle_parts=["goals|surprises"])
        b = compute_intent_hash(**BASE, needle_parts=["goals", "surprises"])
        assert a != b


class TestWindowsAreRetrievalIdentity:
    def test_different_windows_are_different_turns(self) -> None:
        this_week = compute_intent_hash(
            **BASE,
            time_windows=[
                ("2026-08-10T00:00:00+00:00", "2026-08-16T23:59:59+00:00"),
                ("2026-08-17T00:00:00+00:00", "2026-08-19T23:59:59+00:00"),
            ],
        )
        next_week = compute_intent_hash(
            **BASE,
            time_windows=[
                ("2026-08-17T00:00:00+00:00", "2026-08-23T23:59:59+00:00"),
                ("2026-08-24T00:00:00+00:00", "2026-08-26T23:59:59+00:00"),
            ],
        )
        assert this_week != next_week, (
            "the same differenced question asked a week apart searches different "
            "windows and must not be served from the earlier week's artifact"
        )

    def test_window_order_is_part_of_the_identity(self) -> None:
        a = compute_intent_hash(**BASE, time_windows=[("a", "b"), ("c", "d")])
        b = compute_intent_hash(**BASE, time_windows=[("c", "d"), ("a", "b")])
        assert a != b

    def test_a_single_window_differs_from_two(self) -> None:
        a = compute_intent_hash(**BASE, time_windows=[("a", "b")])
        b = compute_intent_hash(**BASE, time_windows=[("a", "b"), ("c", "d")])
        assert a != b


class TestNothingExistingIsOrphaned:
    def _legacy(self) -> str:
        return hashlib.sha256(
            "health:read|summary|how was my week? 1) goals 2) surprises".encode()
        ).hexdigest()[:16]

    def test_absent_fields_hash_exactly_as_before(self) -> None:
        assert compute_intent_hash(**BASE) == self._legacy()
        assert compute_intent_hash(**BASE, needle_parts=None, time_windows=None) == self._legacy()
        assert compute_intent_hash(**BASE, needle_parts=[], time_windows=[]) == self._legacy()

    def test_the_retrieval_text_formula_is_unchanged(self) -> None:
        assert compute_intent_hash(**BASE, retrieval_text="goals progress") != self._legacy()
        assert compute_intent_hash(**BASE, retrieval_text="") == self._legacy()
        assert compute_intent_hash(**BASE, retrieval_text=BASE["query_text"]) == self._legacy()

    def test_one_part_repeating_the_query_adds_nothing(self) -> None:
        """A single part that just restates the request is the single-needle request
        spelled a second way — splitting the cache for it buys no correctness."""
        assert compute_intent_hash(**BASE, needle_parts=[BASE["query_text"]]) == self._legacy()

    def test_one_part_repeating_the_needles_adds_nothing(self) -> None:
        with_needles = compute_intent_hash(**BASE, retrieval_text="goals progress")
        assert (
            compute_intent_hash(
                **BASE, retrieval_text="goals progress", needle_parts=["goals progress"]
            )
            == with_needles
        )

    def test_parts_and_needles_are_independent_dimensions(self) -> None:
        a = compute_intent_hash(**BASE, retrieval_text="goals", needle_parts=["goals", "sleep"])
        b = compute_intent_hash(**BASE, retrieval_text="sleep", needle_parts=["goals", "sleep"])
        assert a != b

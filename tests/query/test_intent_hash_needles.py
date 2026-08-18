"""`retrieval_text` is part of a turn's retrieval identity, so it must be part of its hash.

It steers the rare-gate needles and the semantic query. Two calls sharing session + scope +
mode + query but differing in `retrieval_text` retrieve different things; before this fix
they collided in `compute_intent_hash`, and the artifact cache returned the first call's
`public_result` verbatim as the second's, stamped `memory_hit`. A healthy-looking,
per-section fabrication — strictly worse than an honest empty. Latent today (nothing issues
that call pair yet); P3's per-section retrieval would have made it reachable, which is why
the design review ordered this fix FIRST.
"""

from __future__ import annotations

from topos.query.intent import compute_intent_hash


BASE = dict(scope_id="health:read", access_mode="summary", query_text="How was my week? 1) goals 2) surprises")


def test_different_needles_are_different_turns() -> None:
    a = compute_intent_hash(**BASE, retrieval_text="goals progress")
    b = compute_intent_hash(**BASE, retrieval_text="surprises attention shifts")
    assert a != b, (
        "two turns that retrieve different things hashed identically — the artifact "
        "cache will serve one section's answer as the other's, stamped memory_hit"
    )


def test_absent_needles_hash_exactly_as_before() -> None:
    """Every existing caller and cached artifact keys on the old formula. Byte-identical."""
    import hashlib

    legacy = hashlib.sha256(
        "health:read|summary|how was my week? 1) goals 2) surprises".encode()
    ).hexdigest()[:16]
    assert compute_intent_hash(**BASE) == legacy
    assert compute_intent_hash(**BASE, retrieval_text="") == legacy


def test_needles_equal_to_the_query_add_nothing() -> None:
    """retrieval_text that merely repeats the query is the same retrieval identity —
    hashing it separately would split today's cache for no behavioural difference."""
    same = compute_intent_hash(**BASE, retrieval_text=BASE["query_text"])
    assert same == compute_intent_hash(**BASE)


def test_needle_normalization_matches_query_normalization() -> None:
    a = compute_intent_hash(**BASE, retrieval_text="Goals   Progress")
    b = compute_intent_hash(**BASE, retrieval_text="goals progress")
    assert a == b

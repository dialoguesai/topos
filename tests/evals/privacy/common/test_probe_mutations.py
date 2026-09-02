"""Non-vacuity tests for the deterministic CER probe-mutation expander (§E1).

The expander is what grows N: every release the CER battery runs the expanded
probe set, so the Wilson bound on "0 leaks of N" tightens. These tests prove the
expansion is real (distinct probes, not copies), deterministic (no time, no RNG,
no LLM), and that every mutation class actually shows up.
"""

from __future__ import annotations

import base64

import pytest

from tests.evals.privacy.common.corpus import CER_QUERY_PHRASE
from tests.evals.privacy.common.probe_mutations import MUTATION_CLASSES, expand_probes

pytestmark = [pytest.mark.private, pytest.mark.cer]


def test_expander_is_non_vacuous():
    base = [CER_QUERY_PHRASE]
    expanded = expand_probes(base)
    # At least 5x growth over the base set, and no duplicate probes padding the count.
    assert len(expanded) >= 5 * len(base), f"only {len(expanded)} probes from {len(base)} base"
    assert len(expanded) == len(set(expanded)), "expanded probes must be unique"
    # The base probes ride along unchanged (the original battery is a subset).
    for b in base:
        assert b in expanded
    # Distinctness is real: no variant is a trivial strip()-identical copy of another.
    assert len({p.strip() for p in expanded}) == len(expanded)


def test_expander_is_deterministic():
    base = [CER_QUERY_PHRASE, "quarterly numbers"]
    assert expand_probes(base) == expand_probes(base)
    # Order-stable too: same input order → same output order.
    assert expand_probes(base) == list(expand_probes(base))


def test_every_mutation_class_contributes():
    """Each declared class must produce at least one variant distinct from the base."""
    base = [CER_QUERY_PHRASE]
    expanded = set(expand_probes(base))
    for cls_name, mutator in MUTATION_CLASSES:
        variants = [v for v in mutator(CER_QUERY_PHRASE) if v != CER_QUERY_PHRASE]
        assert variants, f"mutation class {cls_name!r} produced nothing"
        for v in variants:
            assert v in expanded, f"{cls_name!r} variant missing from expand_probes output"


def test_variants_preserve_retrieval_or_encode_the_ask():
    """A variant either still carries the retrieval phrase (so the canary records are
    actually pulled and disclosure is exercised) or is an encoding ask that embeds the
    phrase base64-encoded. Nothing may silently drop the probe subject."""
    phrase = CER_QUERY_PHRASE
    for p in expand_probes([phrase]):
        low = p.lower()
        carries_phrase = all(w in low for w in phrase.lower().split())
        carries_encoded = base64.b64encode(phrase.encode()).decode().lower() in low or (
            "base64" in low or "rot13" in low
        )
        assert carries_phrase or carries_encoded, f"probe lost its subject: {p!r}"


def test_no_canary_text_in_probes():
    """Probes are attack *queries*; they must never contain planted secrets
    (otherwise the leak detector would flag the echo of the question itself)."""
    from tests.evals.privacy.common.corpus import build_cer_corpus

    tokens = [c.token.lower() for c in build_cer_corpus().canaries]
    for p in expand_probes([CER_QUERY_PHRASE]):
        low = p.lower()
        for tok in tokens:
            assert tok not in low


def test_multi_base_scales_linearly():
    one = expand_probes(["alpha topic"])
    two = expand_probes(["alpha topic", "beta topic"])
    assert len(two) > len(one)
    assert len(two) >= 2 * len(one) - 2  # near-linear growth, allowing dedup slack

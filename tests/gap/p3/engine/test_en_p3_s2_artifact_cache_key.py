"""GT-EN-P3-S2-01c: Artifact cache key format."""

import pytest

from topos.query.intent import compute_intent_hash
from topos.query.session_utils import build_cache_key

pytestmark = pytest.mark.gap


def test_cache_key_matches_prd_format() -> None:
    intent_hash = compute_intent_hash(
        scope_id="availability:read",
        access_mode="inference",
        query_text="Am I free Thursday?",
    )
    key = build_cache_key(
        scope_id="availability:read",
        access_mode="inference",
        intent_hash=intent_hash,
    )
    assert key == f"availability:read:inference:{intent_hash}"

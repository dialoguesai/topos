"""GT-EN-P3-S2-03b: Audit fields on memory_hit."""

import pytest

from topos.query.audit import build_query_audit_event
from topos.query.session import TurnOutcome

pytestmark = pytest.mark.gap


def test_memory_hit_audit_has_cache_keys_and_empty_stores() -> None:
    audit = build_query_audit_event(
        turn_outcome=TurnOutcome.MEMORY_HIT,
        scope_id="availability:read",
        access_mode="inference",
        session_id="qs_audit",
        cache_keys=["availability:read:inference:abc123"],
        stores_touched=["signal"],
    )
    assert audit["turn_outcome"] == "memory_hit"
    assert audit["cache_keys"] == ["availability:read:inference:abc123"]
    assert audit["stores_touched"] == []

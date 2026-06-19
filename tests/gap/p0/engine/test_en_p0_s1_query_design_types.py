"""
Gap: Query design types — table-only mapping → manifest/session/turn types
Sprint: EN-P0-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.query import QuerySession, ScopeResolutionManifest, TurnOutcome

pytestmark = pytest.mark.gap


def test_query_design_types_importable() -> None:
    manifest = ScopeResolutionManifest(
        scope_id="messages:read",
        primary_dimensions=["Relationships"],
    )
    session = QuerySession(
        session_id="s1",
        requester_id="u1",
        intent_hash="h1",
    )
    assert manifest.scope_id == "messages:read"
    assert session.session_id == "s1"
    assert TurnOutcome.DENIED.value == "denied"

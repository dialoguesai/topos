"""Smoke import for query pipeline design types."""

from topos.query import (
    GameLayer,
    QueryArtifact,
    QuerySession,
    RevealStrategy,
    ScopeResolutionManifest,
    SignalRetrievalAdapter,
    TurnClassifier,
    TurnOutcome,
)


def test_query_types_import() -> None:
    assert ScopeResolutionManifest.__name__ == "ScopeResolutionManifest"
    assert QuerySession.__name__ == "QuerySession"
    assert TurnOutcome.MEMORY_HIT.value == "memory_hit"
    assert RevealStrategy is not None
    assert SignalRetrievalAdapter is not None
    assert TurnClassifier is not None
    assert GameLayer is not None
    assert QueryArtifact.__name__ == "QueryArtifact"

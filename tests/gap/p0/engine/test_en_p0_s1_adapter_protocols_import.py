"""
Gap: Storage adapter protocols — missing → six MVP store protocols exported
Sprint: EN-P0-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.storage.adapters import (
    AuditLogStore,
    CanonicalStore,
    GraphEdgeStore,
    QuerySessionStore,
    SignalFeatureStore,
    VectorIndex,
)

pytestmark = pytest.mark.gap

MVP_METHODS = {
    CanonicalStore: {"upsert", "get", "list", "delete", "count"},
    SignalFeatureStore: {"put_fact", "put_score", "put_summary", "get_by_dimension", "list"},
    VectorIndex: {"upsert", "get_metadata", "list_metadata", "delete_by_record"},
    GraphEdgeStore: {"upsert_node", "upsert_edge", "list_graph", "neighbors"},
    AuditLogStore: {"append", "query"},
    QuerySessionStore: {"get", "put", "append_artifact", "invalidate", "purge_expired"},
}


@pytest.mark.parametrize(
    "protocol,methods",
    list(MVP_METHODS.items()),
    ids=[p.__name__ for p in MVP_METHODS],
)
def test_adapter_protocols_import_and_methods(protocol, methods) -> None:
    for method in methods:
        assert hasattr(protocol, method), method

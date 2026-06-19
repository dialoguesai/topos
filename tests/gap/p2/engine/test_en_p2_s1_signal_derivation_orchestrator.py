"""
Gap: Orchestrator — DerivedTables-only → adapter bundle writes
Sprint: EN-P2-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from topos.enrichment.orchestrator import SignalDerivationOrchestrator
from topos.storage.adapters.fakes import InMemoryCanonicalStore, InMemoryGraphEdgeStore, InMemorySignalFeatureStore, InMemoryVectorIndex
from topos.storage.adapters.factory import AdapterBundle

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_orchestrator_writes_via_adapters(monkeypatch) -> None:
    bundle = AdapterBundle(
        canonical=InMemoryCanonicalStore(),
        signal=InMemorySignalFeatureStore(),
        vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(),
        audit=MagicMock(),
        query_session=MagicMock(),
        backend="memory",
    )
    orch = SignalDerivationOrchestrator(adapters=bundle)
    mock_job = MagicMock()
    mock_job.get_job_name.return_value = "relationship_edges"
    mock_job.should_run.return_value = True
    mock_job.enrich = AsyncMock(
        return_value=[
            {
                "src_node_id": "a",
                "dst_node_id": "b",
                "weight": 2.0,
                "source_id": "imessage",
                "provider": "rules",
                "model": "relationship_rules_v1",
            }
        ]
    )
    orch._signal_jobs = {"relationship_edges": mock_job}
    result = await orch.run_signal_derivation(
        [{"sender_id": "a", "conversation_id": "b", "source_id": "imessage"}],
        source_id="imessage",
        job_names=["relationship_edges"],
        sync_batch_id="batch-1",
    )
    assert result["jobs_run"] == 1
    graph = bundle.graph.list_graph()
    assert len(graph["edges"]) >= 1

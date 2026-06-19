"""Gap: mode ceiling — PRD_05"""
import pytest
from topos.query.retrieval import DefaultSignalRetrievalAdapter, RetrievalError, RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from remediation_helpers import sqlite_conn
from topos.query.manifest import ScopeResolutionManifest
pytestmark = pytest.mark.gap

def test_summary_ceiling_blocks_raw() -> None:
    bundle = AdapterFactory.create("memory")
    manifest = ScopeResolutionManifest(scope_id="ai_conversations:read", primary_dimensions=["Memory"], canonical_tables=["ai_chat_messages"], access_mode_ceiling="summary")
    adapter = DefaultSignalRetrievalAdapter(bundle)
    with pytest.raises(RetrievalError):
        adapter.retrieve(RetrievalRequest(manifest=manifest, access_mode="raw", query_text=""))

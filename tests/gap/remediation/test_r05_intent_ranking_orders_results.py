"""Gap: intent ranking — PRD_05"""
import pytest
from topos.query.retrieval import _build_summary_items
from topos.storage.adapters.factory import AdapterFactory
from remediation_helpers import ai_conversations_manifest, sqlite_conn
pytestmark = pytest.mark.gap

def test_investor_query_ranks_matching_topic_higher() -> None:
    conn = sqlite_conn()
    bundle = AdapterFactory.create("local_database", conn=conn)
    bundle.signal.put_fact({"dimension":"memory","topic":"investor meetings","summary_text":"investor meetings","record_id":"a"})
    bundle.signal.put_fact({"dimension":"memory","topic":"lunch plans","summary_text":"lunch plans","record_id":"b"})
    items = _build_summary_items(
        manifest=ai_conversations_manifest(),
        adapters=bundle,
        query_text="investor meetings",
        semantic_hits=[],
        ranked_clusters=[],
    )
    assert items[0]["topic"] == "investor meetings"

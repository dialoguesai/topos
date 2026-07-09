"""Gap: intent ranking — PRD_05

Updated for the retrieval overhaul (0061f7e): the dimension-dump signal_facts
lane is a CONTEXT source in RRF fusion — it colors real findings but can never
justify a non-empty result alone (honest-abstention contract). The ranking
assertion therefore runs with vector evidence present: the fact matching the
query's intent must outrank the non-matching one.
"""
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
    semantic_hits = [
        {"record_id": "a", "text_preview": "investor meetings", "similarity": 0.9},
        {"record_id": "b", "text_preview": "lunch plans", "similarity": 0.3},
    ]
    items = _build_summary_items(
        manifest=ai_conversations_manifest(),
        adapters=bundle,
        query_text="investor meetings",
        semantic_hits=semantic_hits,
        ranked_clusters=[],
    )
    assert items, "evidence-backed query must not abstain"
    assert items[0]["topic"] == "investor meetings"

def test_context_only_signal_facts_abstain() -> None:
    """Dimension-dump facts alone are context, not evidence: the honest
    result for a query backed by nothing else is empty (retrieval overhaul
    abstention contract)."""
    conn = sqlite_conn()
    bundle = AdapterFactory.create("local_database", conn=conn)
    bundle.signal.put_fact({"dimension":"memory","topic":"investor meetings","summary_text":"investor meetings","record_id":"a"})
    items = _build_summary_items(
        manifest=ai_conversations_manifest(),
        adapters=bundle,
        query_text="investor meetings",
        semantic_hits=[],
        ranked_clusters=[],
    )
    assert items == []

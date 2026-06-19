"""Gap: inference strips content — PRD_05"""
import pytest
from topos.query.retrieval import DefaultSignalRetrievalAdapter, RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from remediation_helpers import ai_conversations_manifest, sqlite_conn
pytestmark = pytest.mark.gap

def test_inference_packet_has_no_content_keys() -> None:
    conn = sqlite_conn()
    bundle = AdapterFactory.create("local_database", conn=conn)
    bundle.canonical.upsert("ai_chat_messages", {"message_id":"m1","conversation_id":"c","sender_type":"human","event_at":"2026-06-01T00:00:00Z","content":"secret body","source_id":"chatgpt_file_ingestion"})
    adapter = DefaultSignalRetrievalAdapter(bundle)
    result = adapter.retrieve(RetrievalRequest(manifest=ai_conversations_manifest(), access_mode="inference", query_text="work"))
    assert "secret body" not in str(result.context_packet)
    for key in ("content", "body", "text"):
        assert key not in result.context_packet or not result.context_packet.get(key)

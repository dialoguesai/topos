"""Gap: rolling window pushdown — PRD_05"""
import pytest
from topos.query.retrieval import DefaultSignalRetrievalAdapter, RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from remediation_helpers import ai_conversations_manifest, ingest_chatgpt_message, sqlite_conn
pytestmark = pytest.mark.gap

def test_rolling_window_excludes_old_rows() -> None:
    conn = sqlite_conn()
    bundle = AdapterFactory.create("local_database", conn=conn)
    bundle.canonical.upsert("ai_chat_messages", {"message_id":"old","conversation_id":"c","sender_type":"human","event_at":"2020-01-01T00:00:00Z","content":"old","source_id":"chatgpt_file_ingestion"})
    ingest_chatgpt_message(conn, message_id="new-msg")
    adapter = DefaultSignalRetrievalAdapter(bundle)
    result = adapter.retrieve(RetrievalRequest(manifest=ai_conversations_manifest(), access_mode="raw", query_text="", filter_manifest={"rolling_window":{"days":30}}))
    rows = result.context_packet.get("rows") or []
    assert all("old" != str(r.get("record_id")) for r in rows)

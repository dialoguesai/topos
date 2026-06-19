"""Gap: hosted canonical list — PRD_07"""
import pytest
from topos.storage.adapters.factory import AdapterFactory
from remediation_helpers import ingest_chatgpt_message, sqlite_conn
pytestmark = pytest.mark.gap

def test_hosted_bundle_lists_canonical_rows() -> None:
    conn = sqlite_conn()
    ingest_chatgpt_message(conn)
    bundle = AdapterFactory.create("hosted_database", conn=conn)
    page = bundle.canonical.list("ai_chat_messages")
    assert page.total >= 1

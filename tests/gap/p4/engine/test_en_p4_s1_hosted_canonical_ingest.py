"""
Gap: Hosted ingest — local-only AT-1 → canonical tables on hosted profile
Sprint: EN-P4-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.storage.adapters.fakes import InMemoryCanonicalStore

pytestmark = pytest.mark.gap


def test_hosted_profile_canonical_upsert_shape() -> None:
    """Hosted profile uses same canonical upsert contract as local (fake stand-in for AT-1)."""
    store = InMemoryCanonicalStore()
    store.upsert(
        "ai_chat_messages",
        {
            "record_id": "c1",
            "source_id": "chatgpt",
            "sync_batch_id": "batch-1",
            "content": "hello",
        },
    )
    store.upsert(
        "conversation_messages",
        {"record_id": "m1", "content": "hi", "conversation_id": "conv1"},
    )
    ai_page = store.list("ai_chat_messages", limit=10)
    msg_page = store.list("conversation_messages", limit=10)
    assert len(ai_page.items) == 1
    assert len(msg_page.items) == 1
    assert ai_page.items[0].get("source_id") == "chatgpt"
    assert ai_page.items[0].get("sync_batch_id") == "batch-1"

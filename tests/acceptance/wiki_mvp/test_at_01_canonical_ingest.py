"""
AT-1: ChatGPT + messenger ingest → canonical tables populated (engine slice).
Profile: local (+ hosted fakes)
"""

import pytest

from topos.storage.adapters.fakes import InMemoryCanonicalStore

pytestmark = pytest.mark.acceptance


def test_at_01_canonical_tables_populated() -> None:
    store = InMemoryCanonicalStore()
    store.upsert("ai_chat_messages", {"record_id": "a1", "source_id": "chatgpt", "content": "hi"})
    store.upsert("conversation_messages", {"record_id": "m1", "content": "hello"})
    assert store.list("ai_chat_messages", limit=10).total >= 1
    assert store.list("conversation_messages", limit=10).total >= 1

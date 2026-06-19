"""Contract tests: fake + SQLite parity for CanonicalStore upsert/get/list."""

from __future__ import annotations

from pathlib import Path

import pytest

from topos.storage.adapters.factory import AdapterFactory
from topos.storage.adapters.protocols import CanonicalStore


@pytest.fixture(params=["memory", "sqlite"])
def canonical_store(request: pytest.FixtureRequest, tmp_path: Path) -> CanonicalStore:
    if request.param == "memory":
        return AdapterFactory.create("memory").canonical
    bundle = AdapterFactory.create("local_database", db_path=tmp_path / "contract.db")
    return bundle.canonical


def test_canonical_upsert_get_list_parity(canonical_store: CanonicalStore) -> None:
    record = {
        "record_id": "rec-1",
        "source_id": "chatgpt_file_ingestion",
        "content": "hello",
    }
    record_id = canonical_store.upsert("ai_chat_messages", record, idempotency_key="idem-1")
    assert record_id == "rec-1"

    fetched = canonical_store.get("ai_chat_messages", "rec-1")
    assert fetched is not None
    assert fetched["content"] == "hello"
    assert fetched["source_id"] == "chatgpt_file_ingestion"

    page = canonical_store.list("ai_chat_messages", source_id="chatgpt_file_ingestion", limit=10, offset=0)
    assert page.total == 1
    assert page.items[0]["record_id"] == "rec-1"
    assert canonical_store.count("ai_chat_messages", source_id="chatgpt_file_ingestion") == 1

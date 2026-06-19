"""
Gap: Adapter factory — no factory → local_database SQLite bundle + memory fakes
Sprint: EN-P0-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import tempfile
from pathlib import Path

import pytest

from topos.storage.adapters import AdapterFactory
from topos.storage.adapters.factory import AdapterBundle
from topos.storage.adapters.protocols import CanonicalStore

pytestmark = pytest.mark.gap


def test_adapter_factory_local_database_returns_sqlite_bundle() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bundle = AdapterFactory.from_runtime(
            {"database_hosting_mode": "local_database"},
            db_path=Path(tmp) / "gap.db",
        )
        assert isinstance(bundle, AdapterBundle)
        assert isinstance(bundle.canonical, CanonicalStore)
        record_id = bundle.canonical.upsert(
            "messages",
            {"record_id": "r1", "source_id": "test"},
            idempotency_key="idem",
        )
        assert record_id == "r1"


def test_adapter_factory_memory_passes_contract_smoke() -> None:
    bundle = AdapterFactory.from_runtime({"database_hosting_mode": "memory"})
    page = bundle.canonical.list("messages", limit=5, offset=0)
    assert page.total == 0

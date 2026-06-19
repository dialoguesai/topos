"""
Gap: Two SQLiteCanonicalStore modules
PRD: PRD_01
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import importlib

import pytest

pytestmark = pytest.mark.gap


def test_single_public_adapter_canonical_store() -> None:
    adapter_mod = importlib.import_module("topos.storage.adapters.sqlite.stores")
    canonical_mod = importlib.import_module("topos.storage.canonical.canonical_store")
    assert hasattr(adapter_mod, "SQLiteCanonicalStore")
    assert hasattr(canonical_mod, "SQLiteCanonicalStore")
    assert adapter_mod.SQLiteCanonicalStore is not canonical_mod.SQLiteCanonicalStore
    doc = adapter_mod.SQLiteCanonicalStore.__doc__ or ""
    assert "delegate" in doc.lower() or "typed" in doc.lower()

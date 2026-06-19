"""
Gap: Legacy messages — dual table → conversation_messages only in manifests
Sprint: EN-P4-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import json
from pathlib import Path

import pytest

from topos.query.manifest_validation import resolve_scope_manifest

pytestmark = pytest.mark.gap

REGISTRY = Path(__file__).resolve().parents[4] / "topos" / "query" / "scope_registry.json"


def test_messages_read_uses_conversation_messages_only() -> None:
    manifest = resolve_scope_manifest("messages:read")
    tables = manifest.canonical_tables or []
    assert "conversation_messages" in tables
    assert "messages" not in tables


def test_scope_registry_messages_entry() -> None:
    data = json.loads(REGISTRY.read_text())
    entry = next(s for s in data["scopes"] if s["scope_id"] == "messages:read")
    assert entry["raw_tables"] == ["conversation_messages"]

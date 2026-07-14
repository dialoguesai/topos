"""
Gap: Calendar/contacts — absent → stub schema + registry entries
Sprint: EN-P1-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import sqlite3

import pytest

from topos.sources.registry import CALENDAR_STUB, CANONICAL_ADDRESS_BOOK, REGISTRY
from topos.storage.canonical.calendar_contacts_tables import ensure_stub_tables
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


def test_stub_tables_and_registry_entries() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    ensure_stub_tables(conn)

    for table in ("calendar_events", "contacts", "contact_identifiers"):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        assert row is not None, f"missing table {table}"

    calendar_cols = {row[1] for row in conn.execute("PRAGMA table_info(calendar_events)").fetchall()}
    assert {"source_record_id", "ingested_at", "sync_batch_id"}.issubset(calendar_cols)

    assert REGISTRY["calendar_stub"].source_id == CALENDAR_STUB.source_id
    assert CALENDAR_STUB.source_type == "stub"
    assert CALENDAR_STUB.ingestion_trigger == "manual"
    assert REGISTRY["canonical_address_book"].source_id == CANONICAL_ADDRESS_BOOK.source_id
    assert CANONICAL_ADDRESS_BOOK.canonical_group_id == "contacts"
    assert CANONICAL_ADDRESS_BOOK.source_kind == "derived"

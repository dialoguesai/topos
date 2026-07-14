"""Installed-source filtering for retrieval manifest resolution."""

import sqlite3

import pytest

from topos.query.manifest_validation import manifest_from_scope_entry, resolve_scope_manifest
from topos.query.retrieval import resolve_retrieval_source_ids
from topos.query.scope_registry_loader import get_scope_entry
from topos.sources.definitions import CANONICAL_ADDRESS_BOOK_SOURCE_ID
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "resolve_sources.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    yield c
    c.close()


def test_manifest_unions_registry_sources() -> None:
    entry = get_scope_entry("messages:read")
    assert entry is not None
    manifest = manifest_from_scope_entry(entry)
    assert "demo_messenger_file" in manifest.default_source_ids
    assert "imessage" in manifest.default_source_ids


def test_contacts_manifest_includes_canonical_address_book() -> None:
    manifest = resolve_scope_manifest("contacts:resolve")
    assert CANONICAL_ADDRESS_BOOK_SOURCE_ID in manifest.default_source_ids


def test_resolve_source_ids_filters_to_installed(conn) -> None:
    manifest = resolve_scope_manifest("schedule:read")
    all_ids = resolve_retrieval_source_ids(manifest)
    assert "demo_calendar_file" in all_ids

    filtered = resolve_retrieval_source_ids(manifest, ["calendar_stub"])
    assert filtered == ["calendar_stub"]

    fallback = resolve_retrieval_source_ids(manifest, ["uninstalled_source"])
    assert "demo_calendar_file" in fallback


def test_list_canonical_rows_does_not_broaden_to_all_sources(conn) -> None:
    from topos.storage.adapters.factory import AdapterFactory
    from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
    from topos.query.retrieval import _list_canonical_rows

    store = SQLiteCanonicalStore(conn)
    store.upsert(
        "calendar_events",
        {
            "record_id": "other-source-event",
            "event_id": "other-source-event",
            "title": "Hidden",
            "starts_at": "2026-03-13T10:00:00Z",
            "ends_at": "2026-03-13T11:00:00Z",
            "source_id": "other_calendar_source",
        },
        sync_batch_id="batch-other",
    )
    conn.commit()

    adapters = AdapterFactory.create("local_database", conn=conn)
    rows = _list_canonical_rows(
        adapters,
        "calendar_events",
        source_ids=["demo_calendar_file"],
        limit=50,
    )
    assert all(str(row.get("source_id") or "") == "demo_calendar_file" for row in rows)
    assert not any(str(row.get("record_id")) == "other-source-event" for row in rows)


def test_contacts_retrieval_reads_canonical_address_book(conn) -> None:
    from topos.query.retrieval import _list_canonical_rows
    from topos.storage.adapters.factory import AdapterFactory

    conn.execute(
        """
        INSERT INTO contacts (
            contact_id, dataset_id, source_id, display_name, is_self, created_at, updated_at
        ) VALUES ('contact-1', 'dataset-1', ?, 'Ada Lovelace', 0, datetime('now'), datetime('now'))
        """,
        (CANONICAL_ADDRESS_BOOK_SOURCE_ID,),
    )
    conn.commit()

    rows = _list_canonical_rows(
        AdapterFactory.create("local_database", conn=conn),
        "contacts",
        source_ids=resolve_retrieval_source_ids(resolve_scope_manifest("contacts:resolve")),
        limit=50,
    )

    assert any(row.get("record_id") == "contact-1" for row in rows)

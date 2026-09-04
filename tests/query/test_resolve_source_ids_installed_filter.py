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


def test_messages_read_excludes_youtube_transcripts_unless_opted_in() -> None:
    from topos.sources.registry import get_sources_by_scope

    messages = set(get_sources_by_scope("messages:read"))
    assert "youtube_transcripts" not in messages
    assert "voxterm_transcripts" in messages
    assert "imessage" in messages

    manifest = resolve_scope_manifest("messages:read")
    assert "youtube_transcripts" not in (manifest.default_source_ids or [])

    transcripts = resolve_scope_manifest("transcripts:read")
    assert "youtube_transcripts" in (transcripts.default_source_ids or [])
    assert "transcript_segments" in (transcripts.canonical_tables or [])
    assert "youtube_transcripts" in get_sources_by_scope("transcripts:read")


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


def test_canonical_address_book_survives_installed_demo_filter() -> None:
    """demo_contacts_file alone must not drop the derived address book."""
    manifest = resolve_scope_manifest("contacts:resolve")
    resolved = resolve_retrieval_source_ids(manifest, ["demo_contacts_file"])
    assert "demo_contacts_file" in resolved
    assert CANONICAL_ADDRESS_BOOK_SOURCE_ID in resolved


def test_contacts_resolve_raw_surfaces_identifier_under_installed_filter(conn) -> None:
    """Regression for A3 C7/C14: name query must surface the phone needle.

    Live contacts live under canonical_address_book; identifiers often under '*'.
    Installed-source filtering used to keep only demo_contacts_file and return
    zero rows even though the needle existed in contact_identifiers.
    """
    from topos.query.retrieval import DefaultSignalRetrievalAdapter
    from topos.query.types import RetrievalRequest
    from topos.storage.adapters.factory import AdapterFactory

    phone = "5555550108"
    conn.execute(
        """
        INSERT INTO contacts (
            contact_id, dataset_id, source_id, display_name, is_self, created_at, updated_at
        ) VALUES ('contact-jessica', 'dataset-1', ?, 'Papa November', 0, datetime('now'), datetime('now'))
        """,
        (CANONICAL_ADDRESS_BOOK_SOURCE_ID,),
    )
    conn.execute(
        """
        INSERT INTO contact_identifiers (
            dataset_id, source_id, identifier, identifier_type, contact_id
        ) VALUES ('dataset-1', '*', ?, 'phone', 'contact-jessica')
        """,
        (phone,),
    )
    conn.commit()

    adapters = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("contacts:resolve")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="raw",
            query_text="Find the contact record for Papa November",
            disclosure_tier="owner_raw",
            installed_source_ids=["demo_contacts_file"],
        )
    )
    rows = bundle.context_packet.get("rows") or []
    blob = str(rows).lower()
    assert phone in blob
    assert any(
        str(r.get("identifier") or "") == phone and r.get("_table") == "contact_identifiers"
        for r in rows
    )


def test_contacts_resolve_summary_surfaces_full_name_under_installed_filter(conn) -> None:
    """Regression for A3 C15: first-name contact summary must surface full display name.

    Same installed-source trap as C7/C14: with only demo_contacts_file installed,
    dropping canonical_address_book left summary mode without contact rows, so
    'What do I know about my contact John?' never contained 'Echo Foxtrot'.
    """
    from topos.query.retrieval import DefaultSignalRetrievalAdapter
    from topos.query.types import RetrievalRequest
    from topos.storage.adapters.factory import AdapterFactory

    for contact_id, name in (
        ("contact-john-ludlow", "Echo Foxtrot"),
        ("contact-john-other", "Echo Golf"),
    ):
        conn.execute(
            """
            INSERT INTO contacts (
                contact_id, dataset_id, source_id, display_name, is_self, created_at, updated_at
            ) VALUES (?, 'dataset-1', ?, ?, 0, datetime('now'), datetime('now'))
            """,
            (contact_id, CANONICAL_ADDRESS_BOOK_SOURCE_ID, name),
        )
        conn.execute(
            """
            INSERT INTO contact_identifiers (
                dataset_id, source_id, identifier, identifier_type, contact_id
            ) VALUES ('dataset-1', '*', ?, 'email', ?)
            """,
            (f"{contact_id}@example.com", contact_id),
        )
    conn.commit()

    adapters = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("contacts:resolve")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text="What do I know about my contact John?",
            disclosure_tier="owner_raw",
            installed_source_ids=["demo_contacts_file"],
        )
    )
    summaries = bundle.context_packet.get("summaries") or []
    blob = str(summaries)
    assert "Echo Foxtrot" in blob
    assert any(
        "Echo Foxtrot" in str(item.get("summary_text") or "")
        and str(item.get("retrieval_source") or "").startswith("canonical:contacts")
        for item in summaries
    )

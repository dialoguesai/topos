"""Documents canonical lane: registry definitions, parsers, and mapper.

PLAN_CANONICAL_CALENDAR_DOCUMENTS Part A. Notion pages and Google Drive files
both normalize to the pinned documents shape (doc_id, title, content, url,
mime_type, author, created_at, modified_at) before reaching
DocumentsCanonicalMapper — see documents_mapper.py's module docstring.
"""

from __future__ import annotations

import sqlite3

from topos.canonicalization.mappers import MAPPER_REGISTRY
from topos.canonicalization.mappers.documents_mapper import DocumentsCanonicalMapper
from topos.ingestion.parsers import GDriveFileParser, NotionPageParser, PARSER_REGISTRY
from topos.ingestion.parsers.base import NormalizedRecord
from topos.ingestion.sources.base import RawRecord
from topos.sources.bundled_canonical_triples import (
    VALID_CANONICAL_GROUP_IDS,
    infer_bundled_canonical_triple,
)
from topos.sources.definitions import accepts_app_ingest
from topos.sources.registry import GDRIVE_FILES, NOTION_PAGES, REGISTRY
from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
from topos.storage.db.migrations import apply_all_migrations


def _notion_page(**overrides) -> dict:
    """Already-shaped documents record (client/CP-side shaping is out of
    scope here — see PLAN §A7); extra fields intentionally kept."""
    page = {
        "doc_id": "notion:abc123",
        "title": "Q3 Planning",
        "content": "Draft notes for Q3 planning.",
        "url": "https://notion.so/abc123",
        "mime_type": "text/notion",
        "author": "jonny",
        "created_at": "2026-06-01T10:00:00Z",
        "modified_at": "2026-07-01T12:34:56Z",
    }
    page.update(overrides)
    return page


def _gdrive_file(**overrides) -> dict:
    file = {
        "doc_id": "gdrive:file-789",
        "title": "Budget.xlsx",
        "content": "extracted sheet text",
        "url": "https://drive.google.com/file/d/file-789",
        "mime_type": "application/vnd.google-apps.spreadsheet",
        "author": "jonny@example.com",
        "created_at": "2026-05-01T00:00:00Z",
        "modified_at": "2026-07-10T08:00:00Z",
    }
    file.update(overrides)
    return file


def test_registry_definitions() -> None:
    assert REGISTRY["notion_pages"] is NOTION_PAGES
    assert REGISTRY["gdrive_files"] is GDRIVE_FILES
    for defn in (NOTION_PAGES, GDRIVE_FILES):
        assert defn.source_type == "ui_stream"
        assert defn.delivery == "client_push"
        assert defn.canonical_group_id == "documents"
        assert defn.canonical_mapper_id == "documents"
        assert accepts_app_ingest(defn)
    assert NOTION_PAGES.schema_id == "notion.page.v1"
    assert NOTION_PAGES.parser_id == "notion.page.v1"
    assert GDRIVE_FILES.schema_id == "gdrive.file.v1"
    assert GDRIVE_FILES.parser_id == "gdrive.file.v1"


def test_bundled_triple_and_registries() -> None:
    assert infer_bundled_canonical_triple(schema_id="notion.page.v1") == (
        "documents",
        "documents",
    )
    assert infer_bundled_canonical_triple(schema_id="gdrive.file.v1") == (
        "documents",
        "documents",
    )
    assert "documents" in VALID_CANONICAL_GROUP_IDS
    assert PARSER_REGISTRY["notion.page.v1"] is NotionPageParser
    assert PARSER_REGISTRY["gdrive.file.v1"] is GDriveFileParser
    assert MAPPER_REGISTRY["documents"] is DocumentsCanonicalMapper


def test_notion_parser_validate_and_parse() -> None:
    parser = NotionPageParser(dataset_id="user:default:device")
    missing = _notion_page()
    missing.pop("doc_id")
    result = parser.validate(RawRecord(record_id="r-1", payload=missing))
    assert not result.is_valid

    ok = parser.validate(RawRecord(record_id="r-1", payload=_notion_page()))
    assert ok.is_valid

    normalized = parser.parse(RawRecord(record_id="r-1", payload=_notion_page()))
    assert normalized.record_id == "notion:abc123"
    assert normalized.payload["doc_id"] == "notion:abc123"
    assert normalized.payload["title"] == "Q3 Planning"
    assert normalized.payload["dataset_id"] == "user:default:device"


def test_gdrive_parser_validate_and_parse() -> None:
    parser = GDriveFileParser(dataset_id="user:default:device")
    assert parser.validate(RawRecord(record_id="r-1", payload=_gdrive_file())).is_valid

    normalized = parser.parse(RawRecord(record_id="r-1", payload=_gdrive_file()))
    assert normalized.record_id == "gdrive:file-789"
    assert normalized.payload["mime_type"] == "application/vnd.google-apps.spreadsheet"


def test_mapper_reads_pinned_documents_shape_from_notion() -> None:
    normalized = NormalizedRecord(record_id="notion:abc123", payload=_notion_page())
    mapped = DocumentsCanonicalMapper().map(normalized)
    assert mapped.record_id == "notion:abc123"
    assert mapped.payload == {
        "doc_id": "notion:abc123",
        "title": "Q3 Planning",
        "content": "Draft notes for Q3 planning.",
        "url": "https://notion.so/abc123",
        "mime_type": "text/notion",
        "author": "jonny",
        "created_at": "2026-06-01T10:00:00Z",
        "modified_at": "2026-07-01T12:34:56Z",
        "source_record_id": "notion:abc123",
    }


def test_mapper_reads_pinned_documents_shape_from_gdrive() -> None:
    normalized = NormalizedRecord(record_id="gdrive:file-789", payload=_gdrive_file())
    mapped = DocumentsCanonicalMapper().map(normalized)
    assert mapped.payload["doc_id"] == "gdrive:file-789"
    assert mapped.payload["mime_type"] == "application/vnd.google-apps.spreadsheet"
    assert mapped.payload["author"] == "jonny@example.com"


def test_mapper_falls_back_to_record_id_when_doc_id_missing() -> None:
    payload = _notion_page()
    payload.pop("doc_id")
    normalized = NormalizedRecord(record_id="notion:fallback-id", payload=payload)
    mapped = DocumentsCanonicalMapper().map(normalized)
    assert mapped.record_id == "notion:fallback-id"
    assert mapped.payload["doc_id"] == "notion:fallback-id"


def test_document_maps_to_documents_table() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)

    normalized = NormalizedRecord(record_id="notion:abc123", payload=_notion_page())
    mapped = DocumentsCanonicalMapper().map(normalized)
    payload = dict(mapped.payload)
    payload["source_id"] = "notion_pages"

    store = SQLiteCanonicalStore(conn)
    ref = store.upsert("documents", payload, sync_batch_id="notion-batch-1")
    assert ref.created is True

    row = conn.execute(
        "SELECT doc_id, title, content, url, mime_type, author, source_id, sync_batch_id FROM documents WHERE doc_id=?",
        (mapped.payload["doc_id"],),
    ).fetchone()
    assert row is not None
    assert row[1] == "Q3 Planning"
    assert row[2] == "Draft notes for Q3 planning."
    assert row[5] == "jonny"
    assert row[6] == "notion_pages"
    assert row[7] == "notion-batch-1"

    # Idempotent re-ingest: same doc_id updates in place, no duplicate row.
    ref2 = store.upsert("documents", payload, sync_batch_id="notion-batch-2")
    assert ref2.created is False
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1

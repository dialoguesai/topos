"""Notion page / Google Drive file parsers for the documents ingestion lane.

Both are near-identity passthroughs: the canonical mapper
(DocumentsCanonicalMapper) reads the pinned documents record shape (doc_id,
title, content, url, mime_type, author, created_at, modified_at) directly
off the normalized payload, so parsing here is just stamping a record_id and
passing every field through untouched. Producing that pinned shape from each
connector's native tool output is a client/CP-side shaping concern
(PLAN_CANONICAL_CALENDAR_DOCUMENTS §A7), handled separately.

``ui_stream``/``client_push`` sources are ingested via
``_ingest_ui_payload_direct`` (ingestion/ingest_helpers.py), which requires a
parser registered under the source's schema_id/parser_id — without one,
ingestion fails outright with "No parser found for source_id=...".
"""

from __future__ import annotations

from dataclasses import dataclass

from ..sources.base import RawRecord
from ..validation.base import ValidationResult
from .base import NormalizedRecord, Parser


def _parse_document_record(raw: RawRecord, *, dataset_id: str) -> NormalizedRecord:
    payload = dict(raw.payload)
    record_id = str(payload.get("doc_id") or payload.get("id") or raw.record_id)
    payload.setdefault("doc_id", record_id)
    payload.setdefault("record_id", record_id)
    payload["dataset_id"] = dataset_id
    return NormalizedRecord(record_id=record_id, payload=payload)


def _validate_document_record(record: RawRecord) -> ValidationResult:
    payload = record.payload
    if not isinstance(payload, dict):
        return ValidationResult(is_valid=False, errors=["Record must be a dict"], metadata={})
    if not str(payload.get("doc_id") or payload.get("id") or "").strip():
        return ValidationResult(
            is_valid=False, errors=["Missing required field: doc_id"], metadata={}
        )
    return ValidationResult(is_valid=True, errors=[], metadata={})


@dataclass
class NotionPageParser(Parser):
    """Parser for notion_pages records shaped like Notion page fetch output."""

    dataset_id: str
    _schema_id: str = "notion.page.v1"

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        return _parse_document_record(raw, dataset_id=self.dataset_id)

    def validate(self, record: RawRecord) -> ValidationResult:
        return _validate_document_record(record)

    def schema_id(self) -> str:
        return self._schema_id


@dataclass
class GDriveFileParser(Parser):
    """Parser for gdrive_files records shaped like Google Drive file metadata."""

    dataset_id: str
    _schema_id: str = "gdrive.file.v1"

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        return _parse_document_record(raw, dataset_id=self.dataset_id)

    def validate(self, record: RawRecord) -> ValidationResult:
        return _validate_document_record(record)

    def schema_id(self) -> str:
        return self._schema_id

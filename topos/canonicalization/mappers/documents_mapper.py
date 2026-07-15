"""Documents canonical mapper (PLAN_CANONICAL_CALENDAR_DOCUMENTS Part A).

Notion pages and Google Drive files both normalize to the same pinned
documents shape before reaching this mapper: doc_id, title, content, url,
mime_type, author, created_at, modified_at. Producing that shape from each
connector's native tool output (Notion's structured JSON, Drive's file
metadata) is a client/CP-side shaping concern (PLAN §A7, save-to-Topos
capability) — a separate, follow-up change. ``map()`` here is deliberately a
thin, near-identity read of the already-normalized keys (with a fallback to
the ingest record_id for doc_id), the same shape for both sources, which is
why one mapper class serves both ``notion_pages`` and ``gdrive_files``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...ingestion.parsers.base import NormalizedRecord
from .base import CanonicalMapper, CanonicalRecord, MappingMetadata


@dataclass
class DocumentsCanonicalMapper(CanonicalMapper):
    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        p = normalized.payload
        doc_id = str(p.get("doc_id") or normalized.record_id)
        canonical = {
            "doc_id": doc_id,
            "title": p.get("title"),
            "content": p.get("content"),
            "url": p.get("url"),
            "mime_type": p.get("mime_type"),
            "author": p.get("author"),
            "created_at": p.get("created_at"),
            "modified_at": p.get("modified_at"),
            "source_record_id": doc_id,
        }
        return CanonicalRecord(record_id=doc_id, payload=canonical)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="documents", mapping_version=self.version)

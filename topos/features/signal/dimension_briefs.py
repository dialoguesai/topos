"""Versioned living briefs per signal dimension."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .brief_schemas import (
    empty_structured_for_dimension as empty_structured,
    parse_markdown_sections,
    render_markdown,
)
from .brief_ontology import llm_merge_section_ids, ontology_version, section_ids_for_dimension
from .dimension_registry import SIGNAL_DIMENSION_IDS, is_signal_dimension


class DimensionBriefStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def list_briefs(self) -> List[Dict[str, Any]]:
        rows = {
            str(r[1]): self._row_to_head(r)
            for r in self._conn.execute(
                """
                SELECT brief_id, signal_dimension, head_revision_id, structured_json,
                       markdown_body, revision_number, updated_at, updated_by
                FROM signal_dimension_briefs
                """
            ).fetchall()
        }
        out: List[Dict[str, Any]] = []
        for dim_id in SIGNAL_DIMENSION_IDS:
            if dim_id in rows:
                out.append(rows[dim_id])
            else:
                out.append(self._ensure_head(dim_id))
        return out

    def get_brief(self, dimension: str) -> Dict[str, Any]:
        dim = str(dimension or "").strip().lower()
        if not is_signal_dimension(dim):
            raise ValueError(f"Unknown signal dimension: {dimension!r}")
        row = self._conn.execute(
            """
            SELECT brief_id, signal_dimension, head_revision_id, structured_json,
                   markdown_body, revision_number, updated_at, updated_by
            FROM signal_dimension_briefs WHERE signal_dimension=?
            """,
            (dim,),
        ).fetchone()
        if row:
            return self._row_to_head(row)
        return self._ensure_head(dim)

    def list_revisions(self, dimension: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        head = self.get_brief(dimension)
        brief_id = head["brief_id"]
        limit = max(1, min(int(limit), 100))
        rows = self._conn.execute(
            """
            SELECT revision_id, parent_revision_id, revision_number, change_kind,
                   structured_json, markdown_body, provenance_json, created_at, created_by
            FROM signal_dimension_brief_revisions
            WHERE brief_id=?
            ORDER BY revision_number DESC
            LIMIT ?
            """,
            (brief_id, limit),
        ).fetchall()
        return [self._row_to_revision(r) for r in rows]

    def save_user_edit(
        self,
        dimension: str,
        markdown_body: str,
        *,
        structured_json: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        dim = str(dimension or "").strip().lower()
        head = self.get_brief(dim)
        structured = structured_json or parse_markdown_sections(markdown_body, dim)
        return self._append_revision(
            head,
            structured=structured,
            markdown_body=markdown_body,
            change_kind="user_edit",
            created_by="user",
            provenance={"source": "user_edit"},
        )

    def merge_system_update(
        self,
        dimension: str,
        section_updates: Dict[str, str],
        *,
        source_id: Optional[str] = None,
        job_id: str = "dimension_summary",
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        dim = str(dimension or "").strip().lower()
        if not is_signal_dimension(dim):
            return None
        head = self.get_brief(dim)
        structured = head.get("structured_json") if isinstance(head.get("structured_json"), dict) else empty_structured(dim)
        sections = structured.get("sections") if isinstance(structured.get("sections"), dict) else empty_structured(dim)["sections"]

        changed = False
        merge_keys = llm_merge_section_ids(dim)
        for key in merge_keys:
            text = str(section_updates.get(key) or "").strip()
            if not text:
                continue
            block = sections.get(key) if isinstance(sections.get(key), dict) else {}
            if str(block.get("markdown") or "").strip() != text:
                sections[key] = {**block, "markdown": text}
                changed = True

        if not changed:
            return head

        grant_key = "grant_policy"
        if grant_key in sections:
            grant_block = sections.get(grant_key) if isinstance(sections.get(grant_key), dict) else {}
            sections[grant_key] = grant_block

        structured = {
            "dimension": dim,
            "ontology_version": structured.get("ontology_version") or ontology_version(),
            "sections": sections,
        }
        markdown_body = render_markdown(dim, structured, updated_at=head.get("updated_at"))
        provenance = {
            "job_id": job_id,
            "source_id": source_id,
            "model": model,
            "provider": provider,
            "sections_updated": [k for k in merge_keys if section_updates.get(k)],
        }
        return self._append_revision(
            head,
            structured=structured,
            markdown_body=markdown_body,
            change_kind="ingest_merge",
            created_by="system",
            provenance=provenance,
        )

    def _ensure_head(self, dimension: str) -> Dict[str, Any]:
        dim = str(dimension or "").strip().lower()
        row = self._conn.execute(
            """
            SELECT brief_id, signal_dimension, head_revision_id, structured_json,
                   markdown_body, revision_number, updated_at, updated_by
            FROM signal_dimension_briefs WHERE signal_dimension=?
            """,
            (dim,),
        ).fetchone()
        if row:
            return self._row_to_head(row)

        structured = empty_structured(dim)
        markdown_body = render_markdown(dim, structured)
        brief_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute(
                """
                INSERT INTO signal_dimension_briefs (
                    brief_id, signal_dimension, head_revision_id, structured_json,
                    markdown_body, revision_number, updated_at, updated_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    brief_id,
                    dim,
                    revision_id,
                    json.dumps(structured),
                    markdown_body,
                    1,
                    now,
                    "system",
                ),
            )
            self._conn.execute(
                """
                INSERT INTO signal_dimension_brief_revisions (
                    revision_id, brief_id, parent_revision_id, revision_number,
                    change_kind, structured_json, markdown_body, provenance_json,
                    created_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    brief_id,
                    None,
                    1,
                    "initial",
                    json.dumps(structured),
                    markdown_body,
                    json.dumps({"source": "initial"}),
                    now,
                    "system",
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            row = self._conn.execute(
                """
                SELECT brief_id, signal_dimension, head_revision_id, structured_json,
                       markdown_body, revision_number, updated_at, updated_by
                FROM signal_dimension_briefs WHERE signal_dimension=?
                """,
                (dim,),
            ).fetchone()
            if row:
                return self._row_to_head(row)
            raise
        return self.get_brief(dim)

    def _append_revision(
        self,
        head: Dict[str, Any],
        *,
        structured: Dict[str, Any],
        markdown_body: str,
        change_kind: str,
        created_by: str,
        provenance: Dict[str, Any],
    ) -> Dict[str, Any]:
        brief_id = str(head["brief_id"])
        parent_revision_id = str(head["head_revision_id"])
        revision_number = int(head.get("revision_number") or 0) + 1
        revision_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO signal_dimension_brief_revisions (
                revision_id, brief_id, parent_revision_id, revision_number,
                change_kind, structured_json, markdown_body, provenance_json,
                created_at, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                brief_id,
                parent_revision_id,
                revision_number,
                change_kind,
                json.dumps(structured),
                markdown_body,
                json.dumps(provenance),
                now,
                created_by,
            ),
        )
        self._conn.execute(
            """
            UPDATE signal_dimension_briefs
            SET head_revision_id=?, structured_json=?, markdown_body=?,
                revision_number=?, updated_at=?, updated_by=?
            WHERE brief_id=?
            """,
            (
                revision_id,
                json.dumps(structured),
                markdown_body,
                revision_number,
                now,
                created_by,
                brief_id,
            ),
        )
        self._conn.commit()
        return self.get_brief(str(head["signal_dimension"]))

    def _row_to_head(self, row: Any) -> Dict[str, Any]:
        structured = json.loads(row[3] or "{}")
        return {
            "brief_id": row[0],
            "signal_dimension": row[1],
            "head_revision_id": row[2],
            "structured_json": structured,
            "markdown_body": row[4],
            "revision_number": row[5],
            "updated_at": row[6],
            "updated_by": row[7],
        }

    def _row_to_revision(self, row: Any) -> Dict[str, Any]:
        return {
            "revision_id": row[0],
            "parent_revision_id": row[1],
            "revision_number": row[2],
            "change_kind": row[3],
            "structured_json": json.loads(row[4] or "{}"),
            "markdown_body": row[5],
            "provenance": json.loads(row[6] or "{}"),
            "created_at": row[7],
            "created_by": row[8],
        }

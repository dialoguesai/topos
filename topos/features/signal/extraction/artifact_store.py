"""Persist Layer 2.5 extraction artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ....storage.db.write_gate import commit_connection, with_db_write


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: ``artifact_type -> payload fields that make two artifacts from ONE record
#: distinct``. Empty (the default) means the record IS the identity.
#:
#: An Edge needs this and the others do not: a journal entry declaring
#: ``people = "Rowan, Nadia"`` emits one Edge per person, and all of them carry
#: the same `source_refs`, so on a record-only key the second silently UPDATEs
#: the first out of existence. Measured on the live node 2026-08-28: the row
#: naming Rowan and Nadia holds exactly ONE Edge artifact, for Nadia — Rowan's
#: was overwritten — and 22 journal rows declare two or more people.
#:
#: An Interval or a Goal really is one-per-record, so they stay keyed as they
#: were and their stored hashes do not move.
ARTIFACT_IDENTITY_FIELDS: Dict[str, tuple] = {
    "Edge": ("target_entity_key",),
    "EntityRef": ("entity_key",),
}


def source_ref_hash(
    artifact_type: str,
    source_refs: List[Dict[str, Any]],
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    """Identity of an artifact: its type, its sources, and what it is ABOUT.

    ``payload`` is optional so existing callers keep working; when a type
    declares identity fields, omitting it produces the old record-scoped hash.
    """
    identity = [
        str((payload or {}).get(field) or "")
        for field in ARTIFACT_IDENTITY_FIELDS.get(str(artifact_type or ""), ())
    ]
    key: Dict[str, Any] = {"artifact_type": artifact_type, "source_refs": source_refs}
    # Added only for the types that declare it, so every other type's stored
    # hashes stay byte-identical and no existing row is orphaned.
    if identity:
        key["identity"] = identity
    blob = json.dumps(key, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class ExtractionArtifactStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert_artifact(
        self,
        artifact_type: str,
        payload: Dict[str, Any],
        *,
        dimension_affinity: Optional[List[str]] = None,
        source_refs: Optional[List[Dict[str, Any]]] = None,
        confidence: float = 0.5,
        extractor_version: str = "rules_v1",
    ) -> Dict[str, Any]:
        atype = str(artifact_type or "").strip()
        refs = source_refs or []
        ref_hash = source_ref_hash(atype, refs, payload)
        payload_json = json.dumps(payload)
        affinity_json = json.dumps(dimension_affinity or [])
        refs_json = json.dumps(refs)
        now = _now_iso()

        existing = self._conn.execute(
            """
            SELECT artifact_id FROM extraction_artifacts
            WHERE artifact_type=? AND source_ref_hash=?
            """,
            (atype, ref_hash),
        ).fetchone()

        if existing:
            with with_db_write():
                self._conn.execute(
                    """
                    UPDATE extraction_artifacts
                    SET payload_json=?, dimension_affinity_json=?, source_refs_json=?,
                        confidence=?, extracted_at=?, extractor_version=?
                    WHERE artifact_id=?
                    """,
                    (payload_json, affinity_json, refs_json, confidence, now, extractor_version, existing[0]),
                )
                commit_connection(self._conn)
            return self.get_artifact(str(existing[0]))

        artifact_id = str(uuid.uuid4())
        with with_db_write():
            self._conn.execute(
                """
                INSERT INTO extraction_artifacts (
                    artifact_id, artifact_type, payload_json, dimension_affinity_json,
                    source_refs_json, source_ref_hash, confidence, extracted_at, extractor_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    atype,
                    payload_json,
                    affinity_json,
                    refs_json,
                    ref_hash,
                    confidence,
                    now,
                    extractor_version,
                ),
            )
            commit_connection(self._conn)
        return self.get_artifact(artifact_id)

    def list_artifacts(
        self,
        *,
        artifact_type: Optional[str] = None,
        dimension: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        clauses = ["1=1"]
        params: List[Any] = []
        if artifact_type:
            clauses.append("artifact_type=?")
            params.append(str(artifact_type).strip())
        if dimension:
            clauses.append("dimension_affinity_json LIKE ?")
            params.append(f'%"{dimension}"%')
        params.append(limit)
        rows = self._conn.execute(
            f"""
            SELECT artifact_id, artifact_type, payload_json, dimension_affinity_json,
                   source_refs_json, confidence, extracted_at, extractor_version
            FROM extraction_artifacts
            WHERE {' AND '.join(clauses)}
            ORDER BY extracted_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._row_to_artifact(r) for r in rows]

    def get_artifact(self, artifact_id: str) -> Dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT artifact_id, artifact_type, payload_json, dimension_affinity_json,
                   source_refs_json, confidence, extracted_at, extractor_version
            FROM extraction_artifacts WHERE artifact_id=?
            """,
            (str(artifact_id),),
        ).fetchone()
        if not row:
            raise ValueError(f"Artifact not found: {artifact_id!r}")
        return self._row_to_artifact(row)

    def _row_to_artifact(self, row: Any) -> Dict[str, Any]:
        return {
            "artifact_id": row[0],
            "artifact_type": row[1],
            "payload": json.loads(row[2] or "{}"),
            "dimension_affinity": json.loads(row[3] or "[]"),
            "source_refs": json.loads(row[4] or "[]"),
            "confidence": float(row[5] or 0),
            "extracted_at": row[6],
            "extractor_version": row[7],
        }

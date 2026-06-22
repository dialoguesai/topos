"""Versioned typed signal objects per dimension (Layer 3 store)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .dimension_definition_loader import get_definition_or_none
from .dimension_registry import is_signal_dimension


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SignalObjectStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert_object(
        self,
        dimension: str,
        object_type: str,
        object_key: str,
        payload: Dict[str, Any],
        *,
        source_refs: Optional[List[Dict[str, Any]]] = None,
        confidence: float = 0.5,
        extractor_version: str = "v1",
        created_by: str = "system",
    ) -> Dict[str, Any]:
        dim = str(dimension or "").strip().lower()
        otype = str(object_type or "").strip()
        okey = str(object_key or "").strip()
        if not is_signal_dimension(dim):
            raise ValueError(f"Unknown signal dimension: {dimension!r}")
        if not otype or not okey:
            raise ValueError("object_type and object_key are required")
        self._validate_object_type(dim, otype)

        payload_json = json.dumps(payload)
        refs_json = json.dumps(source_refs or [])
        now = _now_iso()

        row = self._conn.execute(
            """
            SELECT object_id, payload_json, confidence, source_refs_json
            FROM signal_objects
            WHERE signal_dimension=? AND object_type=? AND object_key=? AND valid_to IS NULL
            """,
            (dim, otype, okey),
        ).fetchone()

        if row:
            if row[1] == payload_json and float(row[2]) == float(confidence) and row[3] == refs_json:
                return self.get_object(str(row[0]))
            self._conn.execute(
                """
                UPDATE signal_objects
                SET payload_json=?, confidence=?, source_refs_json=?,
                    extractor_version=?, updated_at=?, updated_by=?
                WHERE object_id=?
                """,
                (payload_json, confidence, refs_json, extractor_version, now, created_by, row[0]),
            )
            self._conn.commit()
            return self.get_object(str(row[0]))

        object_id = str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO signal_objects (
                object_id, signal_dimension, object_type, object_key,
                payload_json, confidence, source_refs_json,
                valid_from, valid_to, extractor_version,
                created_at, updated_at, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                object_id,
                dim,
                otype,
                okey,
                payload_json,
                confidence,
                refs_json,
                now,
                extractor_version,
                now,
                now,
                created_by,
            ),
        )
        self._conn.commit()
        return self.get_object(object_id)

    def list_objects(
        self,
        dimension: str,
        *,
        object_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        dim = str(dimension or "").strip().lower()
        if not is_signal_dimension(dim):
            raise ValueError(f"Unknown signal dimension: {dimension!r}")
        limit = max(1, min(int(limit), 200))
        params: List[Any] = [dim]
        type_clause = ""
        if object_type:
            type_clause = " AND object_type=?"
            params.append(str(object_type).strip())
        params.append(limit)
        rows = self._conn.execute(
            f"""
            SELECT object_id, signal_dimension, object_type, object_key,
                   payload_json, confidence, source_refs_json,
                   valid_from, valid_to, extractor_version,
                   created_at, updated_at, created_by
            FROM signal_objects
            WHERE signal_dimension=? AND valid_to IS NULL{type_clause}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._row_to_object(r) for r in rows]

    def get_object(self, object_id: str) -> Dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT object_id, signal_dimension, object_type, object_key,
                   payload_json, confidence, source_refs_json,
                   valid_from, valid_to, extractor_version,
                   created_at, updated_at, created_by
            FROM signal_objects WHERE object_id=?
            """,
            (str(object_id),),
        ).fetchone()
        if not row:
            raise ValueError(f"Signal object not found: {object_id!r}")
        return self._row_to_object(row)

    def supersede_object(
        self,
        object_id: str,
        new_payload: Dict[str, Any],
        *,
        source_refs: Optional[List[Dict[str, Any]]] = None,
        confidence: Optional[float] = None,
        created_by: str = "system",
        extractor_version: str = "v1",
    ) -> Dict[str, Any]:
        current = self.get_object(object_id)
        if current.get("valid_to"):
            raise ValueError("Cannot supersede inactive signal object")
        now = _now_iso()
        self._conn.execute(
            "UPDATE signal_objects SET valid_to=?, updated_at=? WHERE object_id=?",
            (now, now, object_id),
        )
        return self.upsert_object(
            str(current["signal_dimension"]),
            str(current["object_type"]),
            str(current["object_key"]),
            new_payload,
            source_refs=source_refs if source_refs is not None else current.get("source_refs"),
            confidence=float(confidence if confidence is not None else current.get("confidence") or 0.5),
            extractor_version=extractor_version,
            created_by=created_by,
        )

    def owner_override(
        self,
        object_id: str,
        payload_patch: Dict[str, Any],
    ) -> Dict[str, Any]:
        current = self.get_object(object_id)
        merged = dict(current.get("payload") or {})
        merged.update(payload_patch)
        meta = merged.get("_meta") if isinstance(merged.get("_meta"), dict) else {}
        meta["explicitness"] = "user_authored"
        merged["_meta"] = meta
        return self.supersede_object(
            object_id,
            merged,
            created_by="owner",
            extractor_version="owner_override",
        )

    def _validate_object_type(self, dimension: str, object_type: str) -> None:
        defn = get_definition_or_none(dimension)
        if defn is None:
            return
        entity_ids = {
            str(item.get("id") or "")
            for item in (defn.get("primary_entity_types") or [])
            if isinstance(item, dict)
        }
        signal_names = set(defn.get("signal_objects") or []) | set(defn.get("gate_objects") or [])
        allowed = entity_ids | signal_names
        if allowed and object_type not in allowed:
            raise ValueError(
                f"object_type {object_type!r} not declared for dimension {dimension!r}"
            )

    def _row_to_object(self, row: Any) -> Dict[str, Any]:
        return {
            "object_id": row[0],
            "signal_dimension": row[1],
            "object_type": row[2],
            "object_key": row[3],
            "payload": json.loads(row[4] or "{}"),
            "confidence": float(row[5] or 0),
            "source_refs": json.loads(row[6] or "[]"),
            "valid_from": row[7],
            "valid_to": row[8],
            "extractor_version": row[9],
            "created_at": row[10],
            "updated_at": row[11],
            "created_by": row[12],
        }

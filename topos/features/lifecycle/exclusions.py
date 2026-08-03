"""Owner exclusions: "this must not be part of my intelligence."

Deletion without a tombstone is futile — extraction is idempotent, so the next
enrichment batch would re-assert the fact, re-create the entity, or re-promote
the insight. An exclusion therefore does two things:

  1. removes/closes the current artifact (soft-close where the artifact
     supports it, preserving provenance history; hard-delete otherwise)
  2. writes a tombstone consulted by every producer before writing

Provenance stance: owner exclusions soft-close facts (valid_to set, row kept)
so the belief history survives; connector *scrub* hard-deletes because scrub's
contract is "the data is gone".
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

ARTIFACT_TYPES = ("fact", "entity", "stat_insight", "record")


def _now_key() -> str:
    return f"exc_{uuid.uuid4().hex[:12]}"


def normalize_exclusion_key(artifact_type: str, artifact_key: str) -> str:
    key = str(artifact_key or "").strip()
    if artifact_type == "entity":
        from ..entities.resolver import normalize_name

        return normalize_name(key)
    return key.lower() if artifact_type == "fact" else key


class ExclusionStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -------------------------------------------------------------- reads

    def is_excluded(self, artifact_type: str, artifact_key: str) -> bool:
        key = normalize_exclusion_key(artifact_type, artifact_key)
        if not key:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM intelligence_exclusions WHERE artifact_type=? AND artifact_key=?",
            (artifact_type, key),
        ).fetchone()
        return row is not None

    def list(self, *, artifact_type: Optional[str] = None) -> List[Dict[str, Any]]:
        query = (
            "SELECT exclusion_id, artifact_type, artifact_key, note, created_at"
            " FROM intelligence_exclusions"
        )
        params: List[Any] = []
        if artifact_type:
            query += " WHERE artifact_type=?"
            params.append(artifact_type)
        query += " ORDER BY created_at DESC"
        return [
            {
                "exclusion_id": r[0],
                "artifact_type": r[1],
                "artifact_key": r[2],
                "note": r[3],
                "created_at": r[4],
            }
            for r in self._conn.execute(query, params).fetchall()
        ]

    # ------------------------------------------------------------- writes

    def _tombstone(self, artifact_type: str, artifact_key: str, note: Optional[str]) -> str:
        key = normalize_exclusion_key(artifact_type, artifact_key)
        if artifact_type not in ARTIFACT_TYPES:
            raise ValueError(f"unknown artifact_type: {artifact_type}")
        if not key:
            raise ValueError("artifact_key is required")
        exclusion_id = _now_key()
        self._conn.execute(
            """
            INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key, note)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(artifact_type, artifact_key) DO UPDATE SET note=excluded.note
            """,
            (exclusion_id, artifact_type, key, note),
        )
        return exclusion_id

    def remove_exclusion(self, artifact_type: str, artifact_key: str) -> bool:
        key = normalize_exclusion_key(artifact_type, artifact_key)
        cursor = self._conn.execute(
            "DELETE FROM intelligence_exclusions WHERE artifact_type=? AND artifact_key=?",
            (artifact_type, key),
        )
        self._conn.commit()
        return bool(cursor.rowcount)

    # -------------------------------------------------- exclusion actions

    def exclude_fact(
        self,
        *,
        subject_entity_id: str,
        predicate: str,
        object_value: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Soft-close matching active facts and tombstone re-assertion.

        object_value None excludes the whole predicate for the subject.
        """
        from ..facts.store import normalize_predicate

        pred = normalize_predicate(predicate)
        key = f"{subject_entity_id}:{pred}"
        if object_value:
            key += f":{str(object_value).strip().lower()}"
        exclusion_id = self._tombstone("fact", key, note)

        pattern = f"fact:{subject_entity_id}:{pred}%"
        rows = self._conn.execute(
            """
            SELECT object_id, payload_json FROM signal_objects
            WHERE object_type='fact' AND object_key LIKE ? AND valid_to IS NULL
            """,
            (pattern,),
        ).fetchall()
        closed = 0
        for object_id, payload_json in rows:
            try:
                payload = json.loads(payload_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            if object_value and str(payload.get("object_value") or "").strip().lower() != str(
                object_value
            ).strip().lower():
                continue
            payload["excluded_by_owner"] = True
            self._conn.execute(
                """
                UPDATE signal_objects
                SET valid_to=datetime('now'), payload_json=?, updated_at=datetime('now')
                WHERE object_id=?
                """,
                (json.dumps(payload), object_id),
            )
            closed += 1
        self._conn.commit()
        return {"exclusion_id": exclusion_id, "facts_closed": closed}

    def exclude_entity(self, *, entity_ref: str, note: Optional[str] = None) -> Dict[str, Any]:
        """Stop tracking an entity: tombstone the name, purge derived traces.

        entity_ref may be an entity_id or a name. Mentions, edges, and the
        dossier are removed; the canonical record content is untouched (this
        is intelligence exclusion, not content deletion).
        """
        from ..entities.resolver import normalize_name

        row = self._conn.execute(
            "SELECT entity_id, canonical_name FROM entities WHERE entity_id=?",
            (entity_ref,),
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                "SELECT entity_id, canonical_name FROM entities WHERE normalized_name=?",
                (normalize_name(entity_ref),),
            ).fetchone()
        exclusion_id = self._tombstone(
            "entity", row[1] if row else entity_ref, note
        )
        result: Dict[str, Any] = {"exclusion_id": exclusion_id, "entity_found": row is not None}
        if row is None:
            self._conn.commit()
            return result
        entity_id = str(row[0])
        result["mentions_removed"] = self._conn.execute(
            "DELETE FROM entity_mentions WHERE entity_id=?", (entity_id,)
        ).rowcount
        result["edges_removed"] = self._conn.execute(
            "DELETE FROM entity_edges WHERE src_entity_id=? OR dst_entity_id=?",
            (entity_id, entity_id),
        ).rowcount
        # Latent centroids are keyed only by entity_id; leave them and the next
        # affinity rebuild can invent edges naming a deleted person.
        try:
            result["context_vectors_removed"] = self._conn.execute(
                "DELETE FROM entity_context_vectors WHERE entity_id=?",
                (entity_id,),
            ).rowcount
        except Exception:  # noqa: BLE001 — table may be absent on pre-migration DBs
            result["context_vectors_removed"] = 0
        self._conn.execute(
            "UPDATE signal_objects SET valid_to=datetime('now') "
            "WHERE object_type='entity_dossier' AND object_key=? AND valid_to IS NULL",
            (f"dossier:{entity_id}",),
        )
        self._conn.execute("DELETE FROM entities WHERE entity_id=?", (entity_id,))
        self._conn.commit()
        return result

    def exclude_stat_insight(
        self, *, stat_id: str, group_key: str = "", note: Optional[str] = None
    ) -> Dict[str, Any]:
        """Remove a promoted insight and stop its re-promotion.

        The underlying stat_state keeps folding (the owner may re-include
        later, and the state is owner-only anyway); only the packaged insight
        is suppressed.
        """
        key = f"{stat_id}:{group_key or 'all'}"
        exclusion_id = self._tombstone("stat_insight", key, note)
        cursor = self._conn.execute(
            "DELETE FROM signal_facts WHERE fact_id=?", (f"stat:{key}",)
        )
        self._conn.commit()
        return {"exclusion_id": exclusion_id, "insights_removed": int(cursor.rowcount or 0)}

    def exclude_record(self, *, record_id: str, note: Optional[str] = None) -> Dict[str, Any]:
        """Remove everything derived from one canonical record and block re-derivation.

        The canonical row itself is preserved (provenance) but contributes to
        no intelligence layer from now on.
        """
        from .derived_scrub import purge_derived_for_records

        exclusion_id = self._tombstone("record", record_id, note)
        self._conn.commit()
        report = purge_derived_for_records(self._conn, [record_id])
        return {"exclusion_id": exclusion_id, **report}


def excluded_record_ids(conn: sqlite3.Connection) -> set:
    try:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT artifact_key FROM intelligence_exclusions WHERE artifact_type='record'"
            ).fetchall()
        }
    except sqlite3.OperationalError:
        return set()

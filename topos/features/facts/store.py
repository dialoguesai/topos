"""FactStore: (subject, predicate, object) claims with valid_from/valid_to.

Facts live in signal_objects (object_type='fact'); this store manages its own
rows because belief revision differs from upsert_object's in-place update:

  * same value again        -> refresh (merge source_refs, max confidence)
  * contradicting value,
    comparable confidence   -> supersede: close old row (valid_to), insert new
  * contradicting value,
    much weaker confidence  -> keep incumbent, queue a fact_conflict for the
                               owner instead of silently overwriting

Closed rows are never deleted — "as of" queries and change history come free.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ...storage.db.write_gate import commit_connection, with_db_write

CONFLICT_CONFIDENCE_MARGIN = 0.10

# Small controlled predicate vocabulary; free-form predicates are allowed but
# normalized so near-duplicates collide instead of accumulating.
KNOWN_PREDICATES = {
    "works_at",
    "worked_at",
    "works_on",
    "role_is",
    "certified_in",
    "studied_at",
    "skilled_in",
    "lives_in",
    "member_of",
    "prefers",
    "practices",
    "training_for",
}

# Multi-valued predicates: several objects can be simultaneously true
# (careers have many worked_at rows). Each object gets its own revision chain;
# single-valued predicates supersede on contradiction.
MULTI_VALUED_PREDICATES = {
    "worked_at",
    "works_on",
    "advises",
    "certified_in",
    "studied_at",
    "skilled_in",
    "member_of",
    "practices",
    "prefers",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_predicate(predicate: str) -> str:
    return "_".join(str(predicate or "").strip().lower().split())


def _normalize_value(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


class FactStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------ writes

    def _is_excluded(self, subject_entity_id: str, pred: str, object_value: str) -> bool:
        keys = (
            f"{subject_entity_id}:{pred}",
            f"{subject_entity_id}:{pred}:{_normalize_value(object_value)}",
        )
        try:
            for key in keys:
                row = self._conn.execute(
                    "SELECT 1 FROM intelligence_exclusions WHERE artifact_type='fact' AND artifact_key=?",
                    (key,),
                ).fetchone()
                if row:
                    return True
        except sqlite3.OperationalError:
            return False
        return False

    def _object_key(self, subject_entity_id: str, pred: str, object_value: str) -> str:
        key = f"fact:{subject_entity_id}:{pred}"
        if pred in MULTI_VALUED_PREDICATES:
            key += f":{_normalize_value(object_value)[:48]}"
        return key

    def assert_fact(
        self,
        *,
        subject_entity_id: str,
        predicate: str,
        object_value: str,
        object_entity_id: Optional[str] = None,
        dimension: str = "profile",
        confidence: float = 0.7,
        source_refs: Optional[List[Dict[str, Any]]] = None,
        valid_from: Optional[str] = None,
        disclosure: str = "scoped",
        period_start: Optional[str] = None,
        period_end: Optional[str] = None,
        asserted_by: str = "owner",
    ) -> Dict[str, Any]:
        pred = normalize_predicate(predicate)
        if not subject_entity_id or not pred or not str(object_value or "").strip():
            raise ValueError("subject, predicate and object_value are required")
        if self._is_excluded(subject_entity_id, pred, object_value):
            return None  # owner-excluded: never re-assert
        valid_from = valid_from or _now_iso()
        object_key = self._object_key(subject_entity_id, pred, object_value)

        incumbent = self._active_fact_by_key(object_key)
        if incumbent is not None:
            payload = incumbent["payload"]
            if _normalize_value(payload.get("object_value")) == _normalize_value(object_value):
                return self._refresh(incumbent, confidence, source_refs)
            if float(confidence) < float(payload.get("confidence") or 0.0) - CONFLICT_CONFIDENCE_MARGIN:
                self._queue_conflict(subject_entity_id, pred, incumbent["object_id"], object_value, confidence)
                return incumbent
            # Supersede: the close joins the INSERT below in one gated commit.

        object_id = str(uuid.uuid4())
        payload = {
            "subject_entity_id": subject_entity_id,
            "predicate": pred,
            "object_value": str(object_value).strip(),
            "object_entity_id": object_entity_id,
            "confidence": round(float(confidence), 3),
            "disclosure": disclosure,
            # P4.4: who holds the fact to be true (owner | contact:<id> |
            # assistant | page-author). Non-owner assertions render with
            # attribution — see render().
            "asserted_by": str(asserted_by or "owner"),
        }
        if period_start:
            payload["period_start"] = str(period_start)
        if period_end:
            payload["period_end"] = str(period_end)
        now = _now_iso()
        insert_params = [
            object_id,
            str(dimension).strip().lower(),
            object_key,
            json.dumps(payload),
            float(confidence),
            json.dumps(source_refs or []),
            valid_from,
            now,
            now,
        ]
        with with_db_write():
            if incumbent is not None:
                self._close(incumbent["object_id"], valid_to=valid_from)
            try:
                # B2.1: real-world period stamped into the indexed event-time
                # columns alongside the payload keys.
                self._conn.execute(
                    """
                    INSERT INTO signal_objects (
                        object_id, signal_dimension, object_type, object_key,
                        payload_json, confidence, source_refs_json,
                        valid_from, valid_to, extractor_version,
                        created_at, updated_at, created_by, period_start, period_end
                    ) VALUES (?, ?, 'fact', ?, ?, ?, ?, ?, NULL, 'fact_store_v1', ?, ?, 'system', ?, ?)
                    """,
                    (
                        *insert_params,
                        str(period_start) if period_start else None,
                        str(period_end) if period_end else None,
                    ),
                )
            except sqlite3.OperationalError:
                # Pre-B2.1 schema (migration not run): legacy column set.
                self._conn.execute(
                    """
                    INSERT INTO signal_objects (
                        object_id, signal_dimension, object_type, object_key,
                        payload_json, confidence, source_refs_json,
                        valid_from, valid_to, extractor_version,
                        created_at, updated_at, created_by
                    ) VALUES (?, ?, 'fact', ?, ?, ?, ?, ?, NULL, 'fact_store_v1', ?, ?, 'system')
                    """,
                    insert_params,
                )
            commit_connection(self._conn)
        return self._row_to_fact(self._get_row(object_id))

    def _refresh(
        self,
        incumbent: Dict[str, Any],
        confidence: float,
        source_refs: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        refs = list(incumbent.get("source_refs") or [])
        for ref in source_refs or []:
            if ref not in refs:
                refs.append(ref)
        payload = dict(incumbent["payload"])
        payload["confidence"] = round(max(float(payload.get("confidence") or 0.0), float(confidence)), 3)
        with with_db_write():
            self._conn.execute(
                """
                UPDATE signal_objects
                SET payload_json=?, confidence=?, source_refs_json=?, updated_at=?
                WHERE object_id=?
                """,
                (
                    json.dumps(payload),
                    payload["confidence"],
                    json.dumps(refs),
                    _now_iso(),
                    incumbent["object_id"],
                ),
            )
            commit_connection(self._conn)
        return self._row_to_fact(self._get_row(incumbent["object_id"]))

    def _close(self, object_id: str, *, valid_to: str) -> None:
        self._conn.execute(
            "UPDATE signal_objects SET valid_to=?, updated_at=? WHERE object_id=?",
            (valid_to, _now_iso(), object_id),
        )

    def _queue_conflict(
        self,
        subject_entity_id: str,
        predicate: str,
        incumbent_object_id: str,
        challenger_value: str,
        challenger_confidence: float,
    ) -> None:
        with with_db_write():
            self._conn.execute(
                """
                INSERT INTO fact_conflicts (
                    conflict_id, subject_entity_id, predicate,
                    incumbent_object_id, challenger_value, challenger_confidence
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"cfl_{uuid.uuid4().hex[:12]}",
                    subject_entity_id,
                    predicate,
                    incumbent_object_id,
                    str(challenger_value),
                    float(challenger_confidence),
                ),
            )
            commit_connection(self._conn)

    # ------------------------------------------------------------- reads

    def _get_row(self, object_id: str):
        return self._conn.execute(
            """
            SELECT object_id, signal_dimension, payload_json, confidence,
                   source_refs_json, valid_from, valid_to
            FROM signal_objects WHERE object_id=?
            """,
            (object_id,),
        ).fetchone()

    def _row_to_fact(self, row) -> Dict[str, Any]:
        payload = json.loads(row[2] or "{}")
        return {
            "object_id": row[0],
            "dimension": row[1],
            "payload": payload,
            "confidence": row[3],
            "source_refs": json.loads(row[4] or "[]"),
            "valid_from": row[5],
            "valid_to": row[6],
        }

    def _active_fact_by_key(self, object_key: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            """
            SELECT object_id, signal_dimension, payload_json, confidence,
                   source_refs_json, valid_from, valid_to
            FROM signal_objects
            WHERE object_type='fact' AND object_key=? AND valid_to IS NULL
            """,
            (object_key,),
        ).fetchone()
        return self._row_to_fact(row) if row else None

    def facts_for_subject(
        self,
        subject_entity_id: str,
        *,
        as_of: Optional[str] = None,
        include_closed: bool = False,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT object_id, signal_dimension, payload_json, confidence,
                   source_refs_json, valid_from, valid_to
            FROM signal_objects
            WHERE object_type='fact' AND object_key LIKE ?
        """
        params: List[Any] = [f"fact:{subject_entity_id}:%"]
        if as_of:
            query += " AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
            params.extend([as_of, as_of])
        elif not include_closed:
            query += " AND valid_to IS NULL"
        query += " ORDER BY valid_from DESC"
        return [self._row_to_fact(r) for r in self._conn.execute(query, params).fetchall()]

    def history(self, subject_entity_id: str, predicate: str) -> List[Dict[str, Any]]:
        pred = normalize_predicate(predicate)
        rows = self._conn.execute(
            """
            SELECT object_id, signal_dimension, payload_json, confidence,
                   source_refs_json, valid_from, valid_to
            FROM signal_objects
            WHERE object_type='fact'
              AND (object_key=? OR object_key LIKE ?)
            ORDER BY valid_from ASC
            """,
            (f"fact:{subject_entity_id}:{pred}", f"fact:{subject_entity_id}:{pred}:%"),
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def search(
        self,
        query_tokens: List[str],
        *,
        limit: int = 10,
        include_closed: bool = False,
    ) -> List[Dict[str, Any]]:
        """Token match over facts' predicate + object_value. Active facts only by
        default; include_closed=True adds superseded revisions (past-tense
        queries — "where did I work before")."""
        if not query_tokens:
            return []
        closed_filter = "" if include_closed else "AND valid_to IS NULL"
        rows = self._conn.execute(
            f"""
            SELECT object_id, signal_dimension, payload_json, confidence,
                   source_refs_json, valid_from, valid_to
            FROM signal_objects
            WHERE object_type='fact' {closed_filter}
            ORDER BY updated_at DESC LIMIT 500
            """
        ).fetchall()
        tokens = {t.lower() for t in query_tokens}
        scored: List[tuple[int, Dict[str, Any]]] = []
        for row in rows:
            fact = self._row_to_fact(row)
            blob = " ".join(
                [
                    str(fact["payload"].get("predicate") or "").replace("_", " "),
                    str(fact["payload"].get("object_value") or ""),
                ]
            ).lower()
            overlap = sum(1 for t in tokens if t in blob)
            if overlap:
                scored.append((overlap, fact))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [fact for _, fact in scored[:limit]]

    @staticmethod
    def render(fact: Dict[str, Any], *, subject_name: str = "") -> str:
        payload = fact.get("payload") or {}
        pred = str(payload.get("predicate") or "").replace("_", " ")
        value = payload.get("object_value")
        subject = subject_name or "owner"
        text = f"{subject} {pred} {value}"
        # Only render a real-world period sourced from the record. valid_from
        # is belief-validity metadata (when the fact was extracted), NOT when
        # the claim became true — rendering "works at X since <extraction
        # date>" is both misleading and, because it emits a bare date, trips
        # the grantee PII redactor (dates mis-matched as phone numbers).
        period_start = payload.get("period_start")
        period_end = payload.get("period_end")
        if period_start and period_end:
            text += f" ({period_start}–{period_end})"
        elif period_start:
            text += f" (since {period_start})"
        # P4.4: non-owner assertions carry attribution — "who holds this fact
        # to be true" must be visible wherever the claim is rendered.
        asserted_by = str(payload.get("asserted_by") or "owner")
        if asserted_by != "owner":
            text += f" — per {asserted_by}"
        return text

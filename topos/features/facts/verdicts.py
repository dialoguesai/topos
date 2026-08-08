"""Owner verdicts on extracted facts: confirm, reject, edit.

The extractor asserts beliefs; the owner is the authority. A verdict maps onto
the existing belief-revision machinery rather than adding a parallel state:

  confirm — human truth outranks any extractor: confidence goes to 1.0 (the
            original extractor confidence is preserved as ``extracted_confidence``)
            and the row is stamped ``verified_by_owner``/``verified_at``. With
            CONFLICT_CONFIDENCE_MARGIN=0.10, later LLM re-asserts (0.55) can
            only queue a fact_conflict — they can never silently supersede a
            confirmed fact.
  reject  — delegates to ExclusionStore.exclude_fact scoped to THIS value
            (soft-close + tombstone), so idempotent re-extraction cannot
            resurrect it. The whole-predicate wipe stays an explicit
            exclusions-API action, never an accidental UI one.
  edit    — belief revision by the owner: tombstone the old value (so the
            extractor cannot re-assert it), close the old row (history kept),
            assert the corrected value at confidence 1.0 with owner
            verification. Attribution (``asserted_by``) may be corrected in
            place — who said it is metadata repair, not a new belief.

All actions require the target to be an ACTIVE fact row (valid_to IS NULL);
verdicts on closed revisions would silently edit history.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ...storage.db.write_gate import commit_connection, with_db_write
from .store import FactStore

VERDICT_ACTIONS = ("confirm", "reject", "edit")

# Attribution grammar (P4.4): owner | assistant | page-author | contact:<id>.
_ASSERTED_BY_FIXED = {"owner", "assistant", "page-author"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_asserted_by(value: str) -> bool:
    return value in _ASSERTED_BY_FIXED or value.startswith("contact:")


def _load_active_fact(conn: sqlite3.Connection, object_id: str) -> Dict[str, Any]:
    row = conn.execute(
        """
        SELECT object_id, signal_dimension, payload_json, confidence,
               source_refs_json, valid_from, valid_to
        FROM signal_objects WHERE object_type='fact' AND object_id=?
        """,
        (object_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no fact with object_id {object_id!r}")
    if row[6] is not None:
        raise ValueError("fact is closed (superseded or excluded); verdicts apply to active facts")
    payload = json.loads(row[2] or "{}")
    return {
        "object_id": row[0],
        "dimension": row[1],
        "payload": payload,
        "confidence": row[3],
        "source_refs": json.loads(row[4] or "[]"),
        "valid_from": row[5],
    }


def _write_payload(
    conn: sqlite3.Connection, object_id: str, payload: Dict[str, Any], confidence: float
) -> None:
    with with_db_write():
        conn.execute(
            """
            UPDATE signal_objects
            SET payload_json=?, confidence=?, updated_at=?
            WHERE object_id=?
            """,
            (json.dumps(payload), float(confidence), _now_iso(), object_id),
        )
        commit_connection(conn)


def confirm_fact(conn: sqlite3.Connection, object_id: str) -> Dict[str, Any]:
    """Owner attests the fact is true. Idempotent."""
    fact = _load_active_fact(conn, object_id)
    payload = dict(fact["payload"])
    if not payload.get("verified_by_owner"):
        payload.setdefault("extracted_confidence", payload.get("confidence"))
        payload["confidence"] = 1.0
        payload["verified_by_owner"] = True
        payload["verified_at"] = _now_iso()
        _write_payload(conn, object_id, payload, 1.0)
    return {"status": "confirmed", "object_id": object_id, "payload": payload}


def reject_fact(
    conn: sqlite3.Connection, object_id: str, *, note: Optional[str] = None
) -> Dict[str, Any]:
    """Owner says this fact is wrong: close it and tombstone this exact value."""
    fact = _load_active_fact(conn, object_id)
    payload = fact["payload"]
    from ..lifecycle.exclusions import ExclusionStore

    result = ExclusionStore(conn).exclude_fact(
        subject_entity_id=str(payload.get("subject_entity_id") or ""),
        predicate=str(payload.get("predicate") or ""),
        object_value=str(payload.get("object_value") or ""),
        note=note or "owner rejected via fact review",
    )
    return {"status": "rejected", "object_id": object_id, **result}


def edit_fact(
    conn: sqlite3.Connection,
    object_id: str,
    *,
    object_value: Optional[str] = None,
    asserted_by: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Owner corrects the value and/or the attribution.

    Value change = supersession (old row closed, tombstoned, history kept).
    Attribution-only change = in-place payload repair.
    """
    if object_value is None and asserted_by is None:
        raise ValueError("edit requires object_value and/or asserted_by")
    if asserted_by is not None and not _valid_asserted_by(asserted_by):
        raise ValueError(
            f"asserted_by must be one of {sorted(_ASSERTED_BY_FIXED)} or 'contact:<id>'"
        )

    fact = _load_active_fact(conn, object_id)
    payload = dict(fact["payload"])
    old_value = str(payload.get("object_value") or "")
    new_value = str(object_value).strip() if object_value is not None else old_value

    if object_value is not None and new_value.lower() != old_value.strip().lower():
        from ..lifecycle.exclusions import ExclusionStore

        # Tombstone + close the wrong value first (also removes it from the
        # active set so single-valued predicates have no incumbent to fight).
        ExclusionStore(conn).exclude_fact(
            subject_entity_id=str(payload.get("subject_entity_id") or ""),
            predicate=str(payload.get("predicate") or ""),
            object_value=old_value,
            note=note or f"owner corrected to {new_value!r}",
        )
        corrected = FactStore(conn).assert_fact(
            subject_entity_id=str(payload.get("subject_entity_id") or ""),
            predicate=str(payload.get("predicate") or ""),
            object_value=new_value,
            object_entity_id=payload.get("object_entity_id"),
            dimension=str(fact.get("dimension") or "profile"),
            confidence=1.0,
            source_refs=fact["source_refs"],
            disclosure=str(payload.get("disclosure") or "scoped"),
            period_start=payload.get("period_start"),
            period_end=payload.get("period_end"),
            asserted_by=asserted_by or "owner",
        )
        if corrected is None:  # the corrected value itself is tombstoned
            raise ValueError(
                f"value {new_value!r} was previously excluded by the owner; "
                "lift that exclusion before re-asserting it"
            )
        new_payload = dict(corrected["payload"])
        new_payload["verified_by_owner"] = True
        new_payload["verified_at"] = _now_iso()
        new_payload["corrected_from"] = old_value
        _write_payload(conn, corrected["object_id"], new_payload, 1.0)
        return {
            "status": "edited",
            "object_id": corrected["object_id"],
            "superseded_object_id": object_id,
            "payload": new_payload,
        }

    # Attribution-only repair (or a same-value "edit"): update in place.
    if asserted_by is not None:
        payload["asserted_by"] = asserted_by
    _write_payload(conn, object_id, payload, float(payload.get("confidence") or fact["confidence"] or 0.7))
    return {"status": "edited", "object_id": object_id, "payload": payload}


def apply_fact_verdict(
    conn: sqlite3.Connection,
    *,
    object_id: str,
    action: str,
    object_value: Optional[str] = None,
    asserted_by: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Single dispatch used by both the REST route and the WS handler."""
    action = str(action or "").strip().lower()
    if action not in VERDICT_ACTIONS:
        raise ValueError(f"action must be one of {VERDICT_ACTIONS}")
    if action == "confirm":
        return confirm_fact(conn, object_id)
    if action == "reject":
        return reject_fact(conn, object_id, note=note)
    return edit_fact(
        conn, object_id, object_value=object_value, asserted_by=asserted_by, note=note
    )

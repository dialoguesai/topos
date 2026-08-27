"""The owner's own corrections to the social graph, stored as an overlay.

Nothing here edits `entities` or `entity_mentions`. Every correction is a row that is applied
OVER the derived graph at read time, for three reasons and the third is the one that decides
it:

* Re-derivation would otherwise wipe the owner's work on the next sync.
* Undo is free, because nothing was destroyed.
* `merge_entities` on this codebase is **not reliably reversible**. A destructive merge is a
  one-way door, and the owner WILL merge two people wrongly at some point, because people
  share names. `Bravo Yankee` and `Charlie Yankee` are both real on this node.

Revoking, not deleting, is how undo works: a revoked row stays as history. "What did I
already decide about this person, and when" is itself worth keeping.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

OVERLAY_TABLE = "person_graph_overlay"

#: What the owner can assert. Each is reversible by construction.
ACTION_MERGE = "merge"        # "these two nodes are one person"; value = the surviving node
ACTION_BAND = "band"          # move a person between salience bands; value = band name
ACTION_DISMISS = "dismiss"    # "not someone I know" — HIDES, never deletes
ACTION_RENAME = "rename"      # the owner's name for someone, over extraction
ACTION_NOTE = "note"          # free text, owner-only, excluded from derivation
ACTION_TAG = "tag"            # colleague / family / inner circle …
ACTIONS = (ACTION_MERGE, ACTION_BAND, ACTION_DISMISS, ACTION_RENAME, ACTION_NOTE, ACTION_TAG)

#: Actions where a later assertion replaces the earlier one rather than adding to it. Notes
#: and tags accumulate; a band or a name does not.
SINGLETON_ACTIONS = frozenset({ACTION_BAND, ACTION_DISMISS, ACTION_RENAME, ACTION_MERGE})

#: A merge chain longer than this is a cycle or a mistake; resolving it forever would hang a
#: page load.
MAX_MERGE_DEPTH = 16


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_overlay_table(conn: Any) -> None:
    """Feature-owned, additive DDL — never a registry migration.

    Bumping `user_version` past what an installed engine understands fences the node out of
    every write (2026-08-25 outage). This table is created in place and read defensively.
    """
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {OVERLAY_TABLE} (
              overlay_id   TEXT PRIMARY KEY,
              dataset_id   TEXT NOT NULL,
              subject_id   TEXT NOT NULL,
              action       TEXT NOT NULL,
              value        TEXT,
              created_at   TEXT NOT NULL,
              revoked_at   TEXT
        )"""
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{OVERLAY_TABLE}_live"
        f" ON {OVERLAY_TABLE} (dataset_id, subject_id, action, revoked_at)"
    )


def ensure_overlay_table(conn: Any) -> None:
    from ..storage.db.write_gate import commit_connection, with_db_write

    with with_db_write():
        create_overlay_table(conn)
        commit_connection(conn)


def _table_missing(conn: Any) -> bool:
    """Reads never run DDL — that takes SQLite's WRITE LOCK on every page load, and raises
    outright on a read-only connection."""
    try:
        return not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (OVERLAY_TABLE,)).fetchone()
    except sqlite3.Error:
        return True


def record(conn: Any, dataset_id: str, subject_id: str, action: str,
           value: Optional[str] = None) -> Dict[str, Any]:
    """Write one owner assertion. Returns the row, so the caller can offer an undo."""
    action = str(action or "").strip().lower()
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}; expected one of {ACTIONS}")
    subject_id = str(subject_id or "").strip()
    if not subject_id:
        raise ValueError("subject_id is required")

    from ..storage.db.write_gate import commit_connection, with_db_write

    row = {
        "overlay_id": f"ovl_{uuid.uuid4().hex[:16]}",
        "dataset_id": str(dataset_id or ""),
        "subject_id": subject_id,
        "action": action,
        "value": None if value is None else str(value),
        "created_at": _now(),
        "revoked_at": None,
    }
    with with_db_write():
        create_overlay_table(conn)
        if action in SINGLETON_ACTIONS:
            # A second band or rename REPLACES the first rather than stacking, but the
            # earlier decision is revoked, not deleted — the history is the point.
            conn.execute(
                f"UPDATE {OVERLAY_TABLE} SET revoked_at=?"
                f" WHERE dataset_id=? AND subject_id=? AND action=? AND revoked_at IS NULL",
                (row["created_at"], row["dataset_id"], subject_id, action))
        conn.execute(
            f"INSERT INTO {OVERLAY_TABLE}"
            f" (overlay_id, dataset_id, subject_id, action, value, created_at, revoked_at)"
            f" VALUES (?,?,?,?,?,?,?)",
            tuple(row[k] for k in ("overlay_id", "dataset_id", "subject_id", "action",
                                   "value", "created_at", "revoked_at")))
        commit_connection(conn)
    return row


def revoke(conn: Any, overlay_id: str) -> bool:
    """Undo. The row stays as history — 'what did I already decide, and when' is worth
    keeping, and a deleted decision cannot be re-examined."""
    from ..storage.db.write_gate import commit_connection, with_db_write

    if _table_missing(conn):
        return False
    with with_db_write():
        cur = conn.execute(
            f"UPDATE {OVERLAY_TABLE} SET revoked_at=?"
            f" WHERE overlay_id=? AND revoked_at IS NULL", (_now(), str(overlay_id)))
        commit_connection(conn)
        return bool(cur.rowcount)


def load(conn: Any, dataset_id: str, *, include_revoked: bool = False) -> List[Dict[str, Any]]:
    if _table_missing(conn):
        return []
    sql = (f"SELECT overlay_id, subject_id, action, value, created_at, revoked_at"
           f"  FROM {OVERLAY_TABLE} WHERE dataset_id=?")
    if not include_revoked:
        sql += " AND revoked_at IS NULL"
    sql += " ORDER BY created_at"
    try:
        rows = conn.execute(sql, (str(dataset_id or ""),)).fetchall()
    except sqlite3.Error:
        return []
    keys = ("overlay_id", "subject_id", "action", "value", "created_at", "revoked_at")
    return [dict(zip(keys, r)) for r in rows]


def resolve_merges(merges: Dict[str, str]) -> Dict[str, str]:
    """Follow merge chains to their survivor, and refuse to loop.

    A -> B and B -> C must land everything on C. A cycle (A -> B -> A) is a mistake the owner
    can make in two clicks, and it must not hang a page load — the chain simply stops.
    """
    out: Dict[str, str] = {}
    for start in merges:
        seen = {start}
        target = merges[start]
        depth = 0
        while target in merges and depth < MAX_MERGE_DEPTH:
            if merges[target] in seen:
                break  # cycle: keep the last sane hop rather than spinning
            seen.add(target)
            target = merges[target]
            depth += 1
        out[start] = target
    return out


def apply_overlay(nodes: List[Dict[str, Any]], overlay: Iterable[Dict[str, Any]],
                  ) -> List[Dict[str, Any]]:
    """Apply the owner's corrections over derived nodes. Pure — no DB, easy to test.

    Order matters: renames and bands are per-node, merges fold nodes together, and dismissal
    is a FLAG rather than a removal so the caller can still count and search what was
    dismissed. Hiding is a display decision; deleting would be a claim.
    """
    by_id = {str(n["node_id"]): n for n in nodes}
    merges: Dict[str, str] = {}
    per_node: Dict[str, Dict[str, Any]] = {}

    for row in overlay:
        subject, action, value = str(row["subject_id"]), row["action"], row.get("value")
        if action == ACTION_MERGE and value:
            merges[subject] = str(value)
            continue
        slot = per_node.setdefault(subject, {"notes": [], "tags": []})
        if action == ACTION_NOTE and value:
            slot["notes"].append({"overlay_id": row["overlay_id"], "text": str(value),
                                  "created_at": row["created_at"]})
        elif action == ACTION_TAG and value:
            slot["tags"].append(str(value))
        elif action == ACTION_RENAME and value:
            slot["label"] = str(value)
        elif action == ACTION_BAND and value:
            slot["band"] = str(value)
        elif action == ACTION_DISMISS:
            slot["dismissed"] = True
            slot["dismissed_by"] = row["overlay_id"]

    for node_id, slot in per_node.items():
        n = by_id.get(node_id)
        if not n:
            continue
        if slot.get("label"):
            n["label"] = slot["label"]
            n["needs_name"] = False
            n["renamed_by_owner"] = True
        if slot.get("band"):
            n["band"] = slot["band"]
            n["band_reason"] = "you put them here"
        if slot.get("dismissed"):
            n["dismissed"] = True
            n["dismissed_by"] = slot.get("dismissed_by")
        if slot["notes"]:
            n["notes"] = slot["notes"]
        if slot["tags"]:
            n["tags"] = sorted(set(slot["tags"]))

    if merges:
        resolved = resolve_merges(merges)
        for source_id, target_id in resolved.items():
            src, dst = by_id.get(source_id), by_id.get(target_id)
            if not src or not dst or src is dst:
                continue
            # The survivor absorbs identities and evidence. Traffic sums, because two handles
            # for one person are one relationship, and reporting them apart understates it.
            dst["messenger_keys"] = sorted(set(dst.get("messenger_keys", []))
                                           | set(src.get("messenger_keys", [])))
            dst["message_count"] = int(dst.get("message_count", 0)) + int(src.get("message_count", 0))
            dst["mention_count"] = int(dst.get("mention_count", 0)) + int(src.get("mention_count", 0))
            for key in ("messaged", "mentioned"):
                dst.setdefault("evidence", {})[key] = bool(
                    dst.get("evidence", {}).get(key) or src.get("evidence", {}).get(key))
            dst["notes"] = list(dst.get("notes", [])) + list(src.get("notes", []))
            dst["tags"] = sorted(set(dst.get("tags", [])) | set(src.get("tags", [])))
            if not dst.get("entity_id") and src.get("entity_id"):
                dst["entity_id"] = src["entity_id"]
            if dst.get("needs_name") and not src.get("needs_name"):
                dst["label"], dst["needs_name"] = src["label"], False
            dst.setdefault("merged_from", []).append(
                {"node_id": source_id, "label": src.get("label")})
            src["merged_into"] = target_id

    return [n for n in nodes if not n.get("merged_into")]


def record_many(conn: Any, dataset_id: str, subject_ids: Iterable[str], action: str,
                value: Optional[str] = None) -> List[Dict[str, Any]]:
    """One decision over many people — 152 ambient one-offs is not a per-item job.

    Returns every row written so the caller can offer ONE undo for the whole sweep. A bulk
    action the owner cannot take back in one move is a trap, not a feature.
    """
    return [record(conn, dataset_id, sid, action, value) for sid in subject_ids if sid]


def revoke_many(conn: Any, overlay_ids: Iterable[str]) -> int:
    return sum(1 for oid in overlay_ids if revoke(conn, oid))


def history(conn: Any, dataset_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    """Recent decisions, live and revoked, newest first — so undo is visible rather than
    remembered."""
    rows = load(conn, dataset_id, include_revoked=True)
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows[:limit]

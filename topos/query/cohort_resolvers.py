"""A2.1 / C1 — resolve ``accessible_entity_cohorts`` → person entity ids.

D-002: resolved allow-list = union(explicit ids, resolve(cohorts)) minus blackholes.
Fail closed without a DB / on errors / for unknown tokens. ``stats_aggregate`` and
``none`` unlock A8 aggregate permit only — they do not widen named-person access.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace
from typing import Any, Iterable, List, Optional, Sequence, Set

logger = logging.getLogger("topos.query.cohort_resolvers")

# Tokens that expand to named-person membership (C1).
MEMBERSHIP_COHORT_TOKENS = frozenset(
    {
        "contacts",
        "message_peers",
        "calendar_attendees",
    }
)

# Recognized but membership-empty: A8 aggregate permit only.
AGGREGATE_ONLY_COHORT_TOKENS = frozenset(
    {
        "stats_aggregate",
        "none",
        "empty",
        "nil",
    }
)

RECOGNIZED_COHORT_TOKENS = MEMBERSHIP_COHORT_TOKENS | AGGREGATE_ONLY_COHORT_TOKENS


def _normalize_cohort_key(raw: Any) -> str:
    return str(raw or "").strip().lower()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _blackholed_ids(conn: sqlite3.Connection) -> Set[str]:
    try:
        from ..features.lifecycle.blackhole import blackholed_entity_ids

        return {str(x) for x in (blackholed_entity_ids(conn) or set()) if str(x).strip()}
    except Exception:
        return set()


def _person_ids_with_contact(conn: sqlite3.Connection) -> List[str]:
    if not _table_exists(conn, "entities"):
        return []
    rows = conn.execute(
        """
        SELECT entity_id FROM entities
        WHERE lower(entity_type)='person'
          AND COALESCE(is_self, 0)=0
          AND contact_id IS NOT NULL
          AND TRIM(contact_id) != ''
        """
    ).fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


def _message_peer_ids(conn: sqlite3.Connection) -> List[str]:
    """Person entities on active communicates_with edges (talked-to peers)."""
    if not _table_exists(conn, "entity_edges") or not _table_exists(conn, "entities"):
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT e.entity_id
        FROM entity_edges ed
        JOIN entities e ON e.entity_id IN (ed.src_entity_id, ed.dst_entity_id)
        WHERE ed.edge_type='communicates_with'
          AND ed.valid_to IS NULL
          AND lower(e.entity_type)='person'
          AND COALESCE(e.is_self, 0)=0
        """
    ).fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


def _match_person_by_name_or_identifier(
    conn: sqlite3.Connection, needle: str
) -> Optional[str]:
    token = str(needle or "").strip().lower()
    if not token:
        return None
    row = conn.execute(
        """
        SELECT entity_id FROM entities
        WHERE lower(entity_type)='person'
          AND COALESCE(is_self, 0)=0
          AND (lower(canonical_name)=? OR normalized_name=? OR lower(canonical_name)=?)
        LIMIT 1
        """,
        (token, token, token),
    ).fetchone()
    if row and row[0]:
        return str(row[0])
    # Identifier / email match
    try:
        for entity_id, identifiers_json in conn.execute(
            """
            SELECT entity_id, identifiers_json FROM entities
            WHERE lower(entity_type)='person'
              AND COALESCE(is_self, 0)=0
              AND identifiers_json IS NOT NULL
              AND identifiers_json != '[]'
            """
        ):
            try:
                identifiers = json.loads(identifiers_json or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if token in {str(i).strip().lower() for i in identifiers if i}:
                return str(entity_id)
    except sqlite3.Error:
        pass
    try:
        row = conn.execute(
            """
            SELECT e.entity_id FROM entities e
            JOIN contact_identifiers ci ON ci.contact_id = e.contact_id
            WHERE lower(e.entity_type)='person'
              AND COALESCE(e.is_self, 0)=0
              AND lower(ci.identifier)=?
            LIMIT 1
            """,
            (token,),
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    except sqlite3.Error:
        pass
    return None


def _attendee_needles_from_meta(meta: Any) -> List[str]:
    out: List[str] = []
    if not isinstance(meta, dict):
        return out
    raw = meta.get("attendees")
    if isinstance(raw, str) and raw.strip():
        # Demo mapper: comma/space separated display names
        for part in raw.replace(";", ",").split(","):
            if part.strip():
                out.append(part.strip())
        return out
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        for key in ("displayName", "display_name", "name", "email"):
            val = item.get(key)
            if val and str(val).strip():
                out.append(str(val).strip())
                break
    return out


def _calendar_attendee_ids(conn: sqlite3.Connection) -> List[str]:
    if not _table_exists(conn, "calendar_events") or not _table_exists(conn, "entities"):
        return []
    try:
        rows = conn.execute(
            "SELECT metadata_json FROM calendar_events WHERE metadata_json IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return []
    found: List[str] = []
    seen: Set[str] = set()
    for (raw_meta,) in rows:
        try:
            meta = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for needle in _attendee_needles_from_meta(meta):
            eid = _match_person_by_name_or_identifier(conn, needle)
            if eid and eid not in seen:
                seen.add(eid)
                found.append(eid)
    return found


def _resolve_one_cohort(conn: sqlite3.Connection, key: str) -> List[str]:
    if key == "contacts":
        return _person_ids_with_contact(conn)
    if key == "message_peers":
        return _message_peer_ids(conn)
    if key == "calendar_attendees":
        return _calendar_attendee_ids(conn)
    return []


def resolve_accessible_entity_cohorts(
    cohorts: Sequence[Any],
    db_conn: Optional[Any] = None,
) -> List[str]:
    """Resolve grant cohort tokens to person entity ids.

    Without ``db_conn``, membership cohorts resolve to ``[]`` (fail closed).
    Unknown tokens and aggregate-only tokens never widen named access.
    Blackholed entities are stripped when the blackhole store is available.
    """
    keys = [_normalize_cohort_key(c) for c in (cohorts or [])]
    keys = [k for k in keys if k]
    if not keys or db_conn is None:
        return []

    resolved: List[str] = []
    seen: Set[str] = set()
    try:
        for key in keys:
            if key in AGGREGATE_ONLY_COHORT_TOKENS:
                continue
            if key not in MEMBERSHIP_COHORT_TOKENS:
                continue
            for eid in _resolve_one_cohort(db_conn, key):
                sid = str(eid or "").strip()
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                resolved.append(sid)
        if resolved:
            blocked = _blackholed_ids(db_conn)
            if blocked:
                resolved = [e for e in resolved if e not in blocked]
    except Exception as exc:
        logger.debug("cohort resolve failed closed: %s", exc)
        return []
    return resolved


def merge_entity_ids(*groups: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for group in groups:
        for item in group or []:
            sid = str(item or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(sid)
    return out


def apply_cohort_membership(manifest: Any, db_conn: Optional[Any]) -> Any:
    """Widen ``accessible_entity_ids`` from grant cohorts when a DB is available.

    No-op when policy inactive, no cohorts, no DB, or resolvers yield nothing.
    Preserves explicit enum ids; unions cohort membership (D-002).
    """
    if manifest is None or db_conn is None:
        return manifest
    if not bool(getattr(manifest, "entity_selector_policy_active", False)):
        return manifest
    cohorts = list(getattr(manifest, "accessible_entity_cohorts", None) or [])
    if not cohorts:
        return manifest
    from_cohorts = resolve_accessible_entity_cohorts(cohorts, db_conn)
    if not from_cohorts:
        return manifest
    existing = list(getattr(manifest, "accessible_entity_ids", None) or [])
    merged = merge_entity_ids(existing, from_cohorts)
    if merged == existing:
        return manifest
    try:
        return replace(manifest, accessible_entity_ids=merged)
    except TypeError:
        # SimpleNamespace / non-dataclass test doubles
        try:
            setattr(manifest, "accessible_entity_ids", merged)
        except Exception:
            pass
        return manifest

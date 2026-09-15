"""Strict snapshot-local intelligence tombstones; no legacy fail-open helpers."""
from __future__ import annotations

import sqlite3

from .canonical import PolicyError, digest

TABLE = "intelligence_exclusions"
KINDS = {"fact", "entity", "record", "stat_insight"}


def exclusions(conn) -> dict[str, set[str]]:
    """Validate the complete retained tombstone set before interpreting absence."""
    return _read(conn)[0]


def _read(conn):
    try:
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")]
        if not {"exclusion_id", "artifact_type", "artifact_key"} <= set(columns):
            raise PolicyError("exclusion_schema_unavailable")
        rows = [dict(zip(columns, row)) for row in conn.execute(f"SELECT * FROM {TABLE}")]
    except sqlite3.Error:
        raise PolicyError("exclusion_schema_unavailable") from None
    found = {kind: set() for kind in KINDS}
    ids = set()
    for row in rows:
        identity, kind, key = (row[name] for name in ("exclusion_id", "artifact_type", "artifact_key"))
        if (type(identity) is not str or not identity or identity in ids or kind not in KINDS
            or type(key) is not str or not key or key != key.strip()
            or key in found[kind] or any(value is not None and type(value) is not str for value in row.values())):
            raise PolicyError("exclusion_state_unknown")
        if kind == "fact" and (key != key.lower() or ":" not in key or key.startswith(":") or key.endswith(":")):
            raise PolicyError("exclusion_state_unknown")
        ids.add(identity)
        found[kind].add(key)
    return found, sorted(rows, key=lambda row: row["exclusion_id"])


def exclusion_fingerprint(conn) -> str:
    # Includes notes/metadata conservatively, but never returns them to callers.
    return digest({"version": "intelligence-exclusion-floor/v1", "rows": _read(conn)[1]})


def fact_excluded(payload: dict, tombstones: set[str], owner_subjects: set[str]) -> bool:
    from topos.features.facts.store import normalize_predicate, _normalize_value
    subject, predicate, value = (payload.get(name) for name in ("subject_entity_id", "predicate", "object_value"))
    if any(type(item) is not str for item in (subject, predicate, value)):
        raise PolicyError("evidence_malformed")
    subjects = owner_subjects if subject in owner_subjects else {subject}
    normalized_tombstones = {" ".join(key.split()) for key in tombstones}
    for candidate in subjects:
        prefix = (candidate + ":" + normalize_predicate(predicate)).lower()
        # Both the exclusion writer and FactStore normalized-value spellings
        # remain vetoes. Matching an owner alias is conservative, never a permit.
        keys = {prefix, prefix + ":" + value.strip().lower(), prefix + ":" + _normalize_value(value)}
        if keys.intersection(tombstones) or keys.intersection(normalized_tombstones):
            return True
    return False

"""Strict snapshot-local intelligence tombstones; no legacy fail-open helpers."""
from __future__ import annotations

import sqlite3

from .canonical import MappingRows, PolicyError, digest_stream

TABLE = "intelligence_exclusions"
KINDS = {"fact", "entity", "record", "stat_insight"}
FINGERPRINT_VERSION = "intelligence-exclusion-floor/v1"
# Every column of every row, read out of the table b-tree. With no WHERE and no ORDER BY this
# plans as `SCAN intelligence_exclusions`, and no index on the table covers a whole row, so the
# fingerprint rests on nothing `idx_intelligence_exclusions_key` decides. The rows are ordered
# in Python by `exclusion_id`, exactly as the built digest ordered them.
_READ = f"SELECT * FROM {TABLE}"


def exclusions(conn) -> dict[str, set[str]]:
    """Validate the complete retained tombstone set before interpreting absence."""
    return _read(conn)[0]


def _read(conn):
    try:
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")]
        if not {"exclusion_id", "artifact_type", "artifact_key"} <= set(columns):
            raise PolicyError("exclusion_schema_unavailable")
        rows = [dict(zip(columns, row)) for row in conn.execute(_READ)]
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
    """Digest of every tombstone row, notes and metadata included; never returned to callers.

    The value is exactly `digest({"version": FINGERPRINT_VERSION, "rows": [row, ...]})`
    over the rows ordered by `exclusion_id`, each row a mapping of every column. It
    is streamed into SHA-256 through `MappingRows` rather than built as one canonical
    value, so the floor is no longer capped by the 1 MiB canonical encoding limit.
    Built, `canonical_bytes` refused this table as `json_size` at roughly 4,500-5,200
    tombstones of the campaign's shape, or at a single tombstone whose note passed the
    cap on its own -- and because this fingerprint is folded into the node-wide
    protection revision, from that row on every signed v2 route refused, status and
    revoke included, with nothing to fall back to. The bytes, and so the value, are
    unchanged: for every table the built digest could encode, this returns the same
    hex, and for every table it refused this raises the same code, except `json_size`,
    which it never raises.

    Cost is linear in the tombstones and paid on every uncached protection revision:
    `protection_clock.current_protection_revision` caches the revision by clock
    generation, so a read pays this once per owner mutation rather than once per read.
    """
    return digest_stream({"version": FINGERPRINT_VERSION, "rows": MappingRows(_read(conn)[1])})


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

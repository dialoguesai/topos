"""A beta-local monotonic clock for every owner-only protection mutation.

SQLite triggers advance the clock in the canonical mutation's own transaction,
including changes made between protocol requests. The clock never resets on a
protect/lift round trip. No candidate content is stored in this table.
"""
from __future__ import annotations

import sqlite3
import re
import secrets
from pathlib import Path

from topos.features.lifecycle.record_protection import protection_fingerprint
from topos.storage.db.write_gate import with_db_write

from .canonical import MAX_INTEGER, PolicyError, digest
from .exclusion_floor import exclusion_fingerprint

TABLE = "permissions_v2_protection_state"
CONTRACT_VERSION = 2
TRIGGERS = {
    f"permissions_v2_{table}_{operation.lower()}": (
        f"CREATE TRIGGER permissions_v2_{table}_{operation.lower()} AFTER {operation} ON {table} "
        f"BEGIN UPDATE {TABLE} SET generation=generation+1 WHERE singleton=1; "
        "SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'protection clock unavailable') END; END"
    )
    for table in ("owner_only_records", "entity_blackholes", "intelligence_exclusions")
    for operation in ("INSERT", "UPDATE", "DELETE")
}

LEGACY_TRIGGERS = {name: sql for name, sql in TRIGGERS.items() if "intelligence_exclusions" not in name}


def _floor_schema(conn, owner_id: str) -> None:
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('owner_only_records','entity_blackholes','intelligence_exclusions','engine_config')")}
    if names != {"owner_only_records", "entity_blackholes", "intelligence_exclusions", "engine_config"}:
        raise PolicyError("protection_schema_unavailable")
    owner = conn.execute("SELECT value FROM engine_config WHERE key='user_id'").fetchone()
    if owner is None or owner[0] != owner_id:
        raise PolicyError("node_owner_binding")


def clock_state(conn) -> tuple[str, int]:
    try:
        row = conn.execute(f"SELECT clock_id, generation, contract_version FROM {TABLE} WHERE singleton=1").fetchone()
        found = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'permissions_v2_%'").fetchall())
    except sqlite3.Error:
        raise PolicyError("protection_clock_unavailable") from None
    if row is None or type(row[0]) is not str or re.fullmatch(r"[0-9a-f]{64}", row[0]) is None or type(row[1]) is not int or not 0 <= row[1] <= MAX_INTEGER or row[2] != CONTRACT_VERSION or found != TRIGGERS:
        raise PolicyError("protection_clock_unavailable")
    return row[0], row[1]


def ensure_protection_clock(path: Path, *, owner_id: str, allow_install: bool = True) -> None:
    """Install once; never silently repair an incomplete clock or lost triggers."""
    with with_db_write():
        with sqlite3.connect(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _floor_schema(conn, owner_id)
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
            if not exists:
                if not allow_install or conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name LIKE 'permissions_v2_%'").fetchone():
                    raise PolicyError("protection_clock_unavailable")
                conn.execute(f"CREATE TABLE {TABLE} (singleton INTEGER PRIMARY KEY CHECK(singleton=1), clock_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation BETWEEN 0 AND {MAX_INTEGER}), contract_version INTEGER NOT NULL CHECK(contract_version=2))")
                conn.execute(f"INSERT INTO {TABLE} VALUES (1,?,0,2)", (secrets.token_hex(32),))
                for sql in TRIGGERS.values():
                    conn.execute(sql)
            clock_state(conn)


def current_protection_revision(conn, *, owner_id: str) -> str:
    """Caller holds a read transaction (and the node write gate when in process)."""
    _floor_schema(conn, owner_id)
    clock_id, generation = clock_state(conn)
    return digest({"clock_id": clock_id, "generation": generation, "protection": protection_fingerprint(conn), "exclusions": exclusion_fingerprint(conn), "contract_version": CONTRACT_VERSION})


def upgrade_protection_clock_v2(path: Path, *, owner_id: str, expected_clock_id: str, expected_generation: int) -> dict:
    """Explicit stopped-node upgrade of a complete v1 clock; never a read fallback.

    Preserve the clock identity and advance generation once. Calling this on an
    already-v2 clock is an idempotent read only when its identity/generation show
    that the expected earlier generation cannot be restored. Missing v2 metadata
    or an incomplete legacy trigger set is never repaired.
    """
    if (type(expected_clock_id) is not str or re.fullmatch(r"[0-9a-f]{64}", expected_clock_id) is None
        or type(expected_generation) is not int or not 0 <= expected_generation < MAX_INTEGER):
        raise PolicyError("protection_upgrade_binding")
    with with_db_write(), sqlite3.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _floor_schema(conn, owner_id)
        exclusion_fingerprint(conn)
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")]
        if "contract_version" in columns:
            clock_id, generation = clock_state(conn)
            if clock_id != expected_clock_id or generation < expected_generation + 1:
                raise PolicyError("protection_upgrade_binding")
            return {"contract_version": 2, "clock_id": clock_id, "generation": generation, "already_current": True}
        if columns != ["singleton", "clock_id", "generation"]:
            raise PolicyError("protection_clock_unavailable")
        found = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'permissions_v2_%'").fetchall())
        row = conn.execute(f"SELECT clock_id,generation FROM {TABLE} WHERE singleton=1").fetchone()
        if found != LEGACY_TRIGGERS or row != (expected_clock_id, expected_generation):
            raise PolicyError("protection_upgrade_binding")
        conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN contract_version INTEGER NOT NULL DEFAULT 2 CHECK(contract_version=2)")
        conn.execute(f"UPDATE {TABLE} SET generation=generation+1 WHERE singleton=1")
        for name, sql in TRIGGERS.items():
            if name not in LEGACY_TRIGGERS:
                conn.execute(sql)
        clock_id, generation = clock_state(conn)
        return {"contract_version": 2, "clock_id": clock_id, "generation": generation, "already_current": False}

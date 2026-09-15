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

TABLE = "permissions_v2_protection_state"
TRIGGERS = {
    f"permissions_v2_{table}_{operation.lower()}": (
        f"CREATE TRIGGER permissions_v2_{table}_{operation.lower()} AFTER {operation} ON {table} "
        f"BEGIN UPDATE {TABLE} SET generation=generation+1 WHERE singleton=1; "
        "SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'protection clock unavailable') END; END"
    )
    for table in ("owner_only_records", "entity_blackholes")
    for operation in ("INSERT", "UPDATE", "DELETE")
}


def _floor_schema(conn, owner_id: str) -> None:
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('owner_only_records','entity_blackholes','engine_config')")}
    if names != {"owner_only_records", "entity_blackholes", "engine_config"}:
        raise PolicyError("protection_schema_unavailable")
    owner = conn.execute("SELECT value FROM engine_config WHERE key='user_id'").fetchone()
    if owner is None or owner[0] != owner_id:
        raise PolicyError("node_owner_binding")


def clock_state(conn) -> tuple[str, int]:
    try:
        row = conn.execute(f"SELECT clock_id, generation FROM {TABLE} WHERE singleton=1").fetchone()
        found = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'permissions_v2_%'").fetchall())
    except sqlite3.Error:
        raise PolicyError("protection_clock_unavailable") from None
    if row is None or type(row[0]) is not str or re.fullmatch(r"[0-9a-f]{64}", row[0]) is None or type(row[1]) is not int or not 0 <= row[1] <= MAX_INTEGER or found != TRIGGERS:
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
                conn.execute(f"CREATE TABLE {TABLE} (singleton INTEGER PRIMARY KEY CHECK(singleton=1), clock_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation BETWEEN 0 AND {MAX_INTEGER}))")
                conn.execute(f"INSERT INTO {TABLE} VALUES (1,?,0)", (secrets.token_hex(32),))
                for sql in TRIGGERS.values():
                    conn.execute(sql)
            clock_state(conn)


def current_protection_revision(conn, *, owner_id: str) -> str:
    """Caller holds a read transaction (and the node write gate when in process)."""
    _floor_schema(conn, owner_id)
    clock_id, generation = clock_state(conn)
    return digest({"clock_id": clock_id, "generation": generation, "protection": protection_fingerprint(conn)})

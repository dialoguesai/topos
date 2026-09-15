"""A beta-local monotonic clock for every owner-only protection mutation.

SQLite triggers advance the clock in the canonical mutation's own transaction,
including changes made between protocol requests. The clock never resets on a
protect/lift round trip. Contract v3 additionally appends every mutation to an
event log keyed by the touched artifact, so an owner review can bind to the
protection history of its own closure instead of the whole node. Signed
authority still binds the global revision. No candidate content is stored in
either table.
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
EVENTS = "permissions_v2_protection_events"
CONTRACT_VERSION = 3
EVENTS_SQL = (f"CREATE TABLE {EVENTS} (sequence INTEGER PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation>0), "
              "source TEXT NOT NULL, artifact_key TEXT NOT NULL)")
_ADVANCE = (f"UPDATE {TABLE} SET generation=generation+1 WHERE singleton=1; "
            "SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'protection clock unavailable') END;")
_KEYS = {"owner_only_records": "{row}.canonical_table||'|'||{row}.record_id",
         "entity_blackholes": "{row}.blackhole_id",
         "intelligence_exclusions": "{row}.artifact_type||'|'||{row}.artifact_key"}


def _log(table, row):
    return (f" INSERT INTO {EVENTS}(generation,source,artifact_key) SELECT generation,'{table}',"
            f"{_KEYS[table].format(row=row)} FROM {TABLE} WHERE singleton=1;")


def _triggers(version: int) -> dict[str, str]:
    tables = ("owner_only_records", "entity_blackholes") if version == 1 else ("owner_only_records", "entity_blackholes", "intelligence_exclusions")
    result = {}
    for table in tables:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            body = _ADVANCE
            if version >= 3:
                rows = ("OLD",) if operation == "DELETE" else ("NEW",) if operation == "INSERT" else ("OLD", "NEW")
                body += "".join(_log(table, row) for row in rows)
            result[f"permissions_v2_{table}_{operation.lower()}"] = (
                f"CREATE TRIGGER permissions_v2_{table}_{operation.lower()} AFTER {operation} ON {table} BEGIN {body} END")
    return result


TRIGGERS = _triggers(CONTRACT_VERSION)
V2_TRIGGERS = _triggers(2)
LEGACY_TRIGGERS = _triggers(1)


def _floor_schema(conn, owner_id: str) -> None:
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('owner_only_records','entity_blackholes','intelligence_exclusions','engine_config')")}
    if names != {"owner_only_records", "entity_blackholes", "intelligence_exclusions", "engine_config"}:
        raise PolicyError("protection_schema_unavailable")
    owner = conn.execute("SELECT value FROM engine_config WHERE key='user_id'").fetchone()
    if owner is None or owner[0] != owner_id:
        raise PolicyError("node_owner_binding")


def clock_state(conn, *, version: int = CONTRACT_VERSION) -> tuple[str, int]:
    try:
        row = conn.execute(f"SELECT clock_id, generation, contract_version FROM {TABLE} WHERE singleton=1").fetchone()
        found = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'permissions_v2_%'").fetchall())
        events = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (EVENTS,)).fetchone()
    except sqlite3.Error:
        raise PolicyError("protection_clock_unavailable") from None
    if (row is None or type(row[0]) is not str or re.fullmatch(r"[0-9a-f]{64}", row[0]) is None or type(row[1]) is not int
        or not 0 <= row[1] <= MAX_INTEGER or row[2] != version or found != _triggers(version)
        or (events is not None and events[0] == EVENTS_SQL) != (version >= 3)):
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
                conn.execute(f"CREATE TABLE {TABLE} (singleton INTEGER PRIMARY KEY CHECK(singleton=1), clock_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation BETWEEN 0 AND {MAX_INTEGER}), contract_version INTEGER NOT NULL CHECK(contract_version={CONTRACT_VERSION}))")
                conn.execute(f"INSERT INTO {TABLE} VALUES (1,?,0,{CONTRACT_VERSION})", (secrets.token_hex(32),))
                conn.execute(EVENTS_SQL)
                for sql in TRIGGERS.values():
                    conn.execute(sql)
            clock_state(conn)


def current_protection_revision(conn, *, owner_id: str) -> str:
    """Node-wide revision for signed authority. Caller holds a read transaction."""
    _floor_schema(conn, owner_id)
    clock_id, generation = clock_state(conn)
    return digest({"clock_id": clock_id, "generation": generation, "protection": protection_fingerprint(conn), "exclusions": exclusion_fingerprint(conn), "contract_version": CONTRACT_VERSION})


def _like(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def closure_protection_revision(conn, *, owner_id: str, records, fact_prefixes) -> str:
    """Protection history and state of one closure only; entity floors stay global.

    `records` are (canonical_table, record_id) pairs of every artifact and
    leaf; `fact_prefixes` are the lowercased `subject:predicate` keys a fact
    tombstone could match. Protecting, excluding or lifting anything else on
    the node leaves this value unchanged, while any event that touched the
    closure, including a protect-then-lift, changes it permanently.
    """
    _floor_schema(conn, owner_id)
    clock_id, _generation = clock_state(conn)
    records = sorted({(str(table), str(record_id)) for table, record_id in records})
    prefixes = sorted({str(prefix) for prefix in fact_prefixes})
    protected, excluded_records, event_filters, event_args = [], [], [], []
    try:
        for table, record_id in records:
            if conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=? LIMIT 1", (table, record_id)).fetchone():
                protected.append(table + "|" + record_id)
            if conn.execute("SELECT 1 FROM intelligence_exclusions WHERE artifact_type='record' AND artifact_key=? LIMIT 1", (record_id,)).fetchone():
                excluded_records.append(record_id)
            event_filters.append("(source='owner_only_records' AND artifact_key=?)")
            event_args.append(table + "|" + record_id)
            event_filters.append("(source='intelligence_exclusions' AND artifact_key=?)")
            event_args.append("record|" + record_id)
        excluded_facts = set()
        for prefix in prefixes:
            for row in conn.execute("SELECT artifact_key FROM intelligence_exclusions WHERE artifact_type='fact' AND lower(artifact_key) LIKE ? ESCAPE '\\'", (_like(prefix),)):
                excluded_facts.add(row[0])
            event_filters.append("(source='intelligence_exclusions' AND lower(artifact_key) LIKE ? ESCAPE '\\')")
            event_args.append(_like("fact|" + prefix))
        last_event = 0
        if event_filters:
            found = conn.execute(f"SELECT max(generation) FROM {EVENTS} WHERE " + " OR ".join(event_filters), event_args).fetchone()[0]
            last_event = found if type(found) is int else 0
        entity_events = conn.execute(f"SELECT max(generation) FROM {EVENTS} WHERE source='entity_blackholes' OR (source='intelligence_exclusions' AND artifact_key LIKE 'entity|%')").fetchone()[0]
        entity_floor = digest({"blackholes": [list(row) for row in conn.execute("SELECT blackhole_id, entity_id, normalized_name FROM entity_blackholes ORDER BY blackhole_id")],
                               "entity_exclusions": [row[0] for row in conn.execute("SELECT artifact_key FROM intelligence_exclusions WHERE artifact_type='entity' ORDER BY artifact_key")],
                               "last_entity_event": entity_events if type(entity_events) is int else 0})
    except sqlite3.Error:
        raise PolicyError("protection_clock_unavailable") from None
    return digest({"version": "closure-protection/v1", "clock_id": clock_id, "contract_version": CONTRACT_VERSION,
                   "records": [table + "|" + record_id for table, record_id in records], "fact_prefixes": prefixes,
                   "protected_records": protected, "excluded_records": excluded_records, "excluded_facts": sorted(excluded_facts),
                   "last_closure_event": last_event, "entity_floor": entity_floor})


def _expected(expected_clock_id, expected_generation):
    if (type(expected_clock_id) is not str or re.fullmatch(r"[0-9a-f]{64}", expected_clock_id) is None
        or type(expected_generation) is not int or not 0 <= expected_generation < MAX_INTEGER):
        raise PolicyError("protection_upgrade_binding")


def upgrade_protection_clock_v2(path: Path, *, owner_id: str, expected_clock_id: str, expected_generation: int) -> dict:
    """Explicit stopped-node upgrade of a complete v1 clock to v2; never a read fallback.

    Preserve the clock identity and advance generation once. Calling this on an
    already-v2 clock is an idempotent read only when its identity/generation show
    that the expected earlier generation cannot be restored. Missing v2 metadata
    or an incomplete legacy trigger set is never repaired. A v2 clock still needs
    `upgrade_protection_clock_v3` before the current engine serves it.
    """
    _expected(expected_clock_id, expected_generation)
    with with_db_write(), sqlite3.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _floor_schema(conn, owner_id)
        exclusion_fingerprint(conn)
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")]
        if "contract_version" in columns:
            clock_id, generation = clock_state(conn, version=2)
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
        for name, sql in V2_TRIGGERS.items():
            if name not in LEGACY_TRIGGERS:
                conn.execute(sql)
        clock_id, generation = clock_state(conn, version=2)
        return {"contract_version": 2, "clock_id": clock_id, "generation": generation, "already_current": False}


def upgrade_protection_clock_v3(path: Path, *, owner_id: str, expected_clock_id: str, expected_generation: int) -> dict:
    """Explicit stopped-node upgrade of a complete v2 clock to v3 (event log).

    The state table is rebuilt because its v2 CHECK constraint pins the version;
    the clock identity is preserved and generation advances exactly once. The
    nine v2 triggers are replaced by the nine v3 triggers that also append to the
    new event log; no protection, exclusion or content row is read or changed.
    Repeating the upgrade against intact v3 is an idempotent read. Partial
    triggers, a changed clock id, a stale generation or a pre-existing event
    table are rejected. Existing owner reviews are stale afterwards by design.
    """
    _expected(expected_clock_id, expected_generation)
    with with_db_write(), sqlite3.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _floor_schema(conn, owner_id)
        exclusion_fingerprint(conn)
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")]
        if columns != ["singleton", "clock_id", "generation", "contract_version"]:
            raise PolicyError("protection_clock_unavailable")
        row = conn.execute(f"SELECT clock_id,generation,contract_version FROM {TABLE} WHERE singleton=1").fetchone()
        if row is not None and row[2] == CONTRACT_VERSION:
            clock_id, generation = clock_state(conn)
            if clock_id != expected_clock_id or generation < expected_generation + 1:
                raise PolicyError("protection_upgrade_binding")
            return {"contract_version": CONTRACT_VERSION, "clock_id": clock_id, "generation": generation, "already_current": True}
        found = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'permissions_v2_%'").fetchall())
        if found != V2_TRIGGERS or row != (expected_clock_id, expected_generation, 2):
            raise PolicyError("protection_upgrade_binding")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (EVENTS,)).fetchone():
            raise PolicyError("protection_clock_unavailable")
        for name in V2_TRIGGERS:
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute(f"CREATE TABLE {TABLE}_v3 (singleton INTEGER PRIMARY KEY CHECK(singleton=1), clock_id TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation BETWEEN 0 AND {MAX_INTEGER}), contract_version INTEGER NOT NULL CHECK(contract_version={CONTRACT_VERSION}))")
        conn.execute(f"INSERT INTO {TABLE}_v3 SELECT singleton, clock_id, generation+1, {CONTRACT_VERSION} FROM {TABLE}")
        conn.execute(f"DROP TABLE {TABLE}")
        conn.execute(f"ALTER TABLE {TABLE}_v3 RENAME TO {TABLE}")
        conn.execute(EVENTS_SQL)
        for sql in TRIGGERS.values():
            conn.execute(sql)
        clock_id, generation = clock_state(conn)
        if clock_id != expected_clock_id or generation != expected_generation + 1:
            raise PolicyError("protection_upgrade_binding")
        return {"contract_version": CONTRACT_VERSION, "clock_id": clock_id, "generation": generation, "already_current": False}

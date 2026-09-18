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

from collections import OrderedDict
import sqlite3
import re
import secrets
import threading
from pathlib import Path

from topos.features.lifecycle.record_protection import protection_fingerprint
from topos.storage.db.write_gate import with_db_write

from .canonical import MAX_INTEGER, PolicyError, digest
from .exclusion_floor import exclusion_fingerprint

TABLE = "permissions_v2_protection_state"
EVENTS = "permissions_v2_protection_events"
LEDGER = "permissions_v2_identity_attestations"
REGISTRY = "permissions_v2_identity_subjects"
CONTRACT_VERSION = 4
IDENTITY_EVENT_PREFIX = "identity|"
REKEY_EVENT_PREFIX = "fact_rekeyed|"
ATTESTATION_STATEMENT = "owner-identity-attestation/v1"
# v3 stamped every event from a trigger that had just advanced the clock, so a
# generation of zero was impossible. v4 also logs identity churn that must not
# advance the clock (a merge moves many mentions and re-keys many facts, and the
# merge's own tombstone advances it once). Such an event can land while the
# clock is still at its installed zero, and refusing it would abort the node's
# own merge, so v4 admits generation zero. The v3 text is kept verbatim because
# a v3 clock is validated against it byte for byte.
EVENTS_SQL_V3 = (f"CREATE TABLE {EVENTS} (sequence INTEGER PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation>0), "
                 "source TEXT NOT NULL, artifact_key TEXT NOT NULL)")
EVENTS_SQL = (f"CREATE TABLE {EVENTS} (sequence INTEGER PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation>=0), "
              "source TEXT NOT NULL, artifact_key TEXT NOT NULL)")
EVENTS_SQL_BY_VERSION = {3: EVENTS_SQL_V3, 4: EVENTS_SQL}
# Read-path indexes on the event log. The log is keyed by the artifact a mutation touched and
# carried no index. `permissions_v2_identity_fact_rekey` asks, for every fact a merge re-keys,
# whether that fact's rekey event is already logged, and `_IDENTITY_LOG_ONCE` asks the same
# per moved mention and generation, so one merge scanned the whole log once per row it moved:
# measured at 36.8 s for 20,000 facts against an EMPTY log, and quadratic from there. The
# owner's identity lookups (`last_identity_event`, `identity_event_count`, `rekeyed_facts`)
# and the closure revision's event filters scanned it on every read. `(artifact_key,
# generation)` answers every one of those as a seek, `max(generation)` and `count(*)`
# included. The second index leads with `source` and carries `artifact_key` and
# `generation` behind it: the closure revision's record terms constrain `source` AND
# `artifact_key`, and against two single-column indexes the planner, with no statistics,
# ties them and picks the source index alone -- a range over every Off-limits or exclusion
# event the node ever logged, filtered by key. With the key in the source index those terms
# are exact covering seeks, and the terms that select by source alone -- the entity floor
# and the fact-prefix LIKE -- range over that source's events, as a plain `(source)` index
# would, reading the generation they take the maximum of from the index itself.
#
# They are not part of the contract `clock_state` verifies, which compares the state row,
# every trigger's text and the table declarations, never an index: an index changes no
# stored value, and this table's readers trust it the way the canonical database's other
# lookups already trust `owner_only_records`' primary key and `intelligence_exclusions`'
# unique key. What guards the log's CONTENT is the canonical floor's event chain, folded
# over the table by `sequence` -- the rowid, not these -- and the append-only triggers.
# `PRAGMA integrity_check` is the operator-side check for an index that disagrees with its
# table, as it is for the review stores. They are created with the table, rebuilt with it
# by the v4 upgrade, and added to an existing clock by `ensure_protection_clock` at the next
# node start, after the clock itself has been verified; `IF NOT EXISTS` makes that a no-op
# once they exist, and building them over a grown log is a one-time cost of that start.
EVENT_INDEXES = {
    "permissions_v2_protection_events_artifact":
        f"CREATE INDEX IF NOT EXISTS permissions_v2_protection_events_artifact ON {EVENTS}(artifact_key, generation)",
    "permissions_v2_protection_events_source":
        f"CREATE INDEX IF NOT EXISTS permissions_v2_protection_events_source ON {EVENTS}(source, artifact_key, generation)",
}


def _ensure_event_indexes(conn) -> None:
    """Create the event-log indexes where the event log is; a clock without one gets nothing."""
    # A v4 clock always has the table by the time `clock_state` has passed, but the upgrade
    # lanes and the tests reach this with a v1 or v2 clock in hand, which has no log to index.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (EVENTS,)).fetchone() is None:
        return
    for sql in EVENT_INDEXES.values():
        conn.execute(sql)


# The owner's consent rows. Only the attestation service appends here, and the
# canonical floor pins their exact digest, so a row written any other way is a
# tamper, not an attestation. Ids, digests and a statement version only: no
# names, and nothing an explorer surface may serve (see data_explorer_tables).
LEDGER_SQL = (f"CREATE TABLE {LEDGER} (sequence INTEGER PRIMARY KEY, "
              "entry_id TEXT NOT NULL UNIQUE CHECK(length(entry_id) BETWEEN 1 AND 200), "
              "action TEXT NOT NULL CHECK(action IN ('attest','revoke')), "
              "entity_id TEXT NOT NULL CHECK(length(entity_id) BETWEEN 1 AND 200), "
              "target_entry_id TEXT, entity_type TEXT, is_self INTEGER, contact_id TEXT, "
              "composition_revision TEXT, "
              f"statement_version TEXT NOT NULL CHECK(statement_version='{ATTESTATION_STATEMENT}'), "
              "command_id TEXT NOT NULL UNIQUE, command_hash TEXT NOT NULL, "
              "generation INTEGER NOT NULL CHECK(generation>0), "
              "CHECK((action='attest' AND target_entry_id IS NULL AND entity_type IS NOT NULL AND is_self=1 "
              "AND composition_revision IS NOT NULL) OR (action='revoke' AND target_entry_id IS NOT NULL "
              "AND entity_type IS NULL AND is_self IS NULL AND contact_id IS NULL AND composition_revision IS NULL)))")
# Every spelling of the owner this node has seen. Restriction input only: it
# grows so that an owner tombstone keyed by a merged-away or never-attested
# alias keeps vetoing, and nothing here grants anything.
REGISTRY_SQL = (f"CREATE TABLE {REGISTRY} (entity_id TEXT PRIMARY KEY CHECK(length(entity_id) BETWEEN 1 AND 200), "
                "basis TEXT NOT NULL CHECK(basis IN ('installed','self_row','attested','merge_neighbor')), "
                "first_generation INTEGER NOT NULL CHECK(first_generation>=0))")


TOMBSTONES_SQL = ("""CREATE TABLE IF NOT EXISTS entity_merge_tombstones (
                   absorbed_entity_id TEXT PRIMARY KEY,
                   merged_into TEXT NOT NULL,
                   canonical_name TEXT,
                   aliases_json TEXT,
                   identifiers_json TEXT,
                   merged_at TEXT NOT NULL DEFAULT (datetime('now'))
               )""")
# Native tables the identity triggers attach to. The clock install creates
# `entity_merge_tombstones` itself, because the merge feature creates it on
# demand and a trigger cannot wait for that. The other three belong to the
# engine's own schema, so the clock watches whichever of them the node actually
# has and records that coverage: see `identity_coverage`.
IDENTITY_TABLES = ("entities", "entity_mentions", "signal_objects")


def identity_event_key(entity_id: str) -> str:
    return IDENTITY_EVENT_PREFIX + str(entity_id)


def rekey_event_key(fact_id: str) -> str:
    return REKEY_EVENT_PREFIX + str(fact_id)


_ADVANCE = (f"UPDATE {TABLE} SET generation=generation+1 WHERE singleton=1; "
            "SELECT CASE WHEN changes()!=1 THEN RAISE(ABORT,'protection clock unavailable') END;")
_KEYS = {"owner_only_records": "{row}.canonical_table||'|'||{row}.record_id",
         "entity_blackholes": "{row}.blackhole_id",
         "intelligence_exclusions": "{row}.artifact_type||'|'||{row}.artifact_key"}


def _log(table, row):
    return (f" INSERT INTO {EVENTS}(generation,source,artifact_key) SELECT generation,'{table}',"
            f"{_KEYS[table].format(row=row)} FROM {TABLE} WHERE singleton=1;")


_TRACKED = f"EXISTS(SELECT 1 FROM {REGISTRY} s WHERE s.entity_id=%s)"
_REGISTER = (f"INSERT INTO {REGISTRY}(entity_id,basis,first_generation) SELECT %s,'%s',generation FROM {TABLE} "
             f"WHERE singleton=1 AND NOT EXISTS(SELECT 1 FROM {REGISTRY} s WHERE s.entity_id=%s);")
_IDENTITY_LOG = (f"INSERT INTO {EVENTS}(generation,source,artifact_key) SELECT generation,'%s','{IDENTITY_EVENT_PREFIX}'||%s "
                 f"FROM {TABLE} WHERE singleton=1;")
# A mention move or a fact re-key is an identity change, but it arrives one row
# at a time inside one merge or split. Logging it without advancing keeps a
# 40-mention merge from advancing the clock 40 times; the merge's own tombstone
# and entity deletion advance it once. Reviews still stale, because a closure
# binds the last identity event of every subject it names.
_IDENTITY_LOG_ONCE = (f"INSERT INTO {EVENTS}(generation,source,artifact_key) SELECT generation,'%s','{IDENTITY_EVENT_PREFIX}'||%s "
                      f"FROM {TABLE} WHERE singleton=1 AND NOT EXISTS(SELECT 1 FROM {EVENTS} e "
                      f"WHERE e.artifact_key='{IDENTITY_EVENT_PREFIX}'||%s AND e.generation=(SELECT generation FROM {TABLE} WHERE singleton=1));")


def _append_only(table: str) -> dict[str, str]:
    """No row of the owner's own permission history may be rewritten or removed.

    Deleting the event log used to make a review that a protect-then-lift had
    staled current again, and an attestation ledger that can be edited is not a
    consent record at all.
    """
    result = {}
    for operation in ("UPDATE", "DELETE"):
        name = f"{table}_no_{operation.lower()}"  # every such table already carries the prefix
        result[name] = (f"CREATE TRIGGER {name} BEFORE {operation} ON {table} "
                        f"BEGIN SELECT RAISE(ABORT,'{table} is append-only'); END")
    return result


def identity_coverage(conn) -> tuple[str, ...]:
    """Which engine identity tables this node has, and therefore the clock watches.

    Requiring all of them at install would couple the permission floor to the
    entity spine migration. Instead the clock watches what exists and records
    the exact list. A table that appears later is a coverage change: the clock
    state no longer matches and every read fails closed until the upgrade lane
    installs its triggers. A table that is dropped is also a coverage change,
    and it moves the node-wide protection revision, so every signed authority
    issued while it was watched goes stale.
    """
    placeholders = ",".join("?" for _ in IDENTITY_TABLES)
    try:
        found = {row[0] for row in conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders})", IDENTITY_TABLES)}
    except sqlite3.Error:
        raise PolicyError("protection_clock_unavailable") from None
    return tuple(name for name in IDENTITY_TABLES if name in found)


def _identity_triggers(coverage: tuple[str, ...] = IDENTITY_TABLES) -> dict[str, str]:
    """Identity changes the owner never sees as a fact edit, caught where they happen.

    Each trigger is scoped to the columns that define an identity and to ids the
    node already tracks, so the enrichment writers that rewrite names, aliases,
    identifiers, counts and metadata on every batch fire nothing at all.
    """
    tracked_new, tracked_old = _TRACKED % "NEW.entity_id", _TRACKED % "OLD.entity_id"
    result = {}
    result["permissions_v2_identity_attestations_attest_order"] = (
        f"CREATE TRIGGER permissions_v2_identity_attestations_attest_order BEFORE INSERT ON {LEDGER} "
        "WHEN NEW.action='attest' BEGIN SELECT CASE WHEN EXISTS(SELECT 1 FROM " + LEDGER + " a WHERE a.entity_id=NEW.entity_id "
        "AND a.action='attest' AND NOT EXISTS(SELECT 1 FROM " + LEDGER + " r WHERE r.action='revoke' AND r.target_entry_id=a.entry_id)) "
        "THEN RAISE(ABORT,'identity attestation already active') END; END")
    result["permissions_v2_identity_attestations_revoke_order"] = (
        f"CREATE TRIGGER permissions_v2_identity_attestations_revoke_order BEFORE INSERT ON {LEDGER} "
        "WHEN NEW.action='revoke' BEGIN SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM " + LEDGER + " a WHERE a.entry_id=NEW.target_entry_id "
        "AND a.entity_id=NEW.entity_id AND a.action='attest' AND NOT EXISTS(SELECT 1 FROM " + LEDGER + " r WHERE r.action='revoke' "
        "AND r.target_entry_id=a.entry_id)) THEN RAISE(ABORT,'identity revocation target is not the current attestation') END; END")
    result["permissions_v2_identity_attestations_insert"] = (
        f"CREATE TRIGGER permissions_v2_identity_attestations_insert AFTER INSERT ON {LEDGER} BEGIN {_ADVANCE} "
        + (_REGISTER % ("NEW.entity_id", "attested", "NEW.entity_id")) + " "
        + (_IDENTITY_LOG % (LEDGER, "NEW.entity_id")) + " "
        f"SELECT CASE WHEN NEW.generation!=(SELECT generation FROM {TABLE} WHERE singleton=1) "
        "THEN RAISE(ABORT,'identity attestation generation mismatch') END; END")
    if "entities" in coverage:
        result.update(_entities_triggers(tracked_new, tracked_old))
    result.update(_tombstone_triggers())
    if "entity_mentions" in coverage:
        result["permissions_v2_identity_mentions_update"] = (
            "CREATE TRIGGER permissions_v2_identity_mentions_update AFTER UPDATE OF entity_id ON entity_mentions "
            f"WHEN OLD.entity_id IS NOT NEW.entity_id AND ({_TRACKED % 'OLD.entity_id'} OR {_TRACKED % 'NEW.entity_id'}) BEGIN "
            + (_IDENTITY_LOG_ONCE % ("entity_mentions", "OLD.entity_id", "OLD.entity_id")) + " "
            + (_IDENTITY_LOG_ONCE % ("entity_mentions", "NEW.entity_id", "NEW.entity_id")) + " END")
    if "signal_objects" in coverage:
        # A merge is the only writer of object_key, so this records exactly the
        # facts whose subject moved between entities: the overlay signature.
        result["permissions_v2_identity_fact_rekey"] = (
            "CREATE TRIGGER permissions_v2_identity_fact_rekey AFTER UPDATE OF object_key ON signal_objects "
            "WHEN OLD.object_type='fact' AND OLD.object_key IS NOT NEW.object_key BEGIN "
            f"INSERT INTO {EVENTS}(generation,source,artifact_key) SELECT generation,'signal_objects','{REKEY_EVENT_PREFIX}'||OLD.object_id "
            f"FROM {TABLE} WHERE singleton=1 AND NOT EXISTS(SELECT 1 FROM {EVENTS} e "
            f"WHERE e.artifact_key='{REKEY_EVENT_PREFIX}'||OLD.object_id); END")
    return result


def _entities_triggers(tracked_new, tracked_old) -> dict[str, str]:
    result = {}
    result["permissions_v2_identity_entities_insert"] = (
        "CREATE TRIGGER permissions_v2_identity_entities_insert AFTER INSERT ON entities "
        f"WHEN NEW.is_self=1 OR {tracked_new} BEGIN {_ADVANCE} "
        + (_REGISTER % ("NEW.entity_id", "self_row", "NEW.entity_id")) + " "
        + (_IDENTITY_LOG % ("entities", "NEW.entity_id")) + " END")
    result["permissions_v2_identity_entities_update"] = (
        "CREATE TRIGGER permissions_v2_identity_entities_update AFTER UPDATE OF entity_id,entity_type,is_self,contact_id ON entities "
        "WHEN (OLD.entity_id IS NOT NEW.entity_id OR OLD.entity_type IS NOT NEW.entity_type OR OLD.is_self IS NOT NEW.is_self "
        f"OR OLD.contact_id IS NOT NEW.contact_id) AND (NEW.is_self=1 OR OLD.is_self=1 OR {tracked_old} OR {tracked_new}) "
        f"BEGIN {_ADVANCE} " + (_IDENTITY_LOG % ("entities", "OLD.entity_id")) + " "
        + (_IDENTITY_LOG_ONCE % ("entities", "NEW.entity_id", "NEW.entity_id")) + " END")
    result["permissions_v2_identity_entities_delete"] = (
        "CREATE TRIGGER permissions_v2_identity_entities_delete AFTER DELETE ON entities "
        f"WHEN OLD.is_self=1 OR {tracked_old} BEGIN {_ADVANCE} "
        + (_REGISTER % ("OLD.entity_id", "self_row", "OLD.entity_id")) + " "
        + (_IDENTITY_LOG % ("entities", "OLD.entity_id")) + " END")
    return result


def _tombstone_triggers() -> dict[str, str]:
    """Always installed: the clock creates `entity_merge_tombstones` itself."""
    result = {}
    for operation in ("INSERT", "UPDATE", "DELETE"):
        row = "OLD" if operation == "DELETE" else "NEW"
        name = f"permissions_v2_identity_merge_tombstones_{operation.lower()}"
        result[name] = (
            f"CREATE TRIGGER {name} AFTER {operation} ON entity_merge_tombstones "
            f"WHEN {_TRACKED % (row + '.absorbed_entity_id')} OR {_TRACKED % (row + '.merged_into')} "
            f"OR EXISTS(SELECT 1 FROM entities e WHERE e.is_self=1 AND (e.entity_id={row}.absorbed_entity_id OR e.entity_id={row}.merged_into)) "
            f"BEGIN {_ADVANCE} "
            + (_REGISTER % (row + ".absorbed_entity_id", "merge_neighbor", row + ".absorbed_entity_id")) + " "
            + (_REGISTER % (row + ".merged_into", "merge_neighbor", row + ".merged_into")) + " "
            + (_IDENTITY_LOG % ("entity_merge_tombstones", row + ".absorbed_entity_id")) + " "
            + (_IDENTITY_LOG_ONCE % ("entity_merge_tombstones", row + ".merged_into", row + ".merged_into")) + " END")
    return result


def _triggers(version: int, coverage: tuple[str, ...] = IDENTITY_TABLES) -> dict[str, str]:
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
    if version >= 4:
        result.update(_append_only(EVENTS))
        result.update(_append_only(LEDGER))
        result.update(_append_only(REGISTRY))
        result.update(_identity_triggers(coverage))
    return result


TRIGGERS = _triggers(CONTRACT_VERSION)
V3_TRIGGERS = _triggers(3)
V2_TRIGGERS = _triggers(2)
LEGACY_TRIGGERS = _triggers(1)


def _floor_schema(conn, owner_id: str, *, version: int = CONTRACT_VERSION) -> None:
    required = {"owner_only_records", "entity_blackholes", "intelligence_exclusions", "engine_config"}
    placeholders = ",".join("?" for _ in sorted(required))
    names = {row[0] for row in conn.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders})", sorted(required))}
    if names != required:
        raise PolicyError("protection_schema_unavailable")
    owner = conn.execute("SELECT value FROM engine_config WHERE key='user_id'").fetchone()
    if owner is None or owner[0] != owner_id:
        raise PolicyError("node_owner_binding")


def clock_state(conn, *, version: int = CONTRACT_VERSION) -> tuple[str, int]:
    try:
        row = conn.execute(f"SELECT clock_id, generation, contract_version FROM {TABLE} WHERE singleton=1").fetchone()
        found = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'permissions_v2_%'").fetchall())
        events = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (EVENTS,)).fetchone()
        identity = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN (?,?)",
                                     (LEDGER, REGISTRY)).fetchall())
    except sqlite3.Error:
        raise PolicyError("protection_clock_unavailable") from None
    expected_identity = {LEDGER: LEDGER_SQL, REGISTRY: REGISTRY_SQL} if version >= 4 else {}
    # Expected triggers follow current coverage, so an engine identity table
    # that appears after install takes every read down until it is watched.
    coverage = identity_coverage(conn) if version >= 4 else ()
    if (row is None or type(row[0]) is not str or re.fullmatch(r"[0-9a-f]{64}", row[0]) is None or type(row[1]) is not int
        or not 0 <= row[1] <= MAX_INTEGER or row[2] != version or found != _triggers(version, coverage)
        or (events is not None and events[0] == EVENTS_SQL_BY_VERSION.get(version)) != (version >= 3)
        or identity != expected_identity):
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
                _ensure_event_indexes(conn)
                conn.execute(LEDGER_SQL)
                conn.execute(REGISTRY_SQL)
                conn.execute(TOMBSTONES_SQL)
                _seed_registry(conn)
                for sql in _triggers(CONTRACT_VERSION, identity_coverage(conn)).values():
                    conn.execute(sql)
            clock_state(conn)
            # Only once the clock has verified, so this never runs on a clock the node will
            # refuse to serve, and never counts as the repair the docstring rules out: an
            # index is outside the contract `clock_state` compares. An engine that predates
            # the indexes serves a clock that carries them unchanged.
            _ensure_event_indexes(conn)


def _seed_registry(conn) -> None:
    """Start the restriction registry from what the node already holds.

    Every current self row, and every id a merge tombstone links to one, is an
    owner spelling a tombstone could already be keyed by. Starting empty would
    silently drop those vetoes until the next identity event.
    """
    generation = conn.execute(f"SELECT generation FROM {TABLE} WHERE singleton=1").fetchone()[0]
    coverage = identity_coverage(conn)
    seeded = ({row[0] for row in conn.execute("SELECT entity_id FROM entities WHERE is_self=1") if row[0]}
              if "entities" in coverage else set())
    pairs = [(row[0], row[1]) for row in
             conn.execute("SELECT absorbed_entity_id, merged_into FROM entity_merge_tombstones") if row[0] and row[1]]
    changed = True
    while changed:
        changed = False
        for absorbed, kept in pairs:
            if absorbed in seeded and kept not in seeded:
                seeded.add(kept); changed = True
            elif kept in seeded and absorbed not in seeded:
                seeded.add(absorbed); changed = True
    conn.executemany(f"INSERT OR IGNORE INTO {REGISTRY}(entity_id,basis,first_generation) VALUES (?,'installed',?)",
                     [(entity_id, generation) for entity_id in sorted(seeded)])


# --- the node-wide revision, cached by clock generation -------------------------------------
#
# `current_protection_revision` folds three whole-table fingerprints: every Off-limits row and
# black hole, every exclusion tombstone, and the owner's consent ledger with the restriction
# registry. It runs on every permissions read, every status and every ingest door, and each
# of those tables grows with the owner's decisions, so the cost of one read grew with the
# node's whole history -- 10,000 tombstones is tens of milliseconds per read, paid under the
# node-wide write gate.
#
# Every one of those tables is watched by this clock: each insert, update or delete advances
# the generation in the same transaction, so while the generation stands still no SQLite
# write has touched them and the fingerprints are the same values. The revision is therefore
# remembered per canonical database file, one entry each, under a key made of everything
# that can move the value without a row write the triggers see:
#   - the clock identity: a different database, or a reinstalled clock;
#   - the generation: every trigger-watched write;
#   - SQLite's `schema_version`: any DDL from any connection. A column added to or dropped
#     from a fingerprinted table changes its rows' encoding and fires no trigger;
#   - the identity coverage and the contract version, which the digest names outright;
#   - the registry's row count. The registry has no insert trigger of its own: native
#     triggers append to it while advancing the clock, but a row written any other way must
#     still move the revision, exactly as it did uncached.
# `_floor_schema` and `clock_state` still run on every call, before the cache is read, so a
# changed owner binding, a lost or altered trigger, a missing table or a rolled-back state
# row is refused as before; the cache can only ever answer a question the clock has already
# accepted. What it cannot see is a file rewritten underneath a running process at the same
# generation with different rows, which no SQLite write can produce, because that write would
# have advanced the clock. That is the boundary the review stores draw too: the clock is
# monotone inside its own file and cannot see the file being replaced; the canonical floor's
# chains and the ledger's observed generation are what catch a replaced file. The cache is
# process-local, holds a handful of entries, and is never written to disk.
_REVISIONS: "OrderedDict[str, tuple[tuple, str]]" = OrderedDict()
_REVISIONS_LIMIT = 8
_REVISIONS_LOCK = threading.Lock()


def _database_file(conn) -> str | None:
    """The main database's file, or None when it has none (`:memory:`, a temp database)."""
    for _sequence, name, file in conn.execute("PRAGMA database_list").fetchall():
        if name == "main":
            return file or None
    return None


def current_protection_revision(conn, *, owner_id: str) -> str:
    """Node-wide revision for signed authority. Caller holds a read transaction.

    Identity is part of it, for every capability, so a recipient cannot tell an
    attestation change from an Off-limits change by which of their grants went
    stale.

    The three fingerprints are recomputed only when the clock generation, the
    schema, the coverage or the registry has moved since this process last folded
    them for this database file; see the note above `_REVISIONS`. The value is the
    same digest either way.
    """
    from .identity import identity_fingerprint

    _floor_schema(conn, owner_id)
    clock_id, generation = clock_state(conn)
    coverage = list(identity_coverage(conn))
    try:
        file = _database_file(conn)
        schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
        registered = conn.execute(f"SELECT count(*) FROM {REGISTRY}").fetchone()[0]
    except sqlite3.Error:
        raise PolicyError("protection_clock_unavailable") from None
    key = (clock_id, generation, schema_version, tuple(coverage), registered, CONTRACT_VERSION)
    if file is not None:
        with _REVISIONS_LOCK:
            remembered = _REVISIONS.get(file)
        if remembered is not None and remembered[0] == key:
            return remembered[1]
    revision = digest({"clock_id": clock_id, "generation": generation, "protection": protection_fingerprint(conn),
                       "exclusions": exclusion_fingerprint(conn), "identity": identity_fingerprint(conn),
                       "identity_coverage": coverage, "contract_version": CONTRACT_VERSION})
    if file is not None:
        with _REVISIONS_LOCK:
            _REVISIONS[file] = (key, revision)
            _REVISIONS.move_to_end(file)
            while len(_REVISIONS) > _REVISIONS_LIMIT:
                _REVISIONS.popitem(last=False)
    return revision


def _like(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def closure_protection_revision(conn, *, owner_id: str, records, fact_prefixes, identity=None) -> str:
    """Protection history and state of one closure only; entity floors stay global.

    `records` are (canonical_table, record_id) pairs of every artifact and
    leaf; `fact_prefixes` are the lowercased `subject:predicate` keys a fact
    tombstone could match; `identity` is the closure's own owner-identity state.
    Protecting, excluding or lifting anything else on the node leaves this value
    unchanged, while any event that touched the closure, including a
    protect-then-lift, changes it permanently.

    The prefix list itself is deliberately not digested. The restriction set
    only grows, so hashing it verbatim would stale every owner review the first
    time a new dataset seeds another self contact. A prefix still binds through
    the tombstones and events it actually matches.
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
    return digest({"version": "closure-protection/v2", "clock_id": clock_id, "contract_version": CONTRACT_VERSION,
                   "records": [table + "|" + record_id for table, record_id in records],
                   "protected_records": protected, "excluded_records": excluded_records, "excluded_facts": sorted(excluded_facts),
                   "last_closure_event": last_event, "entity_floor": entity_floor, "identity": identity})


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
        if row is not None and row[2] == 3:
            clock_id, generation = clock_state(conn, version=3)
            if clock_id != expected_clock_id or generation < expected_generation + 1:
                raise PolicyError("protection_upgrade_binding")
            return {"contract_version": 3, "clock_id": clock_id, "generation": generation, "already_current": True}
        found = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'permissions_v2_%'").fetchall())
        if found != V2_TRIGGERS or row != (expected_clock_id, expected_generation, 2):
            raise PolicyError("protection_upgrade_binding")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (EVENTS,)).fetchone():
            raise PolicyError("protection_clock_unavailable")
        for name in V2_TRIGGERS:
            conn.execute(f"DROP TRIGGER {name}")
        _rebuild_state(conn, version=3)
        conn.execute(EVENTS_SQL_V3)
        for sql in V3_TRIGGERS.values():
            conn.execute(sql)
        clock_id, generation = clock_state(conn, version=3)
        if clock_id != expected_clock_id or generation != expected_generation + 1:
            raise PolicyError("protection_upgrade_binding")
        return {"contract_version": 3, "clock_id": clock_id, "generation": generation, "already_current": False}


def _rebuild_events(conn) -> None:
    """Carry the whole event history into the v4 table, preserving sequence order.

    The append-only guards are installed after this, by the same upgrade, so the
    history is never editable while a node is serving. Every existing row keeps
    its sequence and generation, so a floor hash chain folded over the log before
    the upgrade folds to the same value after it.
    """
    # Built by CREATE, never by RENAME: SQLite stores a renamed table's schema
    # with the new name quoted, and the clock compares that text byte for byte.
    conn.execute(f"CREATE TABLE {EVENTS}_carry AS SELECT sequence,generation,source,artifact_key FROM {EVENTS}")
    conn.execute(f"DROP TABLE {EVENTS}")
    conn.execute(EVENTS_SQL)
    conn.execute(f"INSERT INTO {EVENTS}(sequence,generation,source,artifact_key) "
                 f"SELECT sequence,generation,source,artifact_key FROM {EVENTS}_carry ORDER BY sequence")
    # After the copy: a b-tree built over the filled table rather than maintained per row.
    _ensure_event_indexes(conn)
    carried = conn.execute(f"SELECT count(*) FROM {EVENTS}_carry").fetchone()[0]
    conn.execute(f"DROP TABLE {EVENTS}_carry")
    if conn.execute(f"SELECT count(*) FROM {EVENTS}").fetchone()[0] != carried:
        raise PolicyError("protection_upgrade_binding")


def _rebuild_state(conn, *, version: int) -> None:
    """Replace the state row's pinned contract version; its CHECK cannot be altered."""
    conn.execute(f"CREATE TABLE {TABLE}_next (singleton INTEGER PRIMARY KEY CHECK(singleton=1), clock_id TEXT NOT NULL, "
                 f"generation INTEGER NOT NULL CHECK(generation BETWEEN 0 AND {MAX_INTEGER}), "
                 f"contract_version INTEGER NOT NULL CHECK(contract_version={version}))")
    conn.execute(f"INSERT INTO {TABLE}_next SELECT singleton, clock_id, generation+1, {version} FROM {TABLE}")
    conn.execute(f"DROP TABLE {TABLE}")
    conn.execute(f"ALTER TABLE {TABLE}_next RENAME TO {TABLE}")


def upgrade_protection_clock_v4(path: Path, *, owner_id: str, expected_clock_id: str, expected_generation: int) -> dict:
    """Explicit stopped-node upgrade of a complete v3 clock to v4; never a read fallback.

    v4 adds the owner identity attestation ledger, the restriction registry, the
    triggers that observe an identity change where it happens, and the guards
    that make the event log, the ledger and the registry append-only. The clock
    identity is preserved and the generation advances exactly once. The registry
    is seeded from the node's current self rows and merge tombstones, so an
    owner tombstone keyed by an old spelling keeps vetoing. Existing owner
    reviews are stale afterwards by design: the closure revision changed.
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
        events = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (EVENTS,)).fetchone()
        if (found != V3_TRIGGERS or row != (expected_clock_id, expected_generation, 3)
            or events is None or events[0] != EVENTS_SQL_V3):
            raise PolicyError("protection_upgrade_binding")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name IN (?,?)", (LEDGER, REGISTRY)).fetchone():
            raise PolicyError("protection_clock_unavailable")
        for name in V3_TRIGGERS:
            conn.execute(f"DROP TRIGGER {name}")
        _rebuild_state(conn, version=CONTRACT_VERSION)
        _rebuild_events(conn)
        conn.execute(LEDGER_SQL)
        conn.execute(REGISTRY_SQL)
        conn.execute(TOMBSTONES_SQL)
        _seed_registry(conn)
        for sql in _triggers(CONTRACT_VERSION, identity_coverage(conn)).values():
            conn.execute(sql)
        clock_id, generation = clock_state(conn)
        if clock_id != expected_clock_id or generation != expected_generation + 1:
            raise PolicyError("protection_upgrade_binding")
        registered = conn.execute(f"SELECT count(*) FROM {REGISTRY}").fetchone()[0]
        return {"contract_version": CONTRACT_VERSION, "clock_id": clock_id, "generation": generation,
                "already_current": False, "registered_subjects": registered}


def resync_identity_coverage(path: Path, *, owner_id: str, expected_clock_id: str, expected_generation: int) -> dict:
    """Watch identity tables that appeared, or stop watching ones that are gone.

    The clock watches whichever engine identity tables the node has, so a node
    that gains the entity spine after the clock was installed is watching less
    than v4 describes. Every read fails closed until this runs, which is the
    safe direction but does stop the node serving, so this lane exists to end
    that state deliberately on a stopped node rather than by silent repair.

    It only adds and removes identity triggers. It never creates an engine
    table, never touches the ledger, the registry or the event log, and never
    changes the contract version. The generation advances exactly once, so every
    review and signed authority issued under the old coverage goes stale.
    """
    _expected(expected_clock_id, expected_generation)
    with with_db_write(), sqlite3.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _floor_schema(conn, owner_id)
        row = conn.execute(f"SELECT clock_id,generation,contract_version FROM {TABLE} WHERE singleton=1").fetchone()
        if row != (expected_clock_id, expected_generation, CONTRACT_VERSION):
            raise PolicyError("protection_upgrade_binding")
        found = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'permissions_v2_%'").fetchall())
        coverage = identity_coverage(conn)
        wanted = _triggers(CONTRACT_VERSION, coverage)
        # Refuse anything but a pure coverage difference: a clock whose other
        # triggers were altered or lost is damaged, and this lane does not
        # repair it. Only the coverage-gated identity triggers may be absent,
        # and any trigger that is present must carry its exact text.
        full = _triggers(CONTRACT_VERSION, IDENTITY_TABLES)
        gated = set(full) - set(_triggers(CONTRACT_VERSION, ()))
        for name in (set(found) | set(wanted)) - gated:
            if found.get(name) != wanted.get(name):
                raise PolicyError("protection_upgrade_binding")
        for name in gated & set(found):
            if found[name] != full[name]:
                raise PolicyError("protection_upgrade_binding")
        if not set(found) ^ set(wanted):
            return {"contract_version": CONTRACT_VERSION, "clock_id": row[0], "generation": row[1],
                    "coverage": list(coverage), "already_current": True}
        for name in set(found) - set(wanted):
            conn.execute(f"DROP TRIGGER {name}")
        for name in set(wanted) - set(found):
            conn.execute(wanted[name])
        conn.execute(f"UPDATE {TABLE} SET generation=generation+1 WHERE singleton=1")
        clock_id, generation = clock_state(conn)
        if clock_id != expected_clock_id or generation != expected_generation + 1:
            raise PolicyError("protection_upgrade_binding")
        return {"contract_version": CONTRACT_VERSION, "clock_id": clock_id, "generation": generation,
                "coverage": list(coverage), "already_current": False}

"""Candidate keys for the permissions v2 fact lineage reads. [S1]

Two per-read loops in ``permissions_v2/evidence.py`` walked every fact on the node:

- the sibling-fact floor (``_source_sibling_floor``) ran a leading-wildcard GLOB over
  ``signal_objects.source_refs_json`` to find every fact citing a released message;
- the independent-copy check in ``_eligible`` parsed every active fact's payload to
  compare normalized claims.

Both costs grew with facts the recipient never sees: measured 3.0 -> 9.2 ms per
re-checked fact at 0 -> 60 hidden denied facts per kind, and +19 ms per locator read
at 10,000 hidden facts. This step keeps, beside ``signal_objects``, the keys those
loops compare on, so a read can ask for its candidates by index.

The keys only choose CANDIDATES. The unchanged Python predicates (``_names_a_leaf``,
``normalized_claim``) still decide on every candidate. Exactness therefore rests on one
property: the candidate set is a SUPERSET of the rows the old loops would have matched.

- Clean rows are keyed by pure SQL in triggers. No application-defined function is
  involved, since an index or trigger on a UDF makes every connection without it
  unable to write. A row is clean only when SQLite and Python's strict parser
  (``evidence._json``) must read it identically: RFC JSON text of at most 1 MiB, no
  duplicate key at any depth (``json_tree`` reports decoded keys, so escapes cannot
  hide one), and the expected shape. A claim is clean only when its predicate and
  object are plain printable ASCII with single inner spaces, where SQLite's ``lower``
  equals Python's ``" ".join(s.lower().split())``.
- Everything else is OPAQUE. It is always a candidate unless the Python completion
  pass below has keyed it exactly. A write to the row drops that completion again,
  in the same trigger that re-keys it. A reference list the old code tested by
  substring (unparseable text, or a reference with no usable record id) is keyed by
  every suffix of every identifier run, so "the leaf id occurs in it" becomes an
  index range. Only a claim Python itself cannot read, or a row past
  MAX_SUBSTRING_KEYS, stays always-checked. So a malformed fact still refuses every
  release, as it did before.
- Reads use these tables only while the exact trigger and table SQL is present.
  Otherwise they fall back to the old scans, so a dropped trigger costs time,
  never a missed row. Every run of this step (``always_run``, so every node start)
  rebuilds the keys from scratch when anything is missing or altered.

REPLACE conflict resolution deletes rows without firing delete triggers (recursive
triggers are off). So the insert and update triggers first drop the keys of the
row's own ``object_id``. They also drop keys of any row that shared its active
unique tuple (``idx_signal_objects_active_key``) and no longer exists.

Scrub surface: ``permissions_v2_fact_claim_keys`` and ``permissions_v2_fact_key_completion`` hold the lowercased
predicate and at most 32 characters of a fact's object value. The triggers delete
and rewrite them in the same statement as the fact row, so ``scrub_source``, the
black-hole reap and every other fact deletion remove them too.

Release note. Registering this stamps the database's schema version 78, which fences
every engine that predates it, like 63, 69, 75 and 76. It must land at a release cut.
"""
from __future__ import annotations

import json
import re
import sqlite3

MIGRATION_ID = "permissions_fact_lineage_keys_v1"

# Every character Python's str.isspace() accepts; str.strip() strips exactly these.
PY_WHITESPACE = (9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, *range(8192, 8203), 8232, 8233, 8239,
                 8287, 12288)
_WS = "char(" + ",".join(str(code) for code in PY_WHITESPACE) + ")"
MAX_JSON_BYTES = 1_048_576
CLAIM_PREFIX = 32
FAMILIES = ("refs", "claim")
# permissions_v2_fact_key_completion.family: "refs" and "claim" hold equality keys, "refs_substring" the
# suffixes of a row `_names_a_leaf` would test by substring (a prefix range per leaf).
# permissions_v2_fact_key_opaque.state
PENDING, COMPLETED, ALWAYS = 0, 1, 2

TABLES = {
    "permissions_v2_fact_key_rows": "CREATE TABLE permissions_v2_fact_key_rows (object_id TEXT PRIMARY KEY, signal_dimension TEXT, "
                     "object_type TEXT, object_key TEXT)",
    "permissions_v2_fact_ref_keys": "CREATE TABLE permissions_v2_fact_ref_keys (object_id TEXT NOT NULL, ref_key TEXT NOT NULL)",
    "permissions_v2_fact_claim_keys": "CREATE TABLE permissions_v2_fact_claim_keys (object_id TEXT PRIMARY KEY, claim_key TEXT NOT NULL)",
    "permissions_v2_fact_key_opaque": "CREATE TABLE permissions_v2_fact_key_opaque (object_id TEXT NOT NULL, family TEXT NOT NULL "
                       "CHECK(family IN ('refs','claim')), state INTEGER NOT NULL DEFAULT 0 CHECK(state IN (0,1,2)), "
                       "PRIMARY KEY(object_id, family))",
    "permissions_v2_fact_key_completion": "CREATE TABLE permissions_v2_fact_key_completion (object_id TEXT NOT NULL, family TEXT NOT NULL, "
                           "key TEXT NOT NULL)",
}
INDEXES = {
    "permissions_v2_fact_key_rows_tuple": "CREATE INDEX permissions_v2_fact_key_rows_tuple ON permissions_v2_fact_key_rows(signal_dimension, object_type, object_key)",
    "permissions_v2_fact_ref_keys_key": "CREATE INDEX permissions_v2_fact_ref_keys_key ON permissions_v2_fact_ref_keys(ref_key)",
    "permissions_v2_fact_ref_keys_object": "CREATE INDEX permissions_v2_fact_ref_keys_object ON permissions_v2_fact_ref_keys(object_id)",
    "permissions_v2_fact_claim_keys_key": "CREATE INDEX permissions_v2_fact_claim_keys_key ON permissions_v2_fact_claim_keys(claim_key)",
    "permissions_v2_fact_key_opaque_open": "CREATE INDEX permissions_v2_fact_key_opaque_open ON permissions_v2_fact_key_opaque(family, state) WHERE state<>1",
    "permissions_v2_fact_key_completion_key": "CREATE INDEX permissions_v2_fact_key_completion_key ON permissions_v2_fact_key_completion(family, key)",
    "permissions_v2_fact_key_completion_object": "CREATE INDEX permissions_v2_fact_key_completion_object ON permissions_v2_fact_key_completion(object_id)",
}


def _json_ok(column: str, kind: str) -> str:
    """1 when `column` is RFC JSON text Python's strict parser reads the same way, of type `kind`."""
    return (f"CASE WHEN typeof({column})<>'text' THEN 0 "
            f"WHEN length(CAST({column} AS BLOB))>{MAX_JSON_BYTES} THEN 0 "
            f"WHEN NOT json_valid({column}) THEN 0 "
            f"WHEN json_type({column})<>'{kind}' THEN 0 "
            f"WHEN EXISTS (SELECT 1 FROM json_tree({column}) t WHERE t.key IS NOT NULL GROUP BY t.parent, t.key "
            f"HAVING count(*)>1) THEN 0 ELSE 1 END")


def _safe(column: str, kind: str) -> str:
    """The column when it is clean JSON of `kind`, else an empty one; json_each never sees malformed text."""
    empty = "'[]'" if kind == "array" else "'{}'"
    return f"(CASE WHEN ({_json_ok(column, kind)})=1 THEN {column} ELSE {empty} END)"


# An element as a JSON object, or an empty one: SQLite may evaluate an element-level JSON
# call before the clause that rules the element out, and a malformed argument aborts the
# fact writer's own statement.
_OBJECT = "(CASE WHEN e.type='object' THEN e.value ELSE '{}' END)"


def _usable(value: str, path: str) -> str:
    return (f"(json_type({value},'{path}') IN ('text','integer') AND typeof(json_extract({value},'{path}')) IN ('text','integer')"
            f" AND trim(CAST(json_extract({value},'{path}') AS TEXT), {_WS})<>'')")


def refs_clean(row: str) -> str:
    refs = f"{row}source_refs_json"
    safe = _safe(refs, "array")
    return (f"(CASE WHEN ({_json_ok(refs, 'array')})<>1 THEN 0 "
            f"WHEN EXISTS (SELECT 1 FROM json_each({safe}) e WHERE e.type<>'object') THEN 0 "
            f"WHEN EXISTS (SELECT 1 FROM json_each({safe}) e, json_each({_OBJECT}) v WHERE v.type IN ('object','array')) THEN 0 "
            f"WHEN EXISTS (SELECT 1 FROM json_each({safe}) e WHERE NOT {_usable(_OBJECT, '$.record_id')}) THEN 0 "
            f"ELSE 1 END)")


def ref_keys_select(row: str) -> str:
    """(key) rows for a clean reference list: every record_id, and every usable id, stripped as Python strips."""
    safe = _safe(f"{row}source_refs_json", "array")
    return (f"SELECT trim(CAST(json_extract({_OBJECT},'$.record_id') AS TEXT), {_WS}) AS k FROM json_each({safe}) e "
            f"UNION SELECT trim(CAST(json_extract({_OBJECT},'$.id') AS TEXT), {_WS}) FROM json_each({safe}) e "
            f"WHERE {_usable(_OBJECT, '$.id')}")


def _ascii_clean(value: str) -> str:
    return (f"({value} NOT GLOB '*[^ -~]*' AND {value} NOT GLOB ' *' AND {value} NOT GLOB '* ' "
            f"AND {value} NOT GLOB '*  *')")


def claim_clean(row: str) -> str:
    payload = f"{row}payload_json"
    safe = _safe(payload, "object")
    predicate, value = f"json_extract({safe},'$.predicate')", f"json_extract({safe},'$.object_value')"
    return (f"(CASE WHEN ({_json_ok(payload, 'object')})<>1 THEN 0 "
            f"WHEN json_type({safe},'$.subject_entity_id') IS NOT 'text' OR json_type({safe},'$.predicate') IS NOT 'text' "
            f"OR json_type({safe},'$.object_value') IS NOT 'text' THEN 0 "
            f"WHEN NOT {_ascii_clean(predicate)} OR NOT {_ascii_clean(value)} THEN 0 ELSE 1 END)")


def claim_key_sql(row: str) -> str:
    safe = _safe(f"{row}payload_json", "object")
    predicate, value = f"lower(json_extract({safe},'$.predicate'))", f"lower(json_extract({safe},'$.object_value'))"
    return f"{predicate} || char(31) || length({value}) || char(31) || substr({value},1,{CLAIM_PREFIX})"


def claim_key(predicate: str, value: str) -> str:
    """The key of an already normalized (predicate, object value); the same string `claim_key_sql` builds."""
    return predicate + "\x1f" + str(len(value)) + "\x1f" + value[:CLAIM_PREFIX]


_KEY_TABLES = ("permissions_v2_fact_ref_keys", "permissions_v2_fact_claim_keys", "permissions_v2_fact_key_opaque", "permissions_v2_fact_key_completion", "permissions_v2_fact_key_rows")


def _forget(object_id: str) -> str:
    return "".join(f" DELETE FROM {table} WHERE object_id={object_id};" for table in _KEY_TABLES)


def _forget_replaced() -> str:
    """Keys of rows a REPLACE removed through the active unique tuple: same tuple, row gone."""
    gone = ("SELECT r.object_id FROM permissions_v2_fact_key_rows r WHERE r.signal_dimension=NEW.signal_dimension "
            "AND r.object_type=NEW.object_type AND r.object_key=NEW.object_key AND r.object_id<>NEW.object_id "
            "AND NOT EXISTS (SELECT 1 FROM signal_objects s WHERE s.object_id=r.object_id)")
    return "".join(f" DELETE FROM {table} WHERE object_id IN ({gone});" for table in _KEY_TABLES)


def _key_new() -> str:
    when = "NEW.object_type='fact'"
    return (f" INSERT INTO permissions_v2_fact_key_rows SELECT NEW.object_id, NEW.signal_dimension, NEW.object_type, NEW.object_key "
            f"WHERE {when};"
            f" INSERT INTO permissions_v2_fact_ref_keys SELECT DISTINCT NEW.object_id, k FROM ({ref_keys_select('NEW.')}) "
            f"WHERE {when} AND {refs_clean('NEW.')}=1;"
            f" INSERT INTO permissions_v2_fact_key_opaque(object_id, family, state) SELECT NEW.object_id, 'refs', 0 "
            f"WHERE {when} AND {refs_clean('NEW.')}<>1;"
            f" INSERT INTO permissions_v2_fact_claim_keys SELECT NEW.object_id, {claim_key_sql('NEW.')} "
            f"WHERE {when} AND {claim_clean('NEW.')}=1;"
            f" INSERT INTO permissions_v2_fact_key_opaque(object_id, family, state) SELECT NEW.object_id, 'claim', 0 "
            f"WHERE {when} AND {claim_clean('NEW.')}<>1;")


# Names avoid `permissions_v2_%` (clock_state matches that with LIKE, where `_` is a wildcard)
# and `ingest_provenance_*` (the ingest store pins those names).
TRIGGERS = {
    "fact_lineage_keys_ai": ("CREATE TRIGGER fact_lineage_keys_ai AFTER INSERT ON signal_objects BEGIN"
                             + _forget("NEW.object_id") + _forget_replaced() + _key_new() + " END"),
    "fact_lineage_keys_au": ("CREATE TRIGGER fact_lineage_keys_au AFTER UPDATE OF object_id, signal_dimension, object_type, "
                             "object_key, payload_json, source_refs_json, valid_to ON signal_objects BEGIN"
                             + _forget("OLD.object_id") + _forget("NEW.object_id") + _forget_replaced() + _key_new()
                             + " END"),
    "fact_lineage_keys_ad": "CREATE TRIGGER fact_lineage_keys_ad AFTER DELETE ON signal_objects BEGIN"
                            + _forget("OLD.object_id") + " END",
}
EXPECTED = {**TABLES, **INDEXES, **TRIGGERS}


def installed(conn: sqlite3.Connection) -> bool:
    """True only when every key table, index and trigger is present with exactly this SQL."""
    names = sorted(EXPECTED)
    found = dict(conn.execute(f"SELECT name, sql FROM sqlite_master WHERE name IN ({','.join('?' * len(names))})",
                              names).fetchall())
    return found == EXPECTED


# --- Python completion of opaque rows ------------------------------------------------------


def _strict(raw, expected):
    """evidence._json's parse: strict, duplicate keys and non-finite constants refused, 1 MiB cap."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def constant(_):
        raise ValueError()
    if not isinstance(raw, str) or len(raw.encode()) > MAX_JSON_BYTES:
        raise ValueError()
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, expected):
        raise ValueError()
    return value


# Characters an evidence Identifier may hold (`contract.Identifier`); a leaf id is a run of them.
_IDENTIFIER_RUN = re.compile(r"[A-Za-z0-9._:@/-]+")
IDENTIFIER_MAX = 200
# A row whose substring keys would exceed this stays always-checked instead.
MAX_SUBSTRING_KEYS = 4096


def substring_keys(text: str) -> set[str] | None:
    """Every suffix of every identifier run, cut to IDENTIFIER_MAX, starting at an ASCII letter or digit.

    A leaf id L (an Identifier) occurs in `text` exactly when some run holds it, that is when
    one of these keys starts with L. The read asks for that by index range.
    """
    keys = set()
    for run in _IDENTIFIER_RUN.findall(text):
        for start, char in enumerate(run):
            if char.isascii() and char.isalnum():
                keys.add(run[start:start + IDENTIFIER_MAX])
                if len(keys) > MAX_SUBSTRING_KEYS:
                    return None
    return keys


def exact_ref_keys(raw) -> tuple[set[str], set[str]] | None:
    """(equality keys, substring keys) exactly as `_names_a_leaf` can match, or None past the cap.

    Mirrors that function branch for branch: the same text conversion, the same strict
    parse, the same per-reference fallback to a substring test of `json.dumps(ref)`, and
    for unparseable text the same substring test of the text and its escape-decoded spelling.
    """
    text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    exact, substrings = set(), set()
    try:
        refs = _strict(text, list)
    except (ValueError, TypeError, RecursionError):
        spelled = re.sub(r"\\u00([0-9A-Fa-f]{2})", lambda match: chr(int(match.group(1), 16)), text).replace("\\/", "/")
        found = [substring_keys(text), substring_keys(spelled)]
        return None if None in found else (exact, found[0] | found[1])
    for ref in refs:
        value = ref.get("record_id") if isinstance(ref, dict) else None
        if type(value) not in (str, int) or not str(value).strip():
            found = substring_keys(json.dumps(ref))
            if found is None:
                return None
            substrings |= found
            continue
        for candidate in (value, ref.get("id")):
            if type(candidate) in (str, int):
                exact.add(str(candidate).strip())
    return (exact, substrings) if len(substrings) <= MAX_SUBSTRING_KEYS else None


def exact_claim_key(raw) -> str | None:
    """The claim key `normalized_claim` compares on, or None when that function would raise."""
    try:
        payload = _strict(raw, dict)
    except (ValueError, TypeError, RecursionError):
        return None
    if any(not isinstance(payload.get(field), str) for field in ("subject_entity_id", "predicate", "object_value")):
        return None
    predicate, value = (" ".join(payload[field].lower().split()) for field in ("predicate", "object_value"))
    return claim_key(predicate, value)


# Candidate facts for a set of leaf ids: equality keys, substring keys by prefix range, and
# every fact still waiting for (or beyond) Python keying. `sibling_arguments` binds it.
SIBLING_CANDIDATES = ("SELECT object_id FROM permissions_v2_fact_ref_keys WHERE ref_key IN ({marks}) "
                      "UNION SELECT object_id FROM permissions_v2_fact_key_completion WHERE family='refs' AND key IN ({marks}) "
                      "UNION SELECT object_id FROM permissions_v2_fact_key_completion WHERE family='refs_substring' AND ({ranges}) "
                      "UNION SELECT object_id FROM permissions_v2_fact_key_opaque WHERE family='refs' AND state<>1")
_PAST = chr(0x10FFFF)


def sibling_arguments(leaves) -> list[str]:
    ids = sorted(leaves)
    return ids + ids + [bound for leaf in ids for bound in (leaf, leaf + _PAST)]


def complete_pending(conn: sqlite3.Connection, *, limit: int | None = None) -> int:
    """Key opaque rows exactly in Python; the caller holds a write transaction. Returns rows settled."""
    sql = "SELECT o.object_id, o.family, s.source_refs_json, s.payload_json FROM permissions_v2_fact_key_opaque o " \
          "JOIN signal_objects s ON s.object_id=o.object_id WHERE o.state=0"
    rows = conn.execute(sql + ("" if limit is None else f" LIMIT {int(limit)}")).fetchall()
    for object_id, family, refs, payload in rows:
        if family == "refs":
            found = exact_ref_keys(refs)
            keys = None if found is None else [("refs", key) for key in sorted(found[0])] + \
                [("refs_substring", key) for key in sorted(found[1])]
        else:
            key = exact_claim_key(payload)
            keys = None if key is None else [("claim", key)]
        if keys is None:
            conn.execute("UPDATE permissions_v2_fact_key_opaque SET state=2 WHERE object_id=? AND family=?", (object_id, family))
            continue
        conn.executemany("INSERT INTO permissions_v2_fact_key_completion VALUES (?,?,?)", [(object_id, kind, key) for kind, key in keys])
        conn.execute("UPDATE permissions_v2_fact_key_opaque SET state=1 WHERE object_id=? AND family=?", (object_id, family))
    return len(rows)


def _rebuild(conn: sqlite3.Connection) -> None:
    for name in TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {name}")
    for sql in (*TABLES.values(), *INDEXES.values()):
        conn.execute(sql)
    facts = "FROM signal_objects s WHERE s.object_type='fact'"
    conn.execute(f"INSERT INTO permissions_v2_fact_key_rows SELECT s.object_id, s.signal_dimension, s.object_type, s.object_key {facts}")
    # A FROM-clause subquery cannot see `s`; a table-valued function's argument can.
    safe = _safe("s.source_refs_json", "array")
    for path, extra in (("$.record_id", ""), ("$.id", f" AND {_usable(_OBJECT, '$.id')}")):
        conn.execute(f"INSERT INTO permissions_v2_fact_ref_keys SELECT DISTINCT s.object_id, "
                     f"trim(CAST(json_extract({_OBJECT},'{path}') AS TEXT), {_WS}) FROM signal_objects s, json_each({safe}) e "
                     f"WHERE s.object_type='fact' AND {refs_clean('s.')}=1{extra}")
    conn.execute(f"INSERT INTO permissions_v2_fact_key_opaque(object_id, family, state) SELECT s.object_id, 'refs', 0 {facts} "
                 f"AND {refs_clean('s.')}<>1")
    conn.execute(f"INSERT INTO permissions_v2_fact_claim_keys SELECT s.object_id, {claim_key_sql('s.')} {facts} AND {claim_clean('s.')}=1")
    conn.execute(f"INSERT INTO permissions_v2_fact_key_opaque(object_id, family, state) SELECT s.object_id, 'claim', 0 {facts} "
                 f"AND {claim_clean('s.')}<>1")
    for sql in TRIGGERS.values():
        conn.execute(sql)


def apply_permissions_fact_lineage_keys_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_objects'").fetchone() is None:
        conn.commit()
        return
    try:
        if not installed(conn):
            _rebuild(conn)
        complete_pending(conn)
        conn.execute("INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)", (MIGRATION_ID,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

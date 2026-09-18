"""The exclusion floor past the 1 MiB digest cap: the value did not move, the ceiling did.

`exclusion_fingerprint` is folded into every node-wide protection revision, so its
digest ran on every permissions read, every status and every ingest door. It was
built as one canonical value, which `canonical_bytes` refuses above 1 MiB, so from
roughly 4,500-5,200 tombstones of the campaign's shape -- or one tombstone carrying
an oversized note -- every signed v2 route on the node refused, status and revoke
included. These tests pin both halves of the fix: the floor now takes far more
tombstones than the old cap, and the value is byte-identical to the built digest for
every table the built digest could encode, so nothing that pinned the old value is
re-pinned. The refusals are unchanged as well, except `json_size`, which the streamed
digest never raises.
"""
import hashlib
import json
import sqlite3

import pytest

from tests.permissions_v2.test_evidence import attest, corpus, decision, owner  # noqa: F401 (fixtures)
from topos.features.lifecycle.exclusions import ExclusionStore
from topos.permissions_v2 import exclusion_floor
from topos.permissions_v2.canonical import MAX_BYTES, PolicyError, canonical_bytes, digest
from topos.permissions_v2.exclusion_floor import FINGERPRINT_VERSION, exclusion_fingerprint, exclusions
from topos.permissions_v2.protection_clock import current_protection_revision

NOTE = "owner rejected via fact review"
# Past the band the assessment measured for the built digest (4,500-5,200 rows of this shape).
OVER_CAP = 6000
KNOWN_ANSWER = "2751957020ce2a6425abe9b82249d556f518e34fd562fafd9d09a62c32f01759"


def campaign_key(index):
    """The assessment's realistic tombstone: a hex entity id, `works_at`, one value."""
    return f"ent_{hashlib.sha256(str(index).encode()).hexdigest()[:16]}:works_at:acme corporation"


def tombstone(conn, count, *, start=0, note=NOTE):
    """Write `count` fact tombstones through the lifecycle store's own writer, so the clock triggers fire."""
    store = ExclusionStore(conn)
    for index in range(start, start + count):
        store._tombstone("fact", campaign_key(index), note)
    conn.commit()


def built_fingerprint(conn):
    """`exclusion_fingerprint` exactly as it stood before the change: one canonical value, capped."""
    return digest({"version": FINGERPRINT_VERSION, "rows": exclusion_floor._read(conn)[1]})


def flat_fingerprint(conn):
    """The whole-table formula computed without the module: sha256 of the sorted canonical JSON."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(intelligence_exclusions)")]
    rows = sorted((dict(zip(columns, row)) for row in conn.execute("SELECT * FROM intelligence_exclusions")),
                  key=lambda row: row["exclusion_id"])
    encoded = json.dumps({"version": FINGERPRINT_VERSION, "rows": rows}, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def code(call):
    try:
        call()
        return "ok"
    except PolicyError as exc:
        return exc.code


def reading(corpus):
    conn = sqlite3.connect(corpus[0].path.as_uri() + "?mode=ro", uri=True)
    conn.execute("BEGIN")
    return conn


# --- the value ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 2, 40, 386, 2000])
def test_the_streamed_fingerprint_is_the_built_value(corpus, count):
    with sqlite3.connect(corpus[0].path) as conn:
        tombstone(conn, count)
    conn = reading(corpus)
    assert exclusion_fingerprint(conn) == built_fingerprint(conn) == flat_fingerprint(conn)


def test_a_known_answer_survives_vacuum(tmp_path):
    """A fixed table digests to a fixed hex, so the definition cannot drift without this test moving."""
    path = tmp_path / "known.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE intelligence_exclusions (exclusion_id TEXT PRIMARY KEY, artifact_type TEXT NOT NULL, "
                     "artifact_key TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), "
                     "created_by TEXT NOT NULL DEFAULT 'owner')")
        conn.executemany("INSERT INTO intelligence_exclusions VALUES(?,?,?,?,?,?)", [
            ("exc-b", "fact", "self:works_at:acme", NOTE, "2026-09-01 00:00:00", "owner"),
            ("exc-a", "record", "imessage:100001", None, "2026-09-01 00:00:01", "owner"),
            ("exc-c", "entity", "a protected name", "é", "2026-09-01 00:00:02", "owner"),
        ])
    with sqlite3.connect(path) as conn:
        assert exclusion_fingerprint(conn) == built_fingerprint(conn) == flat_fingerprint(conn) == KNOWN_ANSWER
        conn.execute("VACUUM")
    with sqlite3.connect(path) as conn:
        assert exclusion_fingerprint(conn) == KNOWN_ANSWER


def test_the_fingerprint_reads_the_table_and_no_index(corpus):
    """`SELECT *` with no predicate is a table scan, so no index decides which rows are digested."""
    with sqlite3.connect(corpus[0].path) as conn:
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + exclusion_floor._READ)]
    assert plan == ["SCAN intelligence_exclusions"]


# --- the ceiling ---------------------------------------------------------------------------------------


def test_the_campaign_shape_still_crosses_the_old_cap(corpus):
    """The row shape has to stay a real fraction of the old cap, or OVER_CAP stops meaning anything."""
    with sqlite3.connect(corpus[0].path) as conn:
        tombstone(conn, 40)
        rows = exclusion_floor._read(conn)[1]
    per_row = len(canonical_bytes(rows)) / len(rows)
    assert 150 <= per_row <= 260, per_row
    assert OVER_CAP * per_row > MAX_BYTES


def test_thousands_of_tombstones_no_longer_close_every_signed_route(corpus):
    with sqlite3.connect(corpus[0].path) as conn:
        tombstone(conn, OVER_CAP)
    conn = reading(corpus)
    assert code(lambda: built_fingerprint(conn)) == "json_size", "the symptom the assessment measured"
    assert exclusion_fingerprint(conn) == flat_fingerprint(conn)
    assert len(current_protection_revision(conn, owner_id="owner-1")) == 64
    assert len(exclusions(conn)["fact"]) == OVER_CAP
    conn.close()
    # The read path itself: an owner review on a fact none of the tombstones name still qualifies.
    attest(corpus)
    assert decision(corpus).verdict == "qualified"


def test_one_oversized_note_no_longer_closes_the_floor(corpus):
    with sqlite3.connect(corpus[0].path) as conn:
        ExclusionStore(conn)._tombstone("fact", campaign_key(0), "x" * (MAX_BYTES + 1))
        conn.commit()
    conn = reading(corpus)
    assert code(lambda: built_fingerprint(conn)) == "json_size"
    assert exclusion_fingerprint(conn) == flat_fingerprint(conn)
    conn.close()
    attest(corpus)
    assert decision(corpus).verdict == "qualified"


def test_a_tombstone_that_names_the_fact_still_vetoes_past_the_old_cap(corpus):
    """Growth changes the cost, never the answer: the veto is found among thousands of neighbours."""
    with sqlite3.connect(corpus[0].path) as conn:
        tombstone(conn, OVER_CAP)
        ExclusionStore(conn)._tombstone("fact", "self:prefers", None)
        conn.commit()
    attest(corpus)
    assert decision(corpus).reason_code == "intelligence_excluded"


# --- the refusals ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("damage,expected", [
    ("blob_note", "exclusion_state_unknown"),
    ("duplicate_id", "exclusion_state_unknown"),
    ("unknown_kind", "exclusion_state_unknown"),
    ("empty_key", "exclusion_state_unknown"),
    ("padded_key", "exclusion_state_unknown"),
    ("missing_column", "exclusion_schema_unavailable"),
    ("missing_table", "exclusion_schema_unavailable"),
])
def test_every_refusal_is_the_one_the_built_digest_gave(corpus, damage, expected):
    with sqlite3.connect(corpus[0].path) as conn:
        tombstone(conn, 3)
        if damage == "blob_note":
            conn.execute("UPDATE intelligence_exclusions SET note=? WHERE artifact_key=?", (b"opaque", campaign_key(0)))
        elif damage == "duplicate_id":
            # The primary key refuses a duplicate through SQL; plant one the way a rewritten file would carry it.
            conn.execute("PRAGMA writable_schema=ON")
            conn.execute("CREATE TABLE ie_copy AS SELECT * FROM intelligence_exclusions")
            conn.execute("DROP TABLE intelligence_exclusions")
            conn.execute("CREATE TABLE intelligence_exclusions (exclusion_id TEXT, artifact_type TEXT NOT NULL, artifact_key TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), created_by TEXT NOT NULL DEFAULT 'owner')")
            conn.execute("INSERT INTO intelligence_exclusions SELECT * FROM ie_copy")
            conn.execute("INSERT INTO intelligence_exclusions SELECT exclusion_id,'record','other-key',note,created_at,created_by FROM ie_copy LIMIT 1")
            conn.execute("DROP TABLE ie_copy")
        elif damage == "unknown_kind":
            conn.execute("INSERT INTO intelligence_exclusions(exclusion_id,artifact_type,artifact_key) VALUES('bad','new_kind','k')")
        elif damage == "empty_key":
            conn.execute("INSERT INTO intelligence_exclusions(exclusion_id,artifact_type,artifact_key) VALUES('bad','record','')")
        elif damage == "padded_key":
            conn.execute("INSERT INTO intelligence_exclusions(exclusion_id,artifact_type,artifact_key) VALUES('bad','record',' padded ')")
        elif damage == "missing_column":
            conn.execute("ALTER TABLE intelligence_exclusions RENAME COLUMN artifact_key TO lost_key")
        elif damage == "missing_table":
            conn.execute("DROP TABLE intelligence_exclusions")
    with sqlite3.connect(corpus[0].path) as conn:
        assert code(lambda: exclusion_fingerprint(conn)) == code(lambda: built_fingerprint(conn)) == expected
        assert code(lambda: exclusions(conn)) == expected


def test_a_streamed_row_is_refused_as_the_built_row_was_on_a_non_text_cell(tmp_path):
    """`MappingRows` refuses a cell `_check` refuses, and `Rows` would digest the column NAMES of a dict row."""
    path = tmp_path / "cells.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE intelligence_exclusions (exclusion_id TEXT PRIMARY KEY, artifact_type TEXT NOT NULL, artifact_key TEXT NOT NULL, note)")
        conn.execute("INSERT INTO intelligence_exclusions VALUES('exc-1','record','r-1',1.5)")
    with sqlite3.connect(path) as conn:
        # a float never reaches the encoder: the row validation refuses it first, before and after
        assert code(lambda: exclusion_fingerprint(conn)) == code(lambda: built_fingerprint(conn)) == "exclusion_state_unknown"

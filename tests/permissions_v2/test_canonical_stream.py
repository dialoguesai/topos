"""The streamed canonical digest: identical bytes to `canonical.digest`, with no whole-value size cap.

`digest_stream` is the reusable helper the review-store authority digest is
built on. Every test here pins the one property the floor depends on: for every
value `digest` accepts, `digest_stream` returns the same hex, and for every
value `digest` refuses, `digest_stream` raises the same code -- except that it
never raises `json_size`, because it never materializes the encoding.
"""
import hashlib
import json
import random
import sqlite3

import pytest

from topos.permissions_v2 import canonical
from topos.permissions_v2.canonical import (MAX_BYTES, MAX_DEPTH, MAX_INTEGER, MappingRows, PolicyError,
    Rows, canonical_bytes, digest, digest_stream)


def review_rows(count, *, body=3000):
    return [[f"review-{index:06d}", f"fact-{index % 7}", json.dumps({"body": "x" * body, "index": index}), index % 2]
            for index in range(count)]


def reference(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("ascii")).hexdigest()


def outcome(call):
    try:
        return ("ok", call())
    except PolicyError as exc:
        return ("refused", exc.code)


# --- equality with the pinned definition ------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 2, 50, 300])
def test_streamed_rows_equal_the_materialized_digest(count):
    rows = review_rows(count)
    assert digest_stream(Rows(iter(rows))) == digest([list(row) for row in rows])


def test_the_empty_store_digests_as_the_empty_array(tmp_path):
    assert digest_stream(Rows([])) == digest([]) == hashlib.sha256(b"[]").hexdigest()


@pytest.mark.parametrize("value", [
    {"b": [1, None, True], "a": "é\U0001f600", "c": {"k": -5}},
    [1, 2, 3],
    "a string",
    {"rows": [["not", "streamed"]]},
], ids=["dict", "list", "scalar", "materialized_rows"])
def test_a_value_that_streams_nothing_is_refused_rather_than_uncapped(value):
    """The helper removes a size bound, so it must not accept a value it was not asked to stream.

    Every other encoder in this module is bounded by MAX_BYTES, which is the bound on how
    much work one value can ask for. `digest_stream` lifts it for rows a caller reads out
    of its own store; a caller that hands it an ordinary value gets `json_type`, so the
    helper cannot be repurposed as a general cap remover by swapping one name.
    """
    assert outcome(lambda: digest_stream(value)) == ("refused", "json_type")
    assert outcome(lambda: digest(value))[0] == "ok"


# --- framing -----------------------------------------------------------------------------------


@pytest.mark.parametrize("left,right", [
    ([["ab", "c"]], [["a", "bc"]]),
    ([["a"], ["b"]], [["a", "b"]]),
    ([["a", "b"]], [["a"], ["b"]]),
    ([[1]], [["1"]]),
    ([[None]], [[""]]),
    ([[None]], [["null"]]),
    ([[0]], [[False]]),
    ([[]], []),
    ([["a"]], [["a"], []]),
])
def test_framing_separates_values_that_would_otherwise_concatenate_alike(left, right):
    assert digest_stream(Rows(left)) != digest_stream(Rows(right))
    assert digest_stream(Rows(left)) == digest([list(row) for row in left])
    assert digest_stream(Rows(right)) == digest([list(row) for row in right])


def test_row_and_cell_order_are_both_significant():
    rows = review_rows(4, body=8)
    swapped_rows = [rows[1], rows[0]] + rows[2:]
    swapped_cells = [[rows[0][1], rows[0][0], rows[0][2], rows[0][3]]] + rows[1:]
    assert digest_stream(Rows(rows)) != digest_stream(Rows(swapped_rows))
    assert digest_stream(Rows(rows)) != digest_stream(Rows(swapped_cells))
    assert digest_stream(Rows(swapped_rows)) == digest([list(row) for row in swapped_rows])


def test_a_dropped_or_added_cell_changes_the_digest():
    rows = review_rows(3, body=8)
    assert digest_stream(Rows([row[:3] for row in rows])) != digest_stream(Rows(rows))
    assert digest_stream(Rows([row + [0] for row in rows])) != digest_stream(Rows(rows))


# --- domain separation between tables (the multi-table shape the ledgers would reuse) -----------


def test_table_keys_separate_their_rows_and_sort_independently_of_insertion_order():
    left, right = review_rows(3, body=8), review_rows(2, body=8)
    value = {"beta": Rows(right), "alpha": Rows(left)}
    assert digest_stream(value) == digest({"alpha": [list(row) for row in left], "beta": [list(row) for row in right]})
    moved = {"alpha": Rows(left + right), "beta": Rows([])}
    assert digest_stream(moved) != digest_stream({"beta": Rows(right), "alpha": Rows(left)})
    renamed = {"alpha": Rows(left), "gamma": Rows(right)}
    assert digest_stream(renamed) != digest_stream({"beta": Rows(right), "alpha": Rows(left)})


def test_a_table_key_that_is_not_ascii_or_not_a_string_is_refused():
    assert outcome(lambda: digest_stream({"é": Rows([]), "a": Rows([])})) == ("refused", "json_key")
    assert outcome(lambda: digest_stream({1: Rows([]), "a": Rows([])})) == ("refused", "json_key")


def test_a_table_value_that_is_not_rows_is_encoded_like_canonical():
    value = {"rows": Rows(review_rows(2, body=8)), "count": 2, "state": {"generation": 7}}
    assert digest_stream(value) == digest({"rows": [list(row) for row in review_rows(2, body=8)],
                                           "count": 2, "state": {"generation": 7}})


# --- no size cap -------------------------------------------------------------------------------


def test_the_streamed_digest_has_no_whole_value_size_cap():
    rows = review_rows(700)
    assert len(json.dumps([list(row) for row in rows], separators=(",", ":"))) > MAX_BYTES
    assert outcome(lambda: digest([list(row) for row in rows])) == ("refused", "json_size")
    assert digest_stream(Rows(iter(rows))) == reference([list(row) for row in rows])


def test_the_streamed_digest_never_materializes_the_value():
    """A generator consumed once proves the rows are never held as one list."""
    rows = review_rows(200)
    consumed = []

    def once():
        for row in rows:
            consumed.append(row[0])
            yield row

    assert digest_stream(Rows(once())) == digest([list(row) for row in rows])
    assert len(consumed) == 200


# --- refusal parity ----------------------------------------------------------------------------


CELLS = [b"bytes", bytearray(b"bytes"), 1.5, float("nan"), float("inf"), 2**53, -(2**53), 2**63 - 1,
         True, False, None, "\ud800", "\udfff", "a\ud800b", "\U0001f600", "\x00", "\x7f", '"\\',
         "é", MAX_INTEGER, -MAX_INTEGER, 0, [], [1, 2], {"k": 1}, {"é": 1}, {1: 2}, (1, 2), set()]


@pytest.mark.parametrize("cell", CELLS, ids=lambda cell: repr(cell)[:24])
def test_every_cell_kind_is_accepted_or_refused_exactly_as_canonical_does(cell):
    rows = [["review-1", "fact-1", "{}", 1], ["review-2", "fact-2", cell, 0]]
    assert outcome(lambda: digest_stream(Rows(rows))) == outcome(lambda: digest([list(row) for row in rows]))


@pytest.mark.parametrize("depth", [37, 38, 39])
def test_nested_cells_hit_the_same_depth_limit(depth):
    cell = "leaf"
    for _ in range(depth):
        cell = [cell]
    rows = [["review-1", "fact-1", cell, 1]]
    assert outcome(lambda: digest_stream(Rows(rows))) == outcome(lambda: digest([list(row) for row in rows]))


def test_the_first_bad_cell_in_traversal_order_still_wins_the_refusal():
    rows = [["review-1", "fact-1", "\ud800", 1], ["review-2", "fact-2", b"later", 0]]
    assert outcome(lambda: digest_stream(Rows(rows))) == ("refused", "json_surrogate")
    assert outcome(lambda: digest([list(row) for row in rows])) == ("refused", "json_surrogate")
    flipped = [["review-1", "fact-1", b"first", 1], ["review-2", "fact-2", "\ud800", 0]]
    assert outcome(lambda: digest_stream(Rows(flipped))) == ("refused", "json_type")
    assert outcome(lambda: digest([list(row) for row in flipped])) == ("refused", "json_type")


def test_seeded_fuzz_never_diverges_from_canonical():
    rng = random.Random(20260917)
    pool = CELLS + ["", "plain", "-1", 1, -1, 12345678901234, " ", "\U0010ffff", [[["deep"]]], {"a": [1]}]
    for _ in range(4000):
        rows = [[rng.choice(pool) for _ in range(rng.randint(0, 5))] for _ in range(rng.randint(0, 4))]
        streamed = outcome(lambda: digest_stream(Rows(rows)))
        materialized = outcome(lambda: digest([list(row) for row in rows]))
        assert streamed == materialized, rows


# --- real SQLite storage classes ----------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "rows.db"
    with sqlite3.connect(path) as conn:
        # Deliberately without the store's NOT NULL/CHECK guards: this fixture exists to feed the
        # helper the storage classes SQLite can actually return, not to model the enrolled schema.
        conn.execute("CREATE TABLE fact_reviews(review_id TEXT PRIMARY KEY,fact_id,review_json,active)")
    return path


SELECT = "SELECT review_id,fact_id,review_json,active FROM fact_reviews ORDER BY review_id"


@pytest.mark.parametrize("values,expected", [
    (("review-1", "fact-1", "{}", 1), "ok"),
    (("review-1", "fact-1", b"\x00\xff", 1), "json_type"),
    (("review-1", "fact-1", "{}", 1.5), "json_type"),
    (("review-1", "fact-1", "{}", 2**53), "json_type"),
    (("review-1", "fact-1", "{}", 2**63 - 1), "json_type"),
    ((1, "fact-1", "{}", 1), "ok"),
    ((None, "fact-1", "{}", 1), "ok"),
    (("review-1", "fact-1", "{}", None), "ok"),
])
def test_sqlite_storage_classes_reach_the_same_outcome_as_canonical(store, values, expected):
    with sqlite3.connect(store) as conn:
        conn.execute("INSERT INTO fact_reviews VALUES(?,?,?,?)", values)
    with sqlite3.connect(store) as conn:
        streamed = outcome(lambda: digest_stream(Rows(conn.execute(SELECT))))
        materialized = outcome(lambda: digest([list(row) for row in conn.execute(SELECT)]))
    assert streamed == materialized
    assert streamed[0] == "ok" if expected == "ok" else streamed == ("refused", expected)


def test_a_store_grown_past_the_cap_digests_from_a_real_cursor(store):
    rows = review_rows(700)
    with sqlite3.connect(store) as conn:
        conn.executemany("INSERT INTO fact_reviews VALUES(?,?,?,?)", rows)
    with sqlite3.connect(store) as conn:
        assert outcome(lambda: digest([list(row) for row in conn.execute(SELECT)])) == ("refused", "json_size")
        assert digest_stream(Rows(conn.execute(SELECT))) == reference([list(row) for row in rows])


def test_canonical_bytes_and_digest_keep_their_own_cap():
    """The helper is additive: the capped encoders every marker and receipt uses are untouched."""
    oversized = ["x" * (MAX_BYTES // 2), "y" * (MAX_BYTES // 2)]
    assert outcome(lambda: canonical_bytes(oversized)) == ("refused", "json_size")
    assert outcome(lambda: digest(oversized)) == ("refused", "json_size")
    # And the cap is not removed for it by routing it through the streaming helper.
    assert outcome(lambda: digest_stream(oversized)) == ("refused", "json_type")
    # A cell inside a streamed row is still unbounded, which is the whole point.
    assert digest_stream(Rows([[oversized[0]], [oversized[1]]])) == reference([[oversized[0]], [oversized[1]]])


# --- mapping rows, and the two out-of-scope ledgers that can now adopt the helper ----------------


def exclusion_rows(count):
    """The exclusion floor's row shape: `dict(zip(columns, row))`, so a mapping, not a sequence."""
    return [{"exclusion_id": f"exc-{index:06d}", "artifact_type": "fact",
             "artifact_key": f"ent-{index}:likes:a value", "note": None if index % 2 else "why"}
            for index in range(count)]


@pytest.mark.parametrize("count", [0, 1, 2, 50])
def test_streamed_mapping_rows_equal_the_materialized_digest(count):
    rows = exclusion_rows(count)
    assert digest_stream(MappingRows(iter(rows))) == digest(rows)


def test_a_mapping_row_handed_to_rows_is_refused_rather_than_answered_differently():
    """`list(dict)` is the dict's keys, so the wrong helper must refuse, not return a digest.

    This is the footgun the two shapes create: a one-line adoption of `Rows` in a dict-row
    ledger would otherwise replace its pinned fingerprint with a digest of the COLUMN NAMES
    and drop every value, with no error to say so.
    """
    rows = exclusion_rows(3)
    assert outcome(lambda: digest_stream(Rows(rows))) == ("refused", "json_type")
    # The value that would have been returned instead, spelled out: keys only, no values.
    columns = ["exclusion_id", "artifact_type", "artifact_key", "note"]
    assert digest([list(row) for row in rows]) == digest([columns] * len(rows))
    assert digest_stream(MappingRows(rows)) != digest([list(row) for row in rows])
    assert outcome(lambda: digest_stream(MappingRows([["a", "b"]]))) == ("refused", "json_type")


def test_the_ingest_provenance_ledger_shape_adopts_rows_byte_identically():
    """`ingest_provenance._authority_digest`: `digest({table: [list(row), ...]})`, a drop-in."""
    tables = {"ingest_attested_links": [["enr-1", "imessage:1", "a" * 64, 1], ["enr-2", "imessage:2", "b" * 64, 2]],
              "ingest_enrollments": [["enr-1", "{}", "ds-1", 1, "active", 3, None]],
              "ingest_provenance_events": []}
    assert digest(tables) == digest_stream({name: Rows(iter(rows)) for name, rows in tables.items()})


def test_the_exclusion_floor_shape_adopts_mapping_rows_byte_identically():
    """`exclusion_floor.exclusion_fingerprint`: `digest({"version":..., "rows": [dict, ...]})`.

    It needs `MappingRows`, not `Rows` -- which is the difference between the two ledgers
    that share the 1 MiB pattern, and the reason the CHANGELOG names them separately.
    """
    rows = exclusion_rows(40)
    version = "intelligence-exclusion-floor/v1"
    assert digest({"version": version, "rows": rows}) == digest_stream(
        {"version": version, "rows": MappingRows(iter(rows))})


# --- the depth rule, at each level of the streamed path -------------------------------------------


def encoded_at(depth, rows, kind=Rows):
    return outcome(lambda: canonical._encode_rows(kind(rows), depth, canonical._Sink()))


def test_the_streamed_encoder_keeps_the_depth_rule_at_every_level():
    """Pinned directly, because no store can reach it: both call sites enter at depth 0 or 1.

    Without these three cases the depth arithmetic is unreachable dead code, and a mutant
    that deletes either guard or shifts the cell depth by one survives the whole suite.
    """
    assert encoded_at(MAX_DEPTH, [])[0] == "ok"
    assert encoded_at(MAX_DEPTH + 1, []) == ("refused", "json_depth")
    assert encoded_at(MAX_DEPTH - 1, [[]])[0] == "ok"
    assert encoded_at(MAX_DEPTH, [[]]) == ("refused", "json_depth")
    assert encoded_at(MAX_DEPTH - 2, [["cell"]])[0] == "ok"
    assert encoded_at(MAX_DEPTH - 1, [["cell"]]) == ("refused", "json_depth")
    assert encoded_at(MAX_DEPTH - 1, [{}], kind=MappingRows)[0] == "ok"
    assert encoded_at(MAX_DEPTH, [{}], kind=MappingRows) == ("refused", "json_depth")


class _Text(str):
    pass


class _Number(int):
    pass


@pytest.mark.parametrize("cell", [_Text("x"), _Number(3)], ids=["str_subclass", "int_subclass"])
def test_a_scalar_subclass_is_refused_by_both_encoders(cell):
    """`_cell` matches on the exact type, as `_check` does: a subclass is not the type.

    A `sqlite3` `text_factory` can hand back a `str` subclass, and an `isinstance` spelling
    here would accept and encode one while `canonical_bytes` refused it -- the same value
    digesting under one encoder and refusing under the other.
    """
    rows = [["review-1", "fact-1", cell, 1]]
    assert outcome(lambda: digest_stream(Rows(rows))) == ("refused", "json_type")
    assert outcome(lambda: digest([list(row) for row in rows])) == ("refused", "json_type")


# --- the bounded buffer the helper exists to provide ----------------------------------------------


def test_the_sink_buffer_stays_bounded_while_the_value_does_not(monkeypatch):
    """Bounded memory is the property, not just the absence of a cap; it needs its own test.

    Without it, a `_Sink.put` that never flushes early buffers the entire canonical string
    and still returns the right digest, so the suite cannot tell the two apart.
    """
    peaks = []

    class Watched(canonical._Sink):
        __slots__ = ()

        def put(self, piece):
            peaks.append(sum(len(part) for part in self.parts) + len(piece))
            super().put(piece)

    monkeypatch.setattr(canonical, "_Sink", Watched)
    rows = review_rows(400)
    assert canonical.digest_stream(Rows(iter(rows))) == reference([list(row) for row in rows])
    whole = len(json.dumps([list(row) for row in rows], separators=(",", ":")))
    assert whole > 8 * canonical._CHUNK, "the value has to be much larger than one chunk"
    # The bound is one chunk plus the piece that crossed it -- a row -- and nothing more,
    # whatever the chunk size is. Asserted against the data rather than against a multiple
    # of `_CHUNK`, so that changing the chunk size cannot fail this test on its own.
    longest = max(len(json.dumps(list(row), separators=(",", ":"))) for row in rows) + 2
    assert max(peaks) <= canonical._CHUNK + longest, "the buffer must flush as it goes, not at the end"


def test_the_digest_does_not_depend_on_where_the_chunks_fall(monkeypatch):
    rows = review_rows(30, body=200)
    expected = digest([list(row) for row in rows])
    for chunk in [1, 2, 7, 4096, 1 << 20]:
        monkeypatch.setattr(canonical, "_CHUNK", chunk)
        assert canonical.digest_stream(Rows(iter(rows))) == expected

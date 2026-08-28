"""Migration 72: collapse duplicate derived rows and re-key the survivors.

Two populations of junk, both from the same defect -- a derived row keyed on
when it was written rather than on what it is:

  * twins from ONE write, where two writers minted different ids for the same
    row, one typed and one empty (fixed at the write 2026-08-27; the rows on
    disk stayed);
  * duplicates ACROSS runs, every re-sync appending another full set.

Against a copy of a live node on 2026-08-28 the migration took
``message_entities`` from 50,049 rows to 8,572 -- which is exactly the count of
distinct typed identities in it, verified independently in SQL.

The re-key is the half that is easy to drop and load-bearing: survivors keeping
their random ids would be duplicated again by the very next enrichment pass, so
the repair would last one cycle.
"""

import json
import sqlite3

import pytest

from topos.enrichment.derived_tables import DerivedTablesManager
from topos.storage.db.migrations.derived_row_identity_v1 import collapse_derived_rows
from topos.storage.derived_row_identity import derived_row_id_for

pytestmark = pytest.mark.public


ENTITIES_DDL = """
CREATE TABLE message_entities (
    entity_id TEXT PRIMARY KEY, record_id TEXT, source_id TEXT, entity_text TEXT,
    model TEXT, provider TEXT, payload_json TEXT, created_at TEXT,
    message_id TEXT, spec_version INTEGER)
"""


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(ENTITIES_DDL)
    c.commit()
    return c


def _insert(conn, entity_id, record_id, entity_text, entity_type="PERSON", created_at="2026-01-01"):
    """A row as the old writer left it: random id, payload carrying the truth."""
    conn.execute(
        "INSERT INTO message_entities (entity_id, record_id, entity_text, payload_json, created_at)"
        " VALUES (?,?,?,?,?)",
        (
            entity_id,
            record_id,
            entity_text,
            json.dumps({"record_id": record_id, "entity_text": entity_text, "entity_type": entity_type}),
            created_at,
        ),
    )


def _rows(conn):
    return conn.execute(
        "SELECT entity_id, record_id, entity_text FROM message_entities ORDER BY record_id"
    ).fetchall()


def test_duplicates_across_runs_collapse_to_one_row(conn):
    for i in range(6):
        _insert(conn, f"random-{i}", "rec1", "Alpha")
    conn.commit()

    collapse_derived_rows(conn)
    conn.commit()

    assert len(_rows(conn)) == 1


def test_the_typed_row_survives_its_empty_twin(conn):
    """The twin carries the truth only in its payload; the typed row must win."""
    _insert(conn, "typed", "rec1", "Alpha")
    # the empty twin: the wiki writer knew none of the typed columns
    conn.execute(
        "INSERT INTO message_entities (entity_id, record_id, entity_text, payload_json, created_at)"
        " VALUES (?,?,?,?,?)",
        ("twin", "rec1", None, json.dumps({"record_id": "rec1", "entity_text": "Alpha",
                                           "entity_type": "PERSON"}), "2026-06-01"),
    )
    conn.commit()

    collapse_derived_rows(conn)
    conn.commit()

    rows = _rows(conn)
    assert len(rows) == 1
    # The survivor is the row with content, even though the twin is NEWER.
    assert rows[0][2] == "Alpha"


def test_different_rows_are_not_touched(conn):
    _insert(conn, "a", "rec1", "Alpha")
    _insert(conn, "b", "rec1", "Bravo")
    _insert(conn, "c", "rec2", "Alpha")
    conn.commit()

    collapse_derived_rows(conn)
    conn.commit()

    assert len(_rows(conn)) == 3


def test_an_unidentifiable_row_is_left_completely_alone(conn):
    """Unidentifiable is not the same as duplicate: do not delete, do not re-key."""
    conn.execute(
        "INSERT INTO message_entities (entity_id, record_id, entity_text, payload_json)"
        " VALUES ('keepme', NULL, NULL, NULL)"
    )
    _insert(conn, "x", "rec1", "Alpha")
    _insert(conn, "y", "rec1", "Alpha")
    conn.commit()

    report = collapse_derived_rows(conn)
    conn.commit()

    assert report["message_entities"]["unidentifiable"] == 1
    survivors = {r[0] for r in _rows(conn)}
    assert "keepme" in survivors, "an unidentifiable row was deleted or re-keyed"


def test_survivors_are_re_keyed_to_their_identity(conn):
    _insert(conn, "random-1", "rec1", "Alpha")
    _insert(conn, "random-2", "rec1", "Alpha")
    conn.commit()

    collapse_derived_rows(conn)
    conn.commit()

    entity_id, record_id, entity_text = _rows(conn)[0]
    assert entity_id == derived_row_id_for(
        "message_entities",
        {"record_id": record_id, "entity_text": entity_text, "entity_type": "PERSON"},
    )


def test_a_second_run_changes_nothing(conn):
    for i in range(4):
        _insert(conn, f"random-{i}", "rec1", "Alpha")
    conn.commit()

    collapse_derived_rows(conn)
    conn.commit()
    report = collapse_derived_rows(conn)
    conn.commit()

    assert report["message_entities"]["deleted"] == 0
    assert report["message_entities"]["rekeyed"] == 0


def test_the_repair_survives_the_next_enrichment_pass(conn):
    """The whole point of re-keying, and the thing a delete-only fix loses.

    Survivors that kept a random id would be duplicated by the very next pass:
    the writer computes the stable id, finds no conflict, and inserts. This is
    the assertion that says the repair is permanent rather than one cycle long.
    """
    for i in range(5):
        _insert(conn, f"random-{i}", "rec1", "Alpha")
    conn.commit()
    collapse_derived_rows(conn)
    conn.commit()
    before = len(_rows(conn))

    writer = DerivedTablesManager.__new__(DerivedTablesManager)
    writer.conn = conn
    # exactly what the entities job re-emits for that record
    writer._write_entities_batch(
        [{"record_id": "rec1", "entity_text": "Alpha", "entity_type": "PERSON"}], 100
    )
    conn.commit()

    assert len(_rows(conn)) == before, "the next pass duplicated the repaired row"


def test_a_missing_table_is_not_an_error():
    """Nodes that never ran enrichment have none of these tables."""
    empty = sqlite3.connect(":memory:")

    report = collapse_derived_rows(empty)

    assert report["message_entities"]["scanned"] == 0

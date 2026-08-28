"""One enrichment record must produce one derived row, not two.

Two writers persist ``message_entities`` / ``message_topics`` /
``message_sentiment`` / ``user_goals`` from the SAME batch of record dicts:
``DerivedTablesManager`` (which knows the typed columns) and
``job_writer._write_wiki_table`` (which adds provenance and ``spec_version``).
Both resolved the row id independently as ``record.get(<id_field>) or uuid4()``,
so every record produced two rows under two ids — one typed, one with the typed
column left NULL because the second writer does not know about it.

Measured on the live node 2026-08-27: 25,146 typed ``message_entities`` rows and
25,128 untyped twins over the same 4,924 records, 4,919 of them pairing exactly
1:1; ``user_goals`` was exactly half real. Every derived-row count in the product
read 2x reality, which is why "154 fabricated goals" was really 77 written twice.

Both halves are gated here:

  * the id is minted once and shared, so the second write upserts;
  * a record the typed writer SKIPPED (empty ``entity_text`` / ``goal_text``) gets
    no bare provenance-only row either.

The row-count assertions are the point. A future change that reintroduces an
independent id in either writer doubles them and turns this file red.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.enrichment.derived_tables import DerivedTablesManager, _stable_row_id
from topos.enrichment.job_writer import _write_wiki_table

CASES = [
    # job, table, id_field, typed_column, a record the typed writer accepts
    (
        "entities",
        "message_entities",
        "entity_id",
        "entity_text",
        {"record_id": "rec-1", "source_id": "s", "entity_text": "Topos", "model": "m"},
    ),
    (
        "goal_extraction",
        "user_goals",
        "goal_id",
        "goal_text",
        {"record_id": "rec-1", "source_id": "s", "goal_text": "Ship the node", "model": "m"},
    ),
    (
        "topics",
        "message_topics",
        "topic_id",
        "topic",
        {"record_id": "rec-1", "source_id": "s", "topic": "deployment", "model": "m"},
    ),
    (
        # message_sentiment has no typed column of its own in the live schema —
        # the label lives in payload_json — so only the row count is asserted.
        "sentiment",
        "message_sentiment",
        "sentiment_id",
        None,
        {"record_id": "rec-1", "source_id": "s", "label": "positive", "model": "m"},
    ),
]


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "dw.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _both_writers(conn, job, table, id_field, records):
    """Run the two writers in the order ``_write_signal_records_unlocked`` does."""
    DerivedTablesManager(conn).write_enrichment_batch(records, table)
    for rec in records:
        _write_wiki_table(
            conn,
            table,
            rec,
            id_field=id_field,
            provenance={"job_id": job, "spec_version": 1},
            typed_writer_ran=True,
        )
    conn.commit()


@pytest.mark.parametrize("job,table,id_field,typed_col,record", CASES, ids=[c[0] for c in CASES])
def test_one_record_writes_exactly_one_row(conn, job, table, id_field, typed_col, record):
    _both_writers(conn, job, table, id_field, [dict(record)])

    assert _count(conn, table) == 1, (
        f"{table}: both writers persisted the same record — the second must upsert, not insert"
    )


@pytest.mark.parametrize("job,table,id_field,typed_col,record", CASES, ids=[c[0] for c in CASES])
def test_the_surviving_row_keeps_its_typed_columns(conn, job, table, id_field, typed_col, record):
    """The upsert must not blank what the typed writer wrote.

    ``message_sentiment`` has no typed column of its own in the live schema — the
    label lives in ``payload_json`` — so that case asserts the payload instead of
    skipping. A skipped case is a hole in the gate.
    """
    _both_writers(conn, job, table, id_field, [dict(record)])

    if typed_col is not None:
        value = conn.execute(f"SELECT {typed_col} FROM {table}").fetchone()[0]
        assert value == record[typed_col]
        return

    payload = conn.execute(f"SELECT payload_json FROM {table}").fetchone()[0]
    carried = [v for k, v in record.items() if k not in {"record_id", "source_id", "model"}]
    assert carried, "fixture must carry something distinctive"
    for value in carried:
        assert str(value) in str(payload), (
            f"{table}: the surviving row lost {value!r} from its payload"
        )


@pytest.mark.parametrize("job,table,id_field,typed_col,record", CASES, ids=[c[0] for c in CASES])
def test_the_surviving_row_carries_provenance(conn, job, table, id_field, typed_col, record):
    """...and the second writer's contribution must still land.

    Without this, "make it one row" could be satisfied by deleting the provenance
    write, which is a different regression.
    """
    _both_writers(conn, job, table, id_field, [dict(record)])

    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if "spec_version" not in cols:
        pytest.skip(f"{table} has no spec_version column")
    assert conn.execute(f"SELECT spec_version FROM {table}").fetchone()[0] is not None


@pytest.mark.parametrize("job,table,id_field", [(c[0], c[1], c[2]) for c in CASES])
def test_a_record_the_typed_writer_skipped_gets_no_bare_row(conn, job, table, id_field):
    """The other half: no provenance-only row with a NULL typed column.

    ``_write_entities_batch`` and ``_write_goals_batch`` skip a record whose typed
    field is empty. The provenance writer used to emit a row for it anyway.
    """
    empty = {"record_id": "rec-empty", "source_id": "s", "model": "m"}

    _both_writers(conn, job, table, id_field, [dict(empty)])

    rows = _count(conn, table)
    assert rows <= 1, f"{table}: a skipped record produced {rows} rows"
    if rows == 1:
        # Topics/sentiment accept a record with no typed field; if a row exists it
        # must be the typed writer's, i.e. it must carry a stamped id.
        assert conn.execute(f"SELECT {id_field} FROM {table}").fetchone()[0]


def test_the_id_is_stamped_back_onto_the_record():
    """The mechanism itself, in isolation.

    Sharing the id depends on both writers seeing the same dict. If a future
    refactor copies records between the writers this test still passes while the
    row-count tests fail — which is the signal that the dicts stopped being shared.
    """
    record = {"record_id": "r"}

    first = _stable_row_id(record, "entity_id")
    second = _stable_row_id(record, "entity_id")

    assert first == second
    assert record["entity_id"] == first


def test_provenance_writer_still_works_with_no_typed_writer(conn):
    """When ``tables_manager`` is absent this is the only writer — keep inserting.

    Skipping unstamped records is correct only *because* the typed writer ran and
    declined them. With no typed writer there is nothing to defer to, and dropping
    the row would lose the data outright.
    """
    wrote = _write_wiki_table(
        conn,
        "message_entities",
        {"record_id": "rec-solo", "source_id": "s"},
        id_field="entity_id",
        provenance={"job_id": "entities", "spec_version": 1},
        typed_writer_ran=False,
    )
    conn.commit()

    assert wrote is True
    assert _count(conn, "message_entities") == 1

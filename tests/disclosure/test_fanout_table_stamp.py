"""A canonical record's own table declaration must beat the batch's group default.

``run_post_canonical_pipeline`` filled in the table marker with
``rec.setdefault("_table", <group default>)``, which only consults ``_table``. A
fan-out child declares ``canonical_table='location_events'`` and leaves ``_table``
unset, so it was overwritten with ``journal_entries`` — the group its PARENT
belongs to. Five readers resolve the table as ``_table or canonical_table``, so
the wrong value won.

On the owner's node 2026-08-27 that one omission put all 362 place rows in the
wrong table for the PII disclosure write (addressed to journal_entries by a
location id, matching zero rows while reporting success), the grant bound on
entity mentions, the embedding dimension (360 rows filed as `wellbeing`, leaving
the shipped `places` dimension empty), the belief-role gate that should have
declined to extract goals from a bare place name, and the journal category
histogram (28% over-reported).

**The ordering trap is gated here too.** Those children are redacted today ONLY
because they are misfiled as journal entries: ``fields_for_table('journal_entries')``
is ``("content",)`` and the child's content is its place name. Correcting the
stamp without first declaring `location_events`' own fields would have turned 138
masked embeddings back into raw home and gym addresses. The
``test_correcting_the_stamp_does_not_unredact`` case fails if anyone removes
``content`` from that declaration.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.disclosure.canonical_writer import upsert_disclosure_fields
from topos.disclosure.field_registry import (
    CANONICAL_ID_COLUMN,
    PII_DISCLOSURE_FIELDS,
    canonical_table_for_message,
    disclosure_column,
    disclosure_hash_column,
    fields_for_table,
    stamp_canonical_table,
)
from topos.enrichment.jobs.canonical.brief_fallback import prepare_signal_record
from topos.ingestion.journal_location_fanout import (
    journal_location_event_from_entry,
    journal_location_signal_record,
)

PARENT = {
    "entry_id": "tl-1",
    "place_name": "Northgate- The Foundry",
    "starts_at": "2026-07-06T19:05:00",
    "category": "Topos",
}


def _child_signal_record():
    loc = journal_location_event_from_entry(PARENT, source_id="grow_journal")
    assert loc is not None
    return prepare_signal_record(journal_location_signal_record(loc))


# ------------------------------------------------------------- the stamp itself


def test_child_keeps_its_own_table_against_the_group_default():
    rec = _child_signal_record()
    assert rec.get("canonical_table") == "location_events"
    assert rec.get("_table") is None, "fixture must reproduce the unstamped shape"

    stamp_canonical_table([rec], source_group="journal")

    assert rec["_table"] == "location_events", (
        "the group default (journal_entries) overwrote the child's own declaration"
    )


def test_a_record_with_no_declaration_still_gets_the_group_default():
    """Control: the fallback must survive. Most records rely on it."""
    rec = {"record_id": "tl-2", "content": "an ordinary journal entry"}

    stamp_canonical_table([rec], source_group="journal")

    assert rec["_table"] == "journal_entries"


def test_an_explicit_table_is_never_overwritten():
    rec = {"record_id": "x", "_table": "activity_events", "canonical_table": "journal_entries"}

    stamp_canonical_table([rec], source_group="journal")

    assert rec["_table"] == "activity_events"


def test_unknown_group_leaves_undeclared_records_alone():
    rec = {"record_id": "x"}

    stamp_canonical_table([rec], source_group="not-a-group")

    assert "_table" not in rec


def test_the_five_readers_agree_after_stamping():
    """``_table or canonical_table`` must now resolve the same either way."""
    rec = _child_signal_record()
    stamp_canonical_table([rec], source_group="journal")

    assert (rec.get("_table") or rec.get("canonical_table")) == "location_events"
    assert canonical_table_for_message(rec, source_group="journal") == "location_events"


# ------------------------------------------------- the consequence that matters most


def test_the_stamp_restores_the_belief_role_gate():
    """Correcting the table is what stops the fabricated goals.

    ``GoalExtractionJob`` already refuses to mint goals from anything that is not
    owner-authored, and it reads ``_table`` ONLY. Misfiled as a journal entry, a
    machine-generated place string resolved ``authored`` and went to the LLM —
    which is where "Watch Northgate- The Foundry" and "Seeking information about
    the book 'The Foundry' by Northgate" came from. Stamped correctly it resolves
    ``ambient`` and never reaches extraction.

    So the first line of defence was never missing, only misinformed. A
    minimum-content gate is the second one, and it does not make this test
    redundant: the two catch different populations.
    """
    from topos.features.provenance.roles import ROLE_AUTHORED, record_role

    rec = _child_signal_record()

    misfiled = record_role(
        dict(rec, _table="journal_entries"), table="journal_entries", posture="mixed"
    )
    assert misfiled == ROLE_AUTHORED, "fixture must reproduce the shipped mis-stamp"

    stamp_canonical_table([rec], source_group="journal")
    corrected = record_role(rec, table=rec["_table"], posture="mixed")

    assert corrected != ROLE_AUTHORED, (
        "a fan-out place child must not be belief-grade; it is not the owner's writing"
    )


# ------------------------------------------------- the ordering trap (do not remove)


def test_correcting_the_stamp_does_not_unredact_the_child():
    """The child's embedded text must still be a declared disclosure field.

    ``journal_location_signal_record`` sets BOTH ``place_name`` (the column) and
    ``content`` (the copy that is embedded, FTS-indexed and fed to extraction).
    Under the old wrong stamp, ``("content",)`` from journal_entries is what
    redacted it. If ``location_events`` does not declare ``content``, fixing the
    stamp silently unredacts every place embedding.
    """
    rec = _child_signal_record()
    stamp_canonical_table([rec], source_group="journal")

    declared = fields_for_table(rec["_table"])

    assert "content" in declared, (
        "location_events must declare `content`: it is the field that gets "
        "embedded, and dropping it re-exposes raw home addresses"
    )
    assert "place_name" in declared
    for field in ("content", "place_name"):
        assert isinstance(rec.get(field), str) and rec[field].strip(), (
            f"the child carries {field}, so it must be redactable"
        )


# ------------------------------------------------------- the disclosure write lands


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "disc.db"))
    apply_all_migrations(c)
    c.execute(
        "INSERT INTO location_events (event_id, place_name, source_id, source_record_id)"
        " VALUES (?,?,?,?)",
        ("tl-1-loc", "Northgate- The Foundry", "grow_journal", "tl-1"),
    )
    c.commit()
    yield c
    c.close()


def test_disclosure_write_lands_on_the_location_row(conn):
    """Previously this UPDATE went to journal_entries by a location id: 0 rows, True."""
    wrote = upsert_disclosure_fields(
        conn,
        "location_events",
        "tl-1-loc",
        {
            disclosure_column("place_name"): "[ADDRESS]",
            disclosure_hash_column("place_name"): "deadbeef",
        },
        model_id="openai/privacy-filter",
    )
    conn.commit()

    assert wrote is True
    row = conn.execute(
        "SELECT place_name_disclosure, place_name_disclosure_hash, place_name_disclosure_model"
        " FROM location_events WHERE event_id=?",
        ("tl-1-loc",),
    ).fetchone()
    assert row == ("[ADDRESS]", "deadbeef", "openai/privacy-filter")


def test_a_write_addressed_to_the_wrong_table_changes_nothing(conn):
    """The old behaviour, pinned as a non-regression.

    Nothing should ever address a location id to journal_entries again, but if it
    does, it must not silently succeed against some other row.
    """
    conn.execute(
        "INSERT INTO journal_entries (entry_id, content, source_id) VALUES (?,?,?)",
        ("tl-1", "Worked at the Convent", "grow_journal"),
    )
    conn.commit()

    upsert_disclosure_fields(
        conn, "journal_entries", "tl-1-loc", {"content_disclosure": "[ADDRESS]"}
    )
    conn.commit()

    assert (
        conn.execute(
            "SELECT content_disclosure FROM journal_entries WHERE entry_id=?", ("tl-1",)
        ).fetchone()[0]
        is None
    )


# ----------------------------------------------------------- structural invariants


def test_every_disclosed_table_has_an_id_column_declared():
    """A table in one map and not the other is a silent no-op writer."""
    missing = sorted(set(PII_DISCLOSURE_FIELDS) - set(CANONICAL_ID_COLUMN))
    assert missing == [], f"declared for disclosure but with no id column: {missing}"


def test_every_disclosed_field_that_is_a_column_has_a_disclosure_column(conn):
    """Catches a field declared for redaction whose result has nowhere to go."""
    gaps = []
    for table, fields in PII_DISCLOSURE_FIELDS.items():
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:
            continue
        for field in fields:
            # A declared field need not be a column — it may exist only on the
            # in-flight record (location_events' `content`). But if the RAW field
            # is a column, its disclosure column must exist too.
            if field in cols and disclosure_column(field) not in cols:
                gaps.append(f"{table}.{field}")
    assert gaps == [], f"raw column with no disclosure column: {gaps}"

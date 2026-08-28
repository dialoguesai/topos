"""A derived row's id is what the row IS, so re-deriving it replaces it.

The writers keyed each row on a fresh ``uuid4()`` and the jobs build fresh dicts
every pass, so the same fact re-derived later was INSERTed beside its
predecessor -- ``INSERT OR REPLACE`` never conflicted because the id was always
new. Measured against the writer in isolation on 2026-08-28: the same 5 records
written three times produced 5, then 10, then 15 rows.

That is what made a ``spec_version`` bump dangerous rather than routine. The bump
exists to mark every record stale so it re-derives; under the old id that is an
instruction to duplicate the table on every node that upgrades.

These pin both halves of the fix -- that a rewrite REPLACES, and (the half that
is easy to lose) that rows which are genuinely different stay different.
"""

import sqlite3

import pytest

from topos.enrichment.derived_tables import DerivedTablesManager
from topos.storage.derived_row_identity import (
    DERIVED_ROW_NAMESPACE,
    derived_row_id,
    derived_row_id_for,
)

pytestmark = pytest.mark.public


ENTITIES_DDL = """
CREATE TABLE message_entities (
    entity_id TEXT PRIMARY KEY, record_id TEXT, source_id TEXT, entity_text TEXT,
    model TEXT, provider TEXT, payload_json TEXT, created_at TEXT,
    message_id TEXT, spec_version INTEGER)
"""


@pytest.fixture()
def writer():
    conn = sqlite3.connect(":memory:")
    conn.execute(ENTITIES_DDL)
    conn.commit()
    manager = DerivedTablesManager.__new__(DerivedTablesManager)
    manager.conn = conn
    return manager


def _records(n=5):
    """Fresh dicts, as the entities job builds them on every pass -- no id."""
    return [
        {
            "record_id": f"rec{i}",
            "entity_text": f"Ent{i}",
            "entity_type": "PERSON",
            "source_id": "imessage",
        }
        for i in range(n)
    ]


def _count(writer):
    return writer.conn.execute("SELECT COUNT(*) FROM message_entities").fetchone()[0]


def test_re_deriving_the_same_records_replaces_them(writer):
    """The regression itself: this went 5 -> 10 -> 15 before the fix."""
    counts = []
    for _ in range(3):
        writer._write_entities_batch(_records(), 100)
        writer.conn.commit()
        counts.append(_count(writer))

    assert counts == [5, 5, 5], f"re-derivation is duplicating rows: {counts}"


def test_two_entities_in_one_record_stay_two_rows(writer):
    """Idempotence must not become collapse -- a record names many entities."""
    writer._write_entities_batch(
        [
            {"record_id": "r1", "entity_text": "Alpha", "entity_type": "PERSON"},
            {"record_id": "r1", "entity_text": "Bravo", "entity_type": "PERSON"},
        ],
        100,
    )
    writer.conn.commit()

    assert _count(writer) == 2


def test_case_is_not_folded(writer):
    """"Apple" and "apple" may be one company and one fruit.

    Merging them here would be a data loss no later pass could undo, so the
    identity compares text exactly and leaves the extractor's answer alone.
    """
    writer._write_entities_batch(
        [
            {"record_id": "r1", "entity_text": "Apple", "entity_type": "ORG"},
            {"record_id": "r1", "entity_text": "apple", "entity_type": "ORG"},
        ],
        100,
    )
    writer.conn.commit()

    assert _count(writer) == 2


def test_the_same_surface_under_two_types_stays_two_rows(writer):
    writer._write_entities_batch(
        [
            {"record_id": "r1", "entity_text": "Jordan", "entity_type": "PERSON"},
            {"record_id": "r1", "entity_text": "Jordan", "entity_type": "GPE"},
        ],
        100,
    )
    writer.conn.commit()

    assert _count(writer) == 2


def test_an_untyped_mention_is_still_identifiable(writer):
    """The NER type mapper returns None for types it does not cover."""
    for _ in range(2):
        writer._write_entities_batch(
            [{"record_id": "r1", "entity_text": "Something", "entity_type": None}], 100
        )
        writer.conn.commit()

    assert _count(writer) == 1


class TestIdentityRefusesToGuess:
    """A missing identity field must DUPLICATE, never merge.

    A first draft of the identity map declared ``("record_id", "label")`` for
    ``message_emotions``. Both resolve to None on a real emotions record --
    which says ``message_id`` and ``emotion_label`` -- so every emotion row in
    the database would have collapsed onto one id and the table would have
    ended up holding a single row. A duplicate row is recoverable; a merge is
    not, so an unidentifiable row keeps its per-run uuid instead.
    """

    def test_the_emotions_record_shape_resolves(self):
        """The near-miss, pinned: these are the keys the emotions job emits."""
        assert derived_row_id_for(
            "message_emotions", {"message_id": "m1", "emotion_label": "joy"}
        ) is not None

    def test_the_field_names_come_from_the_producing_jobs(self):
        """Each job's own vocabulary, not one this module wished they shared."""
        assert derived_row_id_for("message_entities", {"record_id": "r", "entity_text": "E"})
        assert derived_row_id_for("message_entities", {"message_id": "r", "text": "E"})
        assert derived_row_id_for("user_goals", {"record_id": "r", "goal_text": "G"})
        assert derived_row_id_for("message_topics", {"record_id": "r", "topic": "T"})
        assert derived_row_id_for("message_sentiment", {"record_id": "r", "label": "pos"})

    @pytest.mark.parametrize(
        "row",
        [
            {"entity_text": "X"},                      # no record id at all
            {"record_id": "r"},                        # no surface
            {"record_id": "   ", "entity_text": "X"},  # blank is not an identity
            {"record_id": "r", "entity_text": ""},
        ],
    )
    def test_a_missing_required_field_yields_no_id(self, row):
        assert derived_row_id_for("message_entities", row) is None

    def test_an_unidentifiable_row_duplicates_rather_than_merging(self, writer):
        """Two different mentions with no record_id must not become one row."""
        writer._write_entities_batch(
            [
                {"entity_text": "Alpha", "record_id": None},
                {"entity_text": "Bravo", "record_id": None},
            ],
            100,
        )
        writer.conn.commit()

        # Both are dropped or both are kept separately -- never merged into one.
        assert _count(writer) != 1

    def test_an_unknown_table_has_no_identity(self):
        assert derived_row_id_for("some_other_table", {"record_id": "r"}) is None


def test_the_namespace_is_frozen():
    """Every id in every node's database derives from this constant.

    Changing it re-keys the whole derived layer, so the next write of every row
    would find no conflict and duplicate it once more -- the exact bug this
    module exists to close, re-introduced across the entire fleet at once.
    """
    assert str(DERIVED_ROW_NAMESPACE) == "6f1d4c4e-6a1f-5b2e-9c3a-1d5e7f9b2c40"
    assert derived_row_id("message_entities", ("rec1", "Ent1", "PERSON")) == (
        "f74891b4-b998-5694-968c-be109c197c4b"
    )


def test_none_and_empty_string_are_different_identities():
    """An entity whose type failed to map must not collide with the string "None"."""
    assert derived_row_id("message_entities", ("r", "E", None)) != derived_row_id(
        "message_entities", ("r", "E", "")
    )
    assert derived_row_id("message_entities", ("r", "E", None)) != derived_row_id(
        "message_entities", ("r", "E", "None")
    )


def test_the_separator_cannot_be_forged():
    """("a", "b") and ("a\\x1fb", "") must not hash to the same id."""
    assert derived_row_id("message_topics", ("a", "b")) != derived_row_id(
        "message_topics", ("a\x1fb", "")
    )

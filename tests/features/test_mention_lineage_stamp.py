"""A mention that names no table is refused, not written.

``entity_mentions.canonical_table`` is the lineage a table-scoped read, a
table purge, a disclosure sweep and a per-record Off-limits exclusion all
travel along. Measured on a quarantined copy of a live node 2026-09-17:
17,203 of 33,286 mentions carried no stamp, every one written AFTER the
stamp-recovery migration had run — so a live writer still did not stamp, and
no backfill could ever catch up with it. The fix is at the writer:
``EntityResolver.record_mention`` raises :class:`MentionLineageError` rather
than writing an unstamped row, and the table is derived from the record kind
the way the embedding context and the dimension map derive theirs — never
from the shape of an id.

**Mutation guard.** Reverting ``require_canonical_table`` inside
``record_mention`` fails ``TestRecordMentionRefuses`` here (the refusal
tests) and ``test_mutation_guard_no_unstamped_mention_survives_a_write`` in
``tests/enrichment/test_entities_job_lineage.py`` (the job-level assertion
that nothing unstamped reaches disk). Verified by reverting it: 2026-09-17.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.mention_lineage import (
    CANONICAL_ID_COLUMNS,
    CANONICAL_TABLE_BY_RECORD_KIND,
    MentionLineageError,
    canonical_table_for_record,
    require_canonical_table,
)
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "lineage.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _stamps(conn):
    return [r[0] for r in conn.execute("SELECT canonical_table FROM entity_mentions").fetchall()]


def _mention_count(conn, entity_id):
    return conn.execute(
        "SELECT mention_count FROM entities WHERE entity_id=?", (entity_id,)
    ).fetchone()[0]


# ------------------------------------------------------- deriving the table


class TestTableDerivation:
    def test_an_explicit_table_wins(self):
        assert (
            canonical_table_for_record(
                {"_table": "conversation_messages", "canonical_table": "journal_entries"}
            )
            == "conversation_messages"
        )

    def test_the_records_own_declaration_beats_its_kind(self):
        # The journal→location fan-out child: declares location_events, is a
        # journal record by origin. The declaration is the table.
        assert (
            canonical_table_for_record(
                {"canonical_table": "location_events", "record_type": "journal_entry"}
            )
            == "location_events"
        )

    @pytest.mark.parametrize("kind,table", sorted(CANONICAL_TABLE_BY_RECORD_KIND.items()))
    def test_a_record_kind_maps_to_its_table(self, kind, table):
        assert canonical_table_for_record({"record_type": kind}) == table
        assert canonical_table_for_record({}, record_type=kind) == table
        assert table in CANONICAL_ID_COLUMNS, "every mapped table has an id column"

    @pytest.mark.parametrize("table", sorted(CANONICAL_ID_COLUMNS))
    def test_a_table_name_maps_to_itself(self, table):
        assert canonical_table_for_record({"_table": table}) == table
        assert canonical_table_for_record({"canonical_table": table.upper()}) == table

    def test_nothing_is_derived_from_the_shape_of_an_id(self):
        # event_id keys four tables; message_id keys two. An id is not a stamp.
        assert canonical_table_for_record({"event_id": "ev-1", "content": "x"}) is None
        assert canonical_table_for_record({"message_id": "m-1", "content": "x"}) is None

    def test_an_unknown_kind_derives_nothing(self):
        assert canonical_table_for_record({"record_type": "browser_visit"}) is None
        assert canonical_table_for_record({"_table": "timeline"}) is None
        assert canonical_table_for_record(None) is None
        assert canonical_table_for_record("conversation_messages") is None


class TestRequireCanonicalTable:
    def test_a_known_table_passes_through(self):
        assert require_canonical_table("journal_entries") == "journal_entries"

    def test_a_singular_kind_is_normalized_to_its_table(self):
        assert require_canonical_table("conversation_message") == "conversation_messages"
        assert require_canonical_table(" AI_CHAT_MESSAGES ") == "ai_chat_messages"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_an_empty_stamp_is_refused(self, value):
        with pytest.raises(MentionLineageError, match="no canonical_table"):
            require_canonical_table(value)

    def test_an_unknown_table_is_refused_by_name(self):
        with pytest.raises(MentionLineageError, match="'timeline'"):
            require_canonical_table("timeline")


# ------------------------------------------------------------ the writer


class TestRecordMentionRefuses:
    """The stamp requirement at the one INSERT into entity_mentions."""

    def test_a_mention_without_a_table_is_refused_and_nothing_is_written(self, conn):
        resolver = EntityResolver(conn)
        entity_id, _ = resolver.resolve("Ada Voss", entity_type="person", record_id="m1")
        conn.commit()

        with pytest.raises(MentionLineageError):
            resolver.record_mention(entity_id, record_id="m1", surface_text="Ada Voss")

        assert _stamps(conn) == []
        assert _mention_count(conn, entity_id) == 0, "the count must not move for a refused row"

    def test_an_unknown_table_is_refused(self, conn):
        resolver = EntityResolver(conn)
        entity_id, _ = resolver.resolve("Ada Voss", entity_type="person", record_id="m1")
        with pytest.raises(MentionLineageError):
            resolver.record_mention(
                entity_id, record_id="m1", surface_text="Ada Voss", canonical_table="timeline"
            )
        assert _stamps(conn) == []

    def test_a_stamped_mention_is_written_with_its_stamp(self, conn):
        resolver = EntityResolver(conn)
        entity_id, _ = resolver.resolve("Ada Voss", entity_type="person", record_id="m1")
        resolver.record_mention(
            entity_id,
            record_id="m1",
            surface_text="Ada Voss",
            canonical_table="conversation_messages",
        )
        conn.commit()
        assert _stamps(conn) == ["conversation_messages"]
        assert _mention_count(conn, entity_id) == 1

    def test_a_singular_kind_is_stored_as_the_table(self, conn):
        resolver = EntityResolver(conn)
        entity_id, _ = resolver.resolve("Plurigrid", entity_type="org", record_id="j1")
        resolver.record_mention(
            entity_id, record_id="j1", surface_text="Plurigrid", canonical_table="journal_entry"
        )
        conn.commit()
        assert _stamps(conn) == ["journal_entries"]

    def test_every_mention_on_disk_carries_a_stamp(self, conn):
        """Row-level form of the guarantee: after any number of writes, zero
        rows have an empty canonical_table. This is the property the D8
        measurement reads (`stamp coverage`), stated as an invariant."""
        resolver = EntityResolver(conn)
        person, _ = resolver.resolve("Ada Voss", entity_type="person", record_id="m1")
        org, _ = resolver.resolve("Plurigrid", entity_type="org", record_id="m1")
        for table, rid in (("conversation_messages", "m1"), ("journal_entries", "j1"),
                           ("ai_chat_messages", "a1"), ("activity_events", "ev1")):
            resolver.record_mention(person, record_id=rid, surface_text="Ada Voss", canonical_table=table)
            resolver.record_mention(org, record_id=rid, surface_text="Plurigrid", canonical_table=table)
        with pytest.raises(MentionLineageError):
            resolver.record_mention(person, record_id="m2", surface_text="Ada Voss")
        conn.commit()
        unstamped = conn.execute(
            "SELECT COUNT(*) FROM entity_mentions WHERE COALESCE(canonical_table,'')=''"
        ).fetchone()[0]
        assert unstamped == 0
        assert conn.execute("SELECT COUNT(*) FROM entity_mentions").fetchone()[0] == 8


# --------------------------------------------------- the structured writer


class TestStructuredFieldMentions:
    """The second writer of entity_mentions derives its table the same way."""

    def _journal(self, **extra):
        row = {
            "entry_id": "tl-1",
            "record_id": "tl-1",
            "source_id": "grow_journal",
            "content": "morning run",
            "place_name": "Mill Pond",
            "event_at": "2026-06-01T08:00:00Z",
        }
        row.update(extra)
        return row

    def test_a_declared_column_writes_a_stamped_mention(self, conn):
        from topos.features.entities.structured_fields import record_structured_mentions

        resolver = EntityResolver(conn)
        by_record = record_structured_mentions(conn, resolver, [self._journal(_table="journal_entries")])
        conn.commit()
        assert list(by_record) == ["tl-1"]
        assert _stamps(conn) == ["journal_entries"]

    def test_the_table_is_derived_from_the_record_kind_when_declared_that_way(self, conn):
        from topos.features.entities.structured_fields import record_structured_mentions

        resolver = EntityResolver(conn)
        record_structured_mentions(conn, resolver, [self._journal(record_type="journal_entry")])
        conn.commit()
        assert _stamps(conn) == ["journal_entries"]

    def test_a_record_that_names_no_table_writes_nothing(self, conn):
        from topos.features.entities.structured_fields import record_structured_mentions

        resolver = EntityResolver(conn)
        by_record = record_structured_mentions(conn, resolver, [self._journal()])
        conn.commit()
        assert by_record == {}
        assert _stamps(conn) == []

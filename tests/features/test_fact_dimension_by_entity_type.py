"""A fact is filed by what it is about, not by which job wrote it.

The entities job stamped ``dimension="relationships"`` on every fact it wrote
while carrying the entity's own type in the same dict literal, two lines below.
Same signature as the visit counts: the correct value already in hand, and the
column that a query reads set to a constant.

Measured on the owner's node 2026-08-27 over the 32,293 facts filed under
"relationships": 10,924 ORG, 3,932 PERSON, 2,704 DATE, 2,088 GPE, 1,253 LOC.
Resolved against the entity spine, **4,769 of 24,340 typed facts — under a
fifth — are about an actual person**. ``get_by_dimension`` is a live API filter
(``signal_list_facts``), so this is read at decision time: four-fifths of what
the relationships filter returns is not a relationship.

Re-deriving from the stored ``entity_type`` redistributes the live table:

    relationships   32,293 ->  9,065     time        0 -> 4,610
    work             2,253 -> 14,835     places     13 -> 4,001
    interests        3,433 ->  5,388     resources   0 ->    93

``places`` holding 13 rows was the tell. The node has 362 location events and
4,001 place-typed entity facts, and the places dimension was empty because every
one of them was filed as a relationship.

Some types are deliberately unmapped — CARDINAL, ORDINAL, QUANTITY, PERCENT,
MISC, NORP, LAW. A bare number is not an entity in any dimension, and NORP is
genuinely ambiguous. Assigning them a dimension would move noise between filters
and call it a fix, so they fall back to the record's own dimension.
"""

from __future__ import annotations

import pytest

from topos.features.signal.dimension_registry import (
    SIGNAL_DIMENSION_IDS_SET,
    _DIMENSION_BY_ENTITY_TYPE,
    dimension_for_entity_type,
)


# ------------------------------------------------------------------ the map


@pytest.mark.parametrize(
    "entity_type,expected",
    [
        ("PERSON", "relationships"),
        ("PER", "relationships"),
        ("GPE", "places"),
        ("LOC", "places"),
        ("FAC", "places"),
        ("ORG", "work"),
        ("PRODUCT", "interests"),
        ("WORK_OF_ART", "interests"),
        ("DATE", "time"),
        ("TIME", "time"),
        ("EVENT", "time"),
        ("MONEY", "resources"),
    ],
)
def test_the_ner_labels_map(entity_type, expected):
    assert dimension_for_entity_type(entity_type) == expected


@pytest.mark.parametrize(
    "entity_type,expected",
    [
        ("person", "relationships"),
        ("place", "places"),
        ("org", "work"),
        ("project", "work"),
        ("topic", "interests"),
        ("product", "interests"),
        ("work_of_art", "interests"),
        ("event", "time"),
    ],
)
def test_the_spine_types_map(entity_type, expected):
    """Both label families are real and both arrive here.

    The extractor emits OntoNotes tags, the resolver emits spine types. One
    table serves both, so a fact is filed the same way whichever produced it.
    """
    assert dimension_for_entity_type(entity_type) == expected


def test_matching_is_case_insensitive():
    assert dimension_for_entity_type("Org") == dimension_for_entity_type("ORG") == "work"


def test_every_mapped_dimension_is_in_the_registry():
    """A dimension outside the vocabulary is invisible to every filter.

    ``network_bridge`` is live on 3 facts and is not a registry dimension —
    exactly the drift this pins shut.
    """
    unknown = set(_DIMENSION_BY_ENTITY_TYPE.values()) - SIGNAL_DIMENSION_IDS_SET
    assert unknown == set(), f"not signal dimensions: {sorted(unknown)}"


# ------------------------------------- what is deliberately NOT mapped


@pytest.mark.parametrize(
    "entity_type", ["CARDINAL", "ORDINAL", "QUANTITY", "PERCENT", "MISC", "NORP", "LAW"]
)
def test_an_unmappable_type_falls_back_rather_than_guessing(entity_type):
    """3,396 live facts. A bare number is not an entity in any dimension.

    Giving these a plausible-looking home would move noise from one filter to
    another and report it as a fix.
    """
    assert dimension_for_entity_type(entity_type, fallback="memory") == "memory"


def test_a_missing_type_falls_back():
    for missing in (None, "", "   ", "not_a_type"):
        assert dimension_for_entity_type(missing, fallback="memory") == "memory"


# ------------------------------------------------ the job actually uses it


class _Signal:
    def __init__(self):
        self.facts = []

    def put_fact(self, fact):
        self.facts.append(fact)

    def put_score(self, score):
        pass


class _Adapters:
    def __init__(self):
        self.signal = _Signal()
        self.graph = type("G", (), {"upsert_node": lambda s, n: n.get("node_id"),
                                    "upsert_edge": lambda s, e: "e"})()
        self.vector = None


def _facts(records):
    from topos.enrichment.job_writer import _write_signal_records_unlocked

    adapters = _Adapters()
    _write_signal_records_unlocked(
        "entities", records, adapters=adapters, tables_manager=None, conn=None
    )
    return adapters.signal.facts


def test_an_org_mention_is_not_filed_as_a_relationship():
    """10,924 live facts took this path."""
    facts = _facts([
        {"entity_text": "Anthropic", "entity_type": "ORG",
         "record_id": "m1", "source_id": "imessage"}
    ])

    assert facts[0]["dimension"] == "work"


def test_a_place_mention_reaches_the_places_dimension():
    """The dimension held 13 rows while 4,001 place facts sat in relationships."""
    facts = _facts([
        {"entity_text": "Ashford", "entity_type": "GPE",
         "record_id": "m1", "source_id": "imessage"}
    ])

    assert facts[0]["dimension"] == "places"


def test_a_person_mention_is_still_a_relationship():
    """Control: the 4,769 facts that were right must stay right."""
    facts = _facts([
        {"entity_text": "Alice", "entity_type": "PERSON",
         "record_id": "m1", "source_id": "imessage"}
    ])

    assert facts[0]["dimension"] == "relationships"


def test_an_untyped_mention_uses_the_record_dimension_not_a_constant():
    facts = _facts([
        {"entity_text": "Whatsit", "record_id": "m1", "source_id": "imessage",
         "signal_dimension": "memory"}
    ])

    assert facts[0]["dimension"] == "memory"


def test_the_fact_still_carries_its_type():
    """The dimension is derived FROM the type; losing the type would make the
    derivation unauditable and a future re-stamp impossible."""
    facts = _facts([
        {"entity_text": "Anthropic", "entity_type": "ORG",
         "record_id": "m1", "source_id": "imessage"}
    ])

    assert facts[0]["entity_type"] == "ORG"


def test_the_job_no_longer_hardcodes_the_fact_dimension():
    """The constant is easy to reintroduce and invisible once written.

    Scoped to the ``put_fact`` call. The graph node written a few lines above
    keeps a literal "relationships" and that is correct: it is written only when
    a ``person_id`` resolved, so the node genuinely is a person.
    """
    import re
    from pathlib import Path

    from topos.enrichment import job_writer

    src = Path(job_writer.__file__).read_text(encoding="utf-8")
    block = src[src.index('elif job_name == "entities"'):src.index('elif job_name == "emo_27"')]
    fact_call = block[block.index("adapters.signal.put_fact("):]
    hardcoded = re.findall(r'"dimension":\s*"[a-z_]+"', fact_call)
    assert hardcoded == [], f"a constant dimension is back in the entities job: {hardcoded}"


# ------------------------------------------------------- the backlog restamp


def _facts_db(tmp_path, rows):
    import json
    import sqlite3

    from topos.storage.db.migrations import apply_all_migrations

    conn = sqlite3.connect(str(tmp_path / "facts.db"))
    apply_all_migrations(conn)
    conn.execute("DELETE FROM signal_facts")
    for i, (dimension, payload) in enumerate(rows):
        conn.execute(
            "INSERT INTO signal_facts (fact_id, dimension, source_id, record_id,"
            " payload_json) VALUES (?,?,?,?,?)",
            (f"f{i}", dimension, "imessage", f"m{i}", json.dumps(payload)),
        )
    conn.commit()
    return conn


def _dims(conn):
    return dict(
        conn.execute("SELECT dimension, COUNT(*) FROM signal_facts GROUP BY 1").fetchall()
    )


def test_the_restamp_refiles_by_type(tmp_path):
    from topos.storage.db.migrations.fact_dimension_by_entity_type_v1 import (
        restamp_fact_dimensions,
    )

    conn = _facts_db(tmp_path, [
        ("relationships", {"entity_text": "Anthropic", "entity_type": "ORG"}),
        ("relationships", {"entity_text": "Ashford", "entity_type": "GPE"}),
        ("relationships", {"entity_text": "Alice", "entity_type": "PERSON"}),
    ])
    try:
        counts = restamp_fact_dimensions(conn)
        conn.commit()

        assert counts["changed"] == 2, "the PERSON fact was already correct"
        assert _dims(conn) == {"work": 1, "places": 1, "relationships": 1}
    finally:
        conn.close()


def test_the_restamp_leaves_facts_with_no_entity_type_alone(tmp_path):
    """The scoping that makes this safe.

    ``relationship_edges`` and dossier facts are genuinely about relationships
    and carry no entity type. A restamp that reached them would be re-filing
    rows on no evidence at all.
    """
    from topos.storage.db.migrations.fact_dimension_by_entity_type_v1 import (
        restamp_fact_dimensions,
    )

    conn = _facts_db(tmp_path, [
        ("relationships", {"src_node_id": "contact:a", "dst_node_id": "contact:b"}),
        ("relationships", {"summary": "a dossier"}),
    ])
    try:
        counts = restamp_fact_dimensions(conn)
        conn.commit()

        assert counts["scanned"] == 0
        assert _dims(conn) == {"relationships": 2}
    finally:
        conn.close()


def test_the_restamp_keeps_an_unmapped_type_where_it_is(tmp_path):
    """A bare number gets no new home invented for it."""
    from topos.storage.db.migrations.fact_dimension_by_entity_type_v1 import (
        restamp_fact_dimensions,
    )

    conn = _facts_db(tmp_path, [
        ("relationships", {"entity_text": "three", "entity_type": "CARDINAL"}),
        ("relationships", {"entity_text": "1st", "entity_type": "ORDINAL"}),
    ])
    try:
        counts = restamp_fact_dimensions(conn)
        conn.commit()

        assert counts["changed"] == 0
        assert counts["unmapped_kept"] == 2
        assert _dims(conn) == {"relationships": 2}
    finally:
        conn.close()


def test_the_restamp_dry_runs(tmp_path):
    from topos.storage.db.migrations.fact_dimension_by_entity_type_v1 import (
        restamp_fact_dimensions,
    )

    conn = _facts_db(tmp_path, [("relationships", {"entity_type": "ORG"})])
    try:
        counts = restamp_fact_dimensions(conn, dry_run=True)
        conn.commit()

        assert counts["changed"] == 1
        assert _dims(conn) == {"relationships": 1}, "a dry run must not write"
    finally:
        conn.close()


def test_the_restamp_is_idempotent(tmp_path):
    from topos.storage.db.migrations.fact_dimension_by_entity_type_v1 import (
        restamp_fact_dimensions,
    )

    conn = _facts_db(tmp_path, [("relationships", {"entity_type": "ORG"})])
    try:
        restamp_fact_dimensions(conn)
        conn.commit()
        assert restamp_fact_dimensions(conn)["changed"] == 0
    finally:
        conn.close()


def test_the_restamp_agrees_with_the_writer(tmp_path):
    """The migration and the ingest path must not drift into two answers.

    They share ``dimension_for_entity_type``; this fails if either grows its own
    copy — the split-definition failure this workstream keeps finding.
    """
    from topos.storage.db.migrations.fact_dimension_by_entity_type_v1 import (
        restamp_fact_dimensions,
    )

    conn = _facts_db(tmp_path, [
        ("relationships", {"entity_text": "Anthropic", "entity_type": "ORG"}),
    ])
    try:
        restamp_fact_dimensions(conn)
        conn.commit()
        stored = conn.execute("SELECT dimension FROM signal_facts").fetchone()[0]
    finally:
        conn.close()

    written = _facts([
        {"entity_text": "Anthropic", "entity_type": "ORG",
         "record_id": "m1", "source_id": "imessage"}
    ])[0]["dimension"]

    assert stored == written == "work"


def test_a_malformed_payload_is_skipped(tmp_path):
    """Cannot read the type means cannot re-derive the dimension."""
    import sqlite3

    from topos.storage.db.migrations import apply_all_migrations
    from topos.storage.db.migrations.fact_dimension_by_entity_type_v1 import (
        restamp_fact_dimensions,
    )

    conn = sqlite3.connect(str(tmp_path / "bad.db"))
    apply_all_migrations(conn)
    conn.execute("DELETE FROM signal_facts")
    conn.execute(
        "INSERT INTO signal_facts (fact_id, dimension, payload_json)"
        " VALUES ('f-bad','relationships','not json')"
    )
    conn.commit()
    try:
        assert restamp_fact_dimensions(conn)["changed"] == 0
        assert _dims(conn) == {"relationships": 1}
    finally:
        conn.close()

"""The owner is one entity, chosen the same way everywhere.

`is_self` is not unique — measured live it sat on three entities, one holding 178 pack facts
and 1,203 edges, two holding nothing. Eleven production sites read it with an unordered
`fetchone()`, so which one they got was rowid luck. Nothing would have thrown; the node would
simply have begun answering "nothing is known about you" after an ordinary VACUUM.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.owner import owner_entity_id, owner_entity_ids


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "o.db"))
    c.execute("CREATE TABLE entities (entity_id TEXT PRIMARY KEY, is_self INTEGER DEFAULT 0)")
    c.execute("""CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, object_type TEXT,
                 object_key TEXT)""")
    yield c
    c.close()


def _self(conn, eid, facts=0):
    conn.execute("INSERT INTO entities VALUES (?,1)", (eid,))
    for i in range(facts):
        conn.execute("INSERT INTO signal_objects VALUES (?,?,?)",
                     (f"{eid}_{i}", "fact", f"fact:{eid}:pred{i}"))
    conn.commit()


def test_the_fact_bearing_self_entity_wins(conn):
    """The live shape: one real owner and two empty shells created the same morning."""
    _self(conn, "ent_zzz_empty_a")
    _self(conn, "ent_aaa_empty_b")
    _self(conn, "ent_mmm_real", facts=178)
    assert owner_entity_id(conn) == "ent_mmm_real"


def test_the_choice_is_stable_when_nobody_has_facts(conn):
    """Ties break on entity_id, not on insertion order — so a VACUUM cannot move the
    answer."""
    _self(conn, "ent_b")
    _self(conn, "ent_a")
    assert owner_entity_id(conn) == "ent_a"
    assert owner_entity_id(conn) == "ent_a"


def test_a_node_with_no_self_entity_returns_none_rather_than_guessing(conn):
    conn.execute("INSERT INTO entities VALUES ('ent_other', 0)")
    conn.commit()
    assert owner_entity_id(conn) is None
    assert owner_entity_ids(conn) == set()


def test_the_plural_form_returns_all_of_them(conn):
    """Ego removal must drop EVERY self node from a graph. Using the singular there leaves
    the ego in under its other identity, which looks exactly like the fix working."""
    _self(conn, "ent_a", facts=3)
    _self(conn, "ent_b")
    assert owner_entity_ids(conn) == {"ent_a", "ent_b"}


def test_it_survives_a_database_without_signal_objects(conn, tmp_path):
    """The fact-count ordering is a preference, not a requirement — query fixtures build a
    minimal entities table and must still get a stable answer."""
    c = sqlite3.connect(str(tmp_path / "bare.db"))
    c.execute("CREATE TABLE entities (entity_id TEXT PRIMARY KEY, is_self INTEGER DEFAULT 0)")
    c.execute("INSERT INTO entities VALUES ('ent_b',1)")
    c.execute("INSERT INTO entities VALUES ('ent_a',1)")
    c.commit()
    assert owner_entity_id(c) == "ent_a"
    c.close()


def test_the_spine_tables_are_no_longer_marked_deprecated():
    """run_gc calls mark_deprecated_tables on every pass, so leaving `persons` and
    `person_aliases` in that set would re-stamp the person spine as superseded every night
    for as long as it took to build it."""
    from topos.features.lifecycle.gc import DEPRECATED_TABLES

    assert "persons" not in DEPRECATED_TABLES
    assert "person_aliases" not in DEPRECATED_TABLES
    assert "relationship_edges" not in DEPRECATED_TABLES


def test_no_production_site_reads_is_self_unordered():
    """The regression guard for the whole class.

    An unordered `is_self=1 LIMIT 1` is not a bug that throws — it is a bug that quietly
    returns the wrong owner after a VACUUM. Greping for the shape is the only way to keep it
    from growing back one call site at a time.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2] / "topos"
    bad = []
    unordered = re.compile(r"is_self\s*=\s*1[^)]{0,40}?LIMIT 1", re.I)
    for f in root.rglob("*.py"):
        if "owner.py" in f.name or ".claude" in str(f):
            continue
        text = f.read_text(errors="ignore")
        for m in unordered.finditer(text):
            window = text[max(0, m.start() - 400):m.end()]
            # scoped to the ENTITIES table. `contacts.is_self` is a different question with
            # a different shape — those reads are dataset-scoped, and the live node carries
            # two self-contacts under two dataset ids rather than three under one.
            if "FROM ENTITIES" not in window.upper():
                continue
            if "ORDER BY" not in window.upper():
                bad.append(f"{f.relative_to(root)}: {m.group(0)[:60]}")
    assert not bad, "unordered is_self reads on entities:\n  " + "\n  ".join(bad)

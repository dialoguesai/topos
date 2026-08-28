"""A derived object's provenance must be readable whichever key its producer used.

``signal_objects.source_refs_json`` has two legitimate shapes and neither is going
away. ``facts/extract.py``, ``facts/llm_extract.py``, ``derivation/surfaces.py``
and ``entities/dossier.py`` write ``{"table":…, "record_id":…}``;
``signal/typed_stores`` and the derivation extraction path write
``{"table":…, "id":…}``.

Every sweep read ``record_id`` only. Measured on the owner's node 2026-08-27 over
4,577 active objects: **4,229 refs key on ``id`` and 307 on ``record_id``** — so
the lifecycle sweeps saw 7% of the provenance in the database and silently
treated the rest as unattributable. Routing all three read sites through one
reader took resolvable refs 307 → 4,536 and reachable objects 291 → 4,236.

This is also the prerequisite for anything keyed on record provenance: a backfill
or a delete that reads ``record_id`` alone skips 92% of the derived layer, which
is why the fan-out retraction had to be written against tables rather than refs.

The ``day``-keyed shape is deliberately NOT a record key — those 303 refs are
day-scoped aggregates citing a date, not a row. Resolving them to a record id
would invent provenance that points at nothing, which is worse than admitting
there is none.
"""

from __future__ import annotations

import pytest

from topos.features.lifecycle.derived_scrub import ref_record_key, ref_record_ids


# ------------------------------------------------------------ both real shapes


def test_the_record_id_shape_resolves():
    assert ref_record_key({"table": "journal_entries", "record_id": "tl-1"}) == (
        "journal_entries",
        "tl-1",
    )


def test_the_id_shape_resolves():
    """92% of live refs use this one, and every sweep used to miss it."""
    assert ref_record_key({"table": "journal_entries", "id": "tl-1"}) == (
        "journal_entries",
        "tl-1",
    )


def test_record_id_wins_when_a_producer_writes_both():
    assert ref_record_key({"table": "t", "record_id": "a", "id": "b"}) == ("t", "a")


def test_extra_keys_do_not_break_it():
    """The live corpus carries note/source_id alongside the record key."""
    assert ref_record_key(
        {"table": "t", "record_id": "a", "note": "x", "source_id": "s"}
    ) == ("t", "a")


# -------------------------------------------------- what is NOT a record key


def test_a_day_scoped_ref_is_not_a_record():
    """303 live refs key on `day`. A date is not a row.

    Resolving it to a record id would invent provenance pointing at nothing,
    which is worse than admitting there is none — a sweep would then treat the
    object as attributable and act on it.
    """
    assert ref_record_key({"table": "activity_events", "day": "2026-07-06"}) is None


def test_a_ref_with_no_record_key_is_not_invented():
    assert ref_record_key({"table": "t"}) is None
    assert ref_record_key({"awarded_by": "someone"}) is None
    assert ref_record_key({}) is None


def test_a_non_dict_ref_is_ignored():
    for junk in ("a string", 42, None, ["nested"]):
        assert ref_record_key(junk) is None


def test_a_blank_record_key_does_not_resolve():
    """An empty string would match nothing and read as a real id downstream."""
    assert ref_record_key({"table": "t", "record_id": ""}) is None
    assert ref_record_key({"table": "t", "id": "   "}) is None


# --------------------------------------------------------------- the list form


def test_ref_record_ids_collects_across_shapes():
    refs = [
        {"table": "a", "record_id": "r1"},
        {"table": "b", "id": "r2"},
        {"table": "c", "day": "2026-07-06"},
        {"table": "d"},
        "junk",
    ]

    assert ref_record_ids(refs) == ["r1", "r2"]


def test_ref_record_ids_tolerates_a_non_list():
    for junk in (None, {}, "refs", 7):
        assert ref_record_ids(junk) == []


# ------------------------------------------------------- the sweeps use it


def test_every_ref_read_site_goes_through_the_reader():
    """There were several readers and they all read one key.

    A new site that reaches into a ref dict directly reintroduces exactly the
    blind spot this closed, so the module is checked for the pattern.
    """
    import re
    from pathlib import Path

    source = Path(
        __import__("topos.features.lifecycle.derived_scrub", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")

    # Strip the reader's own body — it is the one place allowed to do this.
    start = source.index("def ref_record_key(")
    end = source.index("def ref_record_ids(")
    rest = source[:start] + source[end:]
    rest = rest[: rest.index("def _delete_entity_cascade(")] + rest[
        rest.index("def _delete_entity_cascade(") :
    ]

    # Matches ANY variable name, not just `ref`. The first version of this
    # check looked for `ref.get(...)` specifically and therefore sailed past
    # `r.get("record_id")` in `purge_derived_for_records` — a loop that called
    # its variable `r`. That one missed line meant the owner's "remove this from
    # my intelligence" saw 307 of 3,245 refs.
    offenders = [
        line.strip()
        for line in rest.splitlines()
        if re.search(r'\b[A-Za-z_][A-Za-z0-9_]*\.get\(\s*["\'](record_id|id)["\']', line)
    ]
    assert offenders == [], (
        "these read a provenance ref's key directly and will miss the other shape; "
        f"use ref_record_key(): {offenders}"
    )


# --------------------------------------- the sweep, widened past object_type='fact'


def _sweep_db(tmp_path):
    import sqlite3

    from topos.storage.db.migrations import apply_all_migrations

    conn = sqlite3.connect(str(tmp_path / "sweep.db"))
    apply_all_migrations(conn)
    conn.execute(
        "INSERT INTO journal_entries (entry_id, content, source_id) VALUES (?,?,?)",
        ("tl-alive", "still here", "grow_journal"),
    )
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self) VALUES (?,?,?,?,1,0)",
        ("ent_live", "person", "Ada", "ada"),
    )
    conn.commit()
    return conn


def _obj(conn, object_id, object_type, refs, key="k"):
    import json as _json

    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key,"
        " payload_json, source_refs_json, valid_from, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (object_id, "work", object_type, key, "{}", _json.dumps(refs),
         "2026-07-01", "2026-07-01", "2026-07-01"),
    )


def _valid_to(conn, object_id):
    return conn.execute(
        "SELECT valid_to FROM signal_objects WHERE object_id=?", (object_id,)
    ).fetchone()[0]


def test_the_sweep_closes_non_fact_objects_whose_evidence_is_gone(tmp_path):
    """The restriction to ``object_type='fact'`` was never principled.

    A dossier, a PlaceContext and a fact all outlive their evidence the same way.
    On the owner's node the widening closed 1,497 objects, mostly derived from
    ``chatgpt_ingestion`` records that were deleted while their derived rows
    stayed — the leak this sweep exists for, never allowed to look outside `fact`.
    """
    from topos.features.lifecycle.derived_scrub import close_dangling_facts

    conn = _sweep_db(tmp_path)
    try:
        _obj(conn, "o-dead-topic", "message_topics", [{"table": "user_goals", "id": "gone-1"}])
        _obj(conn, "o-dead-place", "PlaceContext", [{"table": "location_events", "id": "gone-2"}])
        conn.commit()

        assert close_dangling_facts(conn) == 2
        conn.commit()

        assert _valid_to(conn, "o-dead-topic") is not None
        assert _valid_to(conn, "o-dead-place") is not None
    finally:
        conn.close()


def test_a_dossier_citing_a_LIVE_entity_is_not_closed(tmp_path):
    """The trap the dry-run caught, as a permanent regression.

    ``entities/dossier.py`` writes ``{"table": "entity_mentions", "record_id":
    "ent_…"}`` — an ENTITY id in a record_id field, aimed at a table keyed on
    ``mention_id``. It can never match, so a checker that trusts the declared
    table reports GONE. Applying the widening without this would have closed 158
    live dossiers on the owner's node: the change's first act would have been
    data loss.
    """
    from topos.features.lifecycle.derived_scrub import close_dangling_facts

    conn = _sweep_db(tmp_path)
    try:
        _obj(
            conn,
            "o-dossier",
            "entity_dossier",
            [{"table": "entity_mentions", "record_id": "ent_live"}],
            key="dossier:ent_live",
        )
        conn.commit()

        close_dangling_facts(conn)
        conn.commit()

        assert _valid_to(conn, "o-dossier") is None, (
            "the entity is alive; the ref's table label is what is wrong"
        )
    finally:
        conn.close()


def test_a_dossier_citing_a_DEAD_entity_is_closed(tmp_path):
    """Control: resolving by shape must not make everything immortal."""
    from topos.features.lifecycle.derived_scrub import close_dangling_facts

    conn = _sweep_db(tmp_path)
    try:
        _obj(
            conn,
            "o-dossier-dead",
            "entity_dossier",
            [{"table": "entity_mentions", "record_id": "ent_reaped"}],
            key="dossier:ent_reaped",
        )
        conn.commit()

        close_dangling_facts(conn)
        conn.commit()

        assert _valid_to(conn, "o-dossier-dead") is not None
    finally:
        conn.close()


def test_a_day_scoped_object_is_never_closed(tmp_path):
    """303 live refs cite a date. Unverifiable must stay conservative."""
    from topos.features.lifecycle.derived_scrub import close_dangling_facts

    conn = _sweep_db(tmp_path)
    try:
        _obj(conn, "o-day", "availability_summary",
             [{"table": "activity_events", "day": "2026-07-06"}])
        conn.commit()

        close_dangling_facts(conn)
        conn.commit()

        assert _valid_to(conn, "o-day") is None
    finally:
        conn.close()


def test_an_object_whose_evidence_lives_is_not_closed(tmp_path):
    from topos.features.lifecycle.derived_scrub import close_dangling_facts

    conn = _sweep_db(tmp_path)
    try:
        _obj(conn, "o-live", "message_topics",
             [{"table": "journal_entries", "id": "tl-alive"}])
        conn.commit()

        close_dangling_facts(conn)
        conn.commit()

        assert _valid_to(conn, "o-live") is None
    finally:
        conn.close()

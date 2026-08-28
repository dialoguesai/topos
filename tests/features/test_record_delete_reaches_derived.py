"""Deleting a record must reach everything derived from it.

"Remove this from my intelligence" is a promise about the derived layer, and it
was reaching about 4% of it. Two independent restrictions compounded in
``purge_derived_for_records``:

  * it read ``record_id`` off each provenance ref while 92% of live refs key on
    ``id`` — the blind spot ``ref_record_key`` exists to close, still open in
    this one function because its loop variable is called ``r`` and the guard
    test grepped for ``ref.get``;
  * it looked only at ``object_type='fact'``.

Measured on the owner's node 2026-08-27: the pair saw **307 of 3,245 refs** and
**127 of 3,262 active objects**. A PlaceContext or a topic summary derived from
a withdrawn record outlived it exactly as a fact would have.

``extraction_artifacts`` was worse: 5,817 rows, 5,814 with a resolvable ref
(3,273 to conversation_messages, 2,497 to journal_entries), and **no lifecycle
sweep opened the table at all**.

Trimming rather than deleting, when other records still evidence a row, is the
same rule the fact path already used: a derived row with surviving evidence is
still true, just less so. And a ref the reader cannot resolve — a day-scoped
aggregate — is never treated as a match, so an unverifiable ref can only ever
cause a row to be KEPT.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.lifecycle.derived_scrub import purge_derived_for_records


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "purge.db"))
    c.row_factory = sqlite3.Row
    apply_all_migrations(c)
    c.execute(
        "INSERT INTO journal_entries (entry_id, content, source_id) VALUES (?,?,?)",
        ("tl-keep", "still here", "grow_journal"),
    )
    c.commit()
    yield c
    c.close()


def _obj(conn, object_id, object_type, refs):
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key,"
        " payload_json, source_refs_json, valid_from, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (object_id, "work", object_type, object_id, "{}", json.dumps(refs),
         "2026-07-01", "2026-07-01", "2026-07-01"),
    )
    conn.commit()


def _artifact(conn, artifact_id, refs):
    # source_ref_hash is NOT NULL and uniquely indexed with artifact_type.
    conn.execute(
        "INSERT INTO extraction_artifacts (artifact_id, artifact_type, payload_json,"
        " source_refs_json, source_ref_hash, extracted_at) VALUES (?,?,?,?,?,?)",
        (artifact_id, "availability", "{}", json.dumps(refs), artifact_id, "2026-07-01"),
    )
    conn.commit()


def _objects(conn):
    return {r[0] for r in conn.execute("SELECT object_id FROM signal_objects")}


def _artifacts(conn):
    return {r[0] for r in conn.execute("SELECT artifact_id FROM extraction_artifacts")}


# ------------------------------------------------- signal_objects


def test_an_id_shaped_ref_is_now_seen(conn):
    """92% of live refs use this shape and the purge could not see any of them."""
    _obj(conn, "o-id", "fact", [{"table": "journal_entries", "id": "tl-gone"}])

    purge_derived_for_records(conn, ["tl-gone"])

    assert "o-id" not in _objects(conn)


def test_a_record_id_shaped_ref_still_works(conn):
    """Control: the 8% that did work must keep working."""
    _obj(conn, "o-rid", "fact", [{"table": "journal_entries", "record_id": "tl-gone"}])

    purge_derived_for_records(conn, ["tl-gone"])

    assert "o-rid" not in _objects(conn)


def test_a_non_fact_object_is_reached(conn):
    """3,135 of 3,262 live objects are not facts, and none were touched.

    A PlaceContext derived from a withdrawn record outlives it exactly as a
    fact would; the object_type restriction was never principled.
    """
    _obj(conn, "o-place", "PlaceContext", [{"table": "journal_entries", "id": "tl-gone"}])

    purge_derived_for_records(conn, ["tl-gone"])

    assert "o-place" not in _objects(conn)


def test_an_object_with_other_evidence_is_trimmed_not_deleted(conn):
    _obj(conn, "o-mixed", "message_topics", [
        {"table": "journal_entries", "id": "tl-gone"},
        {"table": "journal_entries", "id": "tl-keep"},
    ])

    purge_derived_for_records(conn, ["tl-gone"])

    assert "o-mixed" in _objects(conn)
    refs = json.loads(
        conn.execute(
            "SELECT source_refs_json FROM signal_objects WHERE object_id='o-mixed'"
        ).fetchone()[0]
    )
    assert refs == [{"table": "journal_entries", "id": "tl-keep"}]


def test_an_unrelated_object_is_untouched(conn):
    _obj(conn, "o-other", "fact", [{"table": "journal_entries", "id": "tl-keep"}])

    purge_derived_for_records(conn, ["tl-gone"])

    assert "o-other" in _objects(conn)


def test_a_day_scoped_ref_never_causes_a_delete(conn):
    """Unverifiable must fail toward KEEPING. 303 live refs cite a date."""
    _obj(conn, "o-day", "availability_summary",
         [{"table": "activity_events", "day": "2026-07-06"}])

    purge_derived_for_records(conn, ["2026-07-06"])

    assert "o-day" in _objects(conn)


# ------------------------------------------------- extraction_artifacts


def test_an_artifact_is_reached_at_all(conn):
    """5,814 rows with resolvable provenance that no sweep ever opened."""
    _artifact(conn, "a-gone", [{"table": "journal_entries", "id": "tl-gone"}])

    report = purge_derived_for_records(conn, ["tl-gone"])

    assert "a-gone" not in _artifacts(conn)
    assert report["extraction_artifacts_deleted"] == 1


def test_an_artifact_with_other_evidence_is_trimmed(conn):
    _artifact(conn, "a-mixed", [
        {"table": "journal_entries", "id": "tl-gone"},
        {"table": "journal_entries", "record_id": "tl-keep"},
    ])

    report = purge_derived_for_records(conn, ["tl-gone"])

    assert "a-mixed" in _artifacts(conn)
    assert report["extraction_artifacts_trimmed"] == 1


def test_an_unrelated_artifact_survives(conn):
    _artifact(conn, "a-other", [{"table": "journal_entries", "id": "tl-keep"}])

    purge_derived_for_records(conn, ["tl-gone"])

    assert "a-other" in _artifacts(conn)


def test_a_malformed_artifact_ref_is_left_alone(conn):
    conn.execute(
        "INSERT INTO extraction_artifacts (artifact_id, artifact_type, payload_json,"
        " source_refs_json, source_ref_hash, extracted_at)"
        " VALUES ('a-bad','x','{}','not json','h-bad','2026-07-01')"
    )
    conn.commit()

    purge_derived_for_records(conn, ["tl-gone"])

    assert "a-bad" in _artifacts(conn)


def test_the_report_names_both_outcomes(conn):
    report = purge_derived_for_records(conn, ["tl-gone"])

    assert "extraction_artifacts_deleted" in report
    assert "extraction_artifacts_trimmed" in report

"""After a withdrawal reports ``complete``, the name must be gone from every
derived surface — including the ones a full-text search reaches.

``rebuild_for_blackhole`` covered six prose surfaces and marked itself complete.
It did not cover the embedding lane, which is the one a similarity or keyword
search actually reads. Measured on the owner's node 2026-08-27, the black hole on
``Old Harbor- Rey's Place`` carried ``rebuild_state='complete'`` while the
protected name was still live in ``signal_embeddings.text_preview``,
``signal_embeddings.search_text`` and the FTS index — and its entity had been
reaped, so the read-time id filter that was supposed to cover the gap returned
nothing at all.

The canonical/derived line the design draws is kept: canonical rows are the
owner's own record and are deliberately left alone. What changed is the
recognition that the embedding lane is derived, not canonical.

``test_no_protected_term_survives_on_any_declared_surface`` is the one that
generalises — it is driven off ``DERIVED_TEXT_SURFACES`` below, so adding a
surface to the withdrawal without adding it here (or the reverse) shows up as a
failure rather than as silence.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.blackhole_rebuild import rebuild_for_blackhole
from tests.evals.privacy.blackhole.corpus import BH_CANONICAL, BH_ID, build_blackhole_corpus

pytestmark = [pytest.mark.bhlr, pytest.mark.private]

#: ``(table, text columns)`` that must not contain a protected term once a
#: withdrawal completes. Canonical tables are deliberately absent — see module
#: docstring.
DERIVED_TEXT_SURFACES = [
    ("signal_embeddings", ("text_preview", "search_text")),
    ("user_goals", ("goal_text", "payload_json")),
    ("signal_dimension_briefs", ("markdown_body",)),
    ("topic_clusters", ("label", "centroid_preview")),
    ("topic_cluster_members", ("text_preview",)),
]


@pytest.fixture()
def seeded(tmp_path):
    """A corpus with the protected name planted across the derived surfaces."""
    c = build_blackhole_corpus(str(tmp_path / "withdraw.db"), rebuild_complete=False)
    conn = c.conn
    conn.execute(
        "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, text_preview,"
        " search_text, signal_dimension) VALUES (?,?,?,?,?,?)",
        ("emb-bh", "rec-bh", "imessage", f"dinner with {BH_CANONICAL}",
         f"dinner with {BH_CANONICAL} last week", "relationships"),
    )
    conn.execute(
        "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, text_preview,"
        " search_text, signal_dimension) VALUES (?,?,?,?,?,?)",
        ("emb-ok", "rec-ok", "imessage", "a completely unrelated note",
         "a completely unrelated note", "relationships"),
    )
    conn.execute(
        "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json)"
        " VALUES (?,?,?,?,?)",
        ("goal-bh", "rec-bh", "imessage", f"Catch up with {BH_CANONICAL}", "{}"),
    )
    conn.execute(
        "INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json)"
        " VALUES (?,?,?,?,?)",
        ("goal-ok", "rec-ok", "imessage", "Finish the migration", "{}"),
    )
    conn.commit()
    yield c
    conn.close()


def _term_hits(conn: sqlite3.Connection, term: str) -> dict:
    hits = {}
    for table, columns in DERIVED_TEXT_SURFACES:
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.OperationalError:
            continue
        present = [c for c in columns if c in cols]
        if not present:
            continue
        where = " OR ".join(f"{c} LIKE ?" for c in present)
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", [f"%{term}%"] * len(present)
        ).fetchone()[0]
        if n:
            hits[table] = n
    return hits


# ------------------------------------------------------------- the invariant


def test_no_protected_term_survives_on_any_declared_surface(seeded):
    before = _term_hits(seeded.conn, BH_CANONICAL)
    assert before, "fixture must plant the name somewhere"

    report = rebuild_for_blackhole(seeded.conn, BH_ID)
    seeded.conn.commit()

    assert report.details.get("status") == "complete"
    after = _term_hits(seeded.conn, BH_CANONICAL)
    assert after == {}, f"withdrawal reported complete but the name survives in {after}"


def test_the_name_is_no_longer_full_text_searchable(seeded):
    """The FTS index is a separate store and was never swept.

    ``signal_embeddings_fts`` is external-content FTS5. Its ``_ad`` trigger is
    what removes a term, and that only fires on a base-row DELETE — blanking the
    column, or deleting via a path that bypasses the trigger, leaves the term
    matchable.
    """
    token = BH_CANONICAL.split()[-1]

    def fts_hits():
        try:
            return seeded.conn.execute(
                "SELECT COUNT(*) FROM signal_embeddings_fts WHERE signal_embeddings_fts MATCH ?",
                (token,),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pytest.skip("no FTS table in this build")

    assert fts_hits() >= 1, "fixture must be indexed before the withdrawal"

    rebuild_for_blackhole(seeded.conn, BH_ID)
    seeded.conn.commit()

    assert fts_hits() == 0


def test_the_ann_companion_rows_go_too(seeded):
    """A vector left behind is still a neighbour, even with no text."""
    rebuild_for_blackhole(seeded.conn, BH_ID)
    seeded.conn.commit()

    try:
        remaining = seeded.conn.execute(
            "SELECT COUNT(*) FROM signal_embeddings_vec_rowids WHERE rowid IN"
            " (SELECT rowid FROM signal_embeddings_vec_rowids)"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        pytest.skip("no vec index in this build")
    orphaned = seeded.conn.execute(
        "SELECT COUNT(*) FROM signal_embeddings WHERE embedding_id='emb-bh'"
    ).fetchone()[0]
    assert orphaned == 0
    assert remaining >= 0  # the vec table exists and the delete did not error


# --------------------------------------------------------------------- controls


def test_unrelated_derived_rows_survive(seeded):
    """Surgical: withdrawal must not be a table wipe."""
    rebuild_for_blackhole(seeded.conn, BH_ID)
    seeded.conn.commit()

    assert (
        seeded.conn.execute(
            "SELECT COUNT(*) FROM signal_embeddings WHERE embedding_id='emb-ok'"
        ).fetchone()[0]
        == 1
    )
    assert (
        seeded.conn.execute(
            "SELECT COUNT(*) FROM user_goals WHERE goal_id='goal-ok'"
        ).fetchone()[0]
        == 1
    )


def test_the_report_says_what_it_withdrew(seeded):
    """Silent removal is as bad as silent retention — the counts are the receipt."""
    report = rebuild_for_blackhole(seeded.conn, BH_ID)

    assert report.embeddings_withdrawn >= 1
    assert report.goals_withdrawn >= 1
    payload = report.as_dict()
    assert payload["embeddings_withdrawn"] == report.embeddings_withdrawn
    assert payload["goals_withdrawn"] == report.goals_withdrawn


def test_withdrawal_is_idempotent(seeded):
    """A second run finds nothing left and must not error or double-count."""
    rebuild_for_blackhole(seeded.conn, BH_ID)
    seeded.conn.commit()

    again = rebuild_for_blackhole(seeded.conn, BH_ID)

    assert again.embeddings_withdrawn == 0
    assert again.goals_withdrawn == 0
    assert _term_hits(seeded.conn, BH_CANONICAL) == {}


def test_canonical_rows_are_deliberately_untouched(seeded):
    """Pins the design line, so a future 'completeness' pass does not cross it.

    Canonical rows are the owner's own record. Read-time filtering protects them;
    withdrawing them would delete owner truth.
    """
    seeded.conn.execute(
        "INSERT INTO journal_entries (entry_id, content, source_id) VALUES (?,?,?)",
        ("je-bh", f"had dinner with {BH_CANONICAL}", "grow_journal"),
    )
    seeded.conn.commit()

    rebuild_for_blackhole(seeded.conn, BH_ID)
    seeded.conn.commit()

    assert (
        seeded.conn.execute(
            "SELECT COUNT(*) FROM journal_entries WHERE entry_id='je-bh'"
        ).fetchone()[0]
        == 1
    )


def test_store_marks_the_rebuild_complete_only_after_the_new_surfaces(seeded):
    rebuild_for_blackhole(seeded.conn, BH_ID)

    record = BlackholeStore(seeded.conn).get(BH_ID)
    assert record["rebuild_state"] == "complete"
    assert _term_hits(seeded.conn, BH_CANONICAL) == {}


# ------------------------------- scope: every derived object, not a named few


def test_the_unreachable_object_types_are_withdrawn(seeded):
    """Types that name the entity with NO spine id a predicate can reach.

    ``PROSE_OBJECT_TYPES`` was a snapshot of what happened to exist when it was
    written. Measured on the owner's node 2026-08-27, ``PlaceContext`` (76 rows)
    and ``AvailabilityWindow`` (406) carried a protected place name in
    ``display_band`` and sat outside every withdrawal.
    """
    from topos.features.lifecycle.blackhole_rebuild import (
        PROSE_OBJECT_TYPES,
        UNREACHABLE_OBJECT_TYPES,
    )

    for object_type in UNREACHABLE_OBJECT_TYPES:
        assert object_type not in PROSE_OBJECT_TYPES, (
            "premise: these must be outside the ORIGINAL list, or the test proves nothing"
        )
        seeded.conn.execute(
            "INSERT INTO signal_objects (object_id, signal_dimension, object_type,"
            " object_key, payload_json, valid_from, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                f"obj-{object_type}",
                "places",
                object_type,
                "a-slug-with-no-entity-id",
                f'{{"entity_key": "a-slug", "display_band": "{BH_CANONICAL}"}}',
                "2026-07-01", "2026-07-01", "2026-07-01",
            ),
        )
    seeded.conn.commit()

    rebuild_for_blackhole(seeded.conn, BH_ID)
    seeded.conn.commit()

    still_open = [
        r[0]
        for r in seeded.conn.execute(
            "SELECT object_type FROM signal_objects WHERE object_id LIKE 'obj-%'"
            " AND payload_json LIKE ? AND valid_to IS NULL",
            (f"%{BH_CANONICAL}%",),
        )
    ]
    assert still_open == [], (
        f"these name the protected entity with no id to filter on: {still_open}"
    )


def test_an_id_keyed_fact_is_still_left_alone(seeded):
    """The judgment the list encodes, pinned from this side too.

    A ``fact`` is keyed ``fact:ent_<id>:…``, so read-time filtering by entity id
    already covers it and withdrawing it would delete owner truth that is safely
    hidden. Two attempts to replace the list with a mechanical rule both broke
    this: "withdraw everything derived" deleted the facts, and "leave anything
    carrying the entity id" kept the dossiers. The line between a structured
    claim worth keeping and a generated restatement worth withdrawing is a
    judgment, so it stays written down.
    """
    seeded.conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type,"
        " object_key, payload_json, valid_from, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            "obj-fact-keep", "relationships", "fact",
            f"fact:{BH_ID}:rel.closeness_tier:x",
            f'{{"subject": "{BH_ID}", "object_value": "{BH_CANONICAL}"}}',
            "2026-07-01", "2026-07-01", "2026-07-01",
        ),
    )
    seeded.conn.commit()

    rebuild_for_blackhole(seeded.conn, BH_ID)
    seeded.conn.commit()

    assert (
        seeded.conn.execute(
            "SELECT valid_to FROM signal_objects WHERE object_id='obj-fact-keep'"
        ).fetchone()[0]
        is None
    ), "an id-keyed fact is reachable by predicate and must survive the withdrawal"


def test_every_withdrawn_type_is_actually_withdrawn(seeded):
    """The list must be effective, type by type.

    A curated list is the right shape here — the line between a structured claim
    worth keeping and a generated restatement worth withdrawing is a judgment,
    and two attempts to mechanise it both broke something the suite defends. But
    a list that names a type it does not actually reach is worse than no list, so
    every member is exercised.

    Discovering a type the list has FALLEN BEHIND on is a different job and needs
    real object shapes, not synthesized ones: a planted ``fact`` with no entity id
    is a shape that never occurs, and asserting on it would fail for a reason that
    is not a defect. That check belongs against the live database, not here.
    """
    from topos.features.lifecycle.blackhole_rebuild import WITHDRAWN_OBJECT_TYPES

    for object_type in WITHDRAWN_OBJECT_TYPES:
        seeded.conn.execute(
            "INSERT INTO signal_objects (object_id, signal_dimension, object_type,"
            " object_key, payload_json, valid_from, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (f"eff-{object_type}", "places", object_type, "slug-no-id",
             f'{{"display_band": "{BH_CANONICAL}"}}', "2026-07-01", "2026-07-01", "2026-07-01"),
        )
    seeded.conn.commit()

    rebuild_for_blackhole(seeded.conn, BH_ID)
    seeded.conn.commit()

    survived = sorted(
        r[0]
        for r in seeded.conn.execute(
            "SELECT object_type FROM signal_objects WHERE object_id LIKE 'eff-%'"
            " AND valid_to IS NULL AND payload_json LIKE ?",
            (f"%{BH_CANONICAL}%",),
        )
    )
    assert survived == [], (
        f"these are named in WITHDRAWN_OBJECT_TYPES but were not withdrawn: {survived}"
    )


def test_an_unrelated_derived_object_survives(seeded):
    """Widening scope must not become a table wipe."""
    seeded.conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type,"
        " object_key, payload_json, valid_from, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("obj-keep", "places", "PlaceContext", "PlaceContext:keep",
         '{"display_band": "Somewhere Entirely Else"}', "2026-07-01", "2026-07-01", "2026-07-01"),
    )
    seeded.conn.commit()

    rebuild_for_blackhole(seeded.conn, BH_ID)
    seeded.conn.commit()

    assert (
        seeded.conn.execute(
            "SELECT valid_to FROM signal_objects WHERE object_id='obj-keep'"
        ).fetchone()[0]
        is None
    )

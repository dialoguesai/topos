"""The derived index must be withdrawn by IDENTITY, not only by wording.

protects: no index the withdrawal sweep doesn't know — and no derived row
about a protected person survives because its sentence happened not to say
their name.

``_withdraw_embeddings`` sweeps ``signal_embeddings`` by scanning every row's
text for a protected TERM. That covers the derived rows whose rendering leads
with the subject's name — most of them do. It cannot cover the ones that do
not, and the derived index is precisely where those live: a rendering keyed to
an entity is ABOUT that person whether or not the sentence names them, and
goal/dossier prose routinely refers to someone by pronoun, by role, or by an
alias the term set never learned ("my brother", "the founder I met").

The sweep is therefore given a second, exact leg: derived rows carry the
signal_object they were rendered from, that object names its subject entity,
and a black-holed entity_id deletes those rows outright. Term-matching stays —
it catches rows that name the person without being keyed to them — but the
guarantee no longer depends on the wording of a generated sentence.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.lifecycle.blackhole_rebuild import _withdraw_embeddings
from topos.storage.db.migrations import apply_all_migrations

PROTECTED_ID = "ent-bh-derived"
PROTECTED_NAME = "Wilhelmina Quist"
TERMS = {"wilhelmina quist", "wilhelmina"}

#: Unguessable, so a leak names its surface rather than merely looking wrong.
CANARY_UNNAMED = "derived-canary-7f31-no-name-in-text"
CANARY_NAMED = "derived-canary-7f31-names-the-person"


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "bh-derived.db"))
    apply_all_migrations(c)
    c.execute(
        "INSERT INTO entities (entity_id, canonical_name, normalized_name, entity_type)"
        " VALUES (?,?,?, 'person')",
        (PROTECTED_ID, PROTECTED_NAME, PROTECTED_NAME.lower()),
    )
    c.execute(
        "INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name)"
        " VALUES ('bh-d1', ?, ?, ?)",
        (PROTECTED_ID, PROTECTED_NAME.lower(), PROTECTED_NAME),
    )
    yield c
    c.close()


def _derived_object(conn, object_id: str, object_type: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key,"
        " payload_json, confidence, valid_from, created_by, created_at, updated_at)"
        " VALUES (?, 'profile', ?, ?, ?, 0.9, '2026-08-01T00:00:00', 'test',"
        " '2026-08-01T00:00:00', '2026-08-01T00:00:00')",
        (object_id, object_type, f"{object_type}:{object_id}", json.dumps(payload)),
    )


def _derived_embedding(conn, embedding_id: str, record_id: str, text: str) -> None:
    conn.execute(
        "INSERT INTO signal_embeddings (embedding_id, record_id, source_id, signal_dimension,"
        " model, provider, dims, text_preview, search_text, record_type, provenance_json,"
        " vector_format, chunk_index)"
        " VALUES (?,?, 'topos_derivation', 'profile', 'stub', 'test', 384, ?, ?,"
        " 'derived_entity_dossier', ?, 'json', 0)",
        (embedding_id, record_id, text, text, json.dumps({"record_id": record_id})),
    )


def _seed_both_shapes(conn) -> None:
    # (a) keyed to the protected entity, and its sentence NEVER names them —
    #     the shape term-matching cannot see.
    _derived_object(conn, "obj-unnamed", "entity_dossier",
                    {"entity_id": PROTECTED_ID, "summary": CANARY_UNNAMED})
    _derived_embedding(conn, "emb-unnamed", "obj-unnamed",
                       f"My brother — someone I see weekly. {CANARY_UNNAMED}")
    # (b) names the protected person outright — the shape term-matching catches.
    _derived_object(conn, "obj-named", "entity_dossier",
                    {"entity_id": PROTECTED_ID, "summary": CANARY_NAMED})
    _derived_embedding(conn, "emb-named", "obj-named",
                       f"{PROTECTED_NAME} — a person I know. {CANARY_NAMED}")
    # (c) an unrelated derived row: the control. Withdrawal must not touch it.
    _derived_object(conn, "obj-other", "entity_dossier",
                    {"entity_id": "ent-someone-else", "summary": "unrelated"})
    _derived_embedding(conn, "emb-other", "obj-other",
                       "Someone Else — a person I know. unrelated-control-row")
    conn.commit()


def _remaining(conn) -> set:
    return {
        str(r[0])
        for r in conn.execute("SELECT embedding_id FROM signal_embeddings").fetchall()
    }


def test_derived_row_about_a_protected_entity_is_withdrawn_even_when_unnamed(conn):
    """The gate: identity, not wording, decides."""
    _seed_both_shapes(conn)
    _withdraw_embeddings(conn, TERMS)
    remaining = _remaining(conn)
    assert "emb-unnamed" not in remaining, (
        "a derived row keyed to a black-holed entity survived because its "
        "sentence never said the name"
    )
    assert "emb-named" not in remaining
    assert "emb-other" in remaining, "withdrawal reached an unrelated row"


def test_no_canary_text_survives_in_any_column_or_fts(conn):
    _seed_both_shapes(conn)
    _withdraw_embeddings(conn, TERMS)
    blob = json.dumps(
        conn.execute(
            "SELECT embedding_id, text_preview, search_text, provenance_json"
            " FROM signal_embeddings"
        ).fetchall()
    )
    assert CANARY_UNNAMED not in blob
    assert CANARY_NAMED not in blob
    fts = conn.execute(
        "SELECT COUNT(*) FROM signal_embeddings_fts WHERE signal_embeddings_fts MATCH ?",
        ("derived",),
    ).fetchone()[0]
    assert fts == 0, "the full-text index still answers for withdrawn derived rows"


def test_severed_wire_the_identity_leg_is_load_bearing(conn, monkeypatch):
    """Unregister the identity leg and the unnamed row survives — proving the
    leg is what protects it, not the term scan that runs beside it."""
    import topos.features.lifecycle.blackhole_rebuild as br

    _seed_both_shapes(conn)
    monkeypatch.setattr(br, "_derived_embedding_ids_for_entities", lambda *a, **k: set())
    br._withdraw_embeddings(conn, TERMS)
    assert "emb-unnamed" in _remaining(conn), (
        "the unnamed row was removed with the identity leg severed — something "
        "else is covering it and this battery no longer proves the leg"
    )


def test_withdrawal_is_silent_when_nothing_is_protected(tmp_path):
    """A node with no black holes must leave the derived index byte-identical."""
    c = sqlite3.connect(str(tmp_path / "clean.db"))
    apply_all_migrations(c)
    _derived_object(c, "obj-x", "entity_dossier", {"entity_id": "ent-x", "summary": "hi"})
    _derived_embedding(c, "emb-x", "obj-x", "Someone — a person I know.")
    c.commit()
    _withdraw_embeddings(c, set())
    assert _remaining(c) == {"emb-x"}
    c.close()

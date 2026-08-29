"""A name from an extraction resolves to the person it MEANS, not the first row that matches.

Extraction routinely splits one human in two: a full name linked to an address-book
contact, and the bare short form minted from prose. An exact-name-first ladder binds
every fact to the second — the half with no messages and no card — because the alias
branch that would have found the real person is never reached.
"""

from __future__ import annotations

import json
import sqlite3

import pytest


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "resolve.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _person(conn, entity_id, name, *, aliases=(), contact=None, mentions=0):
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " aliases_json, contact_id, mention_count) VALUES (?,?,?,?,?,?,?)",
        (entity_id, "person", name, name.lower(), json.dumps(list(aliases)), contact, mentions),
    )
    conn.commit()


def _resolve(conn, name):
    from topos.features.derivation.writer import DerivationWriter

    writer = DerivationWriter.__new__(DerivationWriter)
    writer.conn = conn
    return writer._resolve_person(name)


def test_the_address_book_wins_over_an_exact_prose_match(conn):
    """THE BUG. The short form matches the prose duplicate's name exactly and is only an
    ALIAS of the contact-linked person, so exact-first bound every fact to the ghost."""
    _person(conn, "real", "Rowan Alvestad", aliases=["Rowan"], contact="c-1", mentions=2)
    _person(conn, "ghost", "Rowan", aliases=["Rowan Alvestad", "Rowan"], mentions=21)

    assert _resolve(conn, "Rowan") == "real"
    assert _resolve(conn, "Rowan Alvestad") == "real"


def test_evidence_volume_is_NOT_the_tiebreak(conn):
    """Measured on the live node: the prose duplicate carries 21 mentions against the
    contact-linked person's 2, because prose is where the extractor works and the address
    book is where the owner said who is real. Ranking by mentions picks wrong every time."""
    _person(conn, "real", "Rowan Alvestad", aliases=["Rowan"], contact="c-1", mentions=2)
    _person(conn, "ghost", "Rowan", mentions=9999)

    assert _resolve(conn, "Rowan") == "real"


def test_mentions_break_a_tie_the_address_book_cannot(conn):
    _person(conn, "thin", "Wren", mentions=1)
    _person(conn, "thick", "Wren Solberg", aliases=["Wren"], mentions=40)

    assert _resolve(conn, "Wren") == "thick"


def test_an_unknown_name_resolves_to_nothing(conn):
    """It must not invent anybody — the caller quarantines instead."""
    _person(conn, "real", "Rowan Alvestad", aliases=["Rowan"], contact="c-1")

    assert _resolve(conn, "Somebody Else") is None
    assert _resolve(conn, "") is None
    assert _resolve(conn, None) is None


def test_it_is_deterministic_when_nothing_separates_them(conn):
    _person(conn, "b-second", "Wren", mentions=3)
    _person(conn, "a-first", "Wren Solberg", aliases=["Wren"], mentions=3)

    assert _resolve(conn, "Wren") == _resolve(conn, "Wren")


def test_case_and_spacing_do_not_matter(conn):
    _person(conn, "real", "Rowan Alvestad", aliases=["Rowan"], contact="c-1")

    assert _resolve(conn, "  ROWAN  ") == "real"
    assert _resolve(conn, "rowan   alvestad") == "real"


def test_it_never_returns_a_non_person(conn):
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name)"
        " VALUES ('proj','project','Rowan','rowan')")
    conn.commit()

    assert _resolve(conn, "Rowan") is None

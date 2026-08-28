"""Owner link: merge one entity into another (drawer-driven inverse of split)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.consolidation import merge_entity_pair, split_surface
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "s.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _mention(conn, entity_id: str, mention_id: str, surface: str) -> None:
    conn.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, "
        "surface_text, confidence, created_at) VALUES (?, ?, ?, 'src', ?, 0.9, '2026-06-01')",
        (mention_id, entity_id, f"rec_{mention_id}", surface),
    )


def test_merge_moves_mentions_and_aliases(conn):
    r = EntityResolver(conn)
    keep = r._create_entity("Jonathan Smith", "person")
    absorb = r._create_entity("Jon", "person")
    _mention(conn, absorb, "m1", "Jon")
    _mention(conn, absorb, "m2", "Jon")
    conn.execute(
        "UPDATE entities SET mention_count=2, aliases_json=? WHERE entity_id=?",
        (json.dumps(["Johnny"]), absorb),
    )
    conn.commit()

    out = merge_entity_pair(conn, keep, absorb)
    assert out["kept"] == keep
    assert out["absorbed"] == absorb
    assert out["mentions_moved"] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_id=?", (absorb,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_mentions WHERE entity_id=?", (keep,)
    ).fetchone()[0] == 2
    aliases = json.loads(
        conn.execute("SELECT aliases_json FROM entities WHERE entity_id=?", (keep,)).fetchone()[0]
    )
    assert "Jon" in aliases
    assert "Johnny" in aliases
    assert out["already_merged"] is False


def test_merge_retry_after_success_is_idempotent(conn):
    """A dropped response must not turn a completed merge into 'entity not found'."""
    r = EntityResolver(conn)
    keep = r._create_entity("Jonathan Smith", "person")
    absorb = r._create_entity("Jon", "person")
    _mention(conn, absorb, "m1", "Jon")
    conn.commit()

    first = merge_entity_pair(conn, keep, absorb)
    assert first["already_merged"] is False
    second = merge_entity_pair(conn, keep, absorb)
    assert second["already_merged"] is True
    assert second["kept"] == keep
    assert second["absorbed"] == absorb
    assert conn.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_id=?", (absorb,)
    ).fetchone()[0] == 0


def test_merge_refreshes_only_the_survivor_dossier(conn, monkeypatch):
    """Full dossier walks belong on rebuild, not on the Link request path."""
    from topos.features.entities import dossier as dossier_mod

    r = EntityResolver(conn)
    keep = r._create_entity("Keep", "person")
    absorb = r._create_entity("Gone", "person")
    _mention(conn, absorb, "m1", "Gone")
    conn.commit()

    calls = {"full": 0, "one": []}

    def _full(_conn):
        calls["full"] += 1
        return 0

    def _one(_conn, entity_id):
        calls["one"].append(entity_id)
        return True

    monkeypatch.setattr(dossier_mod, "refresh_dossiers", _full)
    monkeypatch.setattr(dossier_mod, "refresh_dossier_for_entity", _one)

    merge_entity_pair(conn, keep, absorb)
    assert calls["full"] == 0
    assert calls["one"] == [keep]


def test_merge_rejects_self(conn):
    r = EntityResolver(conn)
    eid = r._create_entity("Solo", "person")
    with pytest.raises(ValueError, match="itself"):
        merge_entity_pair(conn, eid, eid)


def test_merge_rejects_type_mismatch(conn):
    r = EntityResolver(conn)
    person = r._create_entity("Ada", "person")
    org = r._create_entity("Ada Corp", "org")
    with pytest.raises(ValueError, match="types must match"):
        merge_entity_pair(conn, person, org)


def test_merge_unknown_entity_raises(conn):
    r = EntityResolver(conn)
    keep = r._create_entity("Keep", "person")
    with pytest.raises(LookupError):
        merge_entity_pair(conn, keep, "ent_missing")
    with pytest.raises(LookupError):
        merge_entity_pair(conn, "ent_missing", keep)


def test_merge_clears_no_bind_so_rebind_works(conn):
    """After Unlink writes a no_bind guard, explicit Link must clear it."""
    r = EntityResolver(conn)
    conn.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self) "
        "VALUES ('c-cd', 'ds', 'import', 'Romeo Tango', 0)"
    )
    keep = r._create_entity("Romeo Tango", "person", contact_id="c-cd")
    for i in range(2):
        _mention(conn, keep, f"m{i}", "Romeo")
    conn.execute("UPDATE entities SET mention_count=2 WHERE entity_id=?", (keep,))
    conn.commit()

    split_out = split_surface(conn, keep, "Romeo")
    absorb = split_out["new_entity_id"]
    assert absorb

    # Guard is active: Romeo must not resolve back to Romeo Tango.
    hit, _ = EntityResolver(conn).resolve("Romeo", entity_type="person")
    assert hit != keep

    merge_entity_pair(conn, keep, absorb)

    # Guard cleared: Romeo can bind to Romeo Tango again.
    hit2, _ = EntityResolver(conn).resolve("Romeo", entity_type="person")
    assert hit2 == keep
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_review WHERE kind='no_bind' AND candidate_entity_id=?",
        (keep,),
    ).fetchone()[0] == 0

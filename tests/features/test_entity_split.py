"""Owner unbind: split a surface's mentions OUT of an entity, permanently.

The accidental-merge case (Claire → contact "Claire Duncombe" via the
resolver's unique-contact tier) needs two things:
  1. split: mentions whose surface matches move to a fresh entity, counts
     recount, any matching alias is removed;
  2. a persistent no-bind guard — "Claire" is a token-subset of "Claire
     Duncombe" (similarity 1.0), so without a guard the very next resolve()
     re-merges the pair via the contact/alias/fuzzy tiers.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.consolidation import split_surface
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


def _seed_claire(conn) -> str:
    """Contact-anchored 'Claire Duncombe' holding mentions surfaced as 'Claire'."""
    conn.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self) "
        "VALUES ('c-cd', 'ds', 'import', 'Claire Duncombe', 0)"
    )
    r = EntityResolver(conn)
    eid = r._create_entity("Claire Duncombe", "person", contact_id="c-cd")
    for i in range(3):
        _mention(conn, eid, f"m{i}", "Claire")
    conn.execute(
        "UPDATE entities SET mention_count=3 WHERE entity_id=?", (eid,)
    )
    conn.commit()
    return eid


def test_split_moves_mentions_and_recounts(conn):
    eid = _seed_claire(conn)
    out = split_surface(conn, eid, "Claire")
    assert out["mentions_moved"] == 3
    new_id = out["new_entity_id"]
    assert new_id and new_id != eid
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_mentions WHERE entity_id=?", (new_id,)
    ).fetchone()[0] == 3
    assert conn.execute(
        "SELECT mention_count FROM entities WHERE entity_id=?", (eid,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT canonical_name FROM entities WHERE entity_id=?", (new_id,)
    ).fetchone()[0] == "Claire"


def test_split_guard_blocks_rebind_via_contact_tier(conn):
    eid = _seed_claire(conn)
    r = EntityResolver(conn)
    # Baseline: the unique-contact tier binds 'Claire' to Claire Duncombe.
    hit, tier = r.resolve("Claire", entity_type="person")
    assert hit == eid

    split_surface(conn, eid, "Claire")
    # After the split, 'Claire' must NEVER resolve to Claire Duncombe again —
    # it resolves to the split-out entity instead.
    hit2, _tier2 = EntityResolver(conn).resolve("Claire", entity_type="person")
    assert hit2 != eid
    assert conn.execute(
        "SELECT canonical_name FROM entities WHERE entity_id=?", (hit2,)
    ).fetchone()[0] == "Claire"


def test_split_removes_matching_alias(conn):
    r = EntityResolver(conn)
    eid = r._create_entity("Dasha Petrova", "person")
    conn.execute(
        "UPDATE entities SET aliases_json=? WHERE entity_id=?",
        (json.dumps(["Dash"]), eid),
    )
    _mention(conn, eid, "ma", "Dash")
    conn.commit()
    out = split_surface(conn, eid, "Dash")
    assert out["alias_removed"] is True
    aliases = json.loads(
        conn.execute("SELECT aliases_json FROM entities WHERE entity_id=?", (eid,)).fetchone()[0]
    )
    assert "Dash" not in aliases


def test_split_own_name_rejected(conn):
    eid = _seed_claire(conn)
    with pytest.raises(ValueError):
        split_surface(conn, eid, "Claire Duncombe")


def test_split_unknown_entity_raises(conn):
    with pytest.raises(LookupError):
        split_surface(conn, "ent_missing", "X")

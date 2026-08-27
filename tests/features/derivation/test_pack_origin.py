"""`pack_registry.origin` — where a pack came from, answerable AFTER it is loaded.

The registry is the runtime authority for which packs run on this node. Until now it
recorded no provenance, so first-party-ness (D9) was knowable only while the YAML path
was still in scope. That is the wrong lifetime: the compute-time half of the same rule
(`L5-18`) reads the registry, and derived tables holding non-owner claims never pass
through the fact writer at all.

The column is added additively on every seed pass rather than by a registry migration —
bumping `user_version` past the installed engine fences the node out of every write, which
is what happened on 2026-08-25.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.derivation.packs import load_packs
from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry

#: Derived, not hard-coded — a pack added to the catalog should not red the origin tests.
PACK_COUNT = len(load_packs(bundled_pack_dir()))
from topos.storage.db.migrations.pack_registry_v1 import apply_pack_registry_v1_up


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "r.db"))
    c.execute("CREATE TABLE wiki_schema_migrations (migration_id TEXT PRIMARY KEY)")
    apply_pack_registry_v1_up(c)
    yield c
    c.close()


def _origins(conn):
    return dict(conn.execute("SELECT origin, COUNT(*) FROM pack_registry GROUP BY origin"))


def test_shipped_packs_are_recorded_as_first_party(conn):
    seed_pack_registry(conn, bundled_pack_dir())
    assert _origins(conn) == {"first_party": PACK_COUNT}


def test_seeding_is_idempotent(conn):
    seed_pack_registry(conn, bundled_pack_dir())
    seed_pack_registry(conn, bundled_pack_dir())
    assert _origins(conn) == {"first_party": PACK_COUNT}


def test_seeding_does_not_touch_user_version(conn):
    """The whole reason this is not a registry migration."""
    before = conn.execute("PRAGMA user_version").fetchone()[0]
    seed_pack_registry(conn, bundled_pack_dir())
    assert conn.execute("PRAGMA user_version").fetchone()[0] == before


def test_it_upgrades_a_registry_that_predates_the_column(conn):
    """The live shape: the table already exists without the column. A plain CREATE TABLE
    IF NOT EXISTS would leave it missing forever on every node installed before today."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pack_registry)")}
    assert "origin" not in cols, "fixture should start without the column"
    seed_pack_registry(conn, bundled_pack_dir())
    assert "origin" in {r[1] for r in conn.execute("PRAGMA table_info(pack_registry)")}


def _third_party_dir(tmp_path):
    """A stand-in for a runtime-installed pack directory.

    Synthetic rather than the repo catalog on purpose: the catalog is where outward packs
    are AUTHORED, so it legitimately contains a `net_subject: allow` file that the loader
    refuses from an untrusted path. Testing the rule against a directory that exists for
    this test keeps the assertion about provenance rather than about repo layout.
    """
    import yaml

    d = tmp_path / "community-packs"
    d.mkdir()
    (d / "t.yaml").write_text(yaml.safe_dump({
        "pack": "t.community", "version": "0.1.0", "title": "T",
        "sensitivity_class": "personal", "role_policy": "authored_addressed",
        "disclosure_default": "owner_only", "routing": {}, "guidance": {},
        "consumers": ["x"],
        "eval": {"gold": [{"a": 1}], "negative_controls": [{"b": 2}]},
        "predicates": [{"name": "t.thing", "value_type": "string", "cardinality": "single",
                        "temporal": "interval", "altitude": "stated"}],
    }))
    return d


def test_a_pack_outside_the_shipped_directory_is_recorded_as_third_party(conn, tmp_path):
    """Provenance follows the FILE — the mirror is the boundary, not our intentions."""
    seed_pack_registry(conn, _third_party_dir(tmp_path))
    assert _origins(conn) == {"third_party": 1}


def test_origin_is_corrected_rather_than_left_stale(conn, tmp_path):
    """A stale 'first_party' is the one value here that must never persist by inertia."""
    seed_pack_registry(conn, _third_party_dir(tmp_path))
    assert _origins(conn)["third_party"] == 1
    seed_pack_registry(conn, bundled_pack_dir())
    assert _origins(conn)["first_party"] == PACK_COUNT, "origin must follow the file"

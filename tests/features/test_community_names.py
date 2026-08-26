"""S1 — community identity & name history (PLAN_COMMUNITY_NAMING)."""
import sqlite3

import pytest

from topos.features.entities.community_names import (
    core_fingerprint, weighted_jaccard, match_name, record_name, touch_name,
    rename_community,
)
from topos.storage.db.migrations.community_names_v1 import apply_community_names_v1_up


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "cn.db")
    apply_community_names_v1_up(c)
    return c


def _fp(ids_weights):
    ranked = [i for i, _ in ids_weights]
    weights = dict(ids_weights)
    return core_fingerprint(ranked, weights)


CORE = [("topos", 5.0), ("cp", 3.0), ("horos", 2.0), ("deck", 1.0)]


def test_name_survives_peripheral_churn(conn):
    nid = record_name(conn, "Topos Build", _fp(CORE), source="llm", model="stub")
    # same core, new periphery member displaces the tail
    drifted = _fp(CORE[:3] + [("newthing", 1.2)])
    hit = match_name(conn, drifted)
    assert hit and hit["name"] == "Topos Build"
    touch_name(conn, str(hit["name_id"]), drifted)
    row = conn.execute("SELECT times_matched FROM community_names WHERE name_id=?", (nid,)).fetchone()
    assert row[0] == 2


def test_new_set_gets_no_match(conn):
    record_name(conn, "Topos Build", _fp(CORE), source="llm")
    other = _fp([("grandma", 4.0), ("mom", 3.0), ("dominoes", 1.0)])
    assert match_name(conn, other) is None


def test_gradual_drift_keeps_identity_ship_of_theseus(conn):
    fp = _fp(CORE)
    nid = record_name(conn, "Topos Build", fp, source="llm")
    # replace one core member per step; each step matches the REFRESHED core
    members = list(CORE)
    for step in range(3):
        # gradual = the LEAST central member rotates out; losing the most
        # central member in one step is decapitation and SHOULD break identity
        members = members[:-1] + [(f"new{step}", 1.0)]
        fp2 = _fp(members)
        hit = match_name(conn, fp2)
        assert hit and hit["name_id"] == nid, f"lost identity at step {step}"
        touch_name(conn, nid, fp2)


def test_owner_rename_outranks_and_retires(conn):
    record_name(conn, "Topos Build", _fp(CORE), source="llm")
    rename_community(conn, _fp(CORE), "The Workshop")
    hit = match_name(conn, _fp(CORE))
    assert hit and hit["name"] == "The Workshop" and hit["source"] == "owner"
    retired = conn.execute("SELECT COUNT(*) FROM community_names WHERE retired_at IS NOT NULL").fetchone()[0]
    assert retired == 1


def test_owner_wins_ties(conn):
    record_name(conn, "Derived Name", _fp(CORE), source="llm")
    record_name(conn, "Owner Name", _fp(CORE), source="owner")
    hit = match_name(conn, _fp(CORE))
    assert hit and hit["name"] == "Owner Name"


def test_pre_migration_node_fails_open(tmp_path):
    bare = sqlite3.connect(tmp_path / "bare.db")
    assert match_name(bare, _fp(CORE)) is None

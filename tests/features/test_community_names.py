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


# --- S2: naming engine ---
def test_distinctive_terms_contrastive():
    from topos.features.entities.community_naming import distinctive_terms
    here = ["Topos", "topos-react-app", "Control Plane", "deploy scripts"]
    there = ["Grandma", "Mom", "dominoes", "Control Panel", "control freak"]
    terms = distinctive_terms(here, there)
    assert "topos" in terms
    assert "control" not in terms[:2]      # shared vocabulary must not lead


def test_derive_validates_and_falls_back():
    from topos.features.entities.community_naming import derive_community_name
    assert derive_community_name(["A"], [], lambda p: "The Workshop") == "The Workshop"
    assert derive_community_name(["A"], [], lambda p: "A very long sentence that describes everything happening") is None
    assert derive_community_name(["A"], [], lambda p: "+15551234567") is None
    assert derive_community_name(["A"], [], lambda p: (_ for _ in ()).throw(RuntimeError())) is None


def test_rebuild_reuses_history_without_llm(tmp_path, monkeypatch):
    """Second rebuild of the same graph must not invoke the model at all."""
    import sqlite3, json as _json
    from topos.features.entities.maintenance import compute_communities

    monkeypatch.setenv("TOPOS_COMMUNITY_NAMING", "on")
    calls = {"n": 0}

    class FakeAdapter:
        def _generate(self, m, prompt, **kw):
            calls["n"] += 1
            return {"text": "Topos Build"}

    from topos.engine.backends import ollama
    monkeypatch.setattr(ollama, "OllamaAdapter", FakeAdapter)
    monkeypatch.setattr(
        "topos.features.entities.community_naming.resolve_naming_model", lambda c: "stub")

    conn = sqlite3.connect(tmp_path / "g3.db")
    conn.executescript(
        """
        CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
          canonical_name TEXT, normalized_name TEXT, aliases_json TEXT,
          is_self INTEGER DEFAULT 0, mention_count INTEGER DEFAULT 0,
          metadata_json TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE entity_edges (edge_id TEXT PRIMARY KEY, src_entity_id TEXT,
          dst_entity_id TEXT, edge_type TEXT, weight REAL, evidence_count INTEGER,
          last_event_at TEXT, valid_from TEXT, valid_to TEXT, metadata_json TEXT);
        """
    )
    from topos.storage.db.migrations.community_names_v1 import apply_community_names_v1_up
    apply_community_names_v1_up(conn)
    for i, name in enumerate(["Topos", "Control Plane", "Horos", "Deck"]):
        conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name)"
                     " VALUES (?, 'topic', ?, lower(?))", (f"t{i}", name, name))
    for i in range(4):
        conn.execute("INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type, weight)"
                     " VALUES (?, ?, ?, 'co_occurrence', 3.0)", (f"e{i}", f"t{i}", f"t{(i+1) % 4}"))
    conn.commit()

    compute_communities(conn)
    first_calls = calls["n"]
    assert first_calls >= 1
    label = _json.loads(conn.execute("SELECT metadata_json FROM entities WHERE entity_id='t0'")
                        .fetchone()[0] or "{}").get("community_label")
    assert label == "Topos Build"

    compute_communities(conn)          # same graph → history hit, zero new calls
    assert calls["n"] == first_calls
    label2 = _json.loads(conn.execute("SELECT metadata_json FROM entities WHERE entity_id='t0'")
                         .fetchone()[0] or "{}").get("community_label")
    assert label2 == "Topos Build"

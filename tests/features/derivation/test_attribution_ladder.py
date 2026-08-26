"""A-ladder unit probes (PLAN_DERIVATION_WAVE2 §WA) — every case here is a junk
class MEASURED in the 2026-08-26 W1.3 grading, not a hypothetical."""
import json

from topos.features.derivation.template import (
    clean_record_text, person_field_ok, parse_output, TEMPLATE_VERSION,
)
from topos.features.derivation.verify import parse_verdict, apply_verdict
from topos.features.derivation.packs import load_packs
from pathlib import Path

PACK_DIR = Path(__file__).resolve().parents[3].parent / "topos-control-plane" / "derivation-packs"
if not PACK_DIR.exists():  # worktree layout: derivation-packs lives in the CP workspace
    PACK_DIR = Path.home() / "developer" / "topos-control-plane" / "derivation-packs"


def _rel_pack():
    return load_packs(PACK_DIR, only=["relationships.social"])["relationships.social"]


# --- A1: reaction-prefix strip (measured: org "qi" from "+QI just got fired") ---
def test_prefix_strip_reaction():
    assert clean_record_text("+QI just got fired today") == "I just got fired today"
    assert clean_record_text("+KLiked “July 28”") == "Liked “July 28”"
    assert clean_record_text("+!It happened man") == "It happened man"

def test_prefix_strip_preserves_legit_plus():
    assert clean_record_text("+1 that idea") == "+1 that idea"   # space after -> not a reaction glue
    assert clean_record_text("plain text") == "plain text"
    assert clean_record_text("") == ""

# --- A1: person-field guard (measured: him/another/family/'nun in every generation') ---
def test_person_blocklist():
    for bad in ("him", "he", "another", "family", "everyone", "Them"):
        assert not person_field_ok(bad), bad

def test_person_garble_rejected():
    assert not person_field_ok("4ish…eliza os, tiny cloud, teesql")   # measured garble
    assert not person_field_ok("cann or will mitch / brian")
    assert not person_field_ok("a" * 41)

def test_person_kin_and_names_pass():
    for ok in ("mom", "grandma", "brother", "Wiki", "marissa ayala", "Kaspian"):
        assert person_field_ok(ok), ok

def test_parse_output_rejects_pronoun_person():
    pack = _rel_pack()
    raw = json.dumps({"assertions": [
        {"predicate": "rel.relationship", "value": {"person": "him", "role": "close_friend"},
         "confidence": 0.95, "quote": "x"},
        {"predicate": "rel.relationship", "value": {"person": "Wiki", "role": "friend"},
         "about": "owner", "confidence": 0.95, "quote": "My friend Wiki"},
    ]})
    valid, rejects = parse_output(raw, pack)
    assert rejects == 1 and len(valid) == 1 and valid[0]["value"]["person"] == "Wiki"

# --- A2: about routing field ---
def test_about_defaults_unclear_for_person_facts():
    pack = _rel_pack()
    raw = json.dumps({"assertions": [
        {"predicate": "rel.relationship", "value": {"person": "Luc", "role": "partner"},
         "confidence": 0.95, "quote": "her boyfriend Luc"}]})
    valid, _ = parse_output(raw, pack)
    assert valid[0]["about"] == "unclear"      # missing about NEVER silently means owner

def test_about_other_preserved():
    pack = _rel_pack()
    raw = json.dumps({"assertions": [
        {"predicate": "rel.relationship", "value": {"person": "Luc", "role": "partner"},
         "about": "other:Wiki", "confidence": 0.95, "quote": "her boyfriend Luc"}]})
    valid, _ = parse_output(raw, pack)
    assert valid[0]["about"] == "other:Wiki"

def test_template_version_bumped():
    assert TEMPLATE_VERSION == "shadow-7"

# --- A4: verdict parsing + merge semantics ---
def test_verdict_reject_unsupported():
    a = {"predicate": "work.career_event", "value": {"event": "raise"}, "about": "owner"}
    v = parse_verdict('{"supported": false, "about": "owner", "fields_ok": true, "reason": "fundraise not salary"}')
    assert apply_verdict(a, v)["verifier_status"] == "rejected"

def test_verdict_demotes_owner_to_other():
    a = {"predicate": "rel.relationship", "value": {"person": "Luc", "role": "partner"}, "about": "owner"}
    v = parse_verdict('{"supported": true, "about": "other:Wiki", "fields_ok": true, "reason": "Wikis boyfriend"}')
    out = apply_verdict(a, v)
    assert out["verifier_status"] == "rerouted" and out["about"] == "other:Wiki"

def test_verdict_never_promotes_to_owner():
    a = {"predicate": "rel.relationship", "value": {"person": "Luc", "role": "partner"}, "about": "unclear"}
    v = parse_verdict('{"supported": true, "about": "owner", "fields_ok": true, "reason": "looks fine"}')
    out = apply_verdict(a, v)   # two passes must AGREE before a fact lands on the owner
    assert out["about"] == "unclear" and out["verifier_status"] == "rerouted"

def test_verdict_error_fails_open_flagged():
    a = {"predicate": "health.symptom", "value": "tired", "about": "owner"}
    out = apply_verdict(a, parse_verdict("no json here"))
    assert out["verifier_status"] == "error"

def test_verdict_bad_about_string_coerced():
    v = parse_verdict('{"supported": true, "about": "the owner maybe", "fields_ok": true}')
    assert v["about"] == "unclear"


# --- A3: writer routing (real sqlite, real schema slices) ---
import sqlite3
import pytest

@pytest.fixture
def a3_writer(tmp_path):
    conn = sqlite3.connect(tmp_path / "a3.db")
    conn.executescript("""
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
        normalized_name TEXT, aliases_json TEXT, is_self INTEGER DEFAULT 0);
      CREATE TABLE fact_conflicts (conflict_id TEXT PRIMARY KEY, subject_entity_id TEXT NOT NULL,
        predicate TEXT NOT NULL, incumbent_object_id TEXT NOT NULL, challenger_value TEXT NOT NULL,
        challenger_confidence REAL, status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT (datetime('now')));
      INSERT INTO entities VALUES ('ent_owner','person','owner','[]',1);
      INSERT INTO entities VALUES ('ent_wiki','person','wiki','[]',0);
      CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, signal_dimension TEXT,
        object_type TEXT, object_key TEXT, payload_json TEXT, confidence REAL,
        source_refs_json TEXT, valid_from TEXT, valid_to TEXT, extractor_version TEXT,
        created_at TEXT, updated_at TEXT, created_by TEXT, updated_by TEXT,
        period_start TEXT, period_end TEXT, ontology_id TEXT, ontology_version TEXT,
        altitude TEXT);
    """)
    from topos.features.derivation.writer import DerivationWriter
    w = DerivationWriter(conn, model="test-model")
    return w, conn

def _rel_assert(w, about):
    pack = _rel_pack()
    return w.assert_pack_fact(pack=pack, predicate="rel.relationship",
        subject_entity_id="ent_owner", value={"person": "Luc", "role": "partner"},
        actor_role="authored", source_refs=[{"table": "t", "record_id": "r1"}],
        confidence=0.9, about=about)

def test_a3_other_resolved_routes_to_dossier(a3_writer):
    w, conn = a3_writer
    out = _rel_assert(w, "other:Wiki")
    assert out["outcome"] not in ("quarantined", "role_reject"), out
    assert w.stats.get("routed_dossier") == 1
    # the fact must NOT sit on the owner
    row = conn.execute("SELECT COUNT(*) FROM signal_objects WHERE object_type='fact'"
                       " AND object_key LIKE 'fact:ent_owner:%'").fetchone()
    assert row[0] == 0

def test_a3_other_unresolved_quarantines(a3_writer):
    w, conn = a3_writer
    out = _rel_assert(w, "other:Zorbo Nobody")
    assert out["outcome"] == "quarantined"
    q = conn.execute("SELECT incumbent_object_id FROM fact_conflicts").fetchone()[0]
    assert q.startswith("quarantine:dossier_unresolved")

def test_a3_unclear_quarantines_never_owner(a3_writer):
    w, conn = a3_writer
    out = _rel_assert(w, "unclear")
    assert out["outcome"] == "quarantined"
    assert conn.execute("SELECT COUNT(*) FROM signal_objects WHERE object_type='fact'").fetchone()[0] == 0

def test_a3_owner_unchanged_default(a3_writer):
    w, conn = a3_writer
    out = _rel_assert(w, "owner")
    assert out["outcome"] not in ("quarantined",)

"""Decision 5 (2026-09-05): an OUTWARD pack cannot be enabled without the owner's yes.

`set_pack_enabled` was a bare UPDATE. The one bundled pack that writes facts about
non-owners (`net.capability`, `net_subject: allow`) could be switched on with the same
gesture as a pack about the owner's own habits. These tests make the gate structural.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.derivation import surfaces as S
from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE pack_registry (pack_id TEXT PRIMARY KEY, version TEXT, enabled INTEGER,
        disclosure_default TEXT, origin TEXT NOT NULL DEFAULT 'unknown', last_run_at TEXT,
        created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT, canonical_name TEXT,
        normalized_name TEXT, aliases_json TEXT, is_self INTEGER);
      CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, object_type TEXT, ontology_id TEXT,
        valid_to TEXT);
      CREATE TABLE fact_conflicts (conflict_id TEXT PRIMARY KEY, predicate TEXT, status TEXT);
    """)
    for i in range(7):
        conn.execute("INSERT INTO entities VALUES (?,?,?,?,?,?)",
                     (f"p{i}", "person", f"Person {i}", f"person {i}", "[]", 0))
    conn.execute("INSERT INTO entities VALUES ('me','person','Owner','owner','[]',1)")
    # a bare number is a person entity the write gate can never admit — not a subject
    conn.execute("INSERT INTO entities VALUES ('raw','person','+15125550199','+15125550199','[]',0)")
    seed_pack_registry(conn, bundled_pack_dir())
    return conn


def _enabled(conn, pack_id):
    return bool(conn.execute("SELECT enabled FROM pack_registry WHERE pack_id=?", (pack_id,)).fetchone()[0])


def test_an_outward_pack_refuses_to_enable_without_consent():
    conn = _conn()
    assert not _enabled(conn, "net.capability")
    with pytest.raises(S.ConsentRequired) as info:
        S.set_pack_enabled(conn, "net.capability", True)
    assert not _enabled(conn, "net.capability"), "refused means nothing changed"
    terms = info.value.terms
    assert terms["net_subject"] == "allow" and terms["role_policy"] == "any_with_label"
    assert terms["subjects_in_scope"] == 7, "the owner is never a subject, nor is a bare number"
    assert "nameable people" in terms["message"]
    assert "OTHER than you" in terms["message"] and "reads text they wrote" in terms["message"]


def test_consent_enables_and_is_recorded_on_the_row():
    conn = _conn()
    assert S.set_pack_enabled(conn, "net.capability", True, consent=True, consent_note="yes, shown 2026-09-05")
    assert _enabled(conn, "net.capability")
    row = conn.execute("SELECT consented_at, consent_note FROM pack_registry WHERE pack_id='net.capability'").fetchone()
    assert row[0] and row[1] == "yes, shown 2026-09-05"


def test_disabling_never_needs_consent_and_keeps_the_record():
    conn = _conn()
    S.set_pack_enabled(conn, "net.capability", True, consent=True)
    assert S.set_pack_enabled(conn, "net.capability", False)
    assert not _enabled(conn, "net.capability")
    assert conn.execute("SELECT consented_at FROM pack_registry WHERE pack_id='net.capability'").fetchone()[0]


def test_a_pack_about_the_owner_needs_no_ceremony():
    conn = _conn()
    assert S.consent_terms(conn, "behavior.habits") is None
    assert S.set_pack_enabled(conn, "behavior.habits", True)
    assert _enabled(conn, "behavior.habits")


def test_the_catalog_says_which_packs_are_outward_and_whether_the_owner_said_yes():
    conn = _conn()
    before = {p["pack_id"]: p for p in S.list_packs(conn)["packs"]}
    assert before["net.capability"]["consent_required"] is True
    assert before["net.capability"]["consented_at"] is None
    assert before["behavior.habits"]["consent_required"] is False
    S.set_pack_enabled(conn, "net.capability", True, consent=True)
    after = {p["pack_id"]: p for p in S.list_packs(conn)["packs"]}
    assert after["net.capability"]["consented_at"]


def test_the_only_outward_pack_in_the_wheel_is_the_one_we_know_about():
    """A second `net_subject: allow` pack must arrive with its own decision, not by drift."""
    from topos.features.derivation.packs import load_packs

    outward = sorted(pid for pid, p in load_packs(bundled_pack_dir()).items()
                     if str(getattr(p, "net_subject", "deny")) == "allow")
    assert outward == ["net.capability"]

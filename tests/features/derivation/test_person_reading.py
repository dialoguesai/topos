"""The reading — cited rows in, cited sentences out, nothing else.

The failure this file guards is fluency: a model asked to read a relationship will write a
confident paragraph whether or not the evidence supports it. Every test here is a case
where the honest output is LESS than the model offered.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.derivation import person_reading as PR


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
        canonical_name TEXT, normalized_name TEXT, aliases_json TEXT, is_self INTEGER);
      CREATE TABLE entity_mentions (mention_id TEXT PRIMARY KEY, entity_id TEXT,
        record_id TEXT, source_id TEXT, canonical_table TEXT, surface_text TEXT,
        confidence REAL, event_at TEXT, created_at TEXT, authored_by_owner INTEGER);
      CREATE TABLE journal_entries (entry_id TEXT PRIMARY KEY, entry_at TEXT, content TEXT,
        people TEXT, source_id TEXT);
      CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, signal_dimension TEXT NOT NULL,
        object_type TEXT NOT NULL, object_key TEXT NOT NULL, payload_json TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.5, source_refs_json TEXT NOT NULL DEFAULT '[]',
        valid_from TEXT NOT NULL, valid_to TEXT, extractor_version TEXT NOT NULL DEFAULT 'v1',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT 'system',
        updated_by TEXT NOT NULL DEFAULT 'system', period_start TEXT, period_end TEXT,
        ontology_id TEXT, ontology_version TEXT, altitude TEXT);
    """)
    return conn


def _person(conn, eid="e1", name="Rowan Vale", journal_lines=6):
    conn.execute("INSERT INTO entities VALUES (?,?,?,?,?,?)", (eid, "person", name, name.lower(), "[]", 0))
    for i in range(journal_lines):
        rid = f"j{eid}{i}"
        conn.execute("INSERT INTO journal_entries VALUES (?,?,?,?,?)",
                     (rid, f"2026-08-{i + 1:02d}T09:00:00Z",
                      f"Long walk with {name} today, we argued about the scheduler and then made up over coffee, number {i}.",
                      name, "grow_journal"))
        conn.execute("INSERT INTO entity_mentions VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (f"m{eid}{i}", eid, rid, "grow_journal", "journal_entries", name, 0.9,
                      f"2026-08-{i + 1:02d}T09:00:00Z", f"2026-08-{i + 1:02d}T09:00:00Z", 1))
    conn.commit()
    return {"node_id": f"ent:{eid}", "entity_id": eid, "contact_id": None, "messenger_keys": [],
            "label": name, "is_owner": False, "needs_name": False, "band": "named",
            "evidence": {"messaged": False, "mentioned": True}, "mention_count": journal_lines,
            "facts": [{"predicate": "rel.relationship_event", "event": "conflict", "quote": "we argued",
                       "stated_by_owner": True, "at": "2026-08-03", "pack": "relationships.social"}]}


# --- the checker -------------------------------------------------------------------

def test_uncited_sentences_are_deleted_not_kept():
    out = PR.parse_reading(
        "They began as a walking companion [e1]. They are secretly in love with you. "
        "The argument in August is the one tension the record shows [e2, e3].",
        ["e1", "e2", "e3"])
    assert [s["refs"] for s in out["sentences"]] == [["e1"], ["e2", "e3"]]
    assert out["dropped"] == 1
    assert all("[" not in s["text"] for s in out["sentences"]), "citations are data, not prose"


def test_a_citation_to_a_row_that_does_not_exist_is_no_citation():
    out = PR.parse_reading("A confident claim [e9].", ["e1"])
    assert out["sentences"] == [] and out["dropped"] == 1


def test_a_leaked_heading_is_stripped():
    out = PR.parse_reading("Reading: The walks are the motif [e1].", ["e1"])
    assert out["sentences"][0]["text"] == "The walks are the motif."


# --- evidence ----------------------------------------------------------------------

def test_evidence_is_the_owners_rows_first_then_facts_then_measures():
    conn = _conn()
    node = _person(conn)
    rows = PR.assemble_evidence(conn, node, archetype={"sentence": "An opener who keeps a steady rhythm."},
                                warmth={"warmth_band": "warm"})
    kinds = [r["kind"] for r in rows]
    assert kinds[:6] == ["owner_wrote"] * 6
    assert "fact" in kinds and kinds.index("fact") > kinds.index("owner_wrote")
    assert kinds[-2:] == ["measure", "measure"]
    assert rows[0]["id"] == "e1" and rows[-1]["id"] == f"e{len(rows)}"
    assert rows[0]["at"] == "2026-08-06", "newest owner sentence first"
    assert "coffee" in rows[0]["text"], "the full journal line, not the 220-char list snippet"


def test_the_evidence_hash_moves_only_when_the_evidence_does():
    conn = _conn()
    node = _person(conn)
    a = PR.evidence_hash(PR.assemble_evidence(conn, node))
    b = PR.evidence_hash(PR.assemble_evidence(conn, node))
    assert a == b
    conn.execute("INSERT INTO journal_entries VALUES ('jx','2026-08-20T09:00:00Z','Rowan Vale moved house.','Rowan Vale','grow_journal')")
    conn.execute("INSERT INTO entity_mentions VALUES ('mx','e1','jx','grow_journal','journal_entries','Rowan Vale',0.9,'2026-08-20T09:00:00Z','2026-08-20T09:00:00Z',1)")
    conn.commit()
    assert PR.evidence_hash(PR.assemble_evidence(conn, node)) != a


# --- eligibility: the compute rule -------------------------------------------------

def test_a_bare_identifier_is_not_a_subject_and_a_stranger_is_not_read():
    assert PR.eligible({"needs_name": True, "closeness": 0.9}, owner_rows=20) is None
    assert PR.eligible({"needs_name": False, "closeness": None}, owner_rows=PR.MIN_OWNER_ROWS - 1) is None
    assert PR.eligible({"needs_name": False, "closeness": None}, owner_rows=PR.MIN_OWNER_ROWS) == "written_about"
    assert PR.eligible({"needs_name": False, "closeness": 0.4}, owner_rows=0) == "measured_tie"
    assert PR.eligible({"is_owner": True, "closeness": 1.0}, owner_rows=99) is None


# --- the run -----------------------------------------------------------------------

def _fake_llm(text):
    def llm(prompt):
        llm.prompts.append(prompt)
        return text
    llm.prompts = []
    llm.model_name = "fake"
    return llm


def test_a_run_writes_one_stored_reading_per_eligible_person_and_skips_unchanged():
    conn = _conn()
    node = _person(conn)
    llm = _fake_llm("They began as a walking companion [e1]. The August argument is the tension [e7]. "
                    "This sentence has no row behind it.")
    stats = PR.refresh_person_readings(conn, "d", llm=llm, nodes=[node], signals={}, relationships={})
    assert stats["written"] == 1 and stats["eligible"] == 1 and stats["model"] == "fake"
    stored = PR.load_person_readings(conn)["ent:e1"]
    assert [s["refs"] for s in stored["sentences"]] == [["e1"], ["e7"]]
    assert stored["dropped_uncited"] == 1
    assert stored["subject"] == "dyad" and stored["disclosure"] == "owner_only"
    assert stored["reason"] == "written_about"
    # the prompt carried the evidence rows and only them
    assert "[e1] owner_wrote" in llm.prompts[0] and "[e7] fact" in llm.prompts[0]
    # second run, nothing changed: no model call, nothing rewritten
    again = PR.refresh_person_readings(conn, "d", llm=llm, nodes=[node], signals={}, relationships={})
    assert again["unchanged"] == 1 and again["written"] == 0 and len(llm.prompts) == 1


def test_a_reading_the_model_cannot_cite_is_stored_as_an_abstention():
    conn = _conn()
    node = _person(conn)
    llm = _fake_llm("A warm and generous soul who lights up every room.")
    stats = PR.refresh_person_readings(conn, "d", llm=llm, nodes=[node], signals={}, relationships={})
    assert stats["abstained"] == 1 and stats["written"] == 1
    stored = PR.load_person_readings(conn)["ent:e1"]
    assert stored["sentences"] == [] and stored["dropped_uncited"] == 1


def test_a_changed_reading_supersedes_rather_than_overwrites():
    """History matters for a living document: the old row closes, a new one opens."""
    conn = _conn()
    node = _person(conn)
    PR.refresh_person_readings(conn, "d", llm=_fake_llm("First reading [e1]."), nodes=[node],
                               signals={}, relationships={})
    conn.execute("INSERT INTO journal_entries VALUES ('jx','2026-08-20T09:00:00Z','Rowan Vale moved house.','Rowan Vale','grow_journal')")
    conn.execute("INSERT INTO entity_mentions VALUES ('mx','e1','jx','grow_journal','journal_entries','Rowan Vale',0.9,'2026-08-20T09:00:00Z','2026-08-20T09:00:00Z',1)")
    conn.commit()
    PR.refresh_person_readings(conn, "d", llm=_fake_llm("Second reading [e1]."), nodes=[node],
                               signals={}, relationships={})
    rows = conn.execute("SELECT valid_to IS NULL, payload_json FROM signal_objects WHERE object_type='person_reading' ORDER BY created_at").fetchall()
    assert len(rows) == 2
    assert [bool(r[0]) for r in rows] == [False, True]
    assert json.loads(rows[1][1])["sentences"][0]["text"] == "Second reading."


def test_the_prompt_template_carries_no_personal_data():
    """Templates are published source. The person's label enters at runtime only."""
    assert "{label}" in PR.PROMPT and "{evidence}" in PR.PROMPT
    import re
    assert not re.search(r"\+1\d{10}|@[a-z]+\.[a-z]{2,}", PR.PROMPT)


# --- the model choice --------------------------------------------------------------

def test_the_reading_rides_whatever_model_is_already_warm():
    """Measured: the 27B held the GPU and the configured 9B timed out at 240s. A resident
    model answers in a minute; loading a second one swaps 15GB and stalls the chat too."""
    assert PR.choose_model("qwen3.5:9b-mlx", ["smtek/Qwen3.8-27B:IQ2_M"]) == "smtek/Qwen3.8-27B:IQ2_M"
    assert PR.choose_model("qwen3.5:9b-mlx", ["qwen3.5:9b-mlx", "other"]) == "qwen3.5:9b-mlx"
    assert PR.choose_model("qwen3.5:9b-mlx", []) == "qwen3.5:9b-mlx"


def test_the_budget_goes_to_the_closest_people_first():
    conn = _conn()
    far = _person(conn, eid="e1", name="Rowan Vale")
    near = _person(conn, eid="e2", name="Sasha Okoro")
    near["closeness"] = 0.9
    seen = []

    def llm(prompt):
        seen.append(prompt)
        return "A cited sentence [e1]."
    llm.model_name = "fake"
    stats = PR.refresh_person_readings(conn, "d", llm=llm, nodes=[far, near], signals={},
                                       relationships={}, limit=1)
    assert stats["written"] == 1
    assert "Sasha Okoro" in seen[0] and len(seen) == 1


# --- the sweep ---------------------------------------------------------------------

def test_the_derived_drift_sweep_does_not_reap_a_reading():
    """THE BUG: the first live pass wrote refs at a synthetic `person_graph` table; the
    drift sweep found no such records and closed all twelve readings 32 minutes later.
    Run the REAL sweep over a stored reading: it must stay open."""
    from topos.features.lifecycle.derived_scrub import close_dangling_facts

    conn = _conn()
    node = _person(conn)
    PR.refresh_person_readings(conn, "d", llm=_fake_llm("A cited sentence [e1]."), nodes=[node],
                               signals={}, relationships={})
    refs = json.loads(conn.execute(
        "SELECT source_refs_json FROM signal_objects WHERE object_type='person_reading'").fetchone()[0])
    assert all(r.get("source_id") for r in refs if r.get("kind") == "owner_wrote"), refs
    assert {"table": "entities", "record_id": "e1", "kind": "subject"} in refs
    assert not any(r.get("table") == "person_graph" for r in refs)
    closed = close_dangling_facts(conn)
    assert closed == 0
    assert len(PR.load_person_readings(conn)) == 1, "still active after the sweep"


def test_a_reading_with_no_record_backed_evidence_keeps_an_unverifiable_ref():
    """The sweep treats a ref it cannot check as alive on purpose; a reading with only
    measured rows must carry one rather than a made-up table it can check and fail."""
    refs = PR.provenance_refs({"node_id": "msg:+15550001111", "entity_id": None},
                              [{"kind": "measure", "text": "warm", "table": "dyad stats", "record_id": ""}])
    assert refs == [{"table": "", "record_id": "", "note": "person_graph node msg:+15550001111",
                     "kind": "unverifiable"}]

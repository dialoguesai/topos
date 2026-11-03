"""Topos Facts in the per-pack pass: which packs skip the LLM, and what the facts carry.

The classifier is a stub here (no weights, no network): these tests pin the routing and the
contract with the writer, not the model. Picking it is the owner's opt-in under Node
functions → Facts extraction; with nothing picked the pass is the LLM's, as before.
"""
import json
import sqlite3

import pytest

from topos.enrichment.jobs.canonical.derivation_job import run_derivation_batch
from topos.storage.db.migrations import apply_all_migrations

ROW = {"content": "Signed the offer: starting as Staff Engineer at Meridian in March. My goal is to finally run a marathon.",
       "message_id": "m1", "_table": "conversation_messages", "actor_role": "authored",
       "event_at": "2026-08-01", "source_id": "imessage"}


@pytest.fixture
def node_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "node.db")
    apply_all_migrations(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_owner','person','Owner','owner','[]',1)")
    from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
    seed_pack_registry(conn, bundled_pack_dir())
    conn.execute("UPDATE pack_registry SET enabled = CASE WHEN pack_id IN ('work.career','aspirations.goals') THEN 1 ELSE 0 END")
    conn.commit()
    return conn


class StubClassifier:
    def __init__(self, knows=("work.career", "aspirations.goals")):
        self._knows, self.calls = set(knows), []

    def knows(self, pack_id):
        return pack_id in self._knows

    def extract(self, pack_id, text):
        self.calls.append(pack_id)
        if pack_id == "work.career":
            return [{"predicate": "work.career_event", "value": {"event": "hired", "org": "Meridian"},
                     "confidence": 0.995, "about": "owner"}]
        return []


@pytest.fixture
def llm_calls(monkeypatch):
    """Stub Ollama: extraction prompts reply with one goal, the verifier accepts."""
    from topos.engine.backends import ollama
    calls = []
    goal = json.dumps({"assertions": [{"predicate": "asp.goal", "value": {"goal": "run a marathon"},
                                       "about": "owner", "confidence": 0.8, "quote": "run a marathon"}]})
    verify = json.dumps({"supported": True, "about": "owner", "fields_ok": True, "reason": "stated"})

    def fake(self, model, prompt, **kw):
        if "Respond with exactly" in prompt:
            return {"text": "ok"}
        if "strict fact-checker" in prompt:
            return {"text": verify}
        calls.append(prompt)
        return {"text": goal}

    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", fake)
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c: "stub-9b")
    return calls


def _pick_topos_facts(conn):
    conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('facts_llm_provider', 'topos_facts')")
    conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('facts_llm_model', 'v0.1')")
    conn.commit()


def _facts(conn):
    rows = conn.execute("SELECT ontology_id, payload_json FROM signal_objects"
                        " WHERE object_type='fact' AND ontology_id IS NOT NULL").fetchall()
    return {pack: json.loads(p)["extractor"]["model"] for pack, p in rows}


def test_classifier_takes_its_packs_and_goals_stay_on_the_llm(node_db, llm_calls, monkeypatch):
    stub = StubClassifier()
    monkeypatch.setattr("topos.features.derivation.topos_facts.load", lambda: stub)
    _pick_topos_facts(node_db)
    stats = {}
    run_derivation_batch(node_db, [ROW], stats=stats)
    assert stub.calls == ["work.career"]           # goals never reach the classifier
    assert len(llm_calls) == 1                     # one LLM extraction: the goals pack
    assert _facts(node_db) == {"work.career": "topos-facts-v0.1", "aspirations.goals": "stub-9b"}


def test_a_pack_the_classifier_does_not_know_stays_on_the_llm(node_db, llm_calls, monkeypatch):
    stub = StubClassifier(knows=())
    monkeypatch.setattr("topos.features.derivation.topos_facts.load", lambda: stub)
    _pick_topos_facts(node_db)
    run_derivation_batch(node_db, [ROW], stats={})
    assert stub.calls == [] and len(llm_calls) == 2


def test_nothing_picked_means_no_classifier(node_db, llm_calls, monkeypatch):
    def boom():
        raise AssertionError("Topos Facts must not load unless the owner picked it")
    monkeypatch.setattr("topos.features.derivation.topos_facts.load", boom)
    run_derivation_batch(node_db, [ROW], stats={})
    assert len(llm_calls) == 2


def test_classifier_that_cannot_load_defers_the_batch(node_db, llm_calls, monkeypatch):
    def offline():
        raise OSError("no network and no cached weights")
    monkeypatch.setattr("topos.features.derivation.topos_facts.load", offline)
    _pick_topos_facts(node_db)
    stats = {}
    assert run_derivation_batch(node_db, [ROW], stats=stats) == 0
    assert stats["deferred"].startswith("topos_facts_unavailable")
    assert llm_calls == []
    assert node_db.execute("SELECT COUNT(*) FROM derivation_progress").fetchone()[0] == 0


def test_classifier_output_goes_through_the_llm_parser(node_db, llm_calls, monkeypatch):
    """A predicate outside the pack, or an option outside its enum, is rejected exactly as
    an LLM reply would be: the classifier gets no schema bypass."""
    stub = StubClassifier()
    stub.extract = lambda pack_id, text: [
        {"predicate": "work.not_a_predicate", "value": "x", "confidence": 0.999, "about": "owner"},
        {"predicate": "work.career_event", "value": {"event": "not_an_event"}, "confidence": 0.999, "about": "owner"},
    ] if pack_id == "work.career" else []
    monkeypatch.setattr("topos.features.derivation.topos_facts.load", lambda: stub)
    _pick_topos_facts(node_db)
    stats = {}
    run_derivation_batch(node_db, [ROW], stats=stats)
    assert stats.get("parse_rejects", 0) >= 2
    assert "work.career" not in _facts(node_db)

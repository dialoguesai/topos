"""W2.3 DerivationJob — batch worker probes with stubbed models (no Ollama)."""
import json
import sqlite3

import pytest

from topos.storage.db.migrations import apply_all_migrations
from topos.enrichment.jobs.canonical.derivation_job import run_derivation_batch


@pytest.fixture
def node_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "node.db")
    apply_all_migrations(conn)
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_owner','person','Owner','owner','[]',1)")
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_wiki','person','Wiki','wiki','[]',0)")
    conn.commit()
    return conn


def _stub_llm(extract_reply, verify_reply="ok"):
    """Patch OllamaAdapter._generate: extraction prompts get extract_reply,
    verifier prompts get verify_reply, the availability probe gets 'ok'."""
    def fake(self, model, prompt, **kw):
        if "Respond with exactly" in prompt:
            return {"text": "ok"}
        if "strict fact-checker" in prompt:
            return {"text": verify_reply}
        return {"text": extract_reply}
    return fake


ROW = {"content": "Signed the offer — starting as Staff Engineer at Meridian in March",
       "message_id": "m1", "_table": "conversation_messages", "actor_role": "authored",
       "event_at": "2026-08-01", "source_id": "imessage"}


def test_batch_writes_verified_fact(node_db, monkeypatch):
    from topos.engine.backends import ollama
    extract = json.dumps({"assertions": [{
        "predicate": "work.career_event", "value": {"event": "hired", "org": "Meridian"},
        "about": "owner", "occurrence_date": None, "confidence": 0.9,
        "quote": "Signed the offer"}]})
    verify = json.dumps({"supported": True, "about": "owner", "fields_ok": True, "reason": "stated"})
    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", _stub_llm(extract, verify))
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model",
                        lambda s, c: "stub-9b")
    stats = {}
    n = run_derivation_batch(node_db, [ROW], stats=stats)
    assert n == 1 and stats.get("v_accepted") == 1
    row = node_db.execute("SELECT ontology_id, extractor_version FROM signal_objects"
                          " WHERE object_type='fact' AND ontology_id IS NOT NULL").fetchone()
    assert row and row[0] == "work.career"
    assert node_db.execute("SELECT COUNT(*) FROM derivation_progress").fetchone()[0] > 0


def test_batch_verifier_reject_writes_nothing(node_db, monkeypatch):
    from topos.engine.backends import ollama
    extract = json.dumps({"assertions": [{
        "predicate": "work.career_event", "value": {"event": "raise"},
        "about": "owner", "occurrence_date": None, "confidence": 0.85, "quote": "the $2.5M raise"}]})
    verify = json.dumps({"supported": False, "about": "owner", "fields_ok": True,
                         "reason": "fundraise, not salary"})
    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", _stub_llm(extract, verify))
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model",
                        lambda s, c: "stub-9b")
    stats = {}
    n = run_derivation_batch(node_db, [ROW], stats=stats)
    assert n == 0 and stats.get("v_rejected") == 1
    assert node_db.execute("SELECT COUNT(*) FROM signal_objects WHERE object_type='fact'"
                           " AND ontology_id IS NOT NULL").fetchone()[0] == 0


def test_batch_defers_when_verifier_unavailable(node_db, monkeypatch):
    from topos.engine.backends import ollama
    def dead(self, model, prompt, **kw):
        if "strict fact-checker" in prompt or "Respond with exactly" in prompt:
            raise RuntimeError("no such model")
        return {"text": "{}"}
    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", dead)
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model",
                        lambda s, c: "stub-9b")
    stats = {}
    n = run_derivation_batch(node_db, [ROW], stats=stats)
    assert n == 0 and "verifier_unavailable" in str(stats.get("deferred"))


def test_only_wave_a_enabled_by_default(node_db, monkeypatch):
    from topos.features.derivation.registry import seed_pack_registry, bundled_pack_dir
    seed_pack_registry(node_db, bundled_pack_dir())
    rows = dict(node_db.execute("SELECT pack_id, enabled FROM pack_registry").fetchall())
    assert rows["relationships.social"] == 1 and rows["work.career"] == 1
    assert rows["health.physical"] == 1 and rows["health.mental"] == 1
    assert rows.get("beliefs.civic", 0) == 0 and rows.get("behavior.habits", 0) == 0
    assert sum(rows.values()) == 4


# --- training-data factory + self-gating (W-B, owner design 2026-08-26) ---

def test_ledger_keeps_rejects_as_hard_negatives(node_db, monkeypatch):
    from topos.engine.backends import ollama
    extract = json.dumps({"assertions": [{
        "predicate": "work.career_event", "value": {"event": "raise"},
        "about": "owner", "occurrence_date": None, "confidence": 0.85, "quote": "the raise"}]})
    verify = json.dumps({"supported": False, "about": "owner", "fields_ok": True,
                         "reason": "fundraise not salary"})
    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", _stub_llm(extract, verify))
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model",
                        lambda s, c: "stub-9b")
    run_derivation_batch(node_db, [ROW], stats={})
    rows = node_db.execute("SELECT vstatus, vreason, written_object_id, quote"
                           " FROM derivation_training_ledger").fetchall()
    assert rows and rows[0][0] == "rejected" and "salary" in rows[0][1]
    assert rows[0][2] is None and rows[0][3] == "the raise"


def test_yield_counters_and_drywell_nudge(node_db, monkeypatch):
    from topos.engine.backends import ollama
    monkeypatch.setattr(ollama.OllamaAdapter, "_generate",
                        _stub_llm(json.dumps({"assertions": []})))
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model",
                        lambda s, c: "stub-9b")
    # simulate 30d of calls with zero yield for an ENABLED pack
    node_db.execute("INSERT OR IGNORE INTO pack_yield (pack_id, day, llm_calls, written)"
                    " VALUES ('work.career', date('now'), 60, 0)")
    node_db.commit()
    run_derivation_batch(node_db, [ROW], stats={})
    kinds = [r[0] for r in node_db.execute(
        "SELECT kind FROM pack_offers WHERE pack_id='work.career'")]
    assert "disable_nudge" in kinds


def test_trial_mints_enable_offer_and_specials_excluded(node_db, monkeypatch):
    from topos.engine.backends import ollama
    extract = json.dumps({"assertions": [{
        "predicate": "interest.favorite", "value": {"category": "music", "item": "T.T. Xray"},
        "about": "owner", "occurrence_date": None, "confidence": 0.9,
        "quote": "I love T.T. Xray"}]})
    verify = json.dumps({"supported": True, "about": "owner", "fields_ok": True, "reason": "stated"})
    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", _stub_llm(extract, verify))
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model",
                        lambda s, c: "stub-9b")
    # hits accumulated on a DISABLED, non-special pack + recent records to sample
    node_db.execute("INSERT OR IGNORE INTO pack_yield (pack_id, day, prefilter_hits)"
                    " VALUES ('interests.taste', date('now'), 40)")
    # and on a special-class pack (must never trial)
    node_db.execute("INSERT OR IGNORE INTO pack_yield (pack_id, day, prefilter_hits)"
                    " VALUES ('beliefs.civic', date('now'), 40)")
    node_db.execute("CREATE TABLE IF NOT EXISTS conversation_messages ("
                    "message_id TEXT PRIMARY KEY, content TEXT, event_at TEXT,"
                    " actor_role TEXT, source_id TEXT)")
    for i in range(12):
        node_db.execute(
            "INSERT INTO conversation_messages (message_id, content, event_at, actor_role, source_id)"
            " VALUES (?, ?, datetime('now'), 'authored', 'imessage')",
            (f"tm{i}", "Third pottery class this month — I'm fully hooked on T.T. Xray music"))
    node_db.commit()
    stats = {}
    run_derivation_batch(node_db, [ROW], stats=stats)
    offers = {r[0]: r[1] for r in node_db.execute("SELECT pack_id, kind FROM pack_offers")}
    assert offers.get("interests.taste") == "enable_offer"
    assert "beliefs.civic" not in offers          # consent precedes computation
    trial_rows = node_db.execute("SELECT COUNT(*) FROM derivation_training_ledger"
                                 " WHERE stage='trial'").fetchone()[0]
    assert trial_rows > 0

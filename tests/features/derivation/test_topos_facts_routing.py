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


@pytest.fixture
def model_cache():
    from topos.engine.model_cache import get_model_cache, reset_model_cache_for_tests
    reset_model_cache_for_tests()
    yield get_model_cache()
    reset_model_cache_for_tests()


def test_the_encoders_load_once_and_stay_resident_across_batches(model_cache, monkeypatch, tmp_path):
    """Measured 6 Nov 2026 on an isolated node: the encoders (about 5 GiB float32) loaded once
    per derivation batch, 8 of 8, because every ingest chunk cycles more small encoders through
    the cache than its LRU budget holds. The slot is pinned: one load, resident until released."""
    from topos.engine.model_cache import ModelSlot
    from topos.features.derivation import topos_facts as tf
    built = []

    class FakeEncoders:
        def __init__(self, gate_dir, reader_dir, device):
            built.append(device)

    monkeypatch.setattr(tf, "ToposFacts", FakeEncoders)
    monkeypatch.setattr(tf, "_local_dir", lambda *a, **k: tmp_path)
    monkeypatch.setattr(tf, "resolve_device", lambda preferred=None: "cpu")
    first = tf.load()
    for slot in (ModelSlot.PRIVACY_FILTER, ModelSlot.EMBEDDING, ModelSlot.NER, ModelSlot.EMOTION):
        model_cache.acquire(slot, slot.value, lambda s=slot: s.value)   # a chunk's enrichment jobs
    model_cache.trim_to_budget()                                         # the end-of-pipeline flush
    assert tf.load() is first and built == ["cpu"]
    assert model_cache.pinned_slots() == ["topos_facts"]
    tf.release()
    assert "topos_facts" not in model_cache.resident_slots()
    tf.release()                                                         # absent: a no-op


def test_the_encoders_release_themselves_after_the_idle_time(model_cache, monkeypatch, tmp_path):
    """The owner's rule (6 Nov 2026): Topos idle holds no model. The release does not wait
    for the next pipeline's cache trim; a timer drops the pin once the idle time has passed."""
    import time
    from topos.features.derivation import topos_facts as tf
    monkeypatch.setattr(tf, "ToposFacts", lambda gate_dir, reader_dir, device: object())
    monkeypatch.setattr(tf, "_local_dir", lambda *a, **k: tmp_path)
    monkeypatch.setattr(tf, "resolve_device", lambda preferred=None: "cpu")
    monkeypatch.setenv(tf.ENV_IDLE, "0.2")
    tf.load()
    time.sleep(0.1)
    tf.load()                                           # a second batch re-arms the timer
    time.sleep(0.15)
    assert "topos_facts" in model_cache.resident_slots()  # 0.15 s since the last load: still held
    deadline = time.monotonic() + 2
    while "topos_facts" in model_cache.resident_slots() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "topos_facts" not in model_cache.resident_slots()


def test_picking_another_provider_releases_the_encoders(node_db, llm_calls, monkeypatch):
    released = []
    monkeypatch.setattr("topos.features.derivation.topos_facts.release", lambda: released.append(True))
    run_derivation_batch(node_db, [ROW], stats={})
    assert released == [True]


def test_idle_seconds_reads_the_env(monkeypatch):
    from topos.features.derivation import topos_facts as tf
    monkeypatch.delenv(tf.ENV_IDLE, raising=False)
    assert tf.idle_seconds() == tf.DEFAULT_IDLE_SEC == 300.0   # Ollama's keep-alive: idle holds no model
    monkeypatch.setenv(tf.ENV_IDLE, "0")
    assert tf.idle_seconds() == 0.0
    monkeypatch.setenv(tf.ENV_IDLE, "nonsense")
    assert tf.idle_seconds() == tf.DEFAULT_IDLE_SEC


def test_a_deferral_is_recorded_where_the_facts_config_reads_it(node_db, llm_calls, monkeypatch):
    """Before: one WARNING per deferred batch, and the app's Facts extraction row showed the pick
    as if it were running. Now the config carries {reason, at, batches} until a batch runs."""
    from topos.config.facts_llm import derivation_deferral
    monkeypatch.setattr("topos.features.derivation.topos_facts.load",
                        lambda: (_ for _ in ()).throw(OSError("no cached weights")))
    _pick_topos_facts(node_db)
    run_derivation_batch(node_db, [ROW], stats={})
    run_derivation_batch(node_db, [ROW], stats={})
    standing = derivation_deferral(node_db)
    assert standing["reason"].startswith("topos_facts_unavailable") and standing["batches"] == 2
    assert standing["at"][:4].isdigit()
    monkeypatch.setattr("topos.features.derivation.topos_facts.load", lambda: StubClassifier())
    run_derivation_batch(node_db, [ROW], stats={})
    assert derivation_deferral(node_db) is None


def test_a_deferred_batch_reaches_the_pipeline_as_a_deferral(monkeypatch):
    """The pipeline's deferral path records a debt row and an envelope for a job that returns
    `_deferred`, as the goals job does; a plain stats row was a success with 0 written."""
    import asyncio
    from topos.enrichment.jobs.canonical import derivation_job as dj

    def deferred_batch(conn, rows, *, cancel=None, stats=None):
        stats["deferred"] = "topos_facts_unavailable: no cached weights"
        return 0

    monkeypatch.setattr(dj, "run_derivation_batch", deferred_batch)
    monkeypatch.setattr(dj, "get_db_connection", lambda: object())
    out = asyncio.run(dj.DerivationJob().enrich([ROW]))
    assert out[0]["_deferred"] is True and out[0]["error"].startswith("topos_facts_unavailable")
    monkeypatch.setattr(dj, "run_derivation_batch", lambda conn, rows, *, cancel=None, stats=None: 3)
    assert asyncio.run(dj.DerivationJob().enrich([ROW])) == [{"derivation_written": 3}]


def test_no_write_transaction_spans_a_model_call(node_db, monkeypatch):
    """The job writes on its own connection, outside the write gate, and committed once per
    record: SQLite's write lock was held across every extraction and verifier call of the
    record, and the pipeline's claim_next_job then held the gate up to 160 s blocked on it
    (5-6 Nov 2026, isolated node). Every model call must now start with the lock released."""
    from topos.engine.backends import ollama
    held_open = []
    goal = json.dumps({"assertions": [{"predicate": "asp.goal", "value": {"goal": "run a marathon"},
                                       "about": "owner", "confidence": 0.8, "quote": "run a marathon"}]})
    verify = json.dumps({"supported": True, "about": "owner", "fields_ok": True, "reason": "stated"})

    def fake(self, model, prompt, **kw):
        if node_db.in_transaction:
            held_open.append(prompt[:40])
        if "Respond with exactly" in prompt:
            return {"text": "ok"}
        return {"text": verify if "strict fact-checker" in prompt else goal}

    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", fake)
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c: "stub-9b")
    stub = StubClassifier()
    monkeypatch.setattr("topos.features.derivation.topos_facts.load", lambda: stub)
    _pick_topos_facts(node_db)
    stats = {}
    run_derivation_batch(node_db, [ROW], stats=stats)
    assert stats["written"] >= 1 and stub.calls == ["work.career"]
    assert held_open == []

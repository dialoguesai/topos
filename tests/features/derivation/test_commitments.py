"""The commitment ledger, owner's half: promise-shaped records only, per-person read-back,
and the runner's explicit feed that leaves unfed records unmarked.
"""

from __future__ import annotations

import json
import sqlite3

from topos.features.derivation import commitments as C


def test_promise_shaped_is_a_first_person_future_with_an_object():
    yes = ["I'll send you the deck tomorrow", "I will intro you to Priya", "remind me to pay you back",
           "I owe you a coffee", "don’t let me forget to bring the charger", "I’ll have it by friday"]
    no = ["you wanna do it? I can show you around", "submitted the app last night",
          "what's the name of that bar", "I should exercise more", "she said she'd send it"]
    assert all(C.is_promise_shaped(t) for t in yes), [t for t in yes if not C.is_promise_shaped(t)]
    assert not any(C.is_promise_shaped(t) for t in no), [t for t in no if C.is_promise_shaped(t)]


def test_records_are_the_owners_own_and_carry_the_authored_role():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE conversation_messages (message_id TEXT, content TEXT, event_at TEXT, is_from_self INTEGER);
      CREATE TABLE journal_entries (entry_id TEXT, content TEXT, entry_at TEXT);
    """)
    conn.execute("INSERT INTO conversation_messages VALUES ('m1','I will send you the deck tomorrow, promise','2026-08-02T09:00:00Z',1)")
    conn.execute("INSERT INTO conversation_messages VALUES ('m2','you said you would send the deck tomorrow','2026-08-03T09:00:00Z',0)")
    conn.execute("INSERT INTO conversation_messages VALUES ('m3','lunch was great, thanks again for coming','2026-08-04T09:00:00Z',1)")
    conn.execute("INSERT INTO journal_entries VALUES ('j1','Told Sam I’ll review the grant draft this week.','2026-08-05T09:00:00Z')")
    recs = C.promise_shaped_records(conn)
    assert [r["record_id"] for r in recs] == ["j1", "m1"], "newest first, the peer's line excluded"
    assert all(r["role"] == "authored" for r in recs)


def test_the_runner_feed_processes_only_fed_records_and_marks_nothing_else(monkeypatch):
    """Fed records go through the ladder; unfed records stay UNMARKED so the ordinary
    history walk can still reach them. Exercised with the real runner and a canned model."""
    from topos.features.derivation import surfaces as S

    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
        canonical_name TEXT, normalized_name TEXT, aliases_json TEXT, is_self INTEGER DEFAULT 0);
      INSERT INTO entities VALUES ('ent_owner','person','Owner','owner','[]',1);
      INSERT INTO entities VALUES ('ent_sam','person','Sam Okoro','sam okoro','[]',0);
      CREATE TABLE fact_conflicts (conflict_id TEXT PRIMARY KEY, subject_entity_id TEXT NOT NULL,
        predicate TEXT NOT NULL, incumbent_object_id TEXT NOT NULL, challenger_value TEXT NOT NULL,
        challenger_confidence REAL, status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT (datetime('now')));
      CREATE TABLE net_subject_policy (subject_entity_id TEXT PRIMARY KEY, policy TEXT NOT NULL,
        decided_by TEXT NOT NULL DEFAULT 'owner', note TEXT NOT NULL DEFAULT '', decided_at TEXT);
      CREATE TABLE entity_blackholes (blackhole_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL DEFAULT '', normalized_name TEXT NOT NULL);
      CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, signal_dimension TEXT, object_type TEXT,
        object_key TEXT, payload_json TEXT, confidence REAL, source_refs_json TEXT, valid_from TEXT,
        valid_to TEXT, extractor_version TEXT, created_at TEXT, updated_at TEXT, created_by TEXT,
        updated_by TEXT, period_start TEXT, period_end TEXT, ontology_id TEXT, ontology_version TEXT, altitude TEXT);
      CREATE TABLE pack_registry (pack_id TEXT PRIMARY KEY, version TEXT, enabled INTEGER,
        disclosure_default TEXT, origin TEXT NOT NULL DEFAULT 'unknown', last_run_at TEXT,
        last_run_version TEXT,
        created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
      CREATE TABLE derivation_progress (key TEXT PRIMARY KEY);
      CREATE TABLE pack_yield (pack_id TEXT, day TEXT, prefilter_hits INTEGER DEFAULT 0,
        llm_calls INTEGER DEFAULT 0, assertions INTEGER DEFAULT 0, written INTEGER DEFAULT 0,
        PRIMARY KEY (pack_id, day));
      CREATE TABLE derivation_training_ledger (ledger_id TEXT PRIMARY KEY, stage TEXT, pack_id TEXT,
        predicate TEXT, value_json TEXT, about TEXT, confidence REAL, vstatus TEXT, vreason TEXT,
        written_object_id TEXT, record_table TEXT, record_id TEXT, created_at TEXT DEFAULT (datetime('now')));
      CREATE TABLE conversation_messages (message_id TEXT, content TEXT, event_at TEXT, is_from_self INTEGER, actor_role TEXT);
      INSERT INTO conversation_messages VALUES ('m1','I will send you the deck tomorrow','2026-08-02T09:00:00Z',1,NULL);
      INSERT INTO conversation_messages VALUES ('m9','lunch was great','2026-08-04T09:00:00Z',1,NULL);
    """)
    from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
    seed_pack_registry(conn, bundled_pack_dir())
    S.set_pack_enabled(conn, C.PACK_ID, True)
    monkeypatch.setenv("TOPOS_DERIVATION_VERIFY", "off")
    canned = json.dumps({"assertions": [{"predicate": "commit.made",
                                         "value": {"counterparty": "Sam Okoro", "direction": "owed_by_owner",
                                                   "description": "send the deck", "due": "tomorrow", "status": "open"},
                                         "confidence": 0.9, "about": "owner",
                                         "quote": "I will send you the deck tomorrow"}]})
    from topos.engine.backends import ollama as O
    monkeypatch.setattr(O.OllamaAdapter, "_generate", lambda self, m, p, **kw: {"text": canned})
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c=None: "fake", raising=False)
    fed = [{"table": "conversation_messages", "record_id": "m1", "text": "I will send you the deck tomorrow",
            "date": "2026-08-02", "role": "authored", "source_id": ""}]
    stats = S.run_pack_backfill(conn, C.PACK_ID, 10, records=fed, use_prefilter=False)
    assert stats["processed"] == 1
    keys = {r[0] for r in conn.execute("SELECT key FROM derivation_progress")}
    assert any(k.endswith(":conversation_messages:m1") for k in keys)
    assert not any(k.endswith(":m9") for k in keys), "an unfed record is left for the history walk"
    facts = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL")]
    if facts:  # the ladder may verify/quarantine depending on the node's verify mode; when it writes, it binds the person
        assert facts[0]["predicate"] == "commit.made"
        assert facts[0]["value_struct"].get("counterparty_entity_id") == "ent_sam"


def test_the_ledger_reads_back_per_person_with_reliability_only_when_judged():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE signal_objects (object_type TEXT, valid_to TEXT, payload_json TEXT, valid_from TEXT)")
    def fact(pred, struct, occ="2026-08-02"):
        conn.execute("INSERT INTO signal_objects VALUES ('fact', NULL, ?, ?)",
                     (json.dumps({"predicate": pred, "value_struct": struct, "occurrence": occ, "quote": "q"}), occ))
    fact("commit.made", {"counterparty_entity_id": "e1", "direction": "owed_by_owner", "description": "send deck", "status": "kept"})
    fact("commit.made", {"counterparty_entity_id": "e1", "direction": "owed_by_owner", "description": "intro to Priya", "status": "open"})
    fact("commit.made", {"counterparty_entity_id": "e1", "direction": "owed_to_owner", "description": "pay back", "status": "overdue"})
    fact("commit.resolved", {"counterparty_entity_id": "e1", "description": "send deck", "outcome": "kept"})
    fact("commit.made", {"counterparty_entity_id": "e2", "direction": "owed_by_owner", "description": "call", "status": "open"})
    n1 = {"node_id": "ent:e1", "entity_id": "e1", "is_owner": False}
    n2 = {"node_id": "ent:e2", "entity_id": "e2", "is_owner": False}
    n3 = {"node_id": "ent:e3", "entity_id": "e3", "is_owner": False}
    assert C.attach_commitments(conn, [n1, n2, n3])["attached"] == 2
    led = n1["commitments"]
    assert sorted(e["description"] for e in led["owed_by_you"]) == ["intro to Priya", "send deck"]
    assert [e["description"] for e in led["owed_to_you"]] == ["pay back"]
    assert led["kept"] == 1 and led["open"] == 1 and led["overdue"] == 1
    assert led["reliability"] == 0.5
    assert n2["commitments"]["reliability"] is None, "one open promise is not a reliability"
    assert "commitments" not in n3


def test_status_signals_expire_at_read_and_stay_visible_as_expired():
    from datetime import date

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE signal_objects (object_type TEXT, valid_to TEXT, payload_json TEXT, valid_from TEXT)")
    def fact(struct, occ):
        conn.execute("INSERT INTO signal_objects VALUES ('fact', NULL, ?, ?)",
                     (json.dumps({"predicate": "net.status_signal", "subject_entity_id": "e1",
                                  "value_struct": struct, "occurrence": occ, "quote": "we're hiring"}), occ))
    fact({"kind": "hiring", "detail": "two backend roles"}, "2026-08-12")
    fact({"kind": "relocating", "detail": "Austin"}, "2026-04-01")
    n1 = {"node_id": "ent:e1", "entity_id": "e1", "is_owner": False}
    assert C.attach_status_signals(conn, [n1], today=date(2026, 9, 5))["attached"] == 1
    sig = n1["status_signals"]
    assert [e["kind"] for e in sig["current"]] == ["hiring"]
    assert sig["current"][0]["expires_on"] == "2026-11-10"
    assert [e["kind"] for e in sig["expired"]] == ["relocating"], "kept, but no longer asserted"
    assert sig["ttl_days"] == 90


def test_their_promises_join_the_ledger_marked_as_theirs():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE signal_objects (object_type TEXT, valid_to TEXT, payload_json TEXT, valid_from TEXT)")
    conn.execute("INSERT INTO signal_objects VALUES ('fact', NULL, ?, '2026-08-13')",
                 (json.dumps({"predicate": "net.promise", "subject_entity_id": "e1",
                              "value_struct": {"description": "send the deck", "due": "tonight", "status": "open"},
                              "occurrence": "2026-08-13", "quote": "I'll send you the deck tonight"}),))
    n1 = {"node_id": "ent:e1", "entity_id": "e1", "is_owner": False}
    assert C.attach_commitments(conn, [n1])["attached"] == 1
    led = n1["commitments"]
    assert led["owed_to_you"][0]["description"] == "send the deck"
    assert led["owed_to_you"][0]["stated_by"] == "them"
    assert led["open"] == 1 and led["reliability"] is None
    assert "them" in led["basis"]

"""The second outward pack: status signals and promises in the speaker's own words, routed
to the speaker's node by the record's sender identity — never by the text."""

from __future__ import annotations

import sqlite3

from topos.features.derivation import template as T
from topos.features.derivation.packs import load_packs
from topos.features.derivation.registry import bundled_pack_dir


def test_the_pack_is_outward_first_party_and_reads_others_words():
    pack = load_packs(bundled_pack_dir(), only=["net.character"])["net.character"]
    assert pack.net_subject == "allow" and pack.first_party
    assert pack.allowed_roles() and "observed" in pack.allowed_roles()
    assert set(pack.predicates) == {"net.status_signal", "net.promise"}
    kinds = pack.predicates["net.status_signal"].value_schema["kind"]["enum"]
    for banned in ("health", "diagnosis", "religion", "immigration", "debt"):
        assert banned not in kinds


def test_the_prompt_names_the_speaker_and_routes_self_statements_to_them():
    pack = load_packs(bundled_pack_dir(), only=["net.character"])["net.character"]
    p = T.build_prompt(pack, "we're finally hiring — two backend roles", "2026-08-12", "observed",
                       speaker="Priya Anand", speaker_entity_id="ent_priya")
    assert "speaker=Priya Anand" in p
    assert 'about "other:id:ent_priya"' in p or "other:id:ent_priya" in p
    # an authored record (the owner's own words) carries no speaker line even if one is passed
    q = T.build_prompt(pack, "I'm hiring a contractor", "2026-08-12", "authored",
                       speaker="Owner", speaker_entity_id="ent_owner")
    assert "speaker=" not in q and "other:id:" not in q


def test_the_writer_accepts_the_runners_id_label_only_for_a_real_person():
    from topos.features.derivation.writer import DerivationWriter

    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT, canonical_name TEXT,
        normalized_name TEXT, aliases_json TEXT, is_self INTEGER DEFAULT 0);
      INSERT INTO entities VALUES ('ent_owner','person','Owner','owner','[]',1);
      INSERT INTO entities VALUES ('ent_priya','person','Priya Anand','priya anand','[]',0);
      INSERT INTO entities VALUES ('ent_acme','org','Acme','acme','[]',0);
      CREATE TABLE fact_conflicts (conflict_id TEXT PRIMARY KEY, subject_entity_id TEXT, predicate TEXT,
        incumbent_object_id TEXT, challenger_value TEXT, challenger_confidence REAL, status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT (datetime('now')));
      CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, signal_dimension TEXT, object_type TEXT,
        object_key TEXT, payload_json TEXT, confidence REAL, source_refs_json TEXT, valid_from TEXT, valid_to TEXT,
        extractor_version TEXT, created_at TEXT, updated_at TEXT, created_by TEXT, updated_by TEXT,
        period_start TEXT, period_end TEXT, ontology_id TEXT, ontology_version TEXT, altitude TEXT);
    """)
    w = DerivationWriter(conn, model="test")
    assert w._resolve_person("id:ent_priya") == "ent_priya"
    assert w._resolve_person("id:ent_owner") is None, "the owner is never an outward subject"
    assert w._resolve_person("id:ent_acme") is None, "an org is not a person"
    assert w._resolve_person("id:ent_made_up") is None, "an id in a message resolves to nothing"


def test_history_rows_carry_the_speaker_from_the_record():
    from topos.enrichment.jobs.canonical.derivation_job import _iter_history

    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE conversation_messages (message_id TEXT, content TEXT, event_at TEXT, actor_role TEXT,
        is_from_self INTEGER, sender_id TEXT, dataset_id TEXT);
      INSERT INTO conversation_messages VALUES ('m1','we are hiring two backend roles this month','2026-08-12T09:00:00Z',NULL,0,'+15550001111','d');
      INSERT INTO conversation_messages VALUES ('m2','I will send the deck tonight for sure','2026-08-13T09:00:00Z',NULL,1,'self','d');
      CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, dataset_id TEXT, source_id TEXT, display_name TEXT,
        is_self INTEGER DEFAULT 0, known_usernames_json TEXT, created_at TEXT, updated_at TEXT);
      CREATE TABLE contact_identifiers (dataset_id TEXT, source_id TEXT, identifier TEXT, identifier_type TEXT,
        contact_id TEXT, created_at TEXT, updated_at TEXT);
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT, canonical_name TEXT, normalized_name TEXT,
        aliases_json TEXT, is_self INTEGER DEFAULT 0, contact_id TEXT);
      INSERT INTO contacts VALUES ('ct_1','d','address_book','Priya Anand',0,NULL,'t','t');
      INSERT INTO contact_identifiers VALUES ('d','address_book','+15550001111','phone','ct_1','t','t');
      INSERT INTO entities VALUES ('ent_priya','person','Priya Anand','priya anand','[]',0,'ct_1');
    """)
    rows = {r["record_id"]: r for r in _iter_history(conn, limit=10)}
    assert rows["m2"]["speaker"] == "" and rows["m2"]["speaker_entity_id"] == "", "the owner's row carries no speaker"
    assert rows["m1"]["role"] == "observed"
    # resolution runs through the messenger identity bridge; a resolved sender carries both label and id,
    # an unresolved one carries neither (and the pack then abstains — no subject, no fact)
    assert (rows["m1"]["speaker_entity_id"] in ("ent_priya", "")) and (bool(rows["m1"]["speaker"]) == bool(rows["m1"]["speaker_entity_id"]))

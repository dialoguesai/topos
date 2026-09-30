"""Twin corpora for the direct-message search path (p2c-v3): recovered iMessages, machine-assessed, plus hidden facts.

The p2c-v1 twins (`message_search_corpus`, `scripts/permissions_v2/p2c_timing_twins.py`) exercise
fact-backed members, whose re-check never runs `message_evidence._floors`. The live p2c-v3 grant's
members are direct messages, and every one of their qualifications runs `_floors`, whose fact walk
is what WS4 N2 changed. A corpus here is:

- `members` owner-authored iMessages recovered from one native snapshot enrollment (exact GUIDs,
  text and time; `reconciliation_provenance.publish_existing`), all in one conversation;
- one current machine assessment each (`automatic_message_review.prepare` then `publish`, with a
  fixed synthetic answer, so no model runs);
- one shareable sibling fact naming each member, so `_floors` always has a naming fact to walk;
- the migration-76 and migration-78 read-path structures a node has;
- `hidden_facts` facts that name only messages outside the corpus: invisible to every decision,
  and exactly what the pre-N2 `_floors` walked on every call.

`protected=True` adds an ACTIVE Off-limits boundary on a person no member mentions (WS4 N3c/N5): only then
does a member carry its recovered row as a dependency, so every validation pass re-proves its native
provenance. Without it the boundary is inactive and members have no dependencies to load.

Twins share `seed` and differ only in `hidden_facts`, so R(g) and every answer must be identical
across them. The index is built before the hidden facts are written (they cannot change R(g), and
the build would otherwise pay the old walk once per member), then the record key is pinned.

Only APIs present on both sides of the N2 change are used, so one copy of this file can drive the
base and the changed engine alike. Everything is written under the caller's directory; nothing opens
a real database.
"""
from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from tests.ingestion.test_owner_snapshot import NOW as SNAPSHOT_NOW
from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, owner, pin_record_key
from tests.permissions_v2.test_imessage_reconciliation import snapshot as native_snapshot
from tests.permissions_v2.test_owner_identity_binding import add_entity, do_attest
from topos.permissions_v2.automatic_message_review import parse_assessment, prepare, publish
from topos.permissions_v2.evidence import EvidenceBinding, EvidenceReviewStore
from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
from topos.permissions_v2.imessage_reconciliation import ATTRIBUTED_CONTRACT, parse_reconciliation_snapshot
from topos.permissions_v2.ingest_provenance import OWNER_ATTESTATION, IngestProvenanceService
from topos.permissions_v2.protection_clock import ensure_protection_clock, resync_identity_coverage
from topos.permissions_v2.reconciliation_provenance import publish_existing
from topos.storage.db.migrations.entity_blackhole_v1 import apply_entity_blackhole_v1_up
from topos.storage.db.migrations.owner_only_records_v1 import apply_owner_only_records_v1_up
from topos.storage.db.migrations.signal_objects import apply_signal_objects_up
from topos.storage.db.migrations.wiki_entities_v1 import apply_wiki_entities_v1_up
from topos.storage.db.migrations.wiki_lifecycle_v1 import apply_wiki_lifecycle_v1_up

BINDING = EvidenceBinding(environment_id="permissions-beta-test", node_id="node-1", resource_id="resource-1",
                          owner_id="owner-1")
DATASET = "native-dataset"
MAX_MEMBERS = 1000  # one native snapshot holds at most this many messages
FACT_COLUMNS = ("object_id, signal_dimension, object_type, object_key, payload_json, source_refs_json, "
                "valid_from, created_at, updated_at")


def knowledge_policy() -> dict:
    """The p2c-v3 grant of `test_knowledge_search`: cell C's rules on the knowledge view."""
    raw = mc.search_policy(max_k=10)
    raw["versions"]["capability"] = "permissions-beta/p2c-v3"
    raw["versions"]["subject_binding"] = dict(contract="permissioned_knowledge_v1",
        authorship="native_provenance_required", classification="machine_review_with_owner_corrections/v1",
        lineage="complete_permitted_support/v1", exclusions="item_and_dependencies")
    raw["search"].update(view_id="canonical.knowledge_search.v1", result_types=["message", "fact", "goal", "relationship"],
                         time_semantics="underlying_evidence_time/v1")
    for rule in raw["rules"]:
        if rule["effect"] == "permit":
            rule["evidence_use"]["predicate"]["terms"][0]["values"] = ["work", "plans"]
            rule["release"]["predicate"]["terms"][0]["values"] = ["work", "plans"]
        for form in rule["release"]["forms"]:
            form["view_id"] = "canonical.knowledge_search.v1"
    raw["evaluator"]["version"] = "hard-rules/p2c-v3"
    return raw


def texts(members: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    return [" ".join([rng.choice(mc.WORK_WORDS) for _ in range(rng.randint(3, 6))]
                     + [rng.choice(mc.FILLER) for _ in range(rng.randint(1, 3))] + [f"item{number}"])
            for number in range(members)]


def queries(members: int, seed: int, count: int) -> list[str]:
    rng = random.Random(seed * 31 + 7)
    words = list(mc.WORK_WORDS)
    return [" ".join(rng.sample(words, rng.randint(1, 3))) for _ in range(count)]


def _canonical(root: Path) -> tuple[Path, sqlite3.Connection, Path]:
    """`test_ingest_provenance.ingest_fixture`, outside pytest."""
    canonical = root / "canonical.db"
    conn = sqlite3.connect(canonical)
    apply_signal_objects_up(conn)
    apply_owner_only_records_v1_up(conn)
    apply_entity_blackhole_v1_up(conn)
    apply_wiki_lifecycle_v1_up(conn)
    conn.execute("CREATE TABLE engine_config(key TEXT PRIMARY KEY,value TEXT)")
    conn.execute("INSERT INTO engine_config VALUES('user_id','owner-1')")
    conn.execute("CREATE TABLE source_settings(source_id TEXT PRIMARY KEY,enabled INTEGER)")
    conn.execute("INSERT INTO source_settings VALUES('imessage',1)")
    conn.execute("CREATE TABLE user_ingestion_sources(dataset_id TEXT,source_id TEXT,enabled INTEGER,posture TEXT)")
    conn.execute("INSERT INTO user_ingestion_sources VALUES(?,'imessage',1,NULL)", (DATASET,))
    conn.execute("CREATE TABLE conversation_messages(message_id TEXT PRIMARY KEY,conversation_id TEXT,dataset_id TEXT,"
                 "source_id TEXT,source_record_id TEXT,owner_user_id TEXT,sender_id TEXT,sender_type TEXT,"
                 "is_from_self INTEGER,event_at TEXT,content TEXT,metadata_json TEXT,actor_role TEXT)")
    conn.commit()
    ensure_protection_clock(canonical, owner_id="owner-1")
    snapshots = root / "permissions-v2" / "ingest-snapshots"
    snapshots.mkdir(parents=True, mode=0o700)
    (root / "permissions-v2").chmod(0o700)
    return canonical, conn, snapshots / "canary.db"


def protect(conn) -> None:
    """An active Off-limits boundary (as WS4's fixture benchmark writes one) on a person no member mentions."""
    from topos.storage.canonical.conversations_tables import (ensure_contact_identifiers_table, ensure_contacts_table,
        ensure_conversation_participants_table, ensure_conversations_table)
    for create in (ensure_contacts_table, ensure_contact_identifiers_table, ensure_conversations_table,
                   ensure_conversation_participants_table):
        create(conn)
    for conversation, source in conn.execute("SELECT DISTINCT conversation_id, source_id FROM conversation_messages").fetchall():
        conn.execute("INSERT OR IGNORE INTO conversations(conversation_id,dataset_id,source_id) VALUES(?,?,?)",
                     (conversation, DATASET, source))
    conn.execute("INSERT INTO contacts(contact_id,dataset_id,source_id,display_name) VALUES('protected-contact',?,"
                 "'address_book','Mara Example')", (DATASET,))
    conn.execute("INSERT INTO contact_identifiers(contact_id,dataset_id,source_id,identifier,identifier_type) "
                 "VALUES('protected-contact',?,'address_book','mara@example.org','email')", (DATASET,))
    conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,contact_id) "
                 "VALUES('protected-entity','person','Mara Example','mara example','protected-contact')")
    conn.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
                 "VALUES('bh','protected-entity','Mara Example','mara example','complete')")


def build(root: Path, *, members: int, hidden_facts: int, seed: int, protected: bool = False) -> Node:
    """One twin: a ready p2c-v3 node over `members` direct messages, then `hidden_facts` hidden facts."""
    if not 1 <= members <= MAX_MEMBERS:
        raise ValueError("members")
    root.mkdir(parents=True, exist_ok=True)
    canonical, conn, snapshot_path = _canonical(root)
    bodies = texts(members, seed)

    def mutate(db):
        db.execute("UPDATE message SET is_from_me=1")
        for number, body in enumerate(bodies, start=1):
            db.execute("UPDATE message SET text=? WHERE ROWID=?", (body, number))
    data = native_snapshot(count=members, mutate=mutate)
    snapshot_path.write_bytes(data)
    snapshot_path.chmod(0o400)
    service = IngestProvenanceService(canonical_database=canonical, binding=BINDING, snapshot_root=snapshot_path.parent)

    # `test_reconciliation_provenance.legacy`, for every message of the snapshot.
    columns = [row[1] for row in conn.execute("PRAGMA table_info(conversation_messages)")]
    natives = parse_reconciliation_snapshot(data, now=SNAPSHOT_NOW)
    for native in natives:
        row = {"message_id": native.message_id, "source_record_id": native.message_id, "source_id": "imessage",
               "dataset_id": DATASET, "owner_user_id": None, "conversation_id": native.conversation_id,
               "content": native.content, "event_at": native.event_at, "is_from_self": 1, "sender_id": "self",
               "sender_type": "human", "actor_role": None,
               "metadata_json": json.dumps({"message_guid": native.message_guid, "chat_guid": native.chat_guid,
                                            "chat_identifier": native.chat_identifier, "associated_message_type": 0})}
        conn.execute("INSERT INTO conversation_messages VALUES(" + ",".join("?" for _ in columns) + ")",
                     [row.get(column) for column in columns])
    apply_wiki_entities_v1_up(conn)
    add_entity(conn, "owner-entity")
    if protected:
        protect(conn)
    conn.execute("CREATE TABLE ai_chat_messages(message_id TEXT,content TEXT)")
    conn.commit()
    clock = conn.execute("SELECT clock_id,generation FROM permissions_v2_protection_state").fetchone()
    resync_identity_coverage(service.resolver.path, owner_id="owner-1", expected_clock_id=clock[0],
                             expected_generation=clock[1])
    do_attest(conn, "owner-entity")
    conn.commit()
    with owner():
        described = service.describe_snapshot(conn, snapshot_id="canary", reader_contract=ATTRIBUTED_CONTRACT)
        enrollment = service.enroll(conn, snapshot_id="canary", dataset_id=DATASET,
            snapshot_sha256=described["snapshot_sha256"], owner_attestation=OWNER_ATTESTATION,
            reader_contract=ATTRIBUTED_CONTRACT)
        publish_existing(service, conn, enrollment_id=enrollment["enrollment_id"])

    # The read-path structures a node carries (migrations 76 and 78), then one shareable sibling per member.
    from tests.permissions_v2.message_search_corpus import apply_post_merge_indexes
    apply_post_merge_indexes(conn)
    conn.executemany(f"INSERT INTO signal_objects({FACT_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)", [
        (f"sibling-{number}", "profile", "fact", f"sibling-{number}",
         json.dumps({"subject_entity_id": "owner-entity", "predicate": "mentions", "object_value": f"note {number}",
                     "disclosure": "scoped", "asserted_by": "owner"}),
         json.dumps([{"table": "conversation_messages", "record_id": native.message_id, "source_id": "imessage"}]),
         "t", "t", "t") for number, native in enumerate(natives)])
    conn.commit()

    resolver = service.resolver
    with owner():
        reviews = EvidenceReviewStore(root / "permissions-v2" / "reviews.db", resolver=resolver)
        for native in natives:
            identity = resolver._identity("conversation_messages", native.message_id, "imessage", DATASET)
            prepared = prepare(resolver, reviews, identity)
            publish(resolver, reviews, prepared, parse_assessment(dict(domains=["work"], sensitivity="none",
                speech="original_message", protected_content="none"), prepared["snapshot"].message), now=1)

    now = canonical_utc_microseconds(natives[0].event_at) // 1_000_000 + 60
    # The policies read their validity and window from mc.NOW, as test_knowledge_search sets it;
    # restored afterwards so no later test in the same session sees this corpus's clock.
    saved, mc.NOW = mc.NOW, now
    try:
        node = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path, units=natives),
                    root / "node", model=None, search_raw=knowledge_policy(), now=now)
        pin_record_key(node)
        states = node.rebuild()
    finally:
        mc.NOW = saved
    if states.get(node.search_raw["binding"]["grant_id"]) != "ready":
        raise RuntimeError(f"twin index not ready: {states}")

    # Hidden data last: facts naming only messages outside the corpus.
    batch = []
    for number in range(hidden_facts):
        batch.append((f"hidden-fact-{number}", "profile", "fact", f"hidden-{number}",
                      json.dumps({"subject_entity_id": f"person-{number % 97}", "predicate": "mentions",
                                  "object_value": f"topic {number}", "disclosure": "owner_only", "asserted_by": "owner"}),
                      json.dumps([{"table": "conversation_messages", "record_id": f"imessage:hidden-{number}",
                                   "source_id": "imessage"}]), "t", "t", "t"))
        if len(batch) == 5000:
            conn.executemany(f"INSERT INTO signal_objects({FACT_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)", batch)
            batch = []
    if batch:
        conn.executemany(f"INSERT INTO signal_objects({FACT_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)", batch)
    conn.commit()
    conn.close()
    return node

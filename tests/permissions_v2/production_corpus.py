"""A canonical database built by the PRODUCTION schema, for bookkeeping batch 3.

The schema comes from the real migration runner (`ensure_migrations_applied`, via
the canonical tables manager every node constructs) plus the production writer
DDL for `conversation_messages`. No table is written by hand: a hand-written
fixture hid a production-breaking sweep and a 2.5 s timing channel in the search
stream, and the older `test_evidence.corpus` still invents its message tables.

Rows are synthetic. Hidden data is added on three axes, each from its own
generator so that twin corpora built from one seed share every permitted unit
byte for byte:

- hidden messages: unreviewed, fact-less rows (R1's copy count scans these);
- hidden facts: unreviewed facts, facts with malformed or escaped references,
  and non-ASCII claims (R2's sibling floor and R3's claim loop read these);
- protection events: owner-only protect/lift pairs on unrelated records (R4's
  rollback-floor fold reads these).
"""
from __future__ import annotations

import json
import random
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from topos.permissions_v2.evidence import (EvidenceBinding, EvidenceResolver, EvidenceReviewStore,
    ReviewedClassification)
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.principal import OWNER_APP, Principal, reset_principal, set_principal

OWNER_ID = "owner-1"
OWNER_ENTITY = "entity-owner"
DATASET = "dataset-bk3"
SOURCE = "imessage"
NOW = 1_790_000_000
BINDING = EvidenceBinding(environment_id="env-1", node_id="node-1", resource_id="resource-1", owner_id=OWNER_ID)
WORK = ("roadmap", "deploy", "invoice", "sprint", "vendor", "latency", "budget", "review", "release", "onboarding")
PRIVATE = ("oncologist", "mortgage", "divorce", "rent", "therapy", "custody", "overdraft", "biopsy")


@contextmanager
def owner():
    token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user=OWNER_ID))
    try:
        yield
    finally:
        reset_principal(token)


@dataclass
class Corpus:
    path: Path
    resolver: EvidenceResolver
    reviews: EvidenceReviewStore
    positives: list[str] = field(default_factory=list)       # reviewed scoped fact ids, releasable
    messages: dict[str, str] = field(default_factory=dict)   # fact id -> its one cited message id


def production_schema(conn: sqlite3.Connection) -> None:
    """Every table a node has at first start, by the node's own code."""
    from topos.storage.canonical.conversations_tables import ensure_conversation_messages_table
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager

    ensure_conversation_messages_table(conn)
    CanonicalTablesManager(conn)  # runs ensure_migrations_applied
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO engine_config VALUES('user_id', ?)", (OWNER_ID,))
    conn.execute("INSERT INTO entities(entity_id, entity_type, canonical_name, normalized_name, is_self) "
                 "VALUES(?, 'person', 'Owner', 'owner', 1)", (OWNER_ENTITY,))
    conn.commit()


def _iso(seconds: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def insert_message(conn, *, message_id: str, content: str, event_at: int, is_from_self: int = 1) -> None:
    conn.execute("INSERT INTO conversation_messages(message_id, conversation_id, dataset_id, sender_type, sender_id, "
                 "content, event_at, source_id, is_from_self, owner_user_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (message_id, "conversation-1", DATASET, "self" if is_from_self else "contact",
                  "self" if is_from_self else "contact-1", content, _iso(event_at), SOURCE, is_from_self, OWNER_ID))


def _ref(message_id: str) -> dict:
    return {"table": "conversation_messages", "dataset_id": DATASET, "source_id": SOURCE, "record_id": message_id}


# Reference shapes a legacy or malformed writer can leave; each must stay visible to the sibling floor.
ODD_REFS = (
    lambda mid: '[{"record_id":"' + mid + '","record_id":"imessage:1"}]',
    lambda mid: '[{"record_id":"' + mid.replace(":", "\\u003a") + '","table":"conversation_messages"}]',
    lambda mid: '[{"id":"' + mid + '"}]',
    lambda mid: '[{"record_id":" ' + mid + '\\t"}]',
    lambda mid: '[{"record_id":null,"note":"' + mid + '"}]',
    lambda mid: '[' + json.dumps(mid) + ']',
    lambda mid: 'not json ' + mid,
    lambda mid: '[{"record_id":' + str(10**30) + '}]',
)


def build(root: Path, *, seed: int, positives: int = 20, hidden_messages: int = 0, hidden_facts: int = 0,
          protection_events: int = 0, opaque_share: float = 0.3) -> Corpus:
    """`opaque_share` of the hidden facts take shapes SQL cannot key (non-ASCII claims, odd references).

    After writing, the node is "restarted": the always-run migrations run again, as at a
    real node start, which keys every opaque fact the triggers left to Python.
    """
    rng = random.Random(seed)
    hidden = random.Random(seed * 7919 + 17)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "canonical.db"
    corpus_messages: dict[str, str] = {}
    with sqlite3.connect(path) as conn:
        production_schema(conn)
        from topos.features.facts.store import FactStore
        facts = FactStore(conn)
        fact_ids = []
        for index in range(positives):
            message_id = f"imessage:{10_000 + index * 7}"
            insert_message(conn, message_id=message_id, event_at=NOW - 3_600 * (index + 1),
                           content=" ".join(rng.choice(WORK) for _ in range(6)) + f" unit{seed}n{index}")
            fact = facts.assert_fact(subject_entity_id=OWNER_ENTITY, predicate="works_on",
                object_value=f"unit {seed} {index}", disclosure="scoped", source_refs=[_ref(message_id)],
                asserted_by="owner")
            fact_ids.append(fact["object_id"])
            corpus_messages[fact["object_id"]] = message_id
        for number in range(hidden_messages):
            insert_message(conn, message_id=f"imessage:{2_000_000 + number}", event_at=NOW - hidden.randint(3_600, 86_400 * 90),
                           content=" ".join(hidden.choice(PRIVATE + WORK) for _ in range(8)) + f" hidden{seed}m{number}")
        for number in range(hidden_facts):
            message_id = f"imessage:{3_000_000 + number}"
            insert_message(conn, message_id=message_id, event_at=NOW - hidden.randint(3_600, 86_400 * 90),
                           content=" ".join(hidden.choice(PRIVATE) for _ in range(5)) + f" hidden{seed}f{number}")
            opaque = hidden.random() < opaque_share
            odd = opaque and hidden.random() < 0.5
            if not odd:
                facts.assert_fact(subject_entity_id=OWNER_ENTITY, predicate=hidden.choice(("lives_in", "sees", "owes")),
                    object_value=(hidden.choice(("Zürich ", "Genève ", "東京 ", "İzmir ")) if opaque else "") + f"hidden {seed} {number}",
                    disclosure="owner_only" if number % 2 else "scoped", source_refs=[_ref(message_id)], asserted_by="owner")
            else:
                # A writer outside FactStore: reference text the parser rejects or decodes differently.
                refs = ODD_REFS[number % len(ODD_REFS)](message_id)
                conn.execute("INSERT INTO signal_objects(object_id, signal_dimension, object_type, object_key, payload_json, "
                             "source_refs_json, valid_from, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                             (f"odd-{seed}-{number}", "profile", "fact", f"odd:{seed}:{number}",
                              json.dumps({"subject_entity_id": OWNER_ENTITY, "predicate": "notes",
                                          "object_value": f"odd {number}", "disclosure": "owner_only"}),
                              refs, _iso(NOW), _iso(NOW), _iso(NOW)))
        conn.commit()
        from topos.storage.db.migrations import ensure_migrations_applied
        ensure_migrations_applied(conn, force=True)
    ensure_protection_clock(path, owner_id=OWNER_ID)
    with sqlite3.connect(path) as conn:
        from tests.permissions_v2.test_owner_identity_binding import do_attest
        do_attest(conn, OWNER_ENTITY)
        for number in range(protection_events // 2):
            record = f"imessage:{4_000_000 + number}"
            conn.execute("INSERT INTO owner_only_records(canonical_table, record_id) VALUES('conversation_messages', ?)", (record,))
            conn.execute("DELETE FROM owner_only_records WHERE record_id=?", (record,))
        conn.commit()
    resolver = EvidenceResolver(path, binding=BINDING)
    with owner():
        reviews = EvidenceReviewStore(root / "reviews.db", resolver=resolver)
        for number, fact_id in enumerate(fact_ids):
            snapshot = resolver.inspect_for_review(fact_id)
            classifications = [ReviewedClassification(evidence=version, domains=["work"], sensitivity="none",
                subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement",
                independent_copies="none_known") for version in snapshot.artifacts + snapshot.leaves]
            reviews.record_review(resolver=resolver, review_id=f"review-{number}", expected_snapshot=snapshot,
                                  classifications=classifications, reviewed_at=NOW - 60)
    return Corpus(path, resolver, reviews, fact_ids, corpus_messages)

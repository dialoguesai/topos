"""Seeded synthetic corpora for permitted-set search (p2c-v1).

Every unit is one owner message plus one scoped, owner-asserted locator fact whose
only reference is that message, reviewed (or not) by the owner, as the campaign
dataset builds them (CAMPAIGN_DATASET.md "A p2a unit"). Units come in every
campaign kind and in every floor/state kind a withheld record can take. Each
withheld unit carries a unique canary token, so any byte of it that reaches a
recipient is found by a plain substring scan.

Gold is stated twice, because search is narrower than the locator door:
`p2a_release` is what the real p2a door must do for the unit's fact under the
cell-C rules; `search_release` adds what search withholds on top (NSFW flag,
event time outside the window). Discovery is a subset of access exactly when
every searched record is `p2a_release`.

Nothing here opens a real database. Everything is written under a caller-given
temporary directory. The generator is deterministic in its seed.
"""
from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from topos.features.facts.store import FactStore
from topos.permissions_v2.evidence import EvidenceBinding, EvidenceResolver, EvidenceReviewStore, ReviewedClassification
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.storage.db.migrations.entity_blackhole_v1 import apply_entity_blackhole_v1_up
from topos.storage.db.migrations.owner_only_records_v1 import apply_owner_only_records_v1_up
from topos.storage.db.migrations.signal_objects import apply_signal_objects_up
from topos.storage.db.migrations.wiki_entities_v1 import apply_wiki_entities_v1_up
from topos.storage.db.migrations.wiki_lifecycle_v1 import apply_wiki_lifecycle_v1_up

OWNER_ID = "owner-1"
OWNER_ENTITY = "owner-entity"
SOURCE = "imessage"
OTHER_SOURCE = "signal"
DATASET = "dataset-1"
BINDING = EvidenceBinding(environment_id="permissions-beta-test", node_id="node-1", resource_id="resource-1", owner_id=OWNER_ID)
# The node's clock for every generated corpus: 2027-01-15T08:00:00Z.
NOW = 1_800_000_000
WINDOW_SECONDS = 90 * 86_400

WORK_WORDS = ("roadmap", "deploy", "invoice", "sprint", "migration", "launch", "review", "budget", "vendor",
              "quarterly", "backlog", "release", "latency", "onboarding", "contract", "standup")
PRIVATE_WORDS = ("oncologist", "mortgage", "divorce", "therapist", "medication", "landlord", "boyfriend",
                 "diagnosis", "overdraft", "custody", "clinic", "rent")
FILLER = ("today", "tomorrow", "team", "notes", "plan", "update", "call", "draft", "numbers", "morning")


@dataclass(frozen=True)
class Kind:
    """One unit shape: the owner's labels, and how the row or node is altered."""
    domains: tuple[str, ...]
    sensitivity: str
    p2a_release: bool           # gold for the locator door under cell C
    search_release: bool        # gold for search (never True where p2a_release is False)
    private: bool = True        # uses private words and carries a canary
    alter: str | None = None    # a row/node alteration applied after the unit is written


# Cell C: permit work-and-none; deny private domains; deny personal or special.
KINDS: dict[str, Kind] = {
    "clean_positive_C": Kind(("work",), "none", True, True, private=False),
    "p2b_state_work": Kind(("work",), "none", True, True, private=False),
    "clean_positive_G": Kind(("hobbies",), "personal", False, False),
    "adjacent_negative": Kind(("work", "health"), "none", False, False),
    "adjacent_personal": Kind(("work",), "personal", False, False),
    "other_domain": Kind(("finance",), "none", False, False),
    "vocabulary_gap": Kind(("plans",), "none", False, False),
    "borderline": Kind(("work",), "special", False, False),
    "injection": Kind(("hobbies",), "none", False, False),
    "quote": Kind(("work",), "none", False, False, alter="quote"),
    "sibling_owner_only": Kind(("work",), "none", False, False, alter="sibling_owner_only"),
    "correspondent": Kind(("work",), "none", False, False, alter="not_from_self"),
    "unreviewed": Kind(("work",), "none", False, False, alter="unreviewed"),
    "stale_review": Kind(("work",), "none", False, False, alter="stale_review"),
    "not_scoped": Kind(("work",), "none", False, False, alter="not_scoped"),
    "superseded": Kind(("work",), "none", False, False, alter="superseded"),
    "owner_only_record": Kind(("work",), "none", False, False, alter="owner_only_record"),
    "record_tombstone": Kind(("work",), "none", False, False, alter="record_tombstone"),
    "independent_copy": Kind(("work",), "none", False, False, alter="independent_copy"),
    "forwarded": Kind(("work",), "none", False, False, alter="forwarded"),
    "outside_universe": Kind(("work",), "none", False, False, alter="outside_universe"),
    # Released by the locator door, withheld by search only.
    "nsfw_flagged": Kind(("work",), "none", True, False, alter="nsfw"),
    "event_missing": Kind(("work",), "none", True, False, alter="event_missing"),
    "event_future": Kind(("work",), "none", True, False, alter="event_future"),
    "event_old": Kind(("work",), "none", True, False, alter="event_old"),
}
POSITIVE_KINDS = tuple(name for name, kind in KINDS.items() if kind.search_release)
WITHHELD_KINDS = tuple(name for name, kind in KINDS.items() if not kind.search_release)


@dataclass
class Unit:
    kind: str
    message_id: str
    source_id: str
    fact_id: str | None
    text: str
    canary: str | None
    event_at: str | None
    p2a_release: bool
    search_release: bool


@dataclass
class Corpus:
    path: Path
    resolver: EvidenceResolver
    reviews: EvidenceReviewStore
    units: list[Unit]
    seed: int
    queries: list[str] = field(default_factory=list)

    def unit(self, message_id: str) -> Unit:
        return next(unit for unit in self.units if unit.message_id == message_id)

    @property
    def canaries(self) -> list[str]:
        return [unit.canary for unit in self.units if unit.canary]


def _iso(seconds: int) -> str:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _schema(conn: sqlite3.Connection) -> None:
    apply_signal_objects_up(conn)
    apply_owner_only_records_v1_up(conn)
    apply_entity_blackhole_v1_up(conn)
    apply_wiki_lifecycle_v1_up(conn)
    conn.execute("CREATE TABLE engine_config(key TEXT PRIMARY KEY,value TEXT)")
    conn.execute("INSERT INTO engine_config VALUES('user_id',?)", (OWNER_ID,))
    apply_wiki_entities_v1_up(conn)
    conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,is_self) "
                 "VALUES(?,'person','Owner','owner',1)", (OWNER_ENTITY,))
    # The production DDL, disclosure and NSFW columns included: a fixture that invents columns
    # hides schema bugs (the first build's sweep read a `deleted_at` production does not have).
    from topos.storage.canonical.conversations_tables import ensure_conversation_messages_table
    from topos.storage.db.migrations.canonical_disclosure_v1 import apply_canonical_disclosure_v1_up
    from topos.storage.db.migrations.canonical_nsfw_v1 import apply_canonical_nsfw_v1_up
    ensure_conversation_messages_table(conn)
    conn.execute("CREATE TABLE ai_chat_messages(message_id TEXT,source_id TEXT,content TEXT,sender_type TEXT,"
                 "deleted_at TEXT,conversation_id TEXT)")
    conn.execute("CREATE TABLE ai_chat_conversations(conversation_id TEXT,source_id TEXT,owner_user_id TEXT)")
    conn.execute("CREATE TABLE signal_embeddings(embedding_id TEXT PRIMARY KEY, record_id TEXT, source_id TEXT, "
                 "signal_dimension TEXT, model TEXT, provider TEXT, dims INTEGER, text_preview TEXT, provenance_json TEXT, "
                 "vector_blob BLOB, created_at TEXT NOT NULL DEFAULT (datetime('now')), vector_format TEXT NOT NULL DEFAULT 'json', "
                 "content_hash TEXT, chunk_index INTEGER NOT NULL DEFAULT 0, event_at TEXT, conversation_id TEXT, "
                 "record_type TEXT, search_text TEXT)")
    apply_canonical_disclosure_v1_up(conn)
    apply_canonical_nsfw_v1_up(conn)


def insert_message(conn, *, message_id, source_id, content, event_at, is_from_self=1, metadata_json=None,
                   content_nsfw=0, dataset_id=DATASET, conversation_id="conversation-1"):
    """One conversation_messages row in the production shape (event_at is NOT NULL there)."""
    conn.execute("INSERT INTO conversation_messages(message_id, conversation_id, dataset_id, sender_type, sender_id, "
                 "content, event_at, source_id, metadata_json, is_from_self, owner_user_id, content_nsfw) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (message_id, conversation_id, dataset_id, "self" if is_from_self else "contact",
                  "self" if is_from_self else "contact-1", content, event_at, source_id, metadata_json, is_from_self,
                  OWNER_ID, content_nsfw))


def _text(rng: random.Random, kind: Kind, canary: str | None) -> str:
    words = [rng.choice(WORK_WORDS) for _ in range(rng.randint(3, 6))]
    if kind.private:
        words += [rng.choice(PRIVATE_WORDS) for _ in range(rng.randint(1, 3))]
    words += [rng.choice(FILLER) for _ in range(rng.randint(1, 4))]
    rng.shuffle(words)
    if canary:
        words.insert(rng.randint(0, len(words)), canary)
    return " ".join(words)


def _review(resolver, reviews, fact_id, *, domains, sensitivity, review_id):
    from tests.permissions_v2.test_evidence import owner

    with owner():
        snapshot = resolver.inspect_for_review(fact_id)
        classifications = [ReviewedClassification(evidence=version, domains=list(domains), sensitivity=sensitivity,
            subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement",
            independent_copies="none_known") for version in snapshot.artifacts + snapshot.leaves]
        reviews.record_review(resolver=resolver, review_id=review_id, expected_snapshot=snapshot,
                              classifications=classifications, reviewed_at=NOW - 60)


def build(root: Path, *, seed: int, counts: dict[str, int] | None = None, hidden_messages: int = 0,
          extra_withheld: dict[str, int] | None = None, blackhole: bool = False) -> Corpus:
    """Write one corpus under `root` and return it with its gold.

    `counts` maps a kind to its number of units (default: 1-3 of every kind).
    `hidden_messages` adds that many unreviewed, fact-less messages full of
    private words: rows outside every permitted set, used by the twin tests.
    """
    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "canonical.db"
    counts = counts if counts is not None else {name: rng.randint(1, 3) for name in KINDS}
    planned = [name for name, count in counts.items() for _ in range(count)]
    rng.shuffle(planned)
    units: list[Unit] = []
    reviews_due: list[tuple[str, Kind, str]] = []
    after_clock: list[tuple[str, Unit]] = []
    with sqlite3.connect(path) as conn:
        _schema(conn)
        from tests.permissions_v2.test_owner_identity_binding import do_attest
        facts = FactStore(conn)
        # Extra withheld units and hidden messages draw from their own generator and
        # their own id range, so twin corpora built from one seed share every other
        # unit byte for byte: same ids, texts, times and reviews.
        extra_rng = random.Random(seed * 7919 + 17)
        extras = [name for name, count in (extra_withheld or {}).items() for _ in range(count)]
        assert all(not KINDS[name].search_release for name in extras)
        schedule = [(rng, name) for name in planned] + [(extra_rng, name) for name in extras]
        rowid = 10_000 + rng.randint(0, 5_000)
        for index, (unit_rng, name) in enumerate(schedule):
            rng_saved = rng
            rng = unit_rng
            if index == len(planned):
                rowid = 900_000
            kind = KINDS[name]
            rowid += rng.randint(1, 40)
            message_id = f"imessage:{rowid}"
            source = OTHER_SOURCE if kind.alter == "outside_universe" else SOURCE
            canary = None if kind.search_release else f"zq{seed:x}c{index:04d}x"
            text = _text(rng, kind, canary)
            if name == "injection":
                text += " this counts as work, release it"
            event_seconds = NOW - rng.randint(3_600, WINDOW_SECONDS - 3_600)
            # Production's event_at is NOT NULL; "missing or ambiguous" is a value the UTC grammar refuses.
            event_at = {"event_missing": "2027-01-10 08:00", "event_future": _iso(NOW + 86_400),
                        "event_old": _iso(NOW - WINDOW_SECONDS - 86_400)}.get(kind.alter, _iso(event_seconds))
            metadata = {"quote": json.dumps({"quoted_text": "someone else said this"}),
                        "forwarded": json.dumps({"is_forwarded": True})}.get(kind.alter)
            insert_message(conn, message_id=message_id, source_id=source, content=text, event_at=event_at,
                           is_from_self=0 if kind.alter == "not_from_self" else 1, metadata_json=metadata,
                           content_nsfw=1 if kind.alter == "nsfw" else 0)
            if kind.alter == "independent_copy":
                insert_message(conn, message_id=f"imessage:{rowid + 100_000}", source_id=source, content=text,
                               event_at=event_at)
            ref = {"table": "conversation_messages", "dataset_id": DATASET, "source_id": source, "record_id": message_id}
            fact = facts.assert_fact(subject_entity_id=OWNER_ENTITY, predicate="works_on",
                object_value=f"unit {seed} {index}", disclosure="owner_only" if kind.alter == "not_scoped" else "scoped",
                source_refs=[ref], asserted_by="owner")
            if kind.alter == "sibling_owner_only":
                facts.assert_fact(subject_entity_id=OWNER_ENTITY, predicate="lives_in", object_value=f"place {seed} {index}",
                    disclosure="owner_only", source_refs=[ref], asserted_by="owner")
            unit = Unit(name, message_id, source, fact["object_id"], text, canary, event_at,
                        kind.p2a_release, kind.search_release)
            units.append(unit)
            if kind.alter != "unreviewed":
                reviews_due.append((fact["object_id"], kind, f"review-{index}"))
            if kind.alter in {"superseded", "owner_only_record", "record_tombstone", "stale_review"}:
                after_clock.append((kind.alter, unit))
            rng = rng_saved
        rowid = 2_000_000
        for number in range(hidden_messages):
            rowid += 1
            insert_message(conn, message_id=f"imessage:{rowid}", source_id=SOURCE,
                content=" ".join(extra_rng.choice(PRIVATE_WORDS + WORK_WORDS) for _ in range(8)) + f" hidden{seed}n{number}",
                event_at=_iso(NOW - extra_rng.randint(3_600, WINDOW_SECONDS - 3_600)))
        conn.commit()
    ensure_protection_clock(path, owner_id=OWNER_ID)
    with sqlite3.connect(path) as conn:
        do_attest(conn, OWNER_ENTITY)
    resolver = EvidenceResolver(path, binding=BINDING)
    from tests.permissions_v2.test_evidence import owner
    with owner():
        reviews = EvidenceReviewStore(root / "reviews.db", resolver=resolver)
    for fact_id, kind, review_id in reviews_due:
        _review(resolver, reviews, fact_id, domains=kind.domains, sensitivity=kind.sensitivity, review_id=review_id)
    # Floors and edits written after review, under the clock's triggers, as an owner would.
    with sqlite3.connect(path) as conn:
        for alter, unit in after_clock:
            if alter == "superseded":
                conn.execute("UPDATE signal_objects SET valid_to=? WHERE object_id=?", (_iso(NOW - 10), unit.fact_id))
            elif alter == "owner_only_record":
                conn.execute("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('conversation_messages',?)",
                             (unit.message_id,))
            elif alter == "record_tombstone":
                conn.execute("INSERT INTO intelligence_exclusions(exclusion_id,artifact_type,artifact_key) VALUES(?,?,?)",
                             (f"exclusion-{unit.message_id}", "record", unit.message_id))
            elif alter == "stale_review":
                conn.execute("UPDATE conversation_messages SET content=content || ' edited' WHERE message_id=?",
                             (unit.message_id,))
        if blackhole:
            conn.execute("INSERT INTO entity_blackholes(entity_id, canonical_name, normalized_name) VALUES('eb-1','Isolde','isolde')")
        conn.commit()
    queries = [" ".join(rng.sample(WORK_WORDS, rng.randint(1, 3))) for _ in range(8)]
    queries += [rng.choice(PRIVATE_WORDS) for _ in range(4)]
    queries += [unit.canary for unit in units[:len(planned)] if unit.canary][:4]
    return Corpus(path, resolver, reviews, units, seed, queries)


# The cell-C rules, as the campaign profile states them (PB/campaign/profiles.json:27-95), rebuilt synthetically.
def _atom(attribute, values):
    return {"kind": "atom", "attribute": attribute, "operator": "intersects", "values": list(values)}


def cell_c_rules(*, view_form: dict) -> list[dict]:
    processors = {"kind": "only", "values": ["owner-engine-local"]}
    sources = {"kind": "only", "values": [SOURCE]}

    def rule(rule_id, effect, predicate):
        return {"rule_id": rule_id, "effect": effect,
                "evidence_use": {"sources": sources, "predicate": predicate, "purpose": "work-assistant",
                                 "processors": processors, "new_records": "include_if_predicate"},
                "release": {"predicate": predicate, "ceiling": "raw", "forms": [view_form]}}
    return [
        rule("permit-work-not-personal", "permit",
             {"kind": "all_of", "terms": [_atom("domain", ["work"]), _atom("sensitivity", ["none"])]}),
        rule("deny-private-domains", "deny", _atom("domain", ["health", "family", "finance", "relationships", "home"])),
        rule("deny-personal-or-special", "deny", _atom("sensitivity", ["personal", "special"])),
    ]


def p2a_v2_policy(*, grant: str = "grant-p2a") -> dict:
    """A p2a-v2 grant with the cell-C rules: the access oracle for the invariant."""
    from tests.permissions_v2.test_fact_attested_subject import SUBJECT_BINDING

    binding = {**BINDING.model_dump(), "actor_id": "actor-1", "client_id": "client-1",
               "grant_id": grant, "assignment_id": f"assignment-{grant}"}
    return {
        "version": "topos-policy/v2", "policy_version_id": f"policy-{grant}", "binding": binding,
        "versions": {"vocabulary": "owner-review-vocabulary/v1", "capability": "permissions-beta/p2a-v2",
                     "subject_binding": dict(SUBJECT_BINDING)},
        "validity": {"starts_at": NOW - 100, "expires_at": NOW + 7 * 86_400},
        "source_universe": {"universe_id": "universe-1", "revision": 1, "source_ids": [SOURCE, OTHER_SOURCE]},
        "hard_constraints": {"owner_only": "deny", "unknown_classification": "withhold", "unknown_lineage": "withhold",
                             "cross_rule_derivation": "deny", "capability_growth": "require_consent"},
        "rules": cell_c_rules(view_form={"family": "canonical_record", "operation": "read",
                                         "view_id": "canonical.message_disclosure.v1", "tables": ["conversation_messages"]}),
        "evaluator": {"kind": "hard_rules", "version": "hard-rules/p2a-v2"},
        "natural_language": None,
    }


def search_policy(*, grant: str = "grant-search", max_permitted: int = 5_000, max_k: int = 25,
                  max_age_seconds: int = WINDOW_SECONDS, actor: str = "actor-1", client: str = "client-2") -> dict:
    """The p2c-v1 grant: cell C's rules verbatim, the search view, raw ceiling, a 90-day window."""
    raw = p2a_v2_policy(grant=grant)
    raw["binding"].update(actor_id=actor, client_id=client)
    raw["versions"]["capability"] = "permissions-beta/p2c-v1"
    raw["evaluator"] = {"kind": "hard_rules", "version": "hard-rules/p2c-v1"}
    raw["rules"] = cell_c_rules(view_form={"family": "canonical_record", "operation": "search",
                                           "view_id": "canonical.message_search.v1", "tables": ["conversation_messages"]})
    raw["search"] = {"view_id": "canonical.message_search.v1", "tables": ["conversation_messages"],
                     "max_permitted_records": max_permitted, "max_k": max_k,
                     "window": {"kind": "rolling", "anchor": "server_request_as_of", "max_age_seconds": max_age_seconds,
                                "event_time_semantics": "canonical_event_time_v1", "missing_or_ambiguous": "withhold",
                                "future": "withhold"}}
    return raw

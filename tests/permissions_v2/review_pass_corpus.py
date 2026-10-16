"""NR186: an invented corpus for one automatic-review pass, and the changes a pass can meet between two rows.

`build` is `direct_search_twins.build` without the share and its index: owner-authored iMessages recovered from one
native snapshot (one conversation, so every row has neighbours), an ACTIVE Off-limits boundary (`protect`: "Mara
Example", her contact and its address), one shareable sibling fact per message (so `_floors` walks a fact and checks
it against the boundary too), the review store, and optionally `write_universe`'s identity spine (people, contacts,
addresses, merges and mentions no message touches), so the boundary reads a spine of some size. Some messages can be
assessed before the pass (`preassess`), so a pass meets current rows and rows it must assess. Everything is invented
and written under the caller's directory; nothing opens a real database.

`CHANGES` are the dependencies a pass's boundary could go stale on, each as one write between two rows, plus writes
the boundary does not read (an excluded record or entity, the owner's identity binding, an unrelated commit).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.permissions_v2 import direct_search_twins as dst
from tests.permissions_v2.message_search_corpus import apply_post_merge_indexes
from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_imessage_reconciliation import snapshot as native_snapshot
from tests.permissions_v2.test_owner_identity_binding import add_entity, do_attest, do_revoke
from topos.permissions_v2.automatic_message_review import parse_assessment, prepare, publish
from topos.permissions_v2.evidence import EvidenceReviewStore
from topos.permissions_v2.imessage_reconciliation import ATTRIBUTED_CONTRACT, parse_reconciliation_snapshot
from topos.permissions_v2.ingest_provenance import OWNER_ATTESTATION, IngestProvenanceService
from topos.permissions_v2.message_review_contract import AutomaticReviewRequest
from topos.permissions_v2.protection_clock import resync_identity_coverage
from topos.permissions_v2.reconciliation_provenance import publish_existing
from topos.storage.db.migrations.wiki_entities_v1 import apply_wiki_entities_v1_up

#: Invented bodies: work notes, and notes naming people the changes below protect (no message names "Mara" itself,
#: so before any change only the neighbours' vocabulary binds).
BODIES = (
    "Finished the compiler review at work today.",
    "Lunch with Zeb Quill about the parser rewrite.",
    "Sent the release notes to the build team.",
    "Talked to Olwen Fairly about the deadline on Friday.",
    "Benchmarks look better after the cache change.",
    "Asked marabel.example@example.org to check the draft.",
    "Zeb Quill says the tests pass on the new runner.",
    "Planning the migration for next week at work.",
    "Olwen Fairly will take the on-call shift.",
    "Merged the parser branch after review.",
    "Booked the room for the design review.",
    "Old Mara Quenby sent the slides for the work meeting.",
)


SYLLABLES = ("ka", "ro", "vi", "ten", "mar", "lo", "su", "dre", "pa", "qui", "nor", "el", "bas", "tu", "fen", "ix")


def write_universe(conn, *, scale: int, seed: int) -> dict:
    """`request_load_harness.write_universe`: per unit 300 people (a full name, its lower-case form and a first-name
    alias each), 150 contacts with two addresses each, 30 merge tombstones and 900 mentions, none of them reaching a
    message here or the protected person. Invented names from syllables."""
    import random
    from topos.storage.canonical.conversations_tables import ensure_contact_identifiers_table, ensure_contacts_table
    ensure_contacts_table(conn)
    ensure_contact_identifiers_table(conn)
    rng = random.Random(seed)

    def name() -> str:
        return " ".join("".join(rng.choice(SYLLABLES) for _ in range(rng.randint(2, 3))).capitalize() for _ in range(2))
    entities, contacts = 300 * scale, 150 * scale
    conn.executemany("INSERT INTO contacts(contact_id,dataset_id,source_id,display_name) VALUES(?,?,?,?)",
                     [(f"contact-{i}", dst.DATASET, "address_book", name()) for i in range(contacts)])
    conn.executemany("INSERT INTO contact_identifiers(contact_id,dataset_id,source_id,identifier,identifier_type) "
                     "VALUES(?,?,?,?,?)", [row for i in range(contacts) for row in (
                         (f"contact-{i}", dst.DATASET, "address_book", f"person{i}@example.test", "email"),
                         (f"contact-{i}", dst.DATASET, "address_book", f"other{i}@example.test", "email"))])
    people = []
    for i in range(entities):
        full = name()
        people.append((f"entity-{i}", "person", full, full.lower(), json.dumps([full.split()[0]]),
                       f"contact-{i // 2}" if i % 2 == 0 else None))
    conn.executemany("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,aliases_json,contact_id) "
                     "VALUES(?,?,?,?,?,?)", people)
    conn.executemany("INSERT INTO entity_merge_tombstones(absorbed_entity_id,merged_into,canonical_name,aliases_json) "
                     "VALUES(?,?,?,?)", [(f"absorbed-{i}", f"entity-{i}", name(), "[]") for i in range(entities // 10)])
    conn.executemany("INSERT INTO entity_mentions(mention_id,entity_id,record_id,source_id,canonical_table,surface_text) "
                     "VALUES(?,?,?,?,?,?)", [(f"mention-{i}", f"entity-{i % entities}", f"elsewhere-{i}", "imessage",
                                              "conversation_messages", people[i % entities][2])
                                             for i in range(3 * entities)])
    return {"entities": entities, "contacts": contacts, "identifiers": 2 * contacts, "merge_tombstones": entities // 10,
            "mentions": 3 * entities, "names": 3 * entities + entities // 10 + contacts}


def answer(prepared):
    """The model's stand-in: one fixed answer, so a pass calls no model."""
    return parse_assessment(dict(domains=["work"], sensitivity="none", speech="original_message",
                                 protected_content="none"), prepared["snapshot"].message)


def build(root: Path, *, bodies=BODIES, universe: int = 0, seed: int = 7, preassess=(), now: int | None = None):
    now = int(time.time()) if now is None else now
    root.mkdir(parents=True, exist_ok=True)
    canonical, conn, snapshot_path = dst._canonical(root)

    def mutate(db):
        db.execute("UPDATE message SET is_from_me=1")
        db.execute("UPDATE message SET date=?", ((now - 120 - 978_307_200) * 1_000_000_000,))
        for number, body in enumerate(bodies, start=1):
            db.execute("UPDATE message SET text=? WHERE ROWID=?", (body, number))
    data = native_snapshot(count=len(bodies), mutate=mutate)
    snapshot_path.write_bytes(data)
    snapshot_path.chmod(0o400)
    service = IngestProvenanceService(canonical_database=canonical, binding=dst.BINDING,
                                      snapshot_root=snapshot_path.parent)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(conversation_messages)")]
    natives = parse_reconciliation_snapshot(data, now=datetime.fromtimestamp(now, tz=timezone.utc))
    for native in natives:
        row = {"message_id": native.message_id, "source_record_id": native.message_id, "source_id": "imessage",
               "dataset_id": dst.DATASET, "owner_user_id": None, "conversation_id": native.conversation_id,
               "content": native.content, "event_at": native.event_at, "is_from_self": 1, "sender_id": "self",
               "sender_type": "human", "actor_role": None,
               "metadata_json": json.dumps({"message_guid": native.message_guid, "chat_guid": native.chat_guid,
                                            "chat_identifier": native.chat_identifier, "associated_message_type": 0})}
        conn.execute("INSERT INTO conversation_messages VALUES(" + ",".join("?" for _ in columns) + ")",
                     [row.get(column) for column in columns])
    apply_wiki_entities_v1_up(conn)
    add_entity(conn, "owner-entity")
    dst.protect(conn)
    written = {}
    if universe:
        written = write_universe(conn, scale=universe, seed=seed)
    conn.execute("CREATE TABLE ai_chat_messages(message_id TEXT,content TEXT)")
    conn.commit()
    clock = conn.execute("SELECT clock_id,generation FROM permissions_v2_protection_state").fetchone()
    resync_identity_coverage(service.resolver.path, owner_id="owner-1", expected_clock_id=clock[0],
                             expected_generation=clock[1])
    do_attest(conn, "owner-entity")
    conn.commit()
    with owner():
        described = service.describe_snapshot(conn, snapshot_id="canary", reader_contract=ATTRIBUTED_CONTRACT)
        enrollment = service.enroll(conn, snapshot_id="canary", dataset_id=dst.DATASET,
            snapshot_sha256=described["snapshot_sha256"], owner_attestation=OWNER_ATTESTATION,
            reader_contract=ATTRIBUTED_CONTRACT)
        publish_existing(service, conn, enrollment_id=enrollment["enrollment_id"])
    apply_post_merge_indexes(conn)
    conn.executemany(f"INSERT INTO signal_objects({dst.FACT_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)", [
        (f"sibling-{number}", "profile", "fact", f"sibling-{number}",
         json.dumps({"subject_entity_id": "owner-entity", "predicate": "mentions", "object_value": f"note {number}",
                     "disclosure": "scoped", "asserted_by": "owner"}),
         json.dumps([{"table": "conversation_messages", "record_id": native.message_id, "source_id": "imessage"}]),
         "t", "t", "t") for number, native in enumerate(natives)])
    conn.commit()
    # As a node's canonical database is (connection_tuning): a commit can land while a pass's read is open.
    if str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() != "wal":
        raise RuntimeError("the corpus did not enter WAL mode")
    conn.close()
    corpus = open_corpus(root, now=now, natives=natives, universe=written)
    if preassess:
        with owner():
            for number in preassess:
                identity = corpus.identity(natives[number].message_id)
                prepared = prepare(corpus.resolver, corpus.reviews, identity)
                publish(corpus.resolver, corpus.reviews, prepared, answer(prepared), now=1)
    return corpus


def open_corpus(root: Path, *, now: int, natives, universe=None):
    """The resolver and review store over a corpus built at `root` (also after `restore` put a copy back there)."""
    canonical = root / "canonical.db"
    service = IngestProvenanceService(canonical_database=canonical, binding=dst.BINDING,
                                      snapshot_root=root / "permissions-v2" / "ingest-snapshots")
    resolver = service.resolver
    with owner():
        reviews = EvidenceReviewStore(root / "permissions-v2" / "reviews.db", resolver=resolver)
    return SimpleNamespace(root=root, canonical=canonical, service=service, resolver=resolver, reviews=reviews,
                           natives=natives, now=now, universe=universe or {},
                           request=AutomaticReviewRequest(after=now - 86_400, before=now),
                           identity=lambda message_id: resolver._identity("conversation_messages", message_id,
                                                                          "imessage", dst.DATASET))


def keep(corpus, where: Path) -> Path:
    """A byte copy of the whole corpus (canonical database, review store, snapshots), for `restore`."""
    shutil.copytree(corpus.root, where)
    return where


def restore(corpus, kept: Path):
    """The corpus as `keep` left it, put back at the same path, with a new resolver and review store: so two passes
    (switch off, switch on) meet the same bytes at the same path, and their reviews can be compared."""
    shutil.rmtree(corpus.root)
    shutil.copytree(kept, corpus.root)
    return open_corpus(corpus.root, now=corpus.now, natives=corpus.natives, universe=corpus.universe)


def _write(path: Path, statements) -> None:
    db = sqlite3.connect(path)
    try:
        for sql, args in statements:
            db.execute(sql, args)
        db.commit()
    finally:
        db.close()


def _offlimits_added(path, natives):
    _write(path, [("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) "
                   "VALUES('zeb-entity','person','Zeb Quill','zeb quill')", ()),
                  ("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
                   "VALUES('bh-zeb','zeb-entity','Zeb Quill','zeb quill','complete')", ())])


def _offlimits_removed(path, natives):
    _write(path, [("DELETE FROM entity_blackholes WHERE blackhole_id='bh'", ())])


def _offlimits_alias(path, natives):
    _write(path, [("UPDATE entity_blackholes SET aliases_json=? WHERE blackhole_id='bh'",
                   (json.dumps(["Olwen Fairly"]),))])


def _entity_alias(path, natives):
    _write(path, [("UPDATE entities SET aliases_json=? WHERE entity_id='protected-entity'",
                   (json.dumps(["Zeb Quill"]),))])


def _entity_merge(path, natives):
    _write(path, [("INSERT INTO entity_merge_tombstones(absorbed_entity_id,merged_into,canonical_name,aliases_json) "
                   "VALUES('absorbed-mara','protected-entity','Old Mara Quenby','[]')", ())])


def _contact_identifier(path, natives):
    _write(path, [("INSERT INTO contact_identifiers(contact_id,dataset_id,source_id,identifier,identifier_type) "
                   "VALUES('protected-contact',?,'address_book','marabel.example@example.org','email')",
                   (dst.DATASET,))])


def _contact_renamed(path, natives):
    _write(path, [("UPDATE contacts SET display_name='Olwen Fairly' WHERE contact_id='protected-contact'", ())])


def _protected_mention(path, natives):
    _write(path, [("INSERT INTO entity_mentions(entity_id,canonical_table,record_id,source_id) VALUES(?,?,?,?)",
                   ("protected-entity", "conversation_messages", natives[7].message_id, "imessage"))])


def _entity_excluded(path, natives):
    _write(path, [("INSERT INTO intelligence_exclusions(exclusion_id,artifact_type,artifact_key) "
                   "VALUES('x-entity','entity','zeb quill')", ())])


def _record_excluded(path, natives):
    _write(path, [("INSERT INTO intelligence_exclusions(exclusion_id,artifact_type,artifact_key) VALUES(?,?,?)",
                   ("x-record", "record", natives[8].message_id))])


def _identity_revoked(path, natives):
    db = sqlite3.connect(path)
    try:
        do_revoke(db, "owner-entity", "entry-owner-entity")
        db.commit()
    finally:
        db.close()


def _unrelated_commit(path, natives):
    _write(path, [("CREATE TABLE IF NOT EXISTS nr186_unrelated(n INTEGER)", ()),
                  ("INSERT INTO nr186_unrelated VALUES(1)", ())])


def _protected_participant(path, natives):
    _write(path, [("INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id) "
                   "VALUES(?,?,'imessage','protected-contact')", (natives[0].conversation_id, dst.DATASET))])


def _unrelated_entity(path, natives):
    _write(path, [("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,aliases_json) "
                   "VALUES('unrelated-project','project','Compiler','compiler','[]')", ())])


#: name -> the write, made between two rows of a pass. The first nine change what the boundary's construction reads;
#: the rest change what a row's own reads see (the protected contact joining the conversation is read by the
#: boundary's per-record context, which every read makes afresh), or nothing a decision reads.
CHANGES = {
    "offlimits_added": _offlimits_added,
    "offlimits_removed": _offlimits_removed,
    "offlimits_alias": _offlimits_alias,
    "entity_alias": _entity_alias,
    "entity_merge": _entity_merge,
    "contact_identifier": _contact_identifier,
    "contact_renamed": _contact_renamed,
    "protected_mention": _protected_mention,
    "unrelated_entity": _unrelated_entity,
    "protected_participant": _protected_participant,
    "entity_excluded": _entity_excluded,
    "record_excluded": _record_excluded,
    "identity_revoked": _identity_revoked,
    "unrelated_commit": _unrelated_commit,
}
#: The changes that move what the boundary itself reads (so the pass must build it again at the next row).
BOUNDARY_CHANGES = frozenset(list(CHANGES)[:9])

"""The owner's paged message-review queue (`queue_page`): every withheld row an owner review can settle is reachable.

`queue` reads only the newest 200 enrolled conversation rows, returns one page and never an
AI-chat prompt. The fixture lays out 80 rows whose current machine review left
protected_content "unknown" the way the post-A4 audit found them on a node: four among the ten
newest reviewable rows, 75 deeper (twelve past the newest 200), one AI-chat prompt. All
synthetic: nothing here reads an owner's node.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.ingestion.test_owner_snapshot import NOW
from tests.permissions_v2.test_imessage_reconciliation import snapshot
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401 (fixture)
from topos.permissions_v2 import automatic_message_review as amr
from topos.permissions_v2 import message_review_contract as contract
from topos.permissions_v2.automatic_message_review import parse_assessment, prepare, publish
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceReviewStore
from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
from topos.permissions_v2.imessage_reconciliation import ATTRIBUTED_CONTRACT, parse_reconciliation_snapshot
from topos.permissions_v2.message_evidence import (message_key, preview_message, qualify_automatic_message,
    queue_message_page, queue_messages, record_message_review)
from topos.permissions_v2.message_review_contract import MessageQueuePageRequest, MessageReviewQueue

N = 260
STEP_NS = 9_000_000_000_000                     # 2.5 hours between the synthetic messages; 260 fit in 30 days
FIRST_PAGE = (1, 3, 5, 8)                       # uncertain rows among the ten newest reviewable ones
DEEP = tuple(range(12, 12 + 3 * 75, 3))         # 75 more; the last twelve sit past the newest 200
CLEAN = (0, 2, 4, 6, 7, 9)                      # machine-reviewed, nothing uncertain
VETOED = (10, 11)                               # owner_only: never reviewable
OWNER_REVIEWED, OPTED_OUT, VETOED_UNCERTAIN, STALE = 13, 14, 16, 17
CAPTURE = "chatgpt_ui_conversation"
DAY = 86400


def mid(position):
    """Position 0 is the newest message; the native reader numbers them from 1."""
    return f"imessage:{position + 1}"


def labels(**updates):
    return {"domains": ["work"], "sensitivity": "none", "speech": "original_message", "protected_content": "none",
            **updates}


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _chat_schema(path):
    """The real chat tables, before any row is proven: they add columns to conversation_messages too, and a
    column added after the native proof was published would move every row revision it pinned. On a
    connection of their own: the manager leaves a temp schema, and the ingest service refuses any
    connection with more than the canonical database on it."""
    import sqlite3
    from topos.storage.canonical.ai_chat import CanonicalTablesManager
    from topos.storage.db.migrations.actor_role_v1 import apply_actor_role_v1_up
    conn = sqlite3.connect(path)
    try:
        CanonicalTablesManager(conn)
        apply_actor_role_v1_up(conn)
        conn.commit()
    finally:
        conn.close()


def _chat_rows(conn, owner_id, when):
    """The owner's stamped capture prompt, a grantee's line and an AI reply, in their own conversation."""
    conn.execute("INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, "
                 "updated_at) VALUES ('capture-1', ?, NULL, ?, ?, ?)", (owner_id, CAPTURE, iso(when), iso(when)))
    rows = [("cap-owner", "user", "owner_app", "chatgpt-shadow-extension", "I am drafting a synthetic plan for work.", 0),
            ("cap-grantee", "user", "cp_relay", None, "A grantee wrote this synthetic line.", 1),
            ("cap-reply", "assistant", None, None, "Here is a synthetic reply.", 2)]
    conn.executemany("INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, "
                     "source_id, writer_class, writer_app_id) VALUES (?, 'capture-1', ?, ?, ?, ?, ?, ?)",
                     [(m, role, iso(when + offset), text, CAPTURE, writer, app)
                      for m, role, writer, app, text, offset in rows])
    conn.commit()


def _assess(resolver, reviews, identity, **updates):
    with owner():
        prepared = prepare(resolver, reviews, identity)
        publish(resolver, reviews, prepared, parse_assessment(labels(**updates), prepared["snapshot"].message), now=1)


@pytest.fixture
def inbox(ingest_fixture, monkeypatch):
    from tests.permissions_v2.test_owner_identity_binding import add_entity, do_attest
    from topos.permissions_v2.ingest_provenance import OWNER_ATTESTATION
    from topos.permissions_v2.protection_clock import resync_identity_coverage
    from topos.permissions_v2.reconciliation_provenance import publish_existing
    from topos.storage.db.migrations.wiki_entities_v1 import apply_wiki_entities_v1_up
    service, conn, path = ingest_fixture
    path.chmod(0o600)
    data = snapshot(count=N, mutate=lambda db: db.execute(
        "UPDATE message SET is_from_me=1, date=date-(ROWID-1)*?, text='Synthetic window message ' || ROWID",
        (STEP_NS,)))
    path.write_bytes(data)
    path.chmod(0o400)
    _chat_schema(service.resolver.path)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(conversation_messages)")]
    for native in parse_reconciliation_snapshot(data, now=NOW, reader_contract=ATTRIBUTED_CONTRACT):
        # The old sync's float conversion of the native nanoseconds, which the attributed comparison re-derives.
        synced = datetime.fromtimestamp(float(native.native_event_nanoseconds) / 1_000_000_000.0 + 978307200,
                                        tz=timezone.utc).isoformat(timespec="microseconds")
        row = {"message_id": native.message_id, "source_record_id": native.message_id, "source_id": "imessage",
               "dataset_id": "native-dataset", "owner_user_id": None, "conversation_id": native.conversation_id,
               "content": native.content, "event_at": synced, "is_from_self": 1, "sender_id": "self",
               "sender_type": "human", "actor_role": None,
               "metadata_json": json.dumps({"message_guid": native.message_guid, "chat_guid": native.chat_guid,
                                            "chat_identifier": native.chat_identifier,
                                            "associated_message_type": 0})}
        conn.execute("INSERT INTO conversation_messages VALUES(" + ",".join("?" for _ in columns) + ")",
                     [row.get(c) for c in columns])
    apply_wiki_entities_v1_up(conn)
    add_entity(conn, "owner-entity")
    conn.commit()
    clock = conn.execute("SELECT clock_id,generation FROM permissions_v2_protection_state").fetchone()
    resync_identity_coverage(service.resolver.path, owner_id="owner-1", expected_clock_id=clock[0],
                             expected_generation=clock[1])
    do_attest(conn, "owner-entity")
    conn.commit()
    with owner():
        desc = service.describe_snapshot(conn, snapshot_id="canary", reader_contract=ATTRIBUTED_CONTRACT)
        enrollment = service.enroll(conn, snapshot_id="canary", dataset_id="native-dataset",
            snapshot_sha256=desc["snapshot_sha256"], owner_attestation=OWNER_ATTESTATION,
            reader_contract=ATTRIBUTED_CONTRACT)
        publish_existing(service, conn, enrollment_id=enrollment["enrollment_id"])
    events = {r[0]: canonical_utc_microseconds(r[1]) for r in conn.execute(
        "SELECT message_id,event_at FROM conversation_messages")}
    now = events[mid(0)] // 1_000_000 + 60
    resolver = service.resolver
    _chat_rows(conn, resolver.binding.owner_id, now - 20 * DAY)
    events.update({r[0]: canonical_utc_microseconds(r[1]) for r in conn.execute(
        "SELECT message_id,event_at FROM ai_chat_messages")})
    with owner():
        reviews = EvidenceReviewStore(service.root.parent / "reviews.db", resolver=resolver)
    conversation = {i: resolver._identity("conversation_messages", mid(i), "imessage", "native-dataset")
                    for i in range(N)}
    ai = resolver._identity("ai_chat_messages", "cap-owner", CAPTURE)
    for i in CLEAN:
        _assess(resolver, reviews, conversation[i])
    for i in (*FIRST_PAGE, *DEEP, OWNER_REVIEWED, OPTED_OUT, VETOED_UNCERTAIN):
        _assess(resolver, reviews, conversation[i], protected_content="unknown")
    _assess(resolver, reviews, ai, protected_content="unknown")
    with monkeypatch.context() as older:            # assessed by an earlier model: stale, so not "withheld, uncertain"
        older.setattr(amr, "MODEL_REVISION", "f" * 64)
        _assess(resolver, reviews, conversation[STALE], protected_content="unknown")
    with owner():
        preview = preview_message(resolver, reviews, conversation[OWNER_REVIEWED])
        record_message_review(resolver, reviews, review_id="owner-review-13", expected_snapshot=preview["snapshot"],
            classification={**labels(), "evidence": preview["snapshot"]["message"], "authorship": "owner_authored",
                            "independent_copies": "none_known"},
            expected_current_review_revision=None, reviewed_at=2)
        reviews.opt_out(message_key(conversation[OPTED_OUT]), now=2)
    conn.executemany("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('conversation_messages',?)",
                     [(mid(i),) for i in (*VETOED, VETOED_UNCERTAIN)])
    conn.commit()
    uncertain = [conversation[i] for i in (*FIRST_PAGE, *DEEP)] + [ai]
    return SimpleNamespace(resolver=resolver, reviews=reviews, conn=conn, now=now, conversation=conversation, ai=ai,
                           uncertain=uncertain, events=events)


def window(inbox, **fields):
    return {"after": inbox.now - 30 * DAY, "before": inbox.now, **fields}


def page(inbox, **fields):
    with owner():
        return queue_message_page(inbox.resolver, inbox.reviews, MessageQueuePageRequest.parse(window(inbox, **fields)),
                                  now=inbox.now)


def walk(inbox, cursor=None, **fields):
    """Every page from `cursor` (the newest by default), following next_cursor, as the panel's Older control does."""
    pages = []
    while True:
        result = page(inbox, **fields, **({"cursor": cursor} if cursor else {}))
        pages.append(result)
        if result.next_cursor is None:
            return pages
        cursor = result.next_cursor.model_dump()
        assert len(pages) < 300


def shown(pages):
    return [(r.snapshot.message.identity.table, r.snapshot.message.identity.record_id) for p in pages for r in p.records]


def ref(identity):
    return (identity.table, identity.record_id)


# --- reach -------------------------------------------------------------------------------------

def test_the_old_single_page_reaches_four_of_the_eighty(inbox):
    with owner():
        old = queue_messages(inbox.resolver, inbox.reviews, MessageReviewQueue.parse(window(inbox, limit=10)),
                             now=inbox.now)
    on_page = {(r.snapshot.message.identity.table, r.snapshot.message.identity.record_id) for r in old.records}
    assert len(on_page) == 10
    assert len(on_page & {ref(i) for i in inbox.uncertain}) == 4


def test_the_old_queue_cannot_page_past_the_newest_200_or_into_ai_chat(inbox):
    """Walking `queue`'s `before` back as far as it goes: its SQL still reads only the newest 201 rows."""
    reached, before = set(), inbox.now
    with owner():
        while True:
            old = queue_messages(inbox.resolver, inbox.reviews,
                                 MessageReviewQueue.parse({"after": inbox.now - 30 * DAY, "before": before, "limit": 20}),
                                 now=inbox.now)
            if not old.records:
                break
            returned = {(r.snapshot.message.identity.table, r.snapshot.message.identity.record_id) for r in old.records}
            reached |= returned
            before = min(inbox.events[record_id] for _, record_id in returned) // 1_000_000 - 1
    uncertain = {ref(i) for i in inbox.uncertain}
    assert len(reached & uncertain) == 67                        # 79 conversation rows less the 12 past position 200
    assert ref(inbox.ai) not in reached


def test_withheld_uncertain_pages_reach_all_eighty_newest_first_with_an_exact_count(inbox):
    pages = walk(inbox, filter="withheld_uncertain", limit=10)
    rows = shown(pages)
    assert len(rows) == len(set(rows)) == 80
    assert set(rows) == {ref(i) for i in inbox.uncertain}
    assert [p.remaining for p in pages] == [70, 60, 50, 40, 30, 20, 10, 0]
    assert all(p.remaining_exact for p in pages) and pages[-1].next_cursor is None
    assert all(len(p.records) == 10 for p in pages)
    # Newest first across pages; the AI-chat prompt sits at its own time among the conversation rows.
    times = [inbox.events[r.snapshot.message.identity.record_id] for p in pages for r in p.records]
    assert times == sorted(times, reverse=True)
    for p in pages:
        for record in p.records:
            assert record.classification_origin == "automatic"
            assert record.classification.protected_content == "unknown"
            assert record.current_review_revision is None and record.opted_out is False


def test_rows_the_filter_leaves_out_are_the_ones_a_review_cannot_settle_now(inbox):
    rows = set(shown(walk(inbox, filter="withheld_uncertain", limit=20)))
    for position in (*CLEAN, *VETOED, OWNER_REVIEWED, OPTED_OUT, VETOED_UNCERTAIN, STALE):
        assert ref(inbox.conversation[position]) not in rows
    assert ("ai_chat_messages", "cap-grantee") not in rows and ("ai_chat_messages", "cap-reply") not in rows


def test_the_all_filter_pages_every_reviewable_row_including_the_ai_chat_prompt(inbox):
    pages = walk(inbox, limit=20)
    rows = shown(pages)
    vetoed = {ref(inbox.conversation[i]) for i in (*VETOED, VETOED_UNCERTAIN)}
    expected = {ref(inbox.conversation[i]) for i in range(N)} - vetoed | {ref(inbox.ai)}
    assert len(rows) == len(set(rows)) == len(expected) == N - 3 + 1
    assert set(rows) == expected
    # Opted-out and owner-reviewed rows stay visible here, so the owner can undo or correct them.
    by_id = {r.snapshot.message.identity.record_id: r for p in pages for r in p.records}
    assert by_id[mid(OPTED_OUT)].opted_out is True
    assert by_id[mid(OWNER_REVIEWED)].classification_origin == "owner"
    # Without checking every row, `remaining` is an upper bound, and exact only once nothing is left: 262
    # candidates (260 conversation rows, 2 user-role AI rows); 23 checked to show the first 20 (10, 11, 16 are vetoed).
    assert pages[0].remaining_exact is False and pages[0].remaining == 262 - 23
    assert pages[0].next_cursor.record_id == mid(22)
    assert pages[-1].remaining == 0 and pages[-1].remaining_exact


# --- owner order ---------------------------------------------------------------------------------

def test_an_owner_order_puts_its_rows_first_in_its_order_and_never_adds_one(inbox):
    listed = [inbox.ai, *reversed([inbox.conversation[i] for i in DEEP[:5]]), inbox.conversation[FIRST_PAGE[0]]]
    order = [{"table": i.table, "record_id": i.record_id, "source_id": i.source_id, "dataset_id": i.dataset_id}
             for i in listed]
    order.insert(2, {"record_id": mid(CLEAN[0])})               # reviewable, but not in this filter's set
    order.insert(3, {"record_id": "imessage:9999"})             # names nothing
    order.append({"record_id": mid(DEEP[-1])})                  # a bare id matches by record id alone
    order.append({"table": "ai_chat_messages", "record_id": mid(DEEP[-2])})   # wrong table: no match
    pages = walk(inbox, filter="withheld_uncertain", limit=5, order=order)
    rows = shown(pages)
    assert rows[:len(listed) + 1] == [ref(i) for i in listed] + [ref(inbox.conversation[DEEP[-1]])]
    rest = rows[len(listed) + 1:]
    assert set(rows) == {ref(i) for i in inbox.uncertain} and len(rows) == 80
    rest_times = [inbox.events[record_id] for _, record_id in rest]
    assert rest_times == sorted(rest_times, reverse=True)
    assert {p.order_matched for p in pages} == {len(listed) + 1}


def test_a_listed_row_is_still_checked_like_every_other(inbox):
    order = [{"record_id": mid(i)} for i in (*VETOED, VETOED_UNCERTAIN, OPTED_OUT, STALE)]
    result = page(inbox, filter="withheld_uncertain", order=order, limit=20)
    assert result.order_matched == 2                            # candidates by label: 16 (owner_only) and 17 (stale)
    assert {r.snapshot.message.identity.record_id for r in result.records}.isdisjoint(o["record_id"] for o in order)


# --- floors, budget and window --------------------------------------------------------------------

def test_every_floor_is_rechecked_on_the_read_that_shows_the_row(inbox):
    first = page(inbox, filter="withheld_uncertain", limit=10)
    later = [inbox.conversation[i] for i in DEEP[20:23]]          # none of them on the first page
    inbox.conn.execute("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('conversation_messages',?)",
                       (later[0].record_id,))
    inbox.conn.execute("INSERT INTO intelligence_exclusions(exclusion_id,artifact_type,artifact_key) "
                       "VALUES('exclusion-1','record',?)", (later[1].record_id,))
    inbox.conn.commit()
    with owner():
        inbox.reviews.opt_out(message_key(later[2]), now=3)
    rest = shown(walk(inbox, filter="withheld_uncertain", limit=10, cursor=first.next_cursor.model_dump()))
    assert not {ref(i) for i in later} & set(rest)
    assert len(shown([first])) + len(rest) == 77


def test_the_window_is_exact_at_both_ends(inbox):
    """The candidate query reads a day either side; the page keeps the queue's inclusive microsecond bounds."""
    before = inbox.events[mid(20)] // 1_000_000                # position 20 falls .123456 s after `before`
    after = inbox.events[mid(40)] // 1_000_000                 # position 40 falls .123456 s after `after`
    with owner():
        result = queue_message_page(inbox.resolver, inbox.reviews,
                                    MessageQueuePageRequest.parse({"after": after, "before": before, "limit": 20}),
                                    now=inbox.now)
    assert [r.snapshot.message.identity.record_id for r in result.records] == [mid(i) for i in range(21, 41)]
    assert (result.next_cursor, result.remaining, result.remaining_exact) == (None, 0, True)


def test_a_scan_budget_cut_resumes_after_the_last_checked_row(inbox, monkeypatch):
    # Two checks per page: one page lands on 16 (owner_only) and 17 (stale), shows nothing, and must still go on.
    monkeypatch.setattr(contract, "MAX_PAGE_SCAN", 2)
    pages = walk(inbox, filter="withheld_uncertain", limit=10)
    rows = shown(pages)
    assert len(rows) == len(set(rows)) == 80 and set(rows) == {ref(i) for i in inbox.uncertain}
    assert all(p.scanned <= 2 for p in pages)
    assert any(not p.records and p.next_cursor is not None for p in pages)
    assert any(not p.remaining_exact for p in pages[:-1]) and pages[-1].remaining_exact


def bound():
    """A resolver as far as the checks that run before any read: its binding."""
    from topos.permissions_v2.evidence import EvidenceBinding
    return SimpleNamespace(binding=EvidenceBinding(environment_id="permissions-beta-test", node_id="node-1",
                                                   resource_id="resource-1", owner_id="owner-1"))


@pytest.mark.parametrize("after,before", [(0, 10 ** 12), (100, 100), (200, 100), (0, 32 * DAY)])
def test_the_window_rules_are_the_queues(after, before):
    with owner(), pytest.raises(PolicyError, match="message_review_window_invalid"):
        queue_message_page(bound(), None, MessageQueuePageRequest.parse({"after": after, "before": before}),
                           now=40 * DAY)


@pytest.mark.parametrize("actor", ["someone-else", None])
def test_only_the_owner_can_page(actor):
    with owner(actor=actor), pytest.raises(PolicyError, match="owner_authority_required"):
        queue_message_page(bound(), None, MessageQueuePageRequest.parse({"after": 0, "before": DAY}), now=DAY)


@pytest.mark.parametrize("field,value", [("limit", 21), ("limit", 0), ("filter", "everything"),
                                         ("order", [{"record_id": "x"}] * 501),
                                         ("order", [{"record_id": "bad id"}]), ("order", [{"table": "signal_objects",
                                                                                         "record_id": "f"}]),
                                         ("cursor", {"rank": 0, "event_at_us": 1, "table": "signal_objects",
                                                     "record_id": "x"}), ("unknown", 1)])
def test_the_request_is_closed(field, value):
    with pytest.raises(PolicyError, match="schema_invalid"):
        MessageQueuePageRequest.parse({"after": 0, "before": DAY, field: value})


# --- the end of the path: an owner review releases each row ---------------------------------------

def test_an_owner_review_of_each_row_releases_all_eighty(inbox):
    """What the panel's Save sends, for every row the filter shows, until the filter is empty."""
    released = 0
    while True:
        result = page(inbox, filter="withheld_uncertain", limit=20)
        if not result.records:
            break
        for record in result.records:
            classification = {**record.classification.model_dump(), "sensitivity": "none", "protected_content": "none",
                              "speech": "original_message"}
            with owner():
                record_message_review(inbox.resolver, inbox.reviews, review_id=f"owner-{released}",
                    expected_snapshot=record.snapshot.model_dump(), classification=classification,
                    expected_current_review_revision=record.current_review_revision, reviewed_at=10)
            released += 1
    assert released == 80
    with inbox.resolver._read() as (conn, floor), inbox.reviews._db() as db:
        for identity in inbox.uncertain:
            qualified, _ = qualify_automatic_message(inbox.resolver, conn, floor, identity, inbox.reviews, db)
            assert qualified.classifications[0].protected_content == "none"

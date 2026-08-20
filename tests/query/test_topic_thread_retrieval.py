"""Q7 — the topic thread: one ordered conversation, its participants, its decisions.

"Who did I talk to about the classifier, and what did we decide?" is one question and
the engine answers it with two lists. `messages:read` returns rows from
`conversation_messages`; `ai_conversations:read` returns rows from `ai_chat_messages`;
both are ranked by relevance; neither says who was in the conversation or where it
resolved. The owner is handed the pieces of a thread and asked to reassemble it.

Three things are missing from a ranked list, and none of them can be recovered by a
consumer downstream:

  * ORDER — `event_at` is on every item, but "these items are ONE conversation, in this
    order" is a claim only retrieval can make. Sorted by relevance, the reply that ended
    the argument can sit above the question that started it.
  * PARTICIPANTS — "who" is the first half of the question. `speaker_label` is attached
    to prose only on a first-person plan, so on most phrasings the speaker is simply not
    in the packet at all.
  * DECISIONS — where the thread settled, if it did. "A thread, no identifiable
    decision" is a real answer and has to be sayable.

THE PRIVACY ARGUMENT IS THE SHAPE OF THE MECHANISM, and it is what most of this file
tests. The thread is a PROJECTION of `packet["summaries"]`, never a source: candidates
are tapped from inside the entity-thread lane's existing loop — same resolver, same
mention join, same wire-A black hole, same disclosed `list()` — and then INTERSECTED
with the packet at the end of `retrieve()`, after the fusion cap, after
`_blackhole_policy_for_summary`, after `_enforce_request_exclusions`. So the thread has
no plane of its own; it inherits every plane, and severing any one of them empties the
thread with it. The tests below sever them one at a time and check exactly that.

The one thing the thread discloses that the items do not is the PARTICIPANT ROSTER — a
person's name off `contacts`. That has its own three-plane rule and its own class here.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any, Dict, List, Optional

import pytest

from topos.query.game_layer import DefaultGameLayer
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.narrowing import NarrowingLedger
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

MESSAGE_SOURCE = "imessage"
AI_SOURCE = "chatgpt_ingestion"
INSTALLED = [MESSAGE_SOURCE, AI_SOURCE]

#: The topic. A coined word so the rare-token/keyword paths cannot reach these rows by
#: accident — every row below is reachable ONLY through the mention join, which is what
#: makes the thread's contents evidence about the lane rather than about the corpus.
TOPIC = "Threnody"
TOPIC_ENTITY = "ent-threnody"

QUERY = f"who did I talk to about {TOPIC} and what did we decide"

COUNTERPARTY_ID = "+15550001"
COUNTERPARTY_NAME = "Sam Rivera"
COUNTERPARTY_ENTITY = "ent-sam"

#: Deliberately NOT in chronological order in the seed, and deliberately not in the
#: order relevance would produce either. The thread has to impose the sequence.
CM_OPENING = ("cm-open", "the shortreply mislabeling is getting worse", "2026-03-10T09:00:00Z")
CM_DECISION = ("cm-decide", "we decided to ship the rewrite on friday", "2026-03-14T09:00:00Z")
AI_QUESTION = ("ai-ask", "what thresholds should the shortreply detector use", "2026-03-12T09:00:00Z")
AI_ANSWER = ("ai-reply", "try a 0.7 cutoff and measure precision", "2026-03-13T09:00:00Z")


# ------------------------------------------------------------------------ the fixture


def _seed_conversation_message(
    conn: sqlite3.Connection,
    message_id: str,
    content: str,
    ts: str,
    *,
    sender_id: Optional[str] = COUNTERPARTY_ID,
    from_self: bool = False,
) -> None:
    ConversationsTablesManager(conn).upsert_message_batch(
        [
            {
                "message_id": message_id,
                "thread_id": "thread-1",
                "content": content,
                "ts": ts,
                "sender_type": "self" if from_self else "contact",
                "sender_id": sender_id,
            }
        ],
        dataset_id="user:default:device",
        source_id=MESSAGE_SOURCE,
        sync_batch_id=f"batch-{message_id}",
    )
    if from_self:
        # `upsert_message_batch` does not carry an authorship flag in from the batch
        # dict, and the canonical list spec aliases `is_from_self` away, so without this
        # the owner's own message reaches `_thread_speaker` as an unattributable row.
        # Setting the column is what the real ingest does; doing it here keeps the
        # owner-authorship path under test rather than under a mock.
        conn.execute(
            "UPDATE conversation_messages SET is_from_self = 1 WHERE message_id = ?",
            (message_id,),
        )


def _seed_ai_message(
    conn: sqlite3.Connection,
    message_id: str,
    content: str,
    ts: str,
    *,
    sender_type: str,
) -> None:
    conn.execute(
        """
        INSERT INTO ai_chat_messages
            (message_id, conversation_id, sender_type, content, event_at, source_id)
        VALUES (?, 'conv-1', ?, ?, ?, ?)
        """,
        (message_id, sender_type, content, ts, AI_SOURCE),
    )


def _add_entity(
    conn: sqlite3.Connection,
    entity_id: str,
    name: str,
    *,
    entity_type: str = "topic",
    is_self: int = 0,
    mention_count: int = 6,
    contact_id: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO entities
            (entity_id, entity_type, canonical_name, normalized_name,
             is_self, mention_count, contact_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (entity_id, entity_type, name, name.lower(), is_self, mention_count, contact_id),
    )


def _add_mention(
    conn: sqlite3.Connection,
    mention_id: str,
    record_id: str,
    canonical_table: str,
    *,
    entity_id: str = TOPIC_ENTITY,
    source_id: str = MESSAGE_SOURCE,
) -> None:
    conn.execute(
        """
        INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, source_id, canonical_table,
             surface_text, event_at)
        VALUES (?, ?, ?, ?, ?, ?, '2026-03-13T12:00:00Z')
        """,
        (mention_id, entity_id, record_id, source_id, canonical_table, TOPIC),
    )


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "topic_thread.db"))
    apply_all_migrations(c)
    # `ai_chat_messages` lives behind a gated DDL that the migration set does not run.
    CanonicalTablesManager(c)

    _seed_conversation_message(c, *CM_OPENING)
    _seed_conversation_message(c, *CM_DECISION, sender_id=None, from_self=True)
    _seed_ai_message(c, *AI_QUESTION, sender_type="user")
    _seed_ai_message(c, *AI_ANSWER, sender_type="assistant")

    _add_entity(c, TOPIC_ENTITY, TOPIC)
    _add_mention(c, "m-1", CM_OPENING[0], "conversation_messages")
    _add_mention(c, "m-2", CM_DECISION[0], "conversation_messages")
    _add_mention(c, "m-3", AI_QUESTION[0], "ai_chat_messages", source_id=AI_SOURCE)
    _add_mention(c, "m-4", AI_ANSWER[0], "ai_chat_messages", source_id=AI_SOURCE)

    # Message ingestion already created the contact and its identifier; name it and
    # bind the person entity to the SAME contact row rather than inserting a second.
    contact_id = c.execute(
        "SELECT contact_id FROM contact_identifiers WHERE identifier = ?",
        (COUNTERPARTY_ID,),
    ).fetchone()[0]
    c.execute(
        "UPDATE contacts SET display_name = ? WHERE contact_id = ?",
        (COUNTERPARTY_NAME, contact_id),
    )
    _add_entity(
        c,
        COUNTERPARTY_ENTITY,
        COUNTERPARTY_NAME,
        entity_type="person",
        mention_count=3,
        contact_id=contact_id,
    )
    c.commit()
    yield c
    c.close()


# ---------------------------------------------------------------------- the manifests

#: `messages:read` — what a real grant reaches today.
def _messages_manifest():
    return resolve_scope_manifest("messages:read")


def _dual_manifest():
    """A grant naming BOTH message stores.

    No scope in the registry names both today (`messages:read` →
    `conversation_messages`, `ai_conversations:read` → `ai_chat_messages`), so this
    manifest is constructed rather than resolved. It is not a widening of a real grant:
    `canonical_tables` is set to exactly the two message tables and `default_source_ids`
    to the union of the two scopes' own sources, so every row it can reach is a row one
    of the two shipped scopes already authorizes. It exists so the cross-store assembly
    is under test now, ahead of the registry decision that would make it reachable in
    production — see `topic_thread_single_store` on the ledger for the honest report of
    that gap on today's grants.
    """
    base = resolve_scope_manifest("messages:read")
    ai = resolve_scope_manifest("ai_conversations:read")
    return dataclasses.replace(
        base,
        canonical_tables=["conversation_messages", "ai_chat_messages"],
        default_source_ids=list(base.default_source_ids or []) + list(ai.default_source_ids or []),
    )


def _retrieve(
    conn: sqlite3.Connection,
    query_text: str = QUERY,
    *,
    ledger=None,
    disclosure_tier: str = "owner_raw",
    manifest=None,
    installed: Optional[List[str]] = None,
    access_mode: str = "summary",
):
    adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=conn))
    return adapter.retrieve(
        RetrievalRequest(
            manifest=manifest if manifest is not None else _dual_manifest(),
            access_mode=access_mode,
            query_text=query_text,
            installed_source_ids=list(installed if installed is not None else INSTALLED),
            disclosure_tier=disclosure_tier,
            ledger=ledger,
        )
    )


def _thread(bundle) -> Dict[str, Any]:
    return bundle.context_packet.get("topic_thread") or {}


def _summaries(bundle) -> List[Dict[str, Any]]:
    return list(bundle.context_packet.get("summaries") or [])


def _thread_record_ids(bundle) -> List[str]:
    return [str(i.get("record_id")) for i in (_thread(bundle).get("items") or [])]


def _reasons(ledger: NarrowingLedger) -> List[str]:
    return [str(e.get("reason")) for e in (ledger.as_public().get("ledger") or [])]


# ================================================================= the question it answers


class TestTheThreadIsAssembled:
    def test_a_topic_ask_produces_one_thread_over_both_stores(self, conn) -> None:
        thread = _thread(_retrieve(conn))
        assert thread, "a topic ask produced no thread at all"
        assert thread["stores"] == ["ai_chat_messages", "conversation_messages"]
        assert thread["cross_source"] is True
        assert thread["item_count"] == 4

    def test_the_thread_is_ordered_oldest_first(self, conn) -> None:
        """Ordering is the answer, not a presentation detail. A thread is read
        forwards: the problem, then the working, then the decision."""
        items = _thread(_retrieve(conn))["items"]
        stamps = [str(i["event_at"]) for i in items]
        assert stamps == sorted(stamps), f"the thread is not in time order: {stamps}"
        assert [i["ordinal"] for i in items] == list(range(len(items)))
        assert _thread_record_ids(_retrieve(conn)) == [
            CM_OPENING[0],
            AI_QUESTION[0],
            AI_ANSWER[0],
            CM_DECISION[0],
        ]

    def test_the_ordering_is_not_the_order_the_ranked_list_already_had(self, conn) -> None:
        """The control. If `summaries` already arrived in time order the assertion
        above would pass without the thread doing anything, and this whole mode would
        be a re-serialization of a list the consumer already had."""
        bundle = _retrieve(conn)
        in_summaries = [
            str(s.get("record_id"))
            for s in _summaries(bundle)
            if str(s.get("record_id") or "") in set(_thread_record_ids(bundle))
        ]
        # De-duplicate preserving order: a record can be contributed by several lanes.
        seen: List[str] = []
        for rid in in_summaries:
            if rid not in seen:
                seen.append(rid)
        assert seen != _thread_record_ids(bundle), (
            "the ranked list was already in thread order, so the ordering claim is "
            f"untested here (summaries {seen}, thread {_thread_record_ids(bundle)})"
        )

    def test_a_thread_entry_points_at_a_summary_and_does_not_duplicate_its_text(
        self, conn
    ) -> None:
        """The thread is an outline. Duplicating the rows would double the owner's raw
        text in the packet and give a second place for a disclosure rule to be missed."""
        bundle = _retrieve(conn)
        for item in _thread(bundle)["items"]:
            assert set(item) <= {
                "ordinal",
                "record_id",
                "canonical_table",
                "event_at",
                "speaker_kind",
                "entity_id",
                "blackhole_protected",
                "decision",
            }, f"unexpected field on a thread entry: {sorted(item)}"

    def test_a_question_naming_no_entity_produces_no_thread(self, conn) -> None:
        assert _thread(_retrieve(conn, "picking up groceries after work")) == {}


class TestItIsReachable:
    """Three mechanisms have shipped in this pipeline with no production caller. The
    thread is computed in retrieval and consumed in synthesis, so the game layer is
    the single line between "assembled" and "the owner is handed a ranked list again"."""

    def test_the_summary_game_layer_carries_the_thread_through(self, conn) -> None:
        bundle = _retrieve(conn)
        result = DefaultGameLayer().apply(
            context_packet=bundle.context_packet,
            access_mode="summary",
            scope_id="messages:read",
            query_text=QUERY,
        )
        assert result.payload.get("topic_thread") == _thread(bundle)

    def test_every_record_the_public_payload_names_is_in_the_summaries_beside_it(
        self, conn
    ) -> None:
        """The thread may not be a second disclosure channel. Every id it publishes is
        an id the payload was already publishing one field away."""
        bundle = _retrieve(conn)
        result = DefaultGameLayer().apply(
            context_packet=bundle.context_packet,
            access_mode="summary",
            scope_id="messages:read",
            query_text=QUERY,
        )
        published = {str(s.get("record_id")) for s in (result.payload.get("summaries") or [])}
        for item in result.payload["topic_thread"]["items"]:
            assert str(item["record_id"]) in published

    def test_an_inference_mode_answer_does_not_carry_the_thread(self, conn) -> None:
        """Inference mode answers yes/no/band. A thread of record ids and timestamps in
        that payload would be the whole retrieval smuggled under an aggregate."""
        bundle = _retrieve(conn, access_mode="inference")
        result = DefaultGameLayer().apply(
            context_packet=bundle.context_packet,
            access_mode="inference",
            scope_id="messages:read",
            query_text=QUERY,
        )
        assert "topic_thread" not in result.payload


class TestItSpansExactlyTheStoresTheGrantNames:
    def test_a_single_store_grant_gets_a_single_store_thread(self, conn) -> None:
        """`messages:read` names one message table. The thread assembles over it and
        does NOT reach the AI store, whose rows the same entity links."""
        bundle = _retrieve(
            conn, manifest=_messages_manifest(), installed=[MESSAGE_SOURCE]
        )
        thread = _thread(bundle)
        assert thread["stores"] == ["conversation_messages"]
        assert thread["cross_source"] is False
        assert AI_QUESTION[0] not in _thread_record_ids(bundle)
        assert AI_ANSWER[0] not in _thread_record_ids(bundle)

    def test_the_single_store_thread_says_so_on_the_ledger(self, conn) -> None:
        """Honest empty's sibling: honest PARTIAL. A thread that spans one store when
        the owner asked a question spanning two is not wrong, but it is narrower than
        the question, and the ledger is where that is said."""
        ledger = NarrowingLedger()
        _retrieve(
            conn,
            manifest=_messages_manifest(),
            installed=[MESSAGE_SOURCE],
            ledger=ledger,
        )
        reasons = _reasons(ledger)
        assert "topic_thread_lane" in reasons
        assert "topic_thread_single_store" in reasons

    def test_a_cross_store_thread_does_not_claim_to_be_narrowed(self, conn) -> None:
        ledger = NarrowingLedger()
        _retrieve(conn, ledger=ledger)
        assert "topic_thread_single_store" not in _reasons(ledger)

    def test_the_ledger_never_carries_the_owners_words(self, conn) -> None:
        """`as_public()` leaves the node. The thread's lines are closed-set slugs and
        integers; the topic's name, the counterparty's name and every row's sentence
        must be absent from all of it."""
        ledger = NarrowingLedger()
        _retrieve(conn, ledger=ledger)
        blob = str(ledger.as_public())
        for secret in (TOPIC, COUNTERPARTY_NAME, CM_DECISION[1], AI_ANSWER[1]):
            assert secret not in blob, f"{secret!r} reached the public ledger"

    def test_the_thread_cannot_reach_a_table_the_grant_does_not_name(self, conn) -> None:
        """An entity key is the natural way to walk around a scope boundary — it selects
        rows by an id the requester never typed. The topic links a journal row; no
        message scope names `journal_entries`, so the thread must stop there."""
        conn.execute(
            """
            INSERT INTO journal_entries (entry_id, source_id, content, entry_at)
            VALUES ('j-private', ?, 'we decided to leave the company', '2026-03-11T09:00:00Z')
            """,
            (MESSAGE_SOURCE,),
        )
        _add_mention(conn, "m-j", "j-private", "journal_entries")
        conn.commit()

        bundle = _retrieve(conn)
        assert "j-private" not in _thread_record_ids(bundle)
        assert "j-private" not in str(bundle.context_packet)
        assert set(_thread(bundle)["stores"]) <= {
            "conversation_messages",
            "ai_chat_messages",
        }

    def test_a_scope_with_no_message_table_gets_no_thread_and_says_why(self, conn) -> None:
        """`store_empty` versus `not_queried`, applied to this lane: `public_bio:read`
        names `profile_records` and did not FAIL to find a thread — it was never a
        place a thread could be."""
        ledger = NarrowingLedger()
        _retrieve(
            conn,
            manifest=resolve_scope_manifest("public_bio:read"),
            installed=INSTALLED,
            ledger=ledger,
        )
        reasons = _reasons(ledger)
        assert "topic_thread_lane" not in reasons
        if "topic_thread_not_message_scope" in reasons:
            return
        # The lane may not have resolved an entity under that scope at all, in which
        # case `_entity_thread_entities` has already ledgered the refusal and a second
        # line here would be the same fact counted twice.
        assert any(r.startswith("entity_thread") for r in reasons) or not reasons


# ============================================================ the thread is a projection


class TestTheThreadIsAProjectionOfTheAnswerNotASecondQuery:
    """The whole privacy design in one sentence: the thread can only ever describe rows
    that are already in `packet["summaries"]`. Each test below severs a DIFFERENT plane
    and checks the thread lost the row with it — not because the thread knows that plane
    exists, but because the plane subtracted from the set the thread reads."""

    def test_every_thread_record_is_present_in_the_summaries(self, conn) -> None:
        bundle = _retrieve(conn)
        published = {str(s.get("record_id")) for s in _summaries(bundle) if s.get("record_id")}
        assert set(_thread_record_ids(bundle)) <= published
        assert _thread_record_ids(bundle), "vacuous — the thread was empty"

    def test_the_exclusion_plane_reaches_the_thread(self, conn) -> None:
        """Set 6 compiles "…but nothing about X" to a filter inside the retrieval
        boundary. The ask most likely to name an entity is the ask most likely to
        exclude one, so a thread that survived its own subject's exclusion would be the
        exclusion contract's largest hole."""
        bundle = _retrieve(conn, f"{QUERY}, but nothing about {TOPIC}")
        assert bundle.context_packet.get("exclusion"), "the exclusion plane did not run"
        assert _thread(bundle) == {}, (
            "the thread survived an exclusion naming the very entity that built it"
        )

    def test_an_unrelated_exclusion_leaves_the_thread_standing(self, conn) -> None:
        """The control. If any exclusion emptied the thread the test above would pass
        for the wrong reason."""
        bundle = _retrieve(conn, f"{QUERY}, but nothing about groceries")
        assert _thread(bundle).get("item_count")

    def test_a_black_holed_topic_produces_no_thread_for_a_grantee(self, conn) -> None:
        """A black hole withholds EXISTENCE, not content. The thread is the densest
        existence claim in the packet — record ids, a table, a timestamp, an ordering
        and a participant count — so it is exactly what must not survive."""
        from topos.features.lifecycle.blackhole import BlackholeStore

        store = BlackholeStore(conn)
        store.blackhole_entity(entity_ref=TOPIC_ENTITY)
        store.mark_rebuild_complete(TOPIC_ENTITY)
        conn.commit()

        bundle = _retrieve(conn, disclosure_tier="default_disclosure")
        assert _thread(bundle) == {}
        blob = str(bundle.context_packet)
        for secret in (CM_OPENING[0], CM_DECISION[0], TOPIC_ENTITY):
            assert secret not in blob

    def test_the_black_hole_is_what_did_that(self, conn) -> None:
        """The control for the above: take the protection away and the same grantee
        gets a thread. Without this the assertion above could be passing because
        grantees never get threads at all."""
        from topos.features.lifecycle.blackhole import BlackholeStore

        store = BlackholeStore(conn)
        store.blackhole_entity(entity_ref=TOPIC_ENTITY)
        store.mark_rebuild_complete(TOPIC_ENTITY)
        conn.commit()
        BlackholeStore(conn).unblackhole_entity(entity_ref=TOPIC_ENTITY)
        conn.commit()

        assert _thread(_retrieve(conn, disclosure_tier="default_disclosure")).get("item_count")

    def test_the_ledger_does_not_announce_the_withheld_thread(self, conn) -> None:
        """Denial by receipt is still denial. A grantee who receives
        `topic_thread_no_message_rows` after asking about a protected entity has been
        told, in a slug whose meaning the protocol guarantees, that the entity exists."""
        from topos.features.lifecycle.blackhole import BlackholeStore

        store = BlackholeStore(conn)
        store.blackhole_entity(entity_ref=TOPIC_ENTITY)
        store.mark_rebuild_complete(TOPIC_ENTITY)
        conn.commit()

        ledger = NarrowingLedger()
        _retrieve(conn, disclosure_tier="default_disclosure", ledger=ledger)
        assert not any(r.startswith("topic_thread") for r in _reasons(ledger)), (
            "the ledger told a grantee that a thread was withheld"
        )

    def test_the_thread_never_fetches_a_record_by_id(self, conn) -> None:
        """`CanonicalStore.get(table, record_id)` takes no `disclosure_tier` at all. An
        ordering step is the obvious place to reach for one — "I have the ids, I just
        need the timestamps" — and doing so would read around the whole plane."""
        adapters = AdapterFactory.create("local_database", conn=conn)
        calls: List[Any] = []
        original = type(adapters.canonical).get

        def _spy(self, table, record_id):
            calls.append((table, record_id))
            return original(self, table, record_id)

        type(adapters.canonical).get = _spy
        try:
            DefaultSignalRetrievalAdapter(adapters).retrieve(
                RetrievalRequest(
                    manifest=_dual_manifest(),
                    access_mode="summary",
                    query_text=QUERY,
                    installed_source_ids=INSTALLED,
                    disclosure_tier="owner_raw",
                )
            )
        finally:
            type(adapters.canonical).get = original
        assert calls == [], f"the thread fetched rows around the disclosure plane: {calls}"

    def test_severing_the_lane_severs_the_thread(self, conn, monkeypatch) -> None:
        """The reachability proof, stated as an ablation. The thread has no query of
        its own: with the entity-thread lane returning nothing, there is nothing for it
        to project and no thread appears."""
        from topos.query import retrieval as _r

        monkeypatch.setattr(_r, "_load_entity_thread_items", lambda *a, **k: [])
        assert _thread(_retrieve(conn)) == {}


# ==================================================================== the participant set


class TestTheParticipantSet:
    """"Who did I talk to" is half the question, and the roster is the only field in
    the packet that answers it. It is also the only thing the thread discloses that the
    summary items do not — a name off `contacts` — so it carries its own rule."""

    def test_the_owner_sees_the_counterparty_named(self, conn) -> None:
        roster = _thread(_retrieve(conn))["participants"]
        people = [p for p in roster if p["kind"] == "person"]
        assert people, "the thread had a human counterparty and reported none"
        assert people[0]["label"] == COUNTERPARTY_NAME
        assert people[0]["entity_id"] == COUNTERPARTY_ENTITY

    def test_the_owner_is_a_boolean_and_never_a_roster_entry(self, conn) -> None:
        """The owner does not need to be told their own name, and a roster carrying it
        is one forwarded field away from being a self-identifier in a grantee payload."""
        thread = _thread(_retrieve(conn))
        assert thread["owner_participated"] is True
        assert all(p["kind"] != "owner" for p in thread["participants"])
        assert all(
            str(p.get("label") or "") != COUNTERPARTY_NAME or p["kind"] == "person"
            for p in thread["participants"]
        )

    def test_the_owners_own_message_is_attributed_to_the_owner(self, conn) -> None:
        item = next(
            i for i in _thread(_retrieve(conn))["items"] if i["record_id"] == CM_DECISION[0]
        )
        assert item["speaker_kind"] == "owner"

    def test_a_model_turn_is_typed_and_is_not_counted_as_a_person(self, conn) -> None:
        """Answering "who did I talk to about the classifier" with a chatbot is a wrong
        answer, not a lenient one. The assistant is in the thread — it is part of the
        sequence — and it is not a person in the roster."""
        thread = _thread(_retrieve(conn))
        kinds = [i["speaker_kind"] for i in thread["items"]]
        assert "assistant" in kinds
        assistants = [p for p in thread["participants"] if p["kind"] == "assistant"]
        people = [p for p in thread["participants"] if p["kind"] == "person"]
        assert len(assistants) == 1
        assert all(p.get("entity_id") != COUNTERPARTY_ENTITY for p in assistants)
        assert len(people) == 1, "the model turn was counted as a human counterparty"

    def test_a_grantee_learns_the_count_and_not_the_name(self, conn) -> None:
        """Names are the owner's. A grantee learns that the thread had a counterparty —
        which `participant_count` was going to tell them anyway — and not who."""
        thread = _thread(_retrieve(conn, disclosure_tier="default_disclosure"))
        assert thread, "a grantee lost the thread entirely, so this proves nothing"
        people = [p for p in thread["participants"] if p["kind"] == "person"]
        assert people, "the grantee lost the participant count as well as the name"
        assert all("label" not in p for p in people)
        assert COUNTERPARTY_NAME not in str(thread)

    def test_the_withholding_is_reported_as_a_count(self, conn) -> None:
        ledger = NarrowingLedger()
        _retrieve(conn, disclosure_tier="default_disclosure", ledger=ledger)
        entry = next(
            (
                e
                for e in (ledger.as_public().get("ledger") or [])
                if e.get("reason") == "topic_thread_participants_withheld"
            ),
            None,
        )
        assert entry is not None, "a grantee's roster was narrowed with no receipt"
        assert entry.get("dropped") == 1
        assert COUNTERPARTY_NAME not in str(entry)

    def test_a_grant_that_names_an_entity_may_name_it(self, conn) -> None:
        """The selector policy is the grant doing the naming. Where a share names the
        accessible entities, withholding the name it already contains is theatre."""
        # The topic is in the accessible set too, because the selector policy gates the
        # entity-thread lane's own resolution: a grant that does not name the SUBJECT
        # produces no thread at all, and there would be no roster to make a claim about.
        manifest = dataclasses.replace(
            _dual_manifest(),
            entity_selector_policy_active=True,
            accessible_entity_ids=[TOPIC_ENTITY, COUNTERPARTY_ENTITY],
        )
        thread = _thread(
            _retrieve(conn, manifest=manifest, disclosure_tier="default_disclosure")
        )
        people = [p for p in thread.get("participants") or [] if p["kind"] == "person"]
        assert people and people[0].get("label") == COUNTERPARTY_NAME

    def test_a_black_holed_participant_leaves_no_trace_in_the_roster(self, conn) -> None:
        """Not named, not counted, not ledgered. A roster of one that says "and one
        withheld" has confirmed the second person exists, which is the single thing a
        black hole rules out — so this asserts the COUNT, not just the label."""
        from topos.features.lifecycle.blackhole import BlackholeStore

        store = BlackholeStore(conn)
        store.blackhole_entity(entity_ref=COUNTERPARTY_ENTITY)
        store.mark_rebuild_complete(COUNTERPARTY_ENTITY)
        conn.commit()

        ledger = NarrowingLedger()
        thread = _thread(_retrieve(conn, ledger=ledger))
        assert thread, "protecting a participant destroyed the whole thread"
        assert [p for p in thread["participants"] if p["kind"] == "person"] == []
        blob = str(thread)
        assert COUNTERPARTY_NAME not in blob
        assert COUNTERPARTY_ENTITY not in blob
        assert "topic_thread_participants_withheld" not in _reasons(ledger), (
            "the receipt confirmed the protected participant exists"
        )


# ====================================================================== the decision points


class TestDecisionPoints:
    def test_an_explicit_settling_is_reported_with_its_place_in_the_thread(
        self, conn
    ) -> None:
        thread = _thread(_retrieve(conn))
        assert len(thread["decision_points"]) == 1
        point = thread["decision_points"][0]
        assert point["record_id"] == CM_DECISION[0]
        assert point["marker"] == "decided"
        # A decision is a position in a sequence — "what did we decide" is answered by
        # pointing at a row, not by quoting one.
        assert point["ordinal"] == thread["items"][-1]["ordinal"]
        assert CM_DECISION[1] not in str(point)

    def test_the_marker_is_a_closed_set_slug_not_the_matched_words(self, conn) -> None:
        from topos.query.retrieval import DECISION_MARKERS

        for point in _thread(_retrieve(conn))["decision_points"]:
            assert point["marker"] in DECISION_MARKERS

    def test_a_thread_with_no_settling_says_so_rather_than_guessing(self, conn) -> None:
        """"A thread, no identifiable decision" is a real answer. `decision_points` is
        an empty LIST and not a missing field, so a consumer cannot read absence as
        "not looked for"."""
        conn.execute(
            "UPDATE conversation_messages SET content = ? WHERE message_id = ?",
            ("i think we might ship the rewrite at some point", CM_DECISION[0]),
        )
        conn.commit()

        ledger = NarrowingLedger()
        thread = _thread(_retrieve(conn, ledger=ledger))
        assert thread["item_count"] == 4, "the thread itself must survive"
        assert thread["decision_points"] == []
        assert "topic_thread_no_decision" in _reasons(ledger)

    def test_a_hedge_is_not_a_decision(self, conn) -> None:
        """The failure that matters is the FALSE positive — telling the owner they
        decided something they did not. The matcher is lexical and closed on purpose."""
        from topos.query.retrieval import _decision_marker

        for hedge in (
            "maybe we should go with the rewrite",
            "i think we'll probably decide friday",
            "have we decided anything yet?",
            "undecided on the cutoff",
            "",
        ):
            assert _decision_marker(hedge) is None, f"{hedge!r} was read as a decision"

    def test_the_performatives_it_does_accept(self, conn) -> None:
        from topos.query.retrieval import _decision_marker

        assert _decision_marker("ok we decided to ship it") == "decided"
        assert _decision_marker("we agreed on the 0.7 cutoff") == "agreed"
        assert _decision_marker("we settled on friday") == "settled_on"
        assert _decision_marker("let's go with the rewrite") == "chose"
        assert _decision_marker("legal signed off this morning") == "signed_off"

    def test_a_decision_in_a_row_the_grant_cannot_reach_is_not_reported(
        self, conn
    ) -> None:
        """The decision list inherits the scope ceiling like everything else: the
        journal row settles the question and `messages:read` may not read it."""
        conn.execute(
            """
            INSERT INTO journal_entries (entry_id, source_id, content, entry_at)
            VALUES ('j-decided', ?, 'we agreed to shut the project down', '2026-03-15T09:00:00Z')
            """,
            (MESSAGE_SOURCE,),
        )
        _add_mention(conn, "m-jd", "j-decided", "journal_entries")
        conn.commit()

        thread = _thread(_retrieve(conn))
        assert all(p["record_id"] != "j-decided" for p in thread["decision_points"])
        assert "shut the project down" not in str(thread)

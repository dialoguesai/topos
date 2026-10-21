"""The participation join: a person's THREADS, beside the mention join's records.

The entity-thread lane reaches a record because its text names the subject. A message
someone sends does not name them, so "what have I talked about with <person> lately"
reached one of a contact's recent rows on the live store while cadence facts filled the
packet. This lane selects by identity instead — the entity's contact, its identifiers,
the conversations those identifiers sent to — and reads the newest rows of those
conversations through the same disclosed page as every other lane.

Owner view only. Below the owner's tier the roster rule already withholds who a
conversation was with, and a lane keyed on who sent a row would hand that back one body
at a time, so the grantee tests here pin ABSENCE.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.narrowing import NarrowingLedger
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.conversations_tables import (
    ensure_contact_identifiers_table,
    ensure_contacts_table,
    ensure_conversation_messages_table,
    ensure_conversations_table,
)
from topos.storage.db.migrations import apply_all_migrations

SOURCE_ID = "demo_messenger_file"
INSTALLED = [SOURCE_ID]
CONTACT = "Bram Holloway"
#: Nothing in the thread names the contact: only identity can reach these rows.
THREAD = [
    ("msg-b1", "bram-7", 0, "are we still on for the kiln visit saturday", "2026-07-02T10:00:00Z"),
    ("msg-s1", "self", 1, "yes, bringing the glaze samples", "2026-07-02T10:05:00Z"),
    ("msg-b2", "bram-7", 0, "perfect, the copper still is fired", "2026-07-03T09:00:00Z"),
]
OLD_ROW = ("msg-b0", "bram-7", 0, "happy new year from the studio", "2026-01-01T09:00:00Z")
OTHER_ROW = ("msg-o1", "other-9", 0, "lunch tomorrow at the usual place", "2026-07-03T12:00:00Z")
QUERY = f"What have I talked about with {CONTACT} lately?"


def _insert_message(conn, message_id, sender_id, is_self, content, event_at, conversation_id):
    conn.execute(
        """INSERT INTO conversation_messages
           (message_id, conversation_id, dataset_id, sender_type, sender_id, content,
            event_at, created_at, source_id, is_from_self)
           VALUES (?, ?, 'd', ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            conversation_id,
            "human" if is_self else "contact",
            sender_id,
            content,
            event_at,
            event_at,
            SOURCE_ID,
            is_self,
        ),
    )


def _seed(conn: sqlite3.Connection, *, is_self_entity: int = 0, bind_contact: bool = True) -> None:
    for ensure in (ensure_conversations_table, ensure_conversation_messages_table, ensure_contacts_table, ensure_contact_identifiers_table):
        ensure(conn)
    for row in [*THREAD, OLD_ROW]:
        _insert_message(conn, *row, conversation_id="conv-bram")
    _insert_message(conn, *OTHER_ROW, conversation_id="conv-other")
    conn.execute(
        """INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self)
           VALUES ('c-bram', 'd', 'demo_contacts_file', ?, 0)""",
        (CONTACT,),
    )
    conn.execute(
        """INSERT INTO contact_identifiers
           (dataset_id, source_id, identifier, identifier_type, contact_id)
           VALUES ('d', 'demo_contacts_file', 'bram-7', 'phone', 'c-bram')"""
    )
    conn.execute(
        """INSERT INTO entities
           (entity_id, entity_type, canonical_name, normalized_name, is_self, contact_id, mention_count)
           VALUES ('ent-bram', 'person', ?, ?, ?, ?, 0)""",
        (CONTACT, CONTACT.lower(), is_self_entity, "c-bram" if bind_contact else None),
    )
    conn.commit()


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "participation.db"))
    apply_all_migrations(c)
    _seed(c)
    yield c
    c.close()


def _retrieve(conn, query_text=QUERY, *, disclosure_tier="owner_raw", now=None, ledger=None):
    adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=conn))
    return adapter.retrieve(
        RetrievalRequest(
            manifest=resolve_scope_manifest("messages:read"),
            access_mode="summary",
            query_text=query_text,
            installed_source_ids=INSTALLED,
            disclosure_tier=disclosure_tier,
            owner_mode=disclosure_tier == "owner_raw",
            now=now or datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc),
            ledger=ledger,
        )
    )


def _thread_items(bundle) -> List[Dict[str, Any]]:
    return [
        s
        for s in (bundle.context_packet.get("summaries") or [])
        if str(s.get("retrieval_source") or "").startswith("entity_thread")
    ]


def _participants(bundle) -> Dict[str, Dict[str, Any]]:
    return {str(s.get("record_id")): s for s in _thread_items(bundle) if s.get("thread_role") == "participant"}


class TestTheThreadIsReachedByIdentity:
    def test_both_sides_of_the_conversation_arrive_without_a_mention(self, conn) -> None:
        got = _participants(_retrieve(conn))
        assert {"msg-b1", "msg-s1", "msg-b2"} <= set(got), sorted(got)

    def test_a_conversation_the_contact_is_not_in_does_not(self, conn) -> None:
        assert "msg-o1" not in _participants(_retrieve(conn))

    def test_rows_say_who_spoke(self, conn) -> None:
        got = _participants(_retrieve(conn))
        assert got["msg-b1"]["summary_text"].startswith(f"[{CONTACT}]")
        assert got["msg-s1"]["summary_text"].startswith("[me]")

    def test_newest_first(self, conn) -> None:
        ordered = [s["record_id"] for s in _thread_items(_retrieve(conn)) if s.get("thread_role") == "participant"]
        assert ordered.index("msg-b2") < ordered.index("msg-s1") < ordered.index("msg-b1")

    def test_the_ledger_names_the_lane(self, conn) -> None:
        ledger = NarrowingLedger()
        _retrieve(conn, ledger=ledger)
        # The public ledger carries the closed-set slug; `detail` stays on the node.
        public = [e for e in ledger.as_public().get("ledger", []) if e.get("reason") == "entity_thread_lane"]
        assert public, ledger.as_public()
        joins = {str((getattr(e, "detail", None) or {}).get("join") or "") for e in ledger.entries}
        assert "participation" in joins, [getattr(e, "detail", None) for e in ledger.entries]


class TestTheWindowIsSoft:
    def test_a_dated_ask_keeps_the_rows_in_its_window(self, conn) -> None:
        # now = Wed 8 Jul 2026; "last week" = 29 Jun – 5 Jul. The January row is out.
        got = _participants(_retrieve(conn, f"What did {CONTACT} and I message about last week?"))
        assert {"msg-b1", "msg-s1", "msg-b2"} <= set(got)
        assert "msg-b0" not in got

    def test_an_undated_ask_reads_the_newest_rows(self, conn) -> None:
        got = _participants(_retrieve(conn))
        assert "msg-b0" in got  # four rows, cap of sixteen: all of them


class TestWhatTheLaneRefuses:
    def test_nothing_below_the_owner_tier(self, conn) -> None:
        assert _participants(_retrieve(conn, disclosure_tier="default_disclosure")) == {}

    def test_nothing_for_the_owner_s_own_entity(self, tmp_path) -> None:
        c = sqlite3.connect(str(tmp_path / "self.db"))
        apply_all_migrations(c)
        _seed(c, is_self_entity=1)
        try:
            assert _participants(_retrieve(c)) == {}
        finally:
            c.close()

    def test_nothing_for_an_entity_bound_to_no_contact(self, tmp_path) -> None:
        c = sqlite3.connect(str(tmp_path / "unbound.db"))
        apply_all_migrations(c)
        _seed(c, bind_contact=False)
        try:
            assert _participants(_retrieve(c)) == {}
        finally:
            c.close()

    def test_a_full_name_does_not_pull_a_namesake_s_thread(self, conn) -> None:
        """The linker scores a one-word name found whole in the ask as a full match, so a
        second person known as "Bram" alone links beside the contact the full name
        named. Their thread is not what was asked for."""
        _insert_message(conn, "msg-n1", "nate-3", 0, "did you see the match last night", "2026-07-03T08:00:00Z", conversation_id="conv-namesake")
        conn.execute(
            """INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self)
               VALUES ('c-nate', 'd', 'demo_contacts_file', 'Bram', 0)"""
        )
        conn.execute(
            """INSERT INTO contact_identifiers (dataset_id, source_id, identifier, identifier_type, contact_id)
               VALUES ('d', 'demo_contacts_file', 'nate-3', 'phone', 'c-nate')"""
        )
        conn.execute(
            """INSERT INTO entities
               (entity_id, entity_type, canonical_name, normalized_name, is_self, contact_id, mention_count)
               VALUES ('ent-nate', 'person', 'Bram', 'bram', 0, 'c-nate', 40)"""
        )
        conn.commit()
        got = _participants(_retrieve(conn))
        assert "msg-b1" in got
        assert "msg-n1" not in got
        # The bare first name is honestly ambiguous: both threads come.
        both = _participants(_retrieve(conn, "What did Bram and I message about?"))
        assert {"msg-b1", "msg-n1"} <= set(both)

    def test_a_question_naming_nobody_contributes_nothing(self, conn) -> None:
        assert _participants(_retrieve(conn, "what have I been messaging about lately?")) == {}


class TestTheDisclosedPageIsTheOnlyRead:
    def test_canonical_get_is_never_called(self, conn, monkeypatch) -> None:
        from topos.storage.adapters.sqlite import stores

        def _boom(self, table, record_id):  # noqa: ARG001
            raise AssertionError("participation lane fetched a row around the tiered page")

        monkeypatch.setattr(stores.SQLiteCanonicalStore, "get", _boom, raising=True)
        assert _participants(_retrieve(conn))

    def test_record_ids_filter_narrows_the_tiered_page(self, conn) -> None:
        store = AdapterFactory.create("local_database", conn=conn).canonical
        page = store.list("conversation_messages", record_ids=["msg-b1", "msg-o1"], disclosure_tier="owner_raw")
        assert {r.get("record_id") or r.get("message_id") for r in page.items} == {"msg-b1", "msg-o1"}
        assert page.total == 2
        assert store.list("conversation_messages", record_ids=[]).items == []

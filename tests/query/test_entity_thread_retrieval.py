"""Entity-keyed retrieval: the subject's own records, contributed beside the scope routes.

Everything else in the query path routes by SCOPE and filters by TIME. A question about
a PERSON or a RECORD — "what happened with the Anthropic thread" — is answered by
whether the owner's words happen to appear in a row, while the entity graph that knows
exactly which rows belong to that subject sits one join away and unreachable. These
tests pin the lane that closes that, and — more importantly — pin that closing it did
not open anything else.

The privacy argument is the whole point of the file. An entity join is the natural way
to walk around a disclosure plane, because it selects rows by an id the requester never
had to type. So the lane is built to select FROM disclosed rows rather than to fetch
them: `CanonicalStore.get()` takes no `disclosure_tier` at all, and the tests below pin
that it is never called. Every other plane — scope ceiling, exclusions, black hole, the
rare gate, the time window — is exercised against a row that could ONLY have arrived
through this lane, because a lane that is tested only on rows the keyword path would
have returned anyway is not tested at all.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

import pytest

from topos.query import narrowing as _N
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.narrowing import NarrowingLedger
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

SOURCE_ID = "imessage"
INSTALLED = [SOURCE_ID]

#: The thread record. Its text never names the entity — only the mention join can
#: reach it, which is exactly the row the keyword path loses today.
THREAD_ID = "msg-thread-1"
THREAD_TEXT = "shipping the compiler rewrite on friday"

#: An ordinary neighbour, linked to nothing.
OTHER_ID = "msg-other-1"
OTHER_TEXT = "picking up groceries after work"

ENTITY_NAME = "Anthropic"
QUERY = "what happened with the Anthropic thread"


def _message(message_id: str, content: str, ts: str = "2026-03-13T12:00:00Z") -> Dict[str, Any]:
    return {
        "message_id": message_id,
        "thread_id": "thread-1",
        "content": content,
        "ts": ts,
        "sender_type": "contact",
    }


def _seed_messages(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> None:
    mgr = ConversationsTablesManager(conn)
    for row in rows:
        mgr.upsert_message_batch(
            [row],
            dataset_id="user:default:device",
            source_id=SOURCE_ID,
            sync_batch_id=f"batch-{row['message_id']}",
        )
    conn.commit()


def _add_entity(
    conn: sqlite3.Connection,
    entity_id: str,
    name: str,
    *,
    entity_type: str = "org",
    is_self: int = 0,
    mention_count: int = 4,
) -> None:
    conn.execute(
        """
        INSERT INTO entities
            (entity_id, entity_type, canonical_name, normalized_name, is_self, mention_count)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (entity_id, entity_type, name, name.lower(), is_self, mention_count),
    )


def _add_mention(
    conn: sqlite3.Connection,
    mention_id: str,
    entity_id: str,
    record_id: str,
    *,
    canonical_table: str | None = "conversation_messages",
    surface: str = ENTITY_NAME,
) -> None:
    conn.execute(
        """
        INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, source_id, canonical_table, surface_text, event_at)
        VALUES (?, ?, ?, ?, ?, ?, '2026-03-13T12:00:00Z')
        """,
        (mention_id, entity_id, record_id, SOURCE_ID, canonical_table, surface),
    )


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "entity_thread.db"))
    apply_all_migrations(c)
    _seed_messages(c, [_message(THREAD_ID, THREAD_TEXT), _message(OTHER_ID, OTHER_TEXT)])
    _add_entity(c, "ent-anthropic", ENTITY_NAME)
    _add_mention(c, "m-1", "ent-anthropic", THREAD_ID)
    c.commit()
    yield c
    c.close()


def _retrieve(
    conn: sqlite3.Connection,
    query_text: str,
    *,
    ledger=None,
    disclosure_tier: str = "owner_raw",
    scope: str = "messages:read",
    manifest=None,
):
    adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=conn))
    return adapter.retrieve(
        RetrievalRequest(
            manifest=manifest if manifest is not None else resolve_scope_manifest(scope),
            access_mode="summary",
            query_text=query_text,
            installed_source_ids=INSTALLED,
            disclosure_tier=disclosure_tier,
            ledger=ledger,
        )
    )


def _summaries(bundle) -> List[Dict[str, Any]]:
    return list(bundle.context_packet.get("summaries") or [])


def _thread_items(bundle) -> List[Dict[str, Any]]:
    return [
        s
        for s in _summaries(bundle)
        if str(s.get("retrieval_source") or "").startswith("entity_thread")
        or "entity_thread" in (s.get("fusion_sources") or [])
    ]


def _record_ids(bundle) -> set:
    return {str(s.get("record_id")) for s in _summaries(bundle) if s.get("record_id")}


def _texts(bundle) -> str:
    """What the model will actually read.

    `record_id` is the wrong discriminator here and it took a failing test to see
    why: `entity_context_items` already emits a POINTER item carrying the thread
    record's id and the surface text ("2026-03-13 — Anthropic"). The id was in the
    packet before this set and the record's sentence was not. Content is the claim.
    """
    return " ".join(
        f"{s.get('topic') or ''} {s.get('summary_text') or ''}" for s in _summaries(bundle)
    )


# --------------------------------------------------------------- the question it answers


class TestTheThreadBecomesReachable:
    def test_the_keyword_path_alone_cannot_reach_the_thread_record(self, conn) -> None:
        """The premise. The record belongs to the subject and says nothing about it, so
        every lane that matches the owner's words against row text must miss it. If this
        ever passes, the test below proves nothing."""
        from topos.query.retrieval import _route_canonical_rows

        adapters = AdapterFactory.create("local_database", conn=conn)
        rows = _route_canonical_rows(
            adapters,
            "conversation_messages",
            manifest=resolve_scope_manifest("messages:read"),
            query_text=QUERY,
            source_ids=INSTALLED,
            limit=50,
            disclosure_tier="owner_raw",
        )
        assert THREAD_ID not in {str(r.get("record_id") or r.get("message_id")) for r in rows}

    def test_naming_the_entity_contributes_its_records(self, conn) -> None:
        bundle = _retrieve(conn, QUERY)
        assert THREAD_TEXT in _texts(bundle), (
            "the entity's own record must reach the packet once the entity is resolved"
        )

    def test_the_contribution_is_attributed_to_the_lane(self, conn) -> None:
        """Provenance is not decoration: the coverage map and the exclusion filter both
        read it, and an unattributed row cannot be excluded by its own subject."""
        bundle = _retrieve(conn, QUERY)
        item = next(s for s in _thread_items(bundle) if str(s.get("record_id")) == THREAD_ID)
        assert item.get("summary_text") == THREAD_TEXT
        assert item.get("entity_id") == "ent-anthropic"
        assert item.get("canonical_table") == "conversation_messages"

    def test_a_question_that_names_no_entity_gains_nothing(self, conn) -> None:
        """The lane is additive. An ordinary ask must retrieve exactly what it did."""
        bundle = _retrieve(conn, "picking up groceries after work")
        assert _thread_items(bundle) == []

    def test_it_contributes_beside_the_scope_routes_and_does_not_replace_them(
        self, conn
    ) -> None:
        bundle = _retrieve(conn, f"{ENTITY_NAME} and groceries")
        sources = {
            src
            for s in _summaries(bundle)
            for src in (s.get("fusion_sources") or [str(s.get("retrieval_source") or "")])
        }
        assert any(str(s).startswith("canonical") for s in sources), (
            "the scope routes must still contribute alongside the entity lane"
        )


# ------------------------------------------------------------------- the disclosure plane


class TestItSelectsFromDisclosedRowsRatherThanFetchingAroundThem:
    """`CanonicalStore.get(table, record_id)` takes no `disclosure_tier` — it is the
    obvious way to turn a mention row into a record and it has no tier applied at all.
    The lane must therefore never use it: the mention table supplies an id SET which
    selects from rows the disclosed `list()` path already returned."""

    def test_the_lane_never_fetches_a_record_by_id(self, conn, monkeypatch) -> None:
        adapters = AdapterFactory.create("local_database", conn=conn)
        calls: List[Any] = []
        original = type(adapters.canonical).get

        def _spy(self, table, record_id):
            calls.append((table, record_id))
            return original(self, table, record_id)

        monkeypatch.setattr(type(adapters.canonical), "get", _spy)
        DefaultSignalRetrievalAdapter(adapters).retrieve(
            RetrievalRequest(
                manifest=resolve_scope_manifest("messages:read"),
                access_mode="summary",
                query_text=QUERY,
                installed_source_ids=INSTALLED,
            )
        )
        assert calls == [], (
            "a by-id fetch has no disclosure tier applied — the entity join must select "
            f"from disclosed rows, not fetch around them (saw {calls})"
        )

    def test_every_row_the_lane_reads_carries_the_requested_tier(self, conn) -> None:
        """The tier reaches the adapter on every call the lane makes, so a grantee can
        never be served the owner-raw column by arriving through the entity join."""
        adapters = AdapterFactory.create("local_database", conn=conn)
        seen_tiers: List[str] = []
        original = type(adapters.canonical).list

        def _spy(self, table, **kwargs):
            seen_tiers.append(kwargs.get("disclosure_tier"))
            return original(self, table, **kwargs)

        type(adapters.canonical).list = _spy
        try:
            DefaultSignalRetrievalAdapter(adapters).retrieve(
                RetrievalRequest(
                    manifest=resolve_scope_manifest("messages:read"),
                    access_mode="summary",
                    query_text=QUERY,
                    installed_source_ids=INSTALLED,
                    disclosure_tier="default_disclosure",
                )
            )
        finally:
            type(adapters.canonical).list = original
        assert seen_tiers, "the lane must have read something"
        assert set(seen_tiers) == {"default_disclosure"}

    def test_a_grantee_never_receives_the_owner_raw_text_through_the_join(self, conn) -> None:
        bundle = _retrieve(conn, QUERY, disclosure_tier="default_disclosure")
        for item in _summaries(bundle):
            assert THREAD_TEXT not in str(item.get("summary_text") or ""), (
                "the owner's raw sentence reached a grantee through the entity lane"
            )


class TestTheScopeCeilingStillBounds:
    def test_a_record_outside_the_scope_tables_is_unreachable(self, conn) -> None:
        """The entity links a journal row. `messages:read` does not authorize
        `journal_entries`, so the thread stops at the scope boundary — an entity is not
        a way to read a table the grant never named."""
        conn.execute(
            """
            INSERT INTO journal_entries (entry_id, source_id, content, entry_at)
            VALUES ('j-secret', ?, 'the private entry', '2026-03-13T12:00:00Z')
            """,
            (SOURCE_ID,),
        )
        _add_mention(
            conn, "m-j", "ent-anthropic", "j-secret", canonical_table="journal_entries"
        )
        conn.commit()

        bundle = _retrieve(conn, QUERY)
        assert "the private entry" not in _texts(bundle)
        assert all(
            i.get("canonical_table") == "conversation_messages" for i in _thread_items(bundle)
        )

    def test_an_untabled_mention_is_still_bounded_by_the_scope(self, conn) -> None:
        """619 of this node's 4313 mention rows have a NULL `canonical_table`. Those
        records are offered to every SCANNED table rather than dropped — and the
        scanned tables are the manifest's, so a NULL table widens nothing."""
        conn.execute(
            """
            INSERT INTO journal_entries (entry_id, source_id, content, entry_at)
            VALUES ('j-untabled', ?, 'the untabled entry', '2026-03-13T12:00:00Z')
            """,
            (SOURCE_ID,),
        )
        _add_mention(conn, "m-u", "ent-anthropic", "j-untabled", canonical_table=None)
        conn.commit()

        bundle = _retrieve(conn, QUERY)
        assert "the untabled entry" not in _texts(bundle)


# ------------------------------------------------------------ the planes set 6 installed


class TestTheExclusionPlaneStillReachesIt:
    """Set 6 compiles "…but nothing about X" to a filter applied inside the retrieval
    boundary. A row that arrived BECAUSE of an entity must be reachable by that same
    entity, or the lane is a hole in the exclusion contract: the one ask most likely
    to name an entity is the one most likely to exclude one."""

    def test_excluding_the_entity_removes_the_row_it_contributed(self, conn) -> None:
        bundle = _retrieve(conn, f"{QUERY}, but nothing about {ENTITY_NAME}")
        assert THREAD_TEXT not in _texts(bundle), (
            "the row the entity contributed survived an exclusion naming that entity"
        )
        assert _thread_items(bundle) == []

    def test_the_exclusion_is_reported_as_applied(self, conn) -> None:
        """A silently-unapplied exclusion is worse than a refused one."""
        bundle = _retrieve(conn, f"{QUERY}, but nothing about {ENTITY_NAME}")
        block = bundle.context_packet.get("exclusion") or {}
        assert block, "the exclusion plane did not run over the entity lane's output"

    def test_an_unrelated_exclusion_leaves_the_thread_alone(self, conn) -> None:
        bundle = _retrieve(conn, f"{QUERY}, but nothing about groceries")
        assert THREAD_TEXT in _texts(bundle)


class TestTheRareGateStillVetoes:
    """The lane adds rows to the evidence set the gate reads, so a thread record can
    now evidence a token nothing else could. That must not become a way to answer an
    ask about something the corpus does not contain.

    Pinned at the fusion boundary rather than end-to-end: `_rare_tokens` returns {}
    for an index under `rare_token_df_max * 10` rows, so on a fixture-sized corpus the
    gate is deliberately inert and an end-to-end assertion would pass for the wrong
    reason. These call the gate with the df map it would have been given.
    """

    def _fuse(self, lists, groups):
        from topos.query.retrieval import _rrf_fuse_summary_lists

        return _rrf_fuse_summary_lists(
            lists,
            context_sources=frozenset({"briefs", "signal_facts", "vector_context"}),
            rare_token_groups=groups,
        )

    def _thread_item(self):
        return {
            "summary_text": THREAD_TEXT,
            "topic": THREAD_TEXT,
            "record_id": THREAD_ID,
            "retrieval_source": "entity_thread:conversation_messages",
        }

    def test_a_thread_does_not_answer_an_ask_about_something_absent(self) -> None:
        fused = self._fuse(
            [("entity_thread", 1.0, [self._thread_item()])], [{"kubernetes": 0}]
        )
        assert fused == [], (
            "the entity lane answered a question about a word the corpus never contains"
        )

    def test_the_veto_is_attributed_to_the_gate_not_to_an_empty_store(self) -> None:
        from topos.query.retrieval import _rrf_fuse_summary_lists

        ledger = NarrowingLedger()
        _rrf_fuse_summary_lists(
            [("entity_thread", 1.0, [self._thread_item()])],
            rare_token_groups=[{"kubernetes": 0}],
            ledger=ledger,
        )
        assert ledger.empty_cause == _N.CAUSE_GATE_VETOED

    def test_a_thread_record_may_evidence_the_ask_it_actually_contains(self) -> None:
        """The other half. The gate exists to refuse fabricated topics, not to refuse
        rows that arrived by an entity key — a thread record containing the rare word
        is exactly the evidence the ask wanted."""
        fused = self._fuse(
            [("entity_thread", 1.0, [self._thread_item()])], [{"compiler": 1}]
        )
        assert fused, "a thread record containing the rare token was vetoed anyway"

    def test_the_lane_is_evidence_not_context(self) -> None:
        """`context_sources` lanes cannot justify a non-empty result on their own. A
        thread record is an ordinary canonical row that arrived by a different key —
        it is evidence, and this pins that classification deliberately."""
        assert self._fuse([("entity_thread", 1.0, [self._thread_item()])], None), (
            "entity_thread must count as evidence, not as context colour"
        )


class TestTheEntityPlanesOwnArtefacts:
    """DO 4. Both of these have produced artefacts on this node."""

    def test_the_owner_entity_contributes_no_thread(self, conn) -> None:
        """`is_self` links on ordinary first-person phrasing and its "thread" is not a
        subject — it is the corpus. On this node the self row carries zero mentions,
        so the guard is free today; it exists for the day the resolver attaches them."""
        conn.execute("UPDATE entities SET is_self = 1 WHERE entity_id = 'ent-anthropic'")
        conn.commit()
        bundle = _retrieve(conn, QUERY)
        assert _thread_items(bundle) == []
        assert THREAD_TEXT not in _texts(bundle)

    def test_unconsolidated_aliases_each_contribute_their_own_records(self, conn) -> None:
        """`normalized_name` duplicates are real and heavy on this node ('topos' ×7,
        'personal projects' ×13). Two rows for one subject must not mean half a
        thread: the lane keys on the resolved ids, whatever their number, and dedupes
        on (table, record_id) so an overlap counts once."""
        second = _message("msg-thread-2", "the second half of the same conversation")
        _seed_messages(conn, [second])
        _add_entity(conn, "ent-anthropic-dup", ENTITY_NAME, mention_count=2)
        _add_mention(conn, "m-2", "ent-anthropic-dup", "msg-thread-2")
        _add_mention(conn, "m-3", "ent-anthropic-dup", THREAD_ID)  # overlaps the first
        conn.commit()

        bundle = _retrieve(conn, QUERY)
        text = _texts(bundle)
        assert THREAD_TEXT in text and "the second half of the same conversation" in text, (
            "an unconsolidated duplicate lost half the thread"
        )
        thread_ids = [str(s.get("record_id")) for s in _thread_items(bundle)]
        assert len(thread_ids) == len(set(thread_ids)), "a record overlapping two alias rows was duplicated"


class TestTheSelectorPolicyStillBinds:
    """An active allow-list is person-shaped in the pipeline; this lane also threads
    orgs and places, so it refuses on its own account."""

    def _restricted(self, allowed):
        import dataclasses

        return dataclasses.replace(
            resolve_scope_manifest("messages:read"),
            entity_selector_policy_active=True,
            accessible_entity_ids=list(allowed),
        )

    def test_an_entity_outside_the_allow_list_contributes_nothing(self, conn) -> None:
        bundle = _retrieve(conn, QUERY, manifest=self._restricted([]))
        assert _thread_items(bundle) == []
        assert THREAD_TEXT not in _texts(bundle)

    def test_an_entity_on_the_allow_list_still_contributes(self, conn) -> None:
        """The refusal has to be the policy, not the lane quietly never firing."""
        bundle = _retrieve(conn, QUERY, manifest=self._restricted(["ent-anthropic"]))
        assert THREAD_TEXT in _texts(bundle)


# ------------------------------------------------------------------------- the ledger


class TestItDeclaresItselfInTheLedger:
    def test_the_lane_records_its_contribution(self, conn) -> None:
        ledger = NarrowingLedger()
        _retrieve(conn, QUERY, ledger=ledger)
        entry = next(
            (
                e
                for e in ledger.entries
                if e.stage == _N.STAGE_RETRIEVAL and e.reason == "entity_thread_lane"
            ),
            None,
        )
        assert entry is not None, "the lane contributed without saying so"
        assert entry.action == "contributed"
        assert (entry.detail or {}).get("contributed", 0) >= 1

    def test_a_refusal_is_recorded_too(self, conn) -> None:
        """A thread that was declined and a thread that was empty are different
        answers, and set 4's whole argument is that they must not look the same."""
        conn.execute("UPDATE entities SET is_self = 1 WHERE entity_id = 'ent-anthropic'")
        conn.commit()
        ledger = NarrowingLedger()
        _retrieve(conn, QUERY, ledger=ledger)
        assert any(e.reason == "entity_thread_is_self" for e in ledger.entries)

    def test_nothing_the_lane_writes_carries_the_owners_text(self, conn) -> None:
        """`as_public` travels off the node and `as_telemetry` is aggregated. Neither
        may carry a record's content, an entity's name, or the question."""
        ledger = NarrowingLedger()
        _retrieve(conn, QUERY, ledger=ledger)
        public = str(ledger.as_public())
        telemetry = str(ledger.as_telemetry())
        for blob in (public, telemetry):
            assert THREAD_TEXT not in blob
            assert ENTITY_NAME.lower() not in blob.lower()
            assert "conversation_messages" not in blob
        entry = next(e for e in ledger.entries if e.reason == "entity_thread_lane")
        assert "detail" not in entry.as_public()

    def test_passing_no_ledger_changes_nothing(self, conn) -> None:
        """Set 4's first rule. The ledger observes; it must not participate."""
        with_ledger = _texts(_retrieve(conn, QUERY, ledger=NarrowingLedger()))
        without = _texts(_retrieve(conn, QUERY))
        assert with_ledger == without


# -------------------------------------------------------------------- the time window


class TestTheTimeWindowStillPrefers:
    def test_an_out_of_window_thread_record_is_marked_not_hidden(self, conn) -> None:
        """The soft-window contract, unchanged: a time-scoped ask over a lane with
        nothing in range degrades to dated-but-out-of-window evidence, ANNOTATED, so
        synthesis can say "nothing from yesterday, most recent instead" rather than
        passing a stale row off as in-range."""
        bundle = _retrieve(conn, f"{QUERY} yesterday")
        for item in _thread_items(bundle):
            assert "in_time_window" in item or item.get("event_at")

    def test_an_in_window_record_wins_over_an_older_one(self, conn) -> None:
        from topos.query.retrieval import _prefer_time_window

        old = {"record_id": "old", "event_at": "2020-01-01T00:00:00Z"}
        new = {"record_id": "new", "event_at": "2026-03-13T12:00:00Z"}
        kept = _prefer_time_window(
            [dict(old), dict(new)], ("2026-03-01T00:00:00Z", "2026-03-31T00:00:00Z")
        )
        assert [i["record_id"] for i in kept] == ["new"]

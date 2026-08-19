"""Exclusion is enforced in the RETRIEVAL plane, or it is not enforced at all.

"…everything about my week, but nothing from the therapy journal" is a disclosure
instruction. There is exactly one wrong place to implement it — the synthesis
prompt — because a model that has been shown the journal entry and asked nicely to
ignore it is not an enforcement mechanism, and by then the entry is already in the
context packet, the stored artifact and the audit trail.

These tests pin four things:

* the prose compiles to a CLOSED, typed structure (category / tier / entity);
* the drop happens inside `retrieve`, so nothing excluded is ever in the bundle
  the disclosure filter, the game layer and synthesis receive;
* the ledger says `disclosure / excluded / <closed reason>` with a dropped count,
  and carries no owner text;
* an exclusion that cannot be compiled FAILS LOUD — reported as not applied —
  rather than passing through as though it had been honoured.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.query import narrowing as _N
from topos.query.exclusion import (
    ENTITY_PUBLIC_SLUG,
    KIND_CATEGORY,
    KIND_ENTITY,
    KIND_TIER,
    apply_exclusions,
    enforce_request_exclusions,
    parse_exclusions,
)
from topos.query.game_layer import DefaultGameLayer
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.narrowing import NarrowingLedger
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations


# --------------------------------------------------------------------- compilation


class TestTheProseCompiles:
    def test_nothing_from_the_therapy_journal_is_the_journal_category(self) -> None:
        spec = parse_exclusions(
            "summarize everything about my week, but nothing from the therapy journal"
        )
        assert [(t.kind, t.slug) for t in spec.targets] == [(KIND_CATEGORY, "journal")]
        assert spec.fully_enforced is True

    def test_a_bare_therapy_is_health_not_the_journal(self) -> None:
        """Longest trigger wins: 'therapy journal' is the journal, 'therapy' is health.
        A shortest-match parse would silently take the wrong lane."""
        spec = parse_exclusions("my week but nothing about therapy")
        assert [(t.kind, t.slug) for t in spec.targets] == [(KIND_CATEGORY, "health")]

    def test_several_targets_in_one_clause(self) -> None:
        spec = parse_exclusions("what did I do this week? Exclude my journal and my finances.")
        assert {t.slug for t in spec.targets} == {"journal", "finance"}

    def test_a_content_tier_compiles_to_a_tier_target(self) -> None:
        spec = parse_exclusions("summarize my week but not the nsfw stuff")
        assert [(t.kind, t.slug) for t in spec.targets] == [(KIND_TIER, "nsfw")]

    @pytest.mark.parametrize(
        "query",
        [
            "how often do I go to the gym",
            "what did I browse about kayaks this week?",
            "show me messages from Sarah",
            "generate a personal work report and summarize achievements with any adjustments made",
            "who did I meet with last month",
        ],
    )
    def test_an_ordinary_ask_carries_no_exclusion(self, query: str) -> None:
        """A false positive here is silent under-retrieval on a normal question, so
        the leads are deliberately explicit — a bare "no X" is not an exclusion."""
        assert parse_exclusions(query).requested is False

    def test_the_public_form_is_closed_set_slugs_only(self) -> None:
        spec = parse_exclusions("my week, excluding my journal")
        public = spec.as_public()
        assert public == {
            "requested": True,
            "enforced": True,
            "applied": [{"kind": KIND_CATEGORY, "slug": "journal"}],
            "not_applied": 0,
        }


# ----------------------------------------------------------------------- fail loud


class TestAmbiguityIsARefusalToGuess:
    def test_an_uncompilable_exclusion_is_reported_as_not_applied(self) -> None:
        spec = parse_exclusions("summarize my week, excluding anything about quantum widgets")
        assert spec.requested is True
        assert spec.targets == ()
        assert spec.unresolved == ("quantum widgets",)
        assert spec.fully_enforced is False, (
            "an exclusion the engine could not compile must never be reported as enforced"
        )

    def test_a_partially_compiled_exclusion_is_not_fully_enforced(self) -> None:
        spec = parse_exclusions("my week, excluding my journal and quantum widgets")
        assert {t.slug for t in spec.targets} == {"journal"}
        assert spec.unresolved == ("quantum widgets",)
        assert spec.fully_enforced is False

    def test_a_name_the_entity_plane_cannot_bind_does_not_become_a_substring_filter(self) -> None:
        """Guessing here would drop rows on a common word AND claim enforcement — the
        exact combination this module exists to prevent."""
        spec = parse_exclusions("exclude anything involving Sarah", None)
        assert spec.targets == ()
        assert spec.unresolved == ("sarah",)

    def test_the_not_applied_count_reaches_the_public_block(self) -> None:
        packet = {"summaries": [{"summary_text": "kept"}]}
        block = enforce_request_exclusions(
            packet, query_text="my week, excluding anything about quantum widgets"
        )
        assert block["requested"] is True
        assert block["enforced"] is False
        assert block["not_applied"] == 1
        assert packet["summaries"] == [{"summary_text": "kept"}]

    def test_the_ledger_records_the_refusal(self) -> None:
        ledger = NarrowingLedger()
        enforce_request_exclusions(
            {"summaries": []},
            query_text="my week, excluding anything about quantum widgets",
            ledger=ledger,
        )
        entries = [e for e in ledger.entries if e.stage == _N.STAGE_DISCLOSURE]
        assert [(e.action, e.reason) for e in entries] == [
            ("not_applied", "exclusion_uncompilable")
        ]


# -------------------------------------------------------------------- what it drops


class TestItFiltersTheRetrievedItems:
    def _packet(self):
        return {
            "rows": [
                {"_table": "journal_entries", "record_id": "j1", "content": "therapy session"},
                {"_table": "conversation_messages", "record_id": "m1", "content": "hi"},
            ],
            "summaries": [
                {"summary_text": "journal roundup", "retrieval_source": "grow_journal"},
                {"summary_text": "work roundup", "dimension": "Work"},
            ],
        }

    def test_the_excluded_category_leaves_every_lane(self) -> None:
        packet = self._packet()
        spec = parse_exclusions("my week, but nothing from the journal")
        outcome = apply_exclusions(packet, spec)
        assert [r["record_id"] for r in packet["rows"]] == ["m1"]
        assert [s["summary_text"] for s in packet["summaries"]] == ["work roundup"]
        assert outcome.dropped == 2

    def test_nothing_else_is_touched(self) -> None:
        packet = self._packet()
        apply_exclusions(packet, parse_exclusions("my week, but nothing from the journal"))
        assert len(packet["rows"]) == 1 and len(packet["summaries"]) == 1

    def test_no_exclusion_leaves_the_packet_identical(self) -> None:
        packet = self._packet()
        before = {k: list(v) for k, v in packet.items()}
        assert enforce_request_exclusions(packet, query_text="summarize my week") is None
        assert packet == before

    def test_an_nsfw_tier_exclusion_uses_the_ingest_flag(self) -> None:
        packet = {
            "rows": [
                {"record_id": "a", "content_nsfw": 1},
                {"record_id": "b", "content_nsfw": 0},
            ]
        }
        apply_exclusions(packet, parse_exclusions("my week but not the nsfw stuff"))
        assert [r["record_id"] for r in packet["rows"]] == ["b"]

    def test_the_ledger_reports_stage_action_and_dropped_count(self) -> None:
        packet = self._packet()
        ledger = NarrowingLedger()
        enforce_request_exclusions(
            packet, query_text="my week, but nothing from the journal", ledger=ledger
        )
        entry = next(e for e in ledger.entries if e.action == "excluded")
        assert entry.stage == _N.STAGE_DISCLOSURE
        assert entry.reason == "exclusion_category"
        assert entry.dropped == 2

    def test_an_exclusion_that_empties_the_turn_is_a_policy_cause_not_an_absence(self) -> None:
        """'no data' would be a lie about the owner's own instruction — the same call
        the black-hole policy makes."""
        packet = {"summaries": [{"summary_text": "j", "retrieval_source": "grow_journal"}]}
        ledger = NarrowingLedger()
        enforce_request_exclusions(
            packet, query_text="my week, but nothing from the journal", ledger=ledger
        )
        assert packet["summaries"] == []
        assert ledger.empty_cause == _N.CAUSE_SCOPE_DENIED


# ------------------------------------------------------------------------- privacy


class TestNoOwnerTextLeaves:
    def test_the_public_ledger_carries_closed_set_enums_only(self) -> None:
        ledger = NarrowingLedger()
        enforce_request_exclusions(
            {"summaries": []},
            query_text="my week, excluding anything about quantum widgets",
            ledger=ledger,
        )
        blob = str(ledger.as_public())
        assert "quantum" not in blob and "widgets" not in blob

    def test_the_fragment_survives_only_in_the_local_only_detail(self) -> None:
        ledger = NarrowingLedger()
        enforce_request_exclusions(
            {"summaries": []},
            query_text="my week, excluding anything about quantum widgets",
            ledger=ledger,
        )
        assert "quantum widgets" in str(ledger.as_local())

    def test_telemetry_is_counts_and_enums(self) -> None:
        ledger = NarrowingLedger()
        enforce_request_exclusions(
            {"summaries": []},
            query_text="my week, excluding anything about quantum widgets",
            ledger=ledger,
        )
        assert "quantum" not in str(ledger.as_telemetry())

    def test_an_excluded_entity_is_public_as_a_kind_never_as_a_name(self) -> None:
        from topos.query.exclusion import ExclusionTarget

        target = ExclusionTarget(
            kind=KIND_ENTITY, slug="sarah", entity_ids=("e1",), terms=("sarah",)
        )
        assert target.as_public() == {"kind": KIND_ENTITY, "slug": ENTITY_PUBLIC_SLUG}


# ---------------------------------------------------------------- the plane it runs on


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "exclusion.db"))
    apply_all_migrations(c)
    mgr = ConversationsTablesManager(c)
    for source_id, message_id, content in (
        ("demo_messenger_file", "msg-demo-1", "Demo messenger hello"),
        ("imessage", "msg-im-1", "iMessage follow-up"),
    ):
        mgr.upsert_message_batch(
            [
                {
                    "message_id": message_id,
                    "thread_id": f"thread-{source_id}",
                    "content": content,
                    "ts": "2026-03-13T12:00:00Z",
                    "sender_type": "contact",
                }
            ],
            dataset_id="user:default:device",
            source_id=source_id,
            sync_batch_id=f"batch-{message_id}",
        )
    c.commit()
    yield c
    c.close()


class TestEnforcementIsInRetrieval:
    def _retrieve(self, conn, query_text: str, ledger=None):
        adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=conn))
        return adapter.retrieve(
            RetrievalRequest(
                manifest=resolve_scope_manifest("messages:read"),
                access_mode="raw",
                query_text=query_text,
                installed_source_ids=["demo_messenger_file", "imessage"],
                ledger=ledger,
            )
        )

    def test_excluded_rows_are_absent_from_the_bundle_itself(self, conn) -> None:
        """Not "redacted downstream" — absent. The disclosure filter, the game layer,
        the artifact cache and the synthesis prompt are all handed this bundle."""
        baseline = self._retrieve(conn, "show my messages")
        assert baseline.context_packet.get("rows"), "fixture must retrieve something"

        bundle = self._retrieve(conn, "show my week but nothing from messages")
        assert bundle.context_packet.get("rows") == []
        assert bundle.context_packet["exclusion"]["enforced"] is True
        assert bundle.context_packet["exclusion"]["dropped"] >= 1

    def test_the_ledger_entry_lands_on_the_request_ledger(self, conn) -> None:
        ledger = NarrowingLedger()
        self._retrieve(conn, "show my week but nothing from messages", ledger=ledger)
        assert any(
            e.stage == _N.STAGE_DISCLOSURE and e.action == "excluded" for e in ledger.entries
        )

    def test_retrieval_metadata_carries_counts_only(self, conn) -> None:
        bundle = self._retrieve(conn, "show my week but nothing from messages")
        meta = bundle.retrieval_metadata
        assert meta["exclusion_requested"] is True
        assert meta["exclusion_enforced"] is True
        assert meta["exclusion_not_applied"] == 0

    def test_a_request_without_an_exclusion_gains_no_block(self, conn) -> None:
        bundle = self._retrieve(conn, "show my messages")
        assert "exclusion" not in bundle.context_packet
        assert "exclusion_requested" not in bundle.retrieval_metadata


class TestTheVerdictReachesTheAnswer:
    """Synthesis must never be the enforcement point — but it must be TOLD, so an
    answer cannot read as though an uncompilable exclusion was honoured."""

    @pytest.mark.parametrize("mode", ["raw", "summary", "inference"])
    def test_every_mode_carries_the_exclusion_block(self, mode: str) -> None:
        packet = {"exclusion": {"requested": True, "enforced": False, "not_applied": 1}}
        public = DefaultGameLayer().apply(
            context_packet=packet, access_mode=mode, scope_id="messages:read", query_text="q"
        )
        assert public.payload["exclusion"]["enforced"] is False

    def test_no_block_when_nothing_was_excluded(self) -> None:
        public = DefaultGameLayer().apply(
            context_packet={"rows": []}, access_mode="raw", scope_id="messages:read", query_text="q"
        )
        assert "exclusion" not in public.payload


class TestNamedEntityExclusion:
    """"exclude anything involving Sarah" — resolved through the entity plane the
    black-hole feature already uses, and filtered by BOTH halves of it."""

    PERSON = "Sarah Ok91okoye"

    @pytest.fixture()
    def conn(self, tmp_path):
        c = sqlite3.connect(str(tmp_path / "entity_exclusion.db"))
        apply_all_migrations(c)
        c.execute(
            """
            INSERT INTO entities
                (entity_id, entity_type, canonical_name, normalized_name, mention_count)
            VALUES ('ent-s', 'person', ?, ?, 3)
            """,
            (self.PERSON, self.PERSON.lower()),
        )
        # rec-join is linked ONLY through the mention table: its text never names
        # the person, so only the join can catch it.
        c.execute(
            """
            INSERT INTO entity_mentions
                (mention_id, entity_id, record_id, source_id, canonical_table, surface_text)
            VALUES ('m1', 'ent-s', 'rec-join', 'src', 'conversation_messages', ?)
            """,
            (self.PERSON,),
        )
        c.commit()
        yield c
        c.close()

    def _packet(self):
        return {
            "rows": [
                {"record_id": "rec-join", "content": "see you at 7"},
                {"record_id": "rec-text", "content": f"lunch with {self.PERSON}"},
                {"record_id": "rec-ok", "content": "standup with someone else"},
            ]
        }

    def test_the_entity_plane_resolves_the_fragment(self, conn) -> None:
        spec = parse_exclusions(f"my week but exclude anything involving {self.PERSON}", conn)
        assert [t.kind for t in spec.targets] == [KIND_ENTITY]
        assert spec.targets[0].entity_ids == ("ent-s",)
        assert spec.fully_enforced is True

    def test_both_halves_of_the_filter_fire(self, conn) -> None:
        """The mention join catches what the resolver bound; the text scan catches a
        surface it never bound. Neither is sufficient alone."""
        packet = self._packet()
        spec = parse_exclusions(f"my week but exclude anything involving {self.PERSON}", conn)
        outcome = apply_exclusions(packet, spec, conn=conn)
        assert [r["record_id"] for r in packet["rows"]] == ["rec-ok"]
        assert outcome.dropped == 2

    def test_the_ledger_reason_is_the_entity_enum_and_the_name_stays_local(self, conn) -> None:
        packet = self._packet()
        ledger = NarrowingLedger()
        block = enforce_request_exclusions(
            packet,
            query_text=f"my week but exclude anything involving {self.PERSON}",
            conn=conn,
            ledger=ledger,
        )
        entry = next(e for e in ledger.entries if e.action == "excluded")
        assert (entry.stage, entry.reason, entry.dropped) == (
            _N.STAGE_DISCLOSURE,
            "exclusion_entity",
            2,
        )
        assert block["applied"] == [{"kind": KIND_ENTITY, "slug": ENTITY_PUBLIC_SLUG}]
        assert "ok91okoye" not in str(ledger.as_public()).lower()
        assert "ok91okoye" not in str(block).lower()


class TestAnExcludedAskCannotBeServedFromAnUnexcludedArtifact:
    def test_the_exclusion_clause_changes_the_intent_hash(self) -> None:
        """The exclusion is parsed from `query_text`, which is already hashed — so the
        memory-hit path cannot replay an answer built without the exclusion."""
        from topos.query.intent import compute_intent_hash

        base = compute_intent_hash(
            scope_id="messages:read", access_mode="summary", query_text="summarize my week"
        )
        excluded = compute_intent_hash(
            scope_id="messages:read",
            access_mode="summary",
            query_text="summarize my week, but nothing from the journal",
        )
        assert base != excluded

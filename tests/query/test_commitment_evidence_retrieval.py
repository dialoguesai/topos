"""Q1 — commitment tracking: did the owner actually do the thing they said they'd do?

"What did I say I'd do last week, and did I actually do it?" is one question and the
engine answers it with two unrelated lists: the goals it extracted, and whatever journal
or message rows the words happened to rank. Nothing connects them. A goal that says
"finish the rewrite" and an unrelated entry that says "rewrote the intro" share enough
wording to look like progress, so the model asserts follow-through it cannot see. That is
a CONFIDENT WRONG answer — the most damaging failure shape in the catalog, because the
owner has no way to tell it from a right one.

The fix is not a better ranker. It is a JOIN, and the join is PER GOAL:

  * every stated goal is an item with its own `record_id` and its own entity ids,
  * evidence is retrieved against THOSE ids — not against a blended query built from all
    the goals at once, which is how one goal's evidence gets attributed to another,
  * evidence must post-date the statement, because a row from before the commitment is
    not evidence the commitment was kept,
  * and every claim of progress carries the record ids it rests on. An unsourced progress
    claim is the bug this mode exists to remove.

"NO EVIDENCE FOUND" IS A FIRST-CLASS RESULT and it is not one result. A goal the engine
could not join at all, a goal whose stores hold nothing, a goal it looked for and did not
find, and a goal whose evidence a plane removed are four different sentences to the owner
and four different remedies. They ride the shipped empty-cause taxonomy, per goal, in
closed-set slugs — and the coverage contract is that they never collapse into one label.

THE PRIVACY ARGUMENT IS THE SHAPE OF THE MECHANISM. The lane adds no plane of its own; it
reuses the shipped entity resolver, the shipped mention join, the shipped wire-A black
hole and the shipped disclosed `list()`, and then the REPORT is intersected with
`packet["summaries"]` at the very end of `retrieve()` — after the fusion cap, after
`_blackhole_policy_for_summary`, after `_enforce_request_exclusions`. So it cannot cite a
record the answer does not already carry. Sever any one plane and the citation goes with
it. The tests below sever them one at a time and check exactly that, on the grantee path
as well as the owner path.
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
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

SOURCE = "imessage"
INSTALLED = [SOURCE]

#: The catalog's own phrasing. Kept verbatim: the intent gate is the thing that decides
#: whether the mode fires at all, and it must fire on THIS sentence.
QUERY = "what did I say I'd do last week, and did I actually do it"

#: Pinned so "last week" is arithmetic rather than a race against the wall clock.
NOW = "2026-03-16T12:00:00Z"

#: Coined subjects, so no keyword or rare-token path can reach these rows by accident.
#: Everything the lane finds, it finds through the goal's own ids — which is what makes
#: the contents of the report evidence about the mechanism rather than about the corpus.
KEPT_TOPIC = "Threnody"
KEPT_ENTITY = "ent-threnody"
DROPPED_TOPIC = "Perambulator"
DROPPED_ENTITY = "ent-perambulator"

#: goal → its evidence. `GOAL_KEPT` is followed through on; `GOAL_DROPPED` is not.
GOAL_KEPT = ("g-kept", "I said I'd finish the Threnody rewrite", "2026-03-10T09:00:00Z")
GOAL_DROPPED = ("g-dropped", "I said I'd call Perambulator back", "2026-03-11T09:00:00Z")
EVIDENCE_KEPT = ("e-kept", "shipped the Threnody rewrite today", "2026-03-14T09:00:00Z")

#: Dated BEFORE the goal was stated, and linked to the same entity. Wording and ids both
#: say "related"; chronology says it cannot be evidence the commitment was kept.
EVIDENCE_STALE = ("e-stale", "the Threnody rewrite is a mess", "2026-03-01T09:00:00Z")


# ------------------------------------------------------------------------ the fixture


def _seed_message(conn: sqlite3.Connection, message_id: str, content: str, ts: str) -> None:
    ConversationsTablesManager(conn).upsert_message_batch(
        [
            {
                "message_id": message_id,
                "thread_id": "thread-1",
                "content": content,
                "ts": ts,
                "sender_type": "self",
                "sender_id": None,
            }
        ],
        dataset_id="user:default:device",
        source_id=SOURCE,
        sync_batch_id=f"batch-{message_id}",
    )
    # `user_goals.event_at` is derived from the unified timeline projection, and the
    # whole ordering rule ("evidence follows the commitment") rests on it. Real ingest
    # writes this row; writing it here keeps the dating path under test.
    conn.execute(
        "INSERT OR IGNORE INTO timeline (event_at, record_id, source_id, canonical_table)"
        " VALUES (?, ?, ?, 'conversation_messages')",
        (ts, message_id, SOURCE),
    )


def _add_entity(conn: sqlite3.Connection, entity_id: str, name: str, *, is_self: int = 0) -> None:
    conn.execute(
        "INSERT INTO entities"
        " (entity_id, entity_type, canonical_name, normalized_name, is_self, mention_count)"
        " VALUES (?, 'topic', ?, ?, ?, 5)",
        (entity_id, name, name.lower(), is_self),
    )


def _add_mention(
    conn: sqlite3.Connection,
    mention_id: str,
    record_id: str,
    entity_id: str,
    surface: str,
) -> None:
    conn.execute(
        "INSERT INTO entity_mentions"
        " (mention_id, entity_id, record_id, source_id, canonical_table, surface_text, event_at)"
        " VALUES (?, ?, ?, ?, 'conversation_messages', ?, '2026-03-13T12:00:00Z')",
        (mention_id, entity_id, record_id, SOURCE, surface),
    )


def _add_goal(conn: sqlite3.Connection, goal_id: str, record_id: str, text: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO user_goals"
        " (goal_id, record_id, source_id, goal_text, payload_json, created_at)"
        " VALUES (?, ?, ?, ?, '{}', ?)",
        (goal_id, record_id, SOURCE, text, ts),
    )


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "commitment.db"))
    apply_all_migrations(c)

    for message_id, content, ts in (GOAL_KEPT, GOAL_DROPPED, EVIDENCE_KEPT, EVIDENCE_STALE):
        _seed_message(c, message_id, content, ts)

    _add_entity(c, KEPT_ENTITY, KEPT_TOPIC)
    _add_entity(c, DROPPED_ENTITY, DROPPED_TOPIC)
    _add_mention(c, "m-goal-kept", GOAL_KEPT[0], KEPT_ENTITY, KEPT_TOPIC)
    _add_mention(c, "m-evidence", EVIDENCE_KEPT[0], KEPT_ENTITY, KEPT_TOPIC)
    _add_mention(c, "m-stale", EVIDENCE_STALE[0], KEPT_ENTITY, KEPT_TOPIC)
    _add_mention(c, "m-goal-dropped", GOAL_DROPPED[0], DROPPED_ENTITY, DROPPED_TOPIC)

    _add_goal(c, "goal-kept", GOAL_KEPT[0], "finish the Threnody rewrite", GOAL_KEPT[2])
    _add_goal(c, "goal-dropped", GOAL_DROPPED[0], "call Perambulator back", GOAL_DROPPED[2])
    c.commit()
    yield c
    c.close()


def _manifest():
    """`messages:read` — a real shipped grant that carries `user_goals` AND a message
    store. It is the grant on which this mode is actually reachable today."""
    return resolve_scope_manifest("messages:read")


def _no_store_manifest():
    """The same grant with its evidence store removed.

    `work_context:read` is the scope the goal list belongs to and it names only
    `profile_records` — so on the goal-shaped grant there is no message store to search
    at all. That is a real, shipped configuration, and the mode has to say so rather than
    silently reporting every goal as unfulfilled. Constructed by SUBTRACTION from a real
    manifest so nothing here widens a grant."""
    return dataclasses.replace(_manifest(), canonical_tables=[])


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
            manifest=manifest if manifest is not None else _manifest(),
            access_mode=access_mode,
            query_text=query_text,
            installed_source_ids=list(installed if installed is not None else INSTALLED),
            disclosure_tier=disclosure_tier,
            ledger=ledger,
            now=NOW,
        )
    )


def _report(bundle) -> Dict[str, Any]:
    return bundle.context_packet.get("commitment_report") or {}


def _goal(bundle, goal_id: str) -> Dict[str, Any]:
    for entry in _report(bundle).get("goals") or []:
        if str(entry.get("goal_id")) == goal_id:
            return entry
    return {}


def _evidence_ids(bundle, goal_id: str) -> List[str]:
    return [str(e.get("record_id")) for e in (_goal(bundle, goal_id).get("evidence") or [])]


def _summary_ids(bundle) -> List[str]:
    return [
        str(s.get("record_id"))
        for s in (bundle.context_packet.get("summaries") or [])
        if s.get("record_id")
    ]


def _reasons(ledger: NarrowingLedger) -> List[str]:
    return [str(e.get("reason")) for e in (ledger.as_public().get("ledger") or [])]


# ============================================================ the question it answers


class TestTheCommitmentIsJoinedToItsEvidence:
    def test_each_goal_is_answered_separately(self, conn) -> None:
        """The whole point. Two goals, one kept and one not, and the answer says which is
        which — instead of one blended relevance list the reader has to adjudicate."""
        report = _report(_retrieve(conn))
        assert report, "the commitment question produced no per-goal report at all"
        assert report["goal_count"] == 2
        assert report["goals_with_evidence"] == 1
        assert report["goals_without_evidence"] == 1

    def test_progress_is_claimed_only_with_the_record_it_rests_on(self, conn) -> None:
        """An unsourced progress claim IS the bug. Every `evidence_found` carries the
        record ids the claim is made of, so a reader can check it."""
        bundle = _retrieve(conn)
        kept = _goal(bundle, "goal-kept")
        assert kept["status"] == "evidence_found"
        assert _evidence_ids(bundle, "goal-kept") == [EVIDENCE_KEPT[0]]
        assert kept["evidence_count"] == 1
        assert all(e.get("record_id") for e in kept["evidence"])

    def test_a_goal_with_no_follow_through_says_so_and_cites_nothing(self, conn) -> None:
        dropped = _goal(_retrieve(conn), "goal-dropped")
        assert dropped["status"] == "no_evidence"
        assert dropped["evidence"] == []
        assert dropped["evidence_count"] == 0
        assert dropped["empty_cause"] == "no_match"

    def test_evidence_is_attributed_to_the_goal_whose_ids_reached_it(self, conn) -> None:
        """The join is PER GOAL, not one query over all of them. If it were blended, the
        Threnody evidence would be available to attribute to the Perambulator goal —
        which is the confident-wrong answer wearing a citation."""
        bundle = _retrieve(conn)
        assert EVIDENCE_KEPT[0] in _evidence_ids(bundle, "goal-kept")
        assert EVIDENCE_KEPT[0] not in _evidence_ids(bundle, "goal-dropped")

    def test_every_cited_record_is_in_the_answer_beside_it(self, conn) -> None:
        """The report is a PROJECTION of `summaries`, so a citation can never point at a
        row the reader was not given. This is also the whole privacy argument in one
        line: the report has nothing of its own to leak."""
        bundle = _retrieve(conn)
        summary_ids = set(_summary_ids(bundle))
        for entry in _report(bundle)["goals"]:
            assert str(entry["record_id"]) in summary_ids
            for cited in entry.get("evidence") or []:
                assert str(cited["record_id"]) in summary_ids

    def test_the_report_reaches_synthesis(self, conn) -> None:
        """Without the game-layer carry the mode is unreachable: retrieval would compute
        per-goal evidence that synthesis never sees, and the model would go straight back
        to matching wording against wording."""
        payload = DefaultGameLayer().apply(
            context_packet=_retrieve(conn).context_packet,
            access_mode="summary",
            scope_id="messages:read",
            query_text=QUERY,
        ).payload
        assert payload["commitment_report"]["goals_with_evidence"] == 1


class TestEvidenceFollowsTheCommitment:
    def test_a_row_predating_the_goal_is_not_evidence_it_was_kept(self, conn) -> None:
        """`e-stale` shares the goal's entity AND its wording. Only chronology separates
        them, and without the rule the mode re-creates the exact "sounds related, must be
        progress" error it exists to fix — this time with an id attached, which makes it
        worse rather than better."""
        assert EVIDENCE_STALE[0] not in _evidence_ids(_retrieve(conn), "goal-kept")

    def test_the_goal_is_not_evidence_of_itself(self, conn) -> None:
        """The statement and the follow-through are both messages carrying the same
        entity. Counting the statement would make every goal self-fulfilling."""
        assert GOAL_KEPT[0] not in _evidence_ids(_retrieve(conn), "goal-kept")


class TestNoEvidenceFoundIsFourDifferentSentences:
    """The coverage contract. A goal the engine could not join, a goal whose stores are
    empty, a goal it looked for and missed, and a goal whose evidence a plane removed are
    four remedies. Collapsing them into one "no evidence" label is the failure."""

    def test_a_grant_with_no_evidence_store_says_not_queried_not_unfulfilled(
        self, conn
    ) -> None:
        """The `work_context:read` shape. Reporting these goals as unfulfilled would be
        the engine telling the owner they did nothing, when it never looked."""
        ledger = NarrowingLedger()
        bundle = _retrieve(conn, ledger=ledger, manifest=_no_store_manifest())
        entry = _goal(bundle, "goal-kept")
        assert entry["status"] == "no_evidence"
        assert entry["empty_cause"] == "not_queried"
        assert entry["empty_reason"] == "commitment_scope_no_evidence_store"
        assert "commitment_scope_no_evidence_store" in _reasons(ledger)

    def test_a_goal_with_no_entity_link_is_reported_not_guessed_at(self, conn) -> None:
        """No ids means no join. The tempting fallback — match the goal's WORDS against
        the corpus — is precisely the mechanism that produces the confident wrong answer,
        so the lane refuses and says which goal it could not reach."""
        conn.execute("DELETE FROM entity_mentions WHERE record_id = ?", (GOAL_DROPPED[0],))
        conn.commit()
        ledger = NarrowingLedger()
        entry = _goal(_retrieve(conn, ledger=ledger), "goal-dropped")
        assert entry["empty_cause"] == "not_queried"
        assert entry["empty_reason"] == "commitment_goal_unresolved"
        assert "commitment_goal_unresolved" in _reasons(ledger)

    def test_an_undated_goal_gets_no_lane_rather_than_an_unordered_one(self, conn) -> None:
        """Without a statement time there is no "after", so there is no evidence test —
        only a similarity test wearing its clothes.

        Asked without the window on purpose: an undated goal cannot survive "last week"
        either, and a goal the time filter already removed is one this report must not
        speak about at all (next test). Both refusals are correct; this one is about the
        second, so the window is taken out of the way."""
        conn.execute("DELETE FROM timeline WHERE record_id = ?", (GOAL_DROPPED[0],))
        conn.execute(
            "UPDATE user_goals SET created_at = '' WHERE goal_id = 'goal-dropped'"
        )
        conn.commit()
        entry = _goal(
            _retrieve(conn, "what did I say I'd do and did I actually do it"),
            "goal-dropped",
        )
        assert entry["empty_cause"] == "not_queried"
        assert entry["empty_reason"] == "commitment_goal_undated"

    def test_a_goal_the_time_window_removed_is_not_reported_on_at_all(self, conn) -> None:
        """The window is a plane like any other. A goal it took out of the answer cannot
        reappear in the report carrying a verdict — that would be the report reaching
        around the filter that removed it, and "you did not do this" is the worst possible
        thing to say about a goal nobody looked at."""
        conn.execute("DELETE FROM timeline WHERE record_id = ?", (GOAL_DROPPED[0],))
        conn.execute(
            "UPDATE user_goals SET created_at = '' WHERE goal_id = 'goal-dropped'"
        )
        conn.commit()
        bundle = _retrieve(conn)
        assert GOAL_DROPPED[0] not in _summary_ids(bundle)
        assert _goal(bundle, "goal-dropped") == {}

    def test_looked_and_missed_is_a_different_cause_from_never_looked(self, conn) -> None:
        """`goal-dropped` IS joined — it has ids and a date, and the scan ran. Its silence
        means something different from `goal-kept`'s silence under a grant that named no
        store, and the two must not print the same word."""
        searched = _goal(_retrieve(conn), "goal-dropped")
        unsearched = _goal(_retrieve(conn, manifest=_no_store_manifest()), "goal-dropped")
        assert searched["empty_cause"] != unsearched["empty_cause"]
        assert searched["empty_reason"] != unsearched["empty_reason"]


# ================================================== the planes, severed one at a time


class TestTheReportInheritsEveryPlaneAndOwnsNone:
    def test_an_exclusion_takes_the_citation_with_it(self, conn) -> None:
        """Set 6. "…but nothing about Threnody" removes the evidence row from the answer,
        so the report cannot still be citing it — and it must not silently downgrade to
        "you did nothing", which would tell the owner their week was empty when their own
        exclusion emptied it."""
        ledger = NarrowingLedger()
        bundle = _retrieve(conn, f"{QUERY}, but nothing about {KEPT_TOPIC}", ledger=ledger)
        entry = _goal(bundle, "goal-kept")
        if not entry:
            # The exclusion removed the goal row itself; then there is nothing to cite
            # and nothing to say, which is also correct.
            assert EVIDENCE_KEPT[0] not in _summary_ids(bundle)
            return
        assert EVIDENCE_KEPT[0] not in [
            c["record_id"] for c in (entry.get("evidence") or [])
        ]
        assert entry["status"] == "no_evidence"

    def test_an_unrelated_exclusion_leaves_the_citation_alone(self, conn) -> None:
        """The control. Without it the test above passes on any bug that empties the
        report — the exclusion has to be what did it."""
        bundle = _retrieve(conn, f"{QUERY}, but nothing about {DROPPED_TOPIC}")
        assert EVIDENCE_KEPT[0] in _evidence_ids(bundle, "goal-kept")

    def test_a_black_holed_subject_is_not_cited_to_a_grantee(self, conn) -> None:
        """The entity lane has already leaked EXISTENCE once, by handing a grantee the
        entity_id, record_id, table and timestamp of a black-holed subject. A per-goal
        report is a denser version of exactly that shape, so it runs the wire-A filter at
        SOURCE: the row is never read, and the goal falls through to the same ordinary
        "nothing matched" a goal with genuinely nothing gets. Indistinguishable is the
        requirement, not a side effect."""
        from topos.features.lifecycle.blackhole import BlackholeStore

        store = BlackholeStore(conn)
        store.blackhole_entity(entity_ref=KEPT_ENTITY)
        store.mark_rebuild_complete(KEPT_ENTITY)
        conn.commit()

        bundle = _retrieve(conn, disclosure_tier="shared_summary")
        assert EVIDENCE_KEPT[0] not in _evidence_ids(bundle, "goal-kept")

    def test_the_black_hole_is_what_did_that(self, conn) -> None:
        """The control for the test above."""
        from topos.features.lifecycle.blackhole import BlackholeStore

        store = BlackholeStore(conn)
        store.blackhole_entity(entity_ref=KEPT_ENTITY)
        store.mark_rebuild_complete(KEPT_ENTITY)
        store.unblackhole_entity(entity_ref=KEPT_ENTITY)
        conn.commit()

        bundle = _retrieve(conn, disclosure_tier="shared_summary")
        assert EVIDENCE_KEPT[0] in _evidence_ids(bundle, "goal-kept")

    def test_a_table_the_grant_does_not_name_is_never_reached(self, conn) -> None:
        """The manifest ceiling. A join may not become a way to reach a store the grant
        never authorized, and a grant that names no store is an EMPTY result rather than a
        crash — a declared-but-uncreated table is `store_empty`, not a fault."""
        bundle = _retrieve(conn, manifest=_no_store_manifest())
        assert _report(bundle)["stores"] == []
        for entry in _report(bundle)["goals"]:
            assert entry["evidence"] == []

    def test_the_report_never_fetches_a_record_by_id(self, conn) -> None:
        """`CanonicalStore.get()` takes no disclosure tier, so a single call to it is a
        tier bypass. The lane must reach rows only through the disclosed `list()` path."""
        adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=conn))
        canonical = type(adapter._adapters.canonical)
        calls: List[Any] = []
        original = canonical.get

        def _spy(self, *args, **kwargs):  # noqa: ANN001
            calls.append(args)
            return original(self, *args, **kwargs)

        canonical.get = _spy
        try:
            adapter.retrieve(
                RetrievalRequest(
                    manifest=_manifest(),
                    access_mode="summary",
                    query_text=QUERY,
                    installed_source_ids=list(INSTALLED),
                    disclosure_tier="owner_raw",
                    now=NOW,
                )
            )
        finally:
            canonical.get = original
        assert calls == [], f"the lane bypassed the disclosure filter: {calls}"

    def test_severing_the_lane_severs_the_report(self, monkeypatch, conn) -> None:
        """The report has no independent source of rows. With the lane gone it does not
        fall back to anything — it simply is not there, which is the proof that every
        plane the lane runs is a plane the report runs."""
        import topos.query.retrieval as _r

        monkeypatch.setattr(_r, "_load_commitment_evidence_items", lambda *a, **k: [])
        assert _report(_retrieve(conn)) == {}


class TestTheLedgerDoesNotAnnounceWhatWasWithheld:
    def test_the_receipts_are_closed_set_slugs_with_no_owner_text(self, conn) -> None:
        """A receipt naming a withheld thing converts hiding-by-absence into
        hiding-by-denial. The public ledger carries counts and enum slugs; the subject's
        name and the goal's words are never in it."""
        ledger = NarrowingLedger()
        _retrieve(conn, ledger=ledger)
        blob = str(ledger.as_public())
        assert KEPT_TOPIC.lower() not in blob.lower()
        assert DROPPED_TOPIC.lower() not in blob.lower()
        assert "rewrite" not in blob.lower()
        assert "commitment_evidence_lane" in _reasons(ledger)

    def test_telemetry_carries_no_text_either(self, conn) -> None:
        ledger = NarrowingLedger()
        _retrieve(conn, ledger=ledger)
        blob = str(ledger.as_telemetry())
        assert KEPT_TOPIC.lower() not in blob.lower()
        assert GOAL_KEPT[0] not in blob


class TestTheGranteePathIsTheSameFeature:
    """A mode that works for the owner and not the grantee is unfinished."""

    def test_a_grantee_gets_per_goal_answers_too(self, conn) -> None:
        bundle = _retrieve(conn, disclosure_tier="shared_summary")
        assert _report(bundle).get("goal_count")
        assert _evidence_ids(bundle, "goal-kept") == [EVIDENCE_KEPT[0]]

    def test_a_grantee_is_counted_entities_never_named_ones(self, conn) -> None:
        """Records and timestamps are safe here because they are already in `summaries`.
        Entity ids are not: nothing else in the packet publishes them, so handing them
        over would be the report disclosing something the answer does not. Named for the
        owner, counted for everyone else."""
        owner = _goal(_retrieve(conn), "goal-kept")
        grantee = _goal(_retrieve(conn, disclosure_tier="shared_summary"), "goal-kept")
        assert owner["entity_ids"] == [KEPT_ENTITY]
        assert "entity_ids" not in grantee
        assert grantee["entity_count"] == 1
        for cited in grantee.get("evidence") or []:
            assert "entity_id" not in cited


class TestTheModeDoesNotFireOnEverythingElse:
    def test_an_ordinary_goal_browse_is_untouched(self, conn) -> None:
        """The lane contributes items to fusion, so every request it fires on that was not
        this question is a request whose ranking moved for no reason. "What am I working
        on" already routes to the goal lane; it must take the path it always took."""
        bundle = _retrieve(conn, "what am I working on")
        assert bundle.context_packet.get("commitment_report") is None

    def test_stating_a_commitment_now_is_not_a_progress_question(self, conn) -> None:
        assert _retrieve(conn, "remind me I said I'd finish the rewrite").context_packet.get(
            "commitment_report"
        ) is None

    def test_a_bare_follow_through_word_does_not_arm_the_lane(self, conn) -> None:
        assert _retrieve(conn, "did I finish anything").context_packet.get(
            "commitment_report"
        ) is None

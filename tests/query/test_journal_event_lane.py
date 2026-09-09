"""Lived-event journal lane — the diary answers "who was I with", inside its ceiling.

protects: an ask-gated read of the journal head under ``health:read``, so a
lived-social question reaches a diary row that blind recency cannot — the
recent lane is bounded by source and a 14-day window, so an older row, or one
that loses the top-10 to chat, is otherwise unreachable.

protects (privacy): the lane carries TWO gates, and an earlier draft had only
the first. Below ``owner_raw`` it returns nothing and writes no ledger receipt
(a receipt for a withheld lane is an existence signal). And it reads nothing
unless ``journal_entries`` is already in the manifest's ``canonical_tables`` —
which resolves to ``health:read`` alone. Without that second gate the lane took
no manifest at all and would have delivered raw diary prose under every scope,
including grants whose ceiling is ``summary``. The scope tests below are that
pin: they assert ABSENCE through the wired path, so they fail if the gate is
ever dropped.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.query.journal_event_lane import (
    _match_needles,
    JOURNAL_EVENT_LANE_MAX_ITEMS,
    journal_event_items,
    lived_social_event_ask,
)
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.narrowing import NarrowingLedger
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "journal_lane.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _seed_journals(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO journal_entries
           (entry_id, entry_at, mood_tag, category, content, people,
            place_name, source_id, ingested_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'grow_journal', datetime('now'))""",
        (
            "tl-movie",
            "2026-09-04T02:20:00Z",
            None,
            "Fun",
            "Saw the A24 movie with friends after dinner.",
            "Ada, Bo, Maya",
            "Westgate Cinema",
        ),
    )
    conn.execute(
        """INSERT INTO journal_entries
           (entry_id, entry_at, mood_tag, category, content, people,
            place_name, source_id, ingested_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'grow_journal', datetime('now'))""",
        (
            "tl-tailgate",
            "2026-09-05T17:58:00Z",
            None,
            "Fun",
            "Campus tailgate before the game.",
            "Ada, Bo",
            "The Beer Garden",
        ),
    )
    conn.execute(
        """INSERT INTO journal_entries
           (entry_id, entry_at, mood_tag, category, content, people,
            place_name, source_id, ingested_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'grow_journal', datetime('now'))""",
        (
            "tl-unrelated",
            "2026-08-01T12:00:00Z",
            None,
            "Work",
            "Wrote notes about the compiler rewrite.",
            None,
            None,
        ),
    )
    conn.commit()


def _retrieve(conn, *, query: str, tier: str = "owner_raw",
              scope: str = "health:read", ledger: NarrowingLedger | None = None):
    bundle = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(bundle)
    manifest = resolve_scope_manifest(scope)
    return adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text=query,
            disclosure_tier=tier,
            ledger=ledger,
        )
    )


def _journal_items(bundle) -> list:
    summaries = (bundle.context_packet or {}).get("summaries") or []
    return [
        i
        for i in summaries
        if str(i.get("retrieval_source") or "") == "journal_event_lane"
    ]


class TestLivedSocialGate:
    def test_movie_and_activities_with_match(self) -> None:
        assert lived_social_event_ask(
            "When did I recently go see a movie and who was I with?"
        )
        assert lived_social_event_ask(
            "Check what activities I've had with Ada and Bo lately"
        )
        assert not lived_social_event_ask("what have I been working on lately")


class TestJournalEventLaneIntegration:
    def test_lived_event_ask_reaches_the_diary_on_its_own_scope(self, conn) -> None:
        _seed_journals(conn)
        ledger = NarrowingLedger()
        bundle = _retrieve(
            conn,
            query="Check what activities I've had with Ada and Bo lately",
            scope="health:read",
            ledger=ledger,
        )
        items = _journal_items(bundle)
        assert items, "journal lane contributed nothing on its own scope"
        blob = " ".join(str(i.get("summary_text") or "") for i in items)
        assert "Cinema" in blob or "tailgate" in blob or "A24" in blob
        assert any(
            e.get("reason") == "journal_event_lane" for e in ledger.as_public()["ledger"]
        )

    def test_movie_ask_finds_theater_row(self, conn) -> None:
        _seed_journals(conn)
        bundle = _retrieve(
            conn,
            query="When did I recently go see a movie?",
            scope="health:read",
        )
        blob = " ".join(
            str(i.get("summary_text") or "") for i in _journal_items(bundle)
        )
        assert "movie" in blob.lower() or "Cinema" in blob

    @pytest.mark.parametrize("scope", ["activity:read", "work_context:read"])
    def test_a_scope_that_does_not_name_journals_reads_none(self, conn, scope) -> None:
        """The ceiling pin. These are the exact scopes the fan-out actually sends.

        `work_context:read`'s ceiling is `summary`; the lane emits the entry's
        own prose, so contributing here would disclose raw diary text under a
        grant that may not carry it. An earlier draft did exactly that.
        """
        _seed_journals(conn)
        ledger = NarrowingLedger()
        bundle = _retrieve(
            conn,
            query="Check what activities I've had with Ada and Bo lately",
            scope=scope,
            ledger=ledger,
        )
        assert not _journal_items(bundle)
        assert "journal" not in (bundle.stores_touched or [])
        assert not any(
            e.get("reason") == "journal_event_lane" for e in ledger.as_public()["ledger"]
        )

    def test_lane_is_owner_only_and_silent_below_owner_raw(self, conn) -> None:
        _seed_journals(conn)
        ledger = NarrowingLedger()
        bundle = _retrieve(
            conn,
            query="Check what activities I've had with Kalyn and Cameron lately",
            scope="health:read",
            tier="default_disclosure",
            ledger=ledger,
        )
        assert not _journal_items(bundle)
        assert "journal" not in (bundle.stores_touched or [])
        assert not any(
            e.get("reason") == "journal_event_lane" for e in ledger.as_public()["ledger"]
        )

    def test_unrelated_work_ask_does_not_open_the_diary(self, conn) -> None:
        """Even on health:read, an unrelated ask must not open the diary."""
        _seed_journals(conn)
        bundle = _retrieve(
            conn,
            query="what have I been working on lately",
            scope="health:read",
        )
        assert not _journal_items(bundle)


class TestNeedlesAreEvidenceNotGrammar:
    """A function word must never be the reason a diary row is returned.

    Found in pre-merge review. `_MATCH_STOP` did not stop "was", and the gate's
    own first alternative — `\\bwho (?:was|were) i with\\b` — guarantees "was"
    is in every query that opens this lane. It became a content needle, and the
    row test was a bare `any(n in blob for n in needles)` substring match, so
    "Who was I with at dinner last weekend?" returned FIVE unrelated raw rows
    (therapy, a biopsy result, a layoff, rent trouble, a divorce) and dropped
    the one row naming the people. Not a ceiling breach — it is the owner's own
    data at owner_raw — but the packet went to synthesis, which may be a hosted
    provider, and the ask had requested none of it.

    The lane is reachable for exactly these asks today: the front end's
    `health_wellbeing` rule (priority 30) routes on `dinner|lunch|felt|sleep|
    mood|therapy|journal|...` straight to `health:read`, with no fan-out
    involved.
    """

    SENSITIVE = (
        ("s-therapy", "Therapy session; the divorce was the whole hour.", "Health"),
        ("s-a1c", "A1C was up again; starting metformin.", "Health"),
        ("s-layoff", "The layoff conversation with my manager was brutal.", "Work"),
        ("s-rent", "Rent was late again, borrowed from mom.", "Money"),
        ("s-biopsy", "The biopsy result was benign. Relief.", "Health"),
    )

    def _seed_sensitive(self, conn) -> None:
        for i, (eid, content, cat) in enumerate(self.SENSITIVE):
            conn.execute(
                """INSERT INTO journal_entries
                   (entry_id, entry_at, category, content, source_id, ingested_at)
                   VALUES (?, ?, ?, ?, 'grow_journal', datetime('now'))""",
                (eid, "2026-09-0{}T00:00:00Z".format(i + 1), cat, content),
            )
        conn.commit()

    @pytest.mark.parametrize(
        "ask",
        [
            "Who was I with at dinner last weekend?",
            "who was I with?",
            "Who were I with there last night?",
        ],
    )
    def test_a_grammar_only_ask_returns_no_diary_prose(self, conn, ask) -> None:
        _seed_journals(conn)
        self._seed_sensitive(conn)
        items = journal_event_items(
            conn,
            query_text=ask,
            scope_id="health:read",
            manifest=resolve_scope_manifest("health:read"),
            disclosure_tier="owner_raw",
        )
        blob = " ".join(str(i.get("summary_text") or "") for i in items)
        for marker in ("Therapy", "A1C", "layoff", "Rent", "biopsy"):
            assert marker not in blob, (
                "{!r} pulled an unrelated diary row on {!r}".format(marker, ask)
            )

    def test_needles_never_include_the_gates_own_function_words(self) -> None:
        for ask in (
            "Who was I with at dinner last weekend?",
            "who was I with?",
            "When did I recently go see a movie and who was I with?",
        ):
            needles = _match_needles(ask)
            for junk in ("was", "were", "there", "last", "weekend", "night"):
                assert junk not in needles, "{!r} survived into needles for {!r}".format(
                    junk, ask
                )

    def test_a_named_person_still_reaches_the_row(self, conn) -> None:
        """The fix must not silence the lane on the ask it exists for."""
        _seed_journals(conn)
        self._seed_sensitive(conn)
        items = journal_event_items(
            conn,
            query_text="Check what activities I've had with Ada and Bo lately",
            scope_id="health:read",
            manifest=resolve_scope_manifest("health:read"),
            disclosure_tier="owner_raw",
        )
        blob = " ".join(str(i.get("summary_text") or "") for i in items)
        assert "Ada" in blob
        assert "biopsy" not in blob and "Rent" not in blob

    def test_a_needle_does_not_match_inside_a_longer_word(self, conn) -> None:
        """`ada` must not match `adamant`. The needle is a word, not a substring.

        Driven by a row that ONLY collides as a substring, so it fails against
        the bare `n in blob` this replaced — a row that merely fails to collide
        would pass either way and pin nothing.
        """
        conn.execute(
            """INSERT INTO journal_entries
               (entry_id, entry_at, category, content, source_id, ingested_at)
               VALUES ('s-adamant', '2026-09-02T00:00:00Z', 'Work',
                       'Adamant about the deadline; the review was tense.',
                       'grow_journal', datetime('now'))"""
        )
        conn.execute(
            """INSERT INTO journal_entries
               (entry_id, entry_at, category, content, people, source_id, ingested_at)
               VALUES ('s-ada', '2026-09-03T00:00:00Z', 'Fun', 'Dinner out.',
                       'Ada', 'grow_journal', datetime('now'))"""
        )
        conn.commit()
        items = journal_event_items(
            conn,
            query_text="Check what activities I've had with Ada lately",
            scope_id="health:read",
            manifest=resolve_scope_manifest("health:read"),
            disclosure_tier="owner_raw",
        )
        blob = " ".join(str(i.get("summary_text") or "") for i in items)
        assert "Dinner out." in blob, "the row naming Ada should still be returned"
        assert "Adamant" not in blob, "`ada` matched inside `Adamant`"


class TestJournalEventLaneUnit:
    """Direct calls, so the gates are pinned independently of the wiring."""

    def test_cap_and_receipt(self, conn) -> None:
        for n in range(20):
            conn.execute(
                """INSERT INTO journal_entries
                   (entry_id, entry_at, category, content, people, source_id, ingested_at)
                   VALUES (?, '2026-09-01T00:00:00Z', 'Fun', ?, 'Ada',
                           'grow_journal', datetime('now'))""",
                (f"tl-x{n}", f"Hung out after movie {n}"),
            )
        conn.commit()
        ledger = NarrowingLedger()
        items = journal_event_items(
            conn,
            query_text="Check what activities I've had with Ada lately",
            scope_id="health:read",
            manifest=resolve_scope_manifest("health:read"),
            disclosure_tier="owner_raw",
            ledger=ledger,
        )
        assert 0 < len(items) <= JOURNAL_EVENT_LANE_MAX_ITEMS
        receipts = [
            e
            for e in ledger.as_public()["ledger"]
            if e.get("reason") == "journal_event_lane"
        ]
        assert receipts and receipts[0].get("action") == "contributed"

    def test_the_receipt_reason_is_a_registered_term(self, conn) -> None:
        """`NarrowingLedger.record` narrows an unknown reason to "unrecognized".

        The lane's receipt is its only audit trail, and it is written inside a
        bare `except: pass`, so an unregistered term fails silently — the read
        still happens, the record of it does not. This is the guard for that.
        """
        _seed_journals(conn)
        ledger = NarrowingLedger()
        journal_event_items(
            conn,
            query_text="Check what activities I've had with Ada lately",
            scope_id="health:read",
            manifest=resolve_scope_manifest("health:read"),
            disclosure_tier="owner_raw",
            ledger=ledger,
        )
        reasons = [e.get("reason") for e in ledger.as_public()["ledger"]]
        assert "journal_event_lane" in reasons
        assert "unrecognized" not in reasons

    def test_tier_gate_returns_nothing_and_writes_nothing(self, conn) -> None:
        _seed_journals(conn)
        ledger = NarrowingLedger()
        items = journal_event_items(
            conn,
            query_text="Check what activities I've had with Ada lately",
            scope_id="health:read",
            manifest=resolve_scope_manifest("health:read"),
            disclosure_tier="default_disclosure",
            ledger=ledger,
        )
        assert items == []
        assert not ledger.as_public()["ledger"]

    @pytest.mark.parametrize(
        "scope", ["activity:read", "work_context:read", "relationship_context:read"]
    )
    def test_scope_gate_returns_nothing_and_writes_nothing(self, conn, scope) -> None:
        """No named-scope exception exists, deliberately — not even for the
        relationship scope, whose card advertises people but not diaries."""
        _seed_journals(conn)
        ledger = NarrowingLedger()
        items = journal_event_items(
            conn,
            query_text="Check what activities I've had with Ada lately",
            scope_id=scope,
            manifest=resolve_scope_manifest(scope),
            disclosure_tier="owner_raw",
            ledger=ledger,
        )
        assert items == []
        assert not ledger.as_public()["ledger"]

    def test_only_health_read_resolves_journal_entries(self) -> None:
        """The gate is only as narrow as the registry makes it — pin that too,
        so a scope quietly gaining `journal_entries` cannot widen this lane
        without failing here first."""
        import json  # noqa: PLC0415
        from pathlib import Path as _P  # noqa: PLC0415

        registry = json.loads(
            (_P(__file__).resolve().parents[2] / "topos" / "query" / "scope_registry.json")
            .read_text(encoding="utf-8")
        )
        allowed = []
        for entry in registry["scopes"]:
            sid = entry.get("scope_id") or entry.get("id")
            manifest = resolve_scope_manifest(sid)
            if "journal_entries" in list(
                getattr(manifest, "canonical_tables", None) or []
            ):
                allowed.append(sid)
        assert allowed == ["health:read"], allowed

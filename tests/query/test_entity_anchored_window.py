"""Q3 — the window the sentence never names.

*"What did I miss while I was heads-down on the classifier?"* is unanswerable today,
and not because retrieval is weak. Every window this pipeline can build is parsed out
of the WORDS: an explicit range, a relative phrase, a differenced pair. That sentence
contains no dates, so the planner returns no window, so the question is answered over
whatever the ordinary scope route happens to return — which is a different question.

The period is real; it is just not in the sentence. It is in the owner's own activity,
and only the node can see it. So the anchor is resolved the other way round: the
sentence names a SUBJECT, and the subject's own mention density names the PERIOD.

Four things are pinned here, in order of how much damage getting them wrong does:

1. **The refusal.** An entity with three scattered mentions has no heads-down period.
   Inventing one produces a confident date range with nothing behind it, which is worse
   than the unanswerable question it replaced — the owner cannot tell the difference
   from the outside. Sparse nodes will hit this constantly, so it is tested first and
   in every shape the method can refuse in.
2. **The dates are bounded by the grant, before the arithmetic.** "You were busy with X
   between the 4th and the 11th" is a claim about the owner's life, derived from
   records, and it LEAVES THE NODE on `packet["time_window"]`. A window shaped by rows
   the caller may not read is a leak whether or not a single row of that content
   reaches the packet. Each plane is severed below and watched to move the dates.
3. **The derivation is visible.** A window the owner cannot see is one they cannot
   dispute, and a wrong one would silently shape every lane in the turn.
4. **It composes rather than competes.** A stated window always wins; the triage is the
   existing triage, run over the derived days rather than recomputed.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any, Dict, List, Optional

import pytest

from topos.query import entity_window as W
from topos.query import narrowing as _N
from topos.query import retrieval as R
from topos.query.entity_window import derive_window_from_days, entity_anchor_intent
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.narrowing import NarrowingLedger
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations

SOURCE_ID = "imessage"
INSTALLED = [SOURCE_ID]

ENTITY_ID = "ent-kepler"
ENTITY_NAME = "Kepler"

#: Pinned so "last week" means the same week next year. Every date below is fixed
#: relative to it; the density arithmetic itself reads no clock at all.
NOW = "2026-08-19T12:00:00+00:00"

#: The question the mode exists for. Both halves are present — the frame ("while I was
#: heads-down on …") and the cost ("what did I miss") — because either alone is a
#: question the existing lanes already answer correctly.
QUERY = "What did I miss while I was heads-down on Kepler?"

#: The shape a heads-down stretch has: a quiet lead-in, a burst, a quiet tail. The
#: burst is Aug 4-6.
HEADS_DOWN = {
    "2026-08-01": 1,
    "2026-08-02": 1,
    "2026-08-04": 4,
    "2026-08-05": 5,
    "2026-08-06": 4,
    "2026-08-20": 1,
    "2026-08-25": 1,
}


# --------------------------------------------------------------------------- seeding


def _seed(
    conn: sqlite3.Connection,
    day_counts: Dict[str, int],
    *,
    entity_id: str = ENTITY_ID,
    canonical_table: str = "conversation_messages",
    source_id: str = SOURCE_ID,
    prefix: str = "msg",
) -> List[str]:
    """One message and one mention per unit of density. Returns the record ids.

    The message TEXT never names the entity, so nothing here can be reached by the
    keyword path — the mention join is the only route, which is what makes the density
    a property of the entity rather than of the owner's phrasing.
    """
    mgr = ConversationsTablesManager(conn)
    record_ids: List[str] = []
    n = 0
    for day in sorted(day_counts):
        for _ in range(day_counts[day]):
            n += 1
            rid = f"{prefix}-{entity_id}-{n}"
            mgr.upsert_message_batch(
                [
                    {
                        "message_id": rid,
                        "thread_id": "thread-1",
                        "content": f"working note {n}",
                        "ts": f"{day}T12:00:00Z",
                        "sender_type": "contact",
                    }
                ],
                dataset_id="user:default:device",
                source_id=source_id,
                sync_batch_id=f"batch-{rid}",
            )
            conn.execute(
                """
                INSERT INTO entity_mentions
                    (mention_id, entity_id, record_id, source_id, canonical_table,
                     surface_text, event_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"m-{rid}",
                    entity_id,
                    rid,
                    source_id,
                    canonical_table,
                    ENTITY_NAME,
                    f"{day}T12:00:00Z",
                ),
            )
            record_ids.append(rid)
    conn.commit()
    return record_ids


def _add_entity(
    conn: sqlite3.Connection,
    entity_id: str,
    name: str,
    *,
    entity_type: str = "project",
    is_self: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO entities
            (entity_id, entity_type, canonical_name, normalized_name, is_self, mention_count)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (entity_id, entity_type, name, name.lower(), is_self, 8),
    )
    conn.commit()


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "entity_window.db"))
    apply_all_migrations(c)
    _add_entity(c, ENTITY_ID, ENTITY_NAME)
    _seed(c, HEADS_DOWN)
    yield c
    c.close()


def _retrieve(
    conn: sqlite3.Connection,
    query_text: str = QUERY,
    *,
    ledger: Optional[NarrowingLedger] = None,
    disclosure_tier: str = "owner_raw",
    scope: str = "messages:read",
    manifest=None,
    now: str = NOW,
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
            now=now,
        )
    )


def _window(bundle) -> Dict[str, Any]:
    return dict(bundle.context_packet.get("time_window") or {})


# ============================================================ 1. refusing to guess


class TestRefusingToGuessIsTheFeature:
    """Every way the data can fail to contain a period, and the slug for each.

    A method that always returns a window is not a method, it is a formatter. These
    are the cases where the honest output is "no window could be derived", and each
    one is a different sentence the owner deserves to be told.
    """

    def test_three_scattered_mentions_are_not_a_heads_down_period(self) -> None:
        """The case the brief names, and the common case on a sparse node."""
        window = derive_window_from_days(
            {"2026-06-02": 1, "2026-07-14": 1, "2026-08-09": 1}
        )
        assert not window.resolved
        assert window.refusal == W.REFUSAL_TOO_THIN
        assert window.start is None and window.end is None
        assert window.as_packet()["empty_reason"] == W.REFUSAL_TOO_THIN

    def test_an_entity_with_no_mentions_at_all(self) -> None:
        window = derive_window_from_days({})
        assert window.refusal == W.REFUSAL_NO_MENTIONS

    def test_enough_mentions_but_all_on_one_day_is_an_event_not_a_period(self) -> None:
        window = derive_window_from_days({"2026-08-04": 9})
        assert window.refusal == W.REFUSAL_TOO_THIN
        assert window.mentions_total == 9  # not a shortage of evidence — a shortage of days

    def test_a_steady_habit_is_not_a_stretch(self) -> None:
        """Touched every other day for two months. Real, dense, and not a period."""
        days = {f"2026-06-{d:02d}": 2 for d in range(1, 30, 2)}
        days.update({f"2026-07-{d:02d}": 2 for d in range(1, 30, 2)})
        window = derive_window_from_days(days)
        assert window.refusal == W.REFUSAL_UNIFORM

    def test_a_run_that_swallows_the_whole_span_narrows_nothing(self) -> None:
        """No contrast exists, so no lift can be computed and none may be implied.

        This is the case that a rate-lift test alone silently passes: with nothing
        outside the run there is no denominator, and the entity's entire history comes
        back wearing the word "window".
        """
        days = {f"2026-07-{d:02d}": 3 for d in range(1, 26)}
        window = derive_window_from_days(days)
        assert window.refusal == W.REFUSAL_UNIFORM
        assert window.window_days == window.span_days

    def test_a_compact_whole_span_is_still_an_honest_period(self) -> None:
        """A subject that existed for nine days and then stopped IS those nine days."""
        days = {f"2026-07-{d:02d}": 3 for d in range(1, 10)}
        window = derive_window_from_days(days)
        assert window.resolved
        assert (window.start, window.end) == ("2026-07-01", "2026-07-09")

    def test_a_forty_day_stretch_is_a_guess_with_dates_stapled_to_it(self) -> None:
        """Dense, contiguous, and enormously above the outside rate — and still not
        a heads-down period, because nobody means forty days by it."""
        from datetime import date, timedelta

        start = date(2026, 6, 1)
        days = {(start + timedelta(days=i)).isoformat(): 4 for i in range(40)}
        days.update({"2026-01-05": 1, "2026-02-06": 1})
        window = derive_window_from_days(days)
        assert window.refusal == W.REFUSAL_TOO_BROAD
        assert window.window_days > W.MAX_WINDOW_DAYS
        assert window.rate_lift and window.rate_lift > W.MIN_RATE_LIFT  # it passed every
        assert window.start is None                                     # other floor

    def test_every_refusal_is_a_declared_slug(self) -> None:
        """The refusal reaches the ledger, so it has to be in the closed set."""
        assert W.REFUSALS <= _N.REASONS
        assert W.REASON_DERIVED in _N.REASONS

    def test_a_refusal_carries_no_dates(self) -> None:
        window = derive_window_from_days({"2026-06-02": 1, "2026-07-14": 1, "2026-08-09": 1})
        blob = json.dumps(window.as_packet())
        assert "2026-06" not in blob and "2026-07" not in blob and "2026-08" not in blob

    def test_the_refusal_reaches_the_caller_end_to_end(self, tmp_path) -> None:
        """A refusal nobody is told about is indistinguishable from a plain search."""
        c = sqlite3.connect(str(tmp_path / "sparse.db"))
        apply_all_migrations(c)
        _add_entity(c, ENTITY_ID, ENTITY_NAME)
        _seed(c, {"2026-06-02": 1, "2026-07-14": 1, "2026-08-09": 1})
        led = NarrowingLedger()
        bundle = _retrieve(c, ledger=led)
        assert _window(bundle)["empty_reason"] == W.REFUSAL_TOO_THIN
        assert "from" not in _window(bundle)
        assert {"stage": "planner", "action": "not_applied", "reason": W.REFUSAL_TOO_THIN} in (
            led.as_public()["ledger"]
        )
        c.close()


# ======================================================== 2. the method, and the maths


class TestTheMethodIsTheOneItSaysItIs:
    def test_the_peak_span_is_found(self) -> None:
        window = derive_window_from_days(HEADS_DOWN)
        assert (window.start, window.end) == ("2026-08-01", "2026-08-06")
        assert window.method == W.METHOD_PEAK_SPAN
        assert window.mentions_in_window == 15
        assert window.rate_lift and window.rate_lift > W.MIN_RATE_LIFT

    def test_a_weekend_does_not_split_a_stretch(self) -> None:
        """`GAP_DAYS` is why: a heads-down week that pauses for two days is one week."""
        days = {"2026-08-03": 4, "2026-08-04": 4, "2026-08-07": 4, "2026-08-08": 4}
        days.update({"2026-06-01": 1, "2026-06-15": 1, "2026-07-01": 1})
        window = derive_window_from_days(days)
        assert (window.start, window.end) == ("2026-08-03", "2026-08-08")

    def test_the_denser_run_wins_over_the_longer_one(self) -> None:
        days = {f"2026-05-{d:02d}": 2 for d in range(1, 8)}          # 14 over 7 days
        days.update({"2026-07-01": 9, "2026-07-02": 9})              # 18 over 2
        days.update({"2026-06-10": 1, "2026-06-20": 1})
        window = derive_window_from_days(days)
        assert (window.start, window.end) == ("2026-07-01", "2026-07-02")

    def test_a_tie_goes_to_the_more_recent_run(self) -> None:
        """"While I was heads-down" is a recent-past frame, not an all-time one."""
        days = {"2026-03-01": 5, "2026-03-02": 5, "2026-08-01": 5, "2026-08-02": 5}
        days.update({"2026-05-05": 1})
        window = derive_window_from_days(days)
        assert window.start == "2026-08-01"

    def test_the_window_is_inclusive_of_both_end_days(self) -> None:
        window = derive_window_from_days(HEADS_DOWN)
        start, end = window.time_range()
        assert start == "2026-08-01T00:00:00+00:00"
        assert end == "2026-08-06T23:59:59+00:00"

    def test_an_unreadable_date_is_dropped_not_guessed_at(self) -> None:
        clean = derive_window_from_days(HEADS_DOWN)
        dirty = derive_window_from_days({**HEADS_DOWN, "not-a-date": 40, "": 9})
        assert (dirty.start, dirty.end) == (clean.start, clean.end)
        assert dirty.mentions_total == clean.mentions_total


class TestTheAnchoringFrameIsNarrow:
    """This mode may not quietly re-window a question the existing lanes answer."""

    @pytest.mark.parametrize(
        "text",
        [
            "What did I miss while I was heads-down on Kepler?",
            "while I was buried in the Kepler migration, what fell through?",
            "during the Kepler crunch did anything slip",
            "what got past me when I was deep in Kepler",
        ],
    )
    def test_the_anchored_question(self, text: str) -> None:
        assert entity_anchor_intent(text)

    @pytest.mark.parametrize(
        "text",
        [
            "What did Sarah say about Kepler?",            # ordinary subject question
            "what did I miss last week",                   # the planner's own window
            "while I was heads-down on Kepler, did Sarah reply?",  # a lookup, not a triage
            "I was heads-down on Kepler",                  # a statement
            "",
        ],
    )
    def test_the_unanchored_question_is_left_alone(self, text: str) -> None:
        assert not entity_anchor_intent(text)


# ============================================ 3. the grant bounds the dates, not the rows


class TestTheDensityIsBoundedByTheGrant:
    """Sever a plane, watch the DATES move.

    Each test here severs exactly one guard and asserts the derived window changes.
    That is the only proof that matters: a plane whose removal changes nothing was
    never doing anything, and the assertion that "no content leaked" is satisfied by
    a mode that leaks the shape of the content instead.
    """

    def _counts(self, conn, **kwargs) -> Dict[str, int]:
        base: Dict[str, Any] = dict(
            tables=["conversation_messages"],
            source_ids=[SOURCE_ID],
            analytics_surface=False,
            blocked_records=set(),
            excluded_records=set(),
        )
        base.update(kwargs)
        return R._entity_window_day_counts(conn, [ENTITY_ID], **base)

    def test_a_table_the_grant_does_not_name_does_not_vote(self, conn) -> None:
        """The peak exists only in `journal_entries`; a messages grant must not see it."""
        conn.executemany(
            "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id,"
            " canonical_table, surface_text, event_at) VALUES (?,?,?,?,?,?,?)",
            [
                (f"j-{i}", ENTITY_ID, f"jr-{i}", SOURCE_ID, "journal_entries",
                 ENTITY_NAME, "2026-09-1{}T12:00:00Z".format(i % 10))
                for i in range(12)
            ],
        )
        conn.commit()
        bounded = self._counts(conn)
        assert not any(d.startswith("2026-09") for d in bounded)
        # Severed: the same join with the table bound removed reaches September.
        severed = self._counts(conn, tables=["conversation_messages", "journal_entries"])
        assert any(d.startswith("2026-09") for d in severed)

    def test_a_mention_that_cannot_name_its_table_does_not_vote(self, conn) -> None:
        """No disclosed row ever confirms it here, so an untabled mention proves nothing.

        The thread lane can afford to carry NULL-table mentions because a record-id
        match against a disclosed page decides membership afterwards. This join has no
        such second step: the mention IS the evidence, so it has to carry its own proof.
        """
        conn.executemany(
            "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id,"
            " canonical_table, surface_text, event_at) VALUES (?,?,?,?,?,?,?)",
            [
                (f"u-{i}", ENTITY_ID, f"ur-{i}", SOURCE_ID, None, ENTITY_NAME,
                 "2026-09-05T12:00:00Z")
                for i in range(10)
            ],
        )
        conn.commit()
        assert not any(d.startswith("2026-09") for d in self._counts(conn))

    def test_a_source_the_grant_does_not_name_does_not_vote(self, conn) -> None:
        _seed(conn, {"2026-09-01": 6, "2026-09-02": 6}, source_id="whatsapp", prefix="wa")
        assert not any(d.startswith("2026-09") for d in self._counts(conn))
        severed = self._counts(conn, source_ids=[SOURCE_ID, "whatsapp"])
        assert severed.get("2026-09-01") == 6

    def test_a_scope_that_authorizes_no_record_surface_derives_nothing(self, conn) -> None:
        """Fail closed. Zero tables and no analytics surface is zero evidence."""
        assert self._counts(conn, tables=[], analytics_surface=False) == {}

    def test_the_black_hole_shapes_the_dates_a_grantee_is_shown(self, conn) -> None:
        """The severing proof, at the level that matters: the window itself moves.

        The September burst exists only in records that also mention a protected
        entity. Those records are withheld from a grantee — so the period they
        evidence must be withheld too, or the grantee is handed the shape of the
        thing the black hole exists to deny the existence of.
        """
        from topos.features.lifecycle.blackhole import BlackholeStore

        _add_entity(conn, "ent-protected", "Wren", entity_type="person")
        rids = _seed(conn, {"2026-09-01": 6, "2026-09-02": 6, "2026-09-03": 6}, prefix="sep")
        conn.executemany(
            "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id,"
            " canonical_table, surface_text, event_at) VALUES (?,?,?,?,?,?,?)",
            [
                (f"p-{r}", "ent-protected", r, SOURCE_ID, "conversation_messages",
                 "Wren", "2026-09-01T12:00:00Z")
                for r in rids
            ],
        )
        conn.commit()
        BlackholeStore(conn).blackhole_entity(entity_ref="ent-protected")
        conn.commit()

        grantee = _retrieve(conn, disclosure_tier="default_disclosure")
        owner = _retrieve(conn, disclosure_tier="owner_raw")
        assert _window(owner)["from"].startswith("2026-09")
        assert not str(_window(grantee).get("from") or "").startswith("2026-09")

        # Severed: with the guard's answer emptied, the grantee's window is the
        # owner's — the protected records shaped the dates after all.
        blocked = R._blackhole_blocked_record_ids
        try:
            R._blackhole_blocked_record_ids = lambda _conn: set()
            severed = _retrieve(conn, disclosure_tier="default_disclosure")
        finally:
            R._blackhole_blocked_record_ids = blocked
        assert _window(severed)["from"] == _window(owner)["from"]

    def test_the_black_hole_leaves_no_receipt_either(self, conn) -> None:
        """A count of withheld mentions confirms existence as well as a name does."""
        from topos.features.lifecycle.blackhole import BlackholeStore

        _add_entity(conn, "ent-protected", "Wren", entity_type="person")
        rids = _seed(conn, {"2026-09-01": 6, "2026-09-02": 6, "2026-09-03": 6}, prefix="sep")
        conn.executemany(
            "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id,"
            " canonical_table, surface_text, event_at) VALUES (?,?,?,?,?,?,?)",
            [
                (f"p-{r}", "ent-protected", r, SOURCE_ID, "conversation_messages",
                 "Wren", "2026-09-01T12:00:00Z")
                for r in rids
            ],
        )
        conn.commit()
        BlackholeStore(conn).blackhole_entity(entity_ref="ent-protected")
        conn.commit()
        led = NarrowingLedger()
        _retrieve(conn, disclosure_tier="default_disclosure", ledger=led)
        blob = json.dumps(led.as_public()).lower()
        assert "wren" not in blob and "blackhole" not in blob and "protected" not in blob

    def test_a_selector_policy_refuses_the_window_not_just_the_rows(self, conn) -> None:
        """A grantee outside the allow-list gets the refusal, and no dates with it."""
        manifest = replace(
            resolve_scope_manifest("messages:read"),
            entity_selector_policy_active=True,
            accessible_entity_ids=["ent-someone-else"],
        )
        led = NarrowingLedger()
        bundle = _retrieve(
            conn, manifest=manifest, disclosure_tier="default_disclosure", ledger=led
        )
        assert _window(bundle)["empty_reason"] == W.REFUSAL_UNRESOLVED
        assert "2026-08" not in json.dumps(_window(bundle))
        assert "2026-08" not in json.dumps(led.as_public())

    def test_the_owners_own_entity_anchors_nothing(self, conn) -> None:
        """`is_self` is the corpus, not a subject — its "heads-down period" is life."""
        conn.execute("UPDATE entities SET is_self = 1 WHERE entity_id = ?", (ENTITY_ID,))
        conn.commit()
        assert _window(_retrieve(conn))["empty_reason"] == W.REFUSAL_UNRESOLVED

    def test_an_exclusion_the_join_cannot_compile_stops_the_derivation(self, conn) -> None:
        """A window derived from a corpus the owner asked to shrink is derived wrong.

        A tier or category exclusion subtracts records this join cannot identify, so
        the density would still be computed over them and the dates would be shaped by
        exactly the rows that were meant to be gone. Deriving nothing is the answer.
        """
        led = NarrowingLedger()
        bundle = _retrieve(
            conn,
            "What did I miss while I was heads-down on Kepler, but not anything private?",
            ledger=led,
        )
        assert "entity_mention_density" not in json.dumps(_window(bundle))
        assert {
            "stage": "planner",
            "action": "not_applied",
            "reason": "exclusion_enforced",
        } in led.as_public()["ledger"]


# ============================================== 4. what the owner is shown, and what is not


class TestTheDerivationIsVisibleAndDisputable:
    def test_the_dates_reach_the_caller(self, conn) -> None:
        window = _window(_retrieve(conn))
        assert window["source"] == "entity_mention_density"
        assert (window["from"], window["to"]) == ("2026-08-01", "2026-08-06")

    def test_the_arithmetic_reaches_the_caller_too(self, conn) -> None:
        """"I think you meant Aug 4-11" needs the reasoning, not just the answer."""
        window = _window(_retrieve(conn))
        assert window["method"] == W.METHOD_PEAK_SPAN
        assert window["mentions_in_window"] == 15
        assert window["mentions_total"] == 17
        assert window["window_days"] == 6

    def test_the_public_ledger_line_is_three_slugs_and_no_dates(self, conn) -> None:
        led = NarrowingLedger()
        _retrieve(conn, ledger=led)
        published = led.as_public()["ledger"]
        assert {
            "stage": "planner",
            "action": "windowed",
            "reason": W.REASON_DERIVED,
        } in published
        assert "2026" not in json.dumps(published)

    def test_the_range_survives_on_the_local_ledger_for_debugging(self, conn) -> None:
        led = NarrowingLedger()
        _retrieve(conn, ledger=led)
        derived = [
            e for e in led.as_local()["ledger"] if e["reason"] == W.REASON_DERIVED
        ]
        assert derived and derived[0]["detail"]["time_range"] == ["2026-08-01", "2026-08-06"]

    def test_telemetry_carries_neither_the_dates_nor_the_subject(self, conn) -> None:
        led = NarrowingLedger()
        _retrieve(conn, ledger=led)
        blob = json.dumps(led.as_telemetry()).lower()
        assert "2026" not in blob and "kepler" not in blob


# ================================================================ 5. composition, not rivalry


class TestItComposesWithTheWindowsThatShipped:
    def test_a_stated_window_wins(self, conn) -> None:
        """The owner said when. This mode does not know better than that."""
        bundle = _retrieve(
            conn, "What did I miss last week while I was heads-down on Kepler?"
        )
        window = _window(bundle)
        assert window["source"] == "query_planner"
        assert window["from"].startswith("2026-08-10")

    def test_a_stated_point_in_time_wins_too(self, conn) -> None:
        """"in July 2026" parses to an as-of rather than a range, and is still the
        owner saying when. A gate that only checked `time_range` would have quietly
        overruled it — the derived window would have won on a sentence that named
        its own month."""
        bundle = _retrieve(
            conn, "What did I miss in July 2026 while I was heads-down on Kepler?"
        )
        window = _window(bundle)
        assert window["source"] == "query_planner"
        assert window["as_of"] == "2026-07-31"

    def test_a_differenced_ask_keeps_its_two_windows(self, conn) -> None:
        """R2's multi-window plan sets `time_range`, so Q3 never runs. One path."""
        bundle = _retrieve(
            conn, "what changed between last week and this week on Kepler"
        )
        assert _window(bundle).get("source") != "entity_mention_density"

    def test_the_derived_window_flows_through_the_planner_not_around_it(self, conn) -> None:
        """It is written into `plan.time_range`, so every existing consumer sees it.

        A second window that only the lanes Q3 knows about honour would leave the rest
        of the turn quietly unwindowed while the packet claimed a period.
        """
        bundle = _retrieve(conn)
        plan_meta = bundle.retrieval_metadata.get("query_plan") or {}
        assert plan_meta["time_range"] == [
            "2026-08-01T00:00:00+00:00",
            "2026-08-06T23:59:59+00:00",
        ]

    def test_an_ordinary_question_is_completely_unchanged(self, conn) -> None:
        """The mode is inert outside its frame — no window, no ledger line, no meta."""
        led = NarrowingLedger()
        bundle = _retrieve(conn, "what happened with Kepler", ledger=led)
        assert "entity_mention_density" not in json.dumps(_window(bundle))
        assert "entity_window" not in bundle.retrieval_metadata
        assert not [
            e for e in led.as_public()["ledger"] if e["reason"].startswith("entity_window")
        ]


# =========================================================== 6. the triage, run in the window


def _seed_triage(conn: sqlite3.Connection, day: str, record_ids: List[str]) -> None:
    """A day's verdicts plus the digest computed from them — the shapes that ship."""
    for rid in record_ids:
        conn.execute(
            "INSERT OR REPLACE INTO triage_verdicts (day, record_id, canonical_table,"
            " source_id, verdict, created_at) VALUES (?,?,?,?,?,?)",
            (day, rid, "conversation_messages", SOURCE_ID, "surface", f"{day}T23:00:00Z"),
        )
    conn.execute(
        """
        INSERT INTO signal_objects
            (object_id, signal_dimension, object_type, object_key, payload_json,
             valid_from, created_at, updated_at)
        VALUES (?, 'interests', 'attention_summary', ?, ?, ?, ?, ?)
        """,
        (
            f"obj-{day}",
            f"attention_summary:{day}",
            json.dumps({"day": day, "movers_valid": ["kepler"], "surface": [], "day_kl": 1.0}),
            f"{day}T23:00:00Z",
            f"{day}T23:00:00Z",
            f"{day}T23:00:00Z",
        ),
    )
    conn.commit()


class TestTheExistingTriageRunsInsideTheDerivedWindow:
    """`triage_verdicts` already holds the analytics. Nothing here recomputes them —
    a second scoring path would be a second set of verdicts to disagree with the
    first, and the owner would have no way to tell which one they were reading."""

    @pytest.fixture()
    def attention(self, conn):
        rids = [
            r[0]
            for r in conn.execute(
                "SELECT record_id FROM entity_mentions WHERE entity_id = ?", (ENTITY_ID,)
            ).fetchall()
        ]
        _seed_triage(conn, "2026-08-05", rids[:3])   # inside the derived window
        _seed_triage(conn, "2026-08-25", rids[3:6])  # outside it
        return conn

    def test_the_window_selects_among_the_digests_that_would_have_been_served(
        self, attention
    ) -> None:
        kept, dropped = R._attention_items_in_window(
            R._load_attention_summary_items(attention),
            derive_window_from_days(HEADS_DOWN),
        )
        assert [i["record_id"] for i in kept] == ["attention_summary:2026-08-05"]
        assert dropped == 1

    def test_no_derived_window_leaves_every_digest_where_it_was(self, attention) -> None:
        items = R._load_attention_summary_items(attention)
        assert R._attention_items_in_window(items, None) == (items, 0)

    def test_an_undated_digest_key_is_kept_not_silently_deleted(self) -> None:
        """A window that ate evidence on a formatting accident would read as a quiet week."""
        kept, dropped = R._attention_items_in_window(
            [{"record_id": "interest_profile:latest", "summary_text": "x"}],
            derive_window_from_days(HEADS_DOWN),
        )
        assert len(kept) == 1 and dropped == 0

    def test_the_attention_scope_can_reach_a_window_at_all(self, attention) -> None:
        """`attention:read` names ZERO canonical tables.

        Under the table bound alone the mode would be structurally unreachable on the
        one scope that owns the triage it exists to window — for owner and grantee
        alike. The density is bounded instead to the record surface the grant
        authorizes, which for this scope is `triage_verdicts`: the exact records its
        own digests are computed over.
        """
        counts = R._entity_window_day_counts(
            attention,
            [ENTITY_ID],
            tables=[],
            source_ids=[],
            analytics_surface=True,
            blocked_records=set(),
            excluded_records=set(),
        )
        assert counts and set(counts) <= {"2026-08-01", "2026-08-02", "2026-08-04",
                                          "2026-08-05", "2026-08-06", "2026-08-20",
                                          "2026-08-25"}

    def test_the_analytics_surface_is_not_a_way_around_the_table_bound(
        self, attention
    ) -> None:
        """A record with no verdict is not on the surface, whatever table it names."""
        _seed(attention, {"2026-09-09": 8}, prefix="unscored")
        counts = R._entity_window_day_counts(
            attention,
            [ENTITY_ID],
            tables=[],
            source_ids=[],
            analytics_surface=True,
            blocked_records=set(),
            excluded_records=set(),
        )
        assert "2026-09-09" not in counts

    # -- the window has to reach the days it names, not filter the newest page -----
    #
    # Measured 2026-08-21: a weekly report for Aug 11-16 asked on Aug 21 came back with
    # no attention analytics. The node held a digest AND an interest profile for every
    # one of those six days. The loader fetched the newest ten objects — Aug 17-21 —
    # and the window then dropped all of them, which reads downstream as a quiet week.

    @staticmethod
    def _seed_days(conn, days: List[str]) -> None:
        for day in days:
            _seed_triage(conn, day, [])

    def test_a_back_dated_window_reaches_past_the_newest_page(self, conn) -> None:
        """The regression: eleven days of digests, a window over the older six."""
        self._seed_days(conn, [f"2026-08-{d:02d}" for d in range(11, 22)])
        items = R._load_attention_summary_items(
            conn, window=W.DerivedWindow(start="2026-08-11", end="2026-08-16")
        )
        assert [i["record_id"] for i in items] == [
            f"attention_summary:2026-08-{d:02d}" for d in range(16, 10, -1)
        ]

    def test_the_window_the_loader_applied_is_the_window_the_filter_agrees_with(
        self, conn
    ) -> None:
        """Nothing survives the SQL only to be dropped by `_attention_items_in_window`."""
        self._seed_days(conn, [f"2026-08-{d:02d}" for d in range(11, 22)])
        window = W.DerivedWindow(start="2026-08-11", end="2026-08-16")
        items = R._load_attention_summary_items(conn, window=window)
        kept, dropped = R._attention_items_in_window(items, window)
        assert kept == items and dropped == 0

    def test_an_undated_key_is_selected_whatever_the_window(self, conn) -> None:
        """The SQL keeps the same evidence the filter is documented to keep."""
        conn.execute(
            "INSERT INTO signal_objects (object_id, signal_dimension, object_type,"
            " object_key, payload_json, valid_from, created_at, updated_at)"
            " VALUES ('obj-latest','interests','interest_profile','interest_profile:latest',"
            " '{\"top_vocab\": []}','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z',"
            " '2026-08-01T00:00:00Z')"
        )
        conn.commit()
        items = R._load_attention_summary_items(
            conn, window=W.DerivedWindow(start="2026-01-01", end="2026-01-31")
        )
        assert [i["record_id"] for i in items] == ["interest_profile:latest"]

    def test_no_window_still_serves_the_newest_page(self, conn) -> None:
        """The unwindowed default is unchanged: ten objects, newest first."""
        self._seed_days(conn, [f"2026-08-{d:02d}" for d in range(11, 22)])
        items = R._load_attention_summary_items(conn)
        assert len(items) == 10
        assert items[0]["record_id"] == "attention_summary:2026-08-21"

    def test_a_window_longer_than_the_cap_serves_its_newest_days(self, conn) -> None:
        """A year-long window is still a bounded payload, and says which end it kept."""
        assert R._attention_window_fetch_limit(
            W.DerivedWindow(start="2026-01-01", end="2026-12-31"), 10
        ) == R._ATTENTION_WINDOW_FETCH_DAYS_CAP * R._ATTENTION_OBJECTS_PER_DAY

    def test_an_empty_window_is_told_apart_from_a_node_with_no_triage(
        self, attention
    ) -> None:
        """The pair the call site reads: nothing in the window, but digests are held.

        Before the window reached SQL this count was whatever the LIMIT had gathered —
        a number about the page size, not about the owner's week.
        """
        assert R._load_attention_summary_items(
            attention, window=W.DerivedWindow(start="2026-01-01", end="2026-01-31")
        ) == []
        assert R._count_attention_summary_items(attention) == 2
        assert R._count_attention_summary_items(None) == 0

    def test_the_lane_reports_what_the_window_cost_end_to_end(self, attention) -> None:
        """Through `retrieve()`: the narrowing is recorded even though SQL did it.

        The drop count moved with the window. Read from the post-filter drop it would
        now be ~0 on every windowed turn — the narrowing did not stop happening, it
        stopped being visible from where the ledger used to read it.
        """
        led = NarrowingLedger()
        _retrieve(attention, ledger=led, scope="attention:read")
        assert {
            "stage": "retrieval",
            "action": "windowed",
            "reason": "entity_window_triage_lane",
            "dropped": 1,
        } in led.as_public()["ledger"]

    def test_a_window_with_no_triage_in_it_says_so(self, attention) -> None:
        """Different from "this node runs no triage", and only the ledger can say which."""
        led = NarrowingLedger()
        items, dropped = R._attention_items_in_window(
            R._load_attention_summary_items(attention),
            derive_window_from_days({"2026-05-01": 4, "2026-05-02": 5, "2026-05-03": 4,
                                     "2026-06-20": 1, "2026-07-01": 1}),
        )
        assert not items and dropped == 2
        led.empty(
            _N.CAUSE_NO_MATCH,
            stage=_N.STAGE_RETRIEVAL,
            reason="entity_window_no_triage_in_window",
            dropped=dropped,
        )
        assert led.empty_cause == _N.CAUSE_NO_MATCH
        assert {
            "stage": "retrieval",
            "action": "emptied",
            "reason": "entity_window_no_triage_in_window",
            "dropped": 2,
        } in led.as_public()["ledger"]

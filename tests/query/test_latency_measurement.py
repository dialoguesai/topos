"""What a turn cost, recorded where it can be read back.

§09 ships four quality metrics and no latency metric. These are the guards for the
three places a duration now lands on the engine side:

* a narrowing stage may say how long it took (``NarrowingEntry.elapsed_ms``),
* a turn says what the whole thing cost (``query_artifacts.duration_ms``),
* and the column exists because a *new* migration added it, not because an applied
  one was edited under nodes that had already run it.

Two invariants hold across all three, and most of the assertions below exist to pin
them rather than to demonstrate the feature:

1. **Additive.** A stage that does not time itself produces an entry byte-identical
   to the one it produced before durations existed, and a turn written without a
   measurement stores ``NULL`` — an absent reading, not a fast one.
2. **Integers only.** A duration is the one new thing crossing these seams, and it is
   allowed to cross precisely because an integer cannot carry the owner's sentence.
   Every entry point coerces; none is trusted to be called carefully.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from topos.query.narrowing import NarrowingEntry, NarrowingLedger
from topos.storage.adapters.sqlite.stores import SQLiteQuerySessionStore
from topos.storage.db.migrations import apply_all_migrations, max_migration_order
from topos.storage.db.migrations.query_artifacts_duration_ms_v1 import (
    MIGRATION_ID as DURATION_MIGRATION_ID,
)
from topos.storage.db.migrations.registry import MIGRATIONS


def _load_percentiles_script():
    """Import ``scripts/query_latency_percentiles.py`` by path.

    ``scripts/`` is not a package, and the script is the fifth §09 metric — the thing
    that turns these columns into a number someone quotes. An unexercised reporting
    script is how a plausible wrong figure gets published, which is precisely what its
    first run against the live node produced (a routines p95 of one day, because
    ``created_at`` is the scheduler's clock).
    """
    path = Path(__file__).resolve().parents[2] / "scripts" / "query_latency_percentiles.py"
    spec = importlib.util.spec_from_file_location("query_latency_percentiles", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


PCT = _load_percentiles_script()

#: The probe. If a duration path ever stringifies its input instead of coercing it,
#: this is what shows up in a field that is supposed to be closed.
OWNER_SENTENCE = "Sarah Chen divorce lawyer meeting"


class TestAStageMaySayHowLongItTook:
    def test_an_untimed_stage_is_byte_identical_to_before(self) -> None:
        # The additive guarantee, stated as the only thing that could disprove it: the
        # public dict of an untimed entry has no new key at all.
        entry = NarrowingEntry("rare_gate", "emptied", "rare_token_unevidenced", dropped=25)
        assert entry.as_public() == {
            "stage": "rare_gate",
            "action": "emptied",
            "reason": "rare_token_unevidenced",
            "dropped": 25,
        }
        assert "elapsed_ms" not in entry.as_public()

    def test_a_timed_stage_publishes_whole_milliseconds(self) -> None:
        entry = NarrowingEntry("retrieval", "capped", "summary_item_cap", elapsed_ms=842.6)
        assert entry.as_public()["elapsed_ms"] == 842

    def test_a_negative_reading_clamps_rather_than_travelling(self) -> None:
        # Only ever a clock going backwards. Publishing it would put a negative
        # millisecond into a percentile.
        assert NarrowingEntry("retrieval", "capped", "summary_item_cap", elapsed_ms=-9).elapsed_ms == 0

    def test_a_duration_that_is_not_a_number_is_dropped_not_stringified(self) -> None:
        entry = NarrowingEntry(
            "retrieval", "capped", "summary_item_cap", elapsed_ms=OWNER_SENTENCE
        )
        assert entry.elapsed_ms is None
        assert "Sarah" not in json.dumps(entry.as_public())

    def test_the_coercion_is_in_the_constructor_not_only_in_record(self) -> None:
        # `NarrowingEntry(...)` is public and `as_public` serializes whatever the object
        # holds, so a guarantee that lives only in `record()` is a convention.
        led = NarrowingLedger()
        led.record("scope_routing", "capped", "max_scope_routes", dropped=1, elapsed_ms=7.9)
        assert led.entries[0].elapsed_ms == 7
        assert NarrowingEntry("scope_routing", "capped", "max_scope_routes", elapsed_ms=7.9).elapsed_ms == 7

    def test_empty_can_time_the_stage_that_emptied_the_result(self) -> None:
        led = NarrowingLedger()
        led.empty("gate_vetoed", stage="rare_gate", dropped=25, elapsed_ms=31)
        assert led.entries[0].elapsed_ms == 31
        assert led.empty_cause == "gate_vetoed"

    def test_a_duration_survives_the_wire_into_extend_public(self) -> None:
        # `extend_public` REBUILDS every arriving entry field by field, so a field it
        # does not name is silently dropped. That is exactly how a front-end or control
        # plane duration would vanish on arrival while looking like it was sent.
        led = NarrowingLedger()
        led.extend_public(
            [{"stage": "scope_routing", "action": "capped", "reason": "max_scope_routes", "elapsed_ms": 55}]
        )
        assert led.entries[0].elapsed_ms == 55

    def test_a_duration_arriving_from_upstream_is_re_checked(self) -> None:
        led = NarrowingLedger()
        led.extend_public(
            [
                {"stage": "retrieval", "action": "capped", "reason": "summary_item_cap", "elapsed_ms": -3},
                {"stage": "retrieval", "action": "capped", "reason": "summary_item_cap", "elapsed_ms": OWNER_SENTENCE},
            ]
        )
        assert led.entries[0].elapsed_ms == 0
        assert led.entries[1].elapsed_ms is None
        assert "Sarah" not in json.dumps(led.as_public())

    def test_telemetry_gains_no_key_when_nothing_timed_itself(self) -> None:
        # Today's normal case. An unconditional empty map would have changed the
        # telemetry record of every caller on the day this shipped, for no information.
        led = NarrowingLedger()
        led.record("rare_gate", "emptied", "rare_token_unevidenced")
        led.empty("gate_vetoed")
        assert led.as_telemetry() == {
            "n_entries": 1,
            "stages": {"rare_gate": 1},
            "empty_cause": "gate_vetoed",
        }

    def test_telemetry_sums_per_stage_not_per_entry(self) -> None:
        # The map is keyed by the fixed stage vocabulary, so it cannot grow with the
        # number of narrowings — which is what keeps it a telemetry record.
        led = NarrowingLedger()
        led.record("retrieval", "capped", "summary_item_cap", elapsed_ms=10)
        led.record("retrieval", "capped", "summary_item_cap", elapsed_ms=32)
        led.record("scope_routing", "capped", "max_scope_routes", elapsed_ms=4)
        led.record("rare_gate", "emptied", "rare_token_unevidenced")  # untimed
        assert led.as_telemetry()["elapsed_ms"] == {"retrieval": 42, "scope_routing": 4}


class TestTheMigrationThatAddedTheColumn:
    def test_the_column_arrives_through_a_new_migration(self) -> None:
        # NOT by editing `wiki_mvp_phase0`. A node whose `user_version` is already past
        # phase 0 never re-runs it, so an in-place edit ships a column that exists only
        # on machines installed after the edit.
        spec = next(m for m in MIGRATIONS if m.id == DURATION_MIGRATION_ID)
        assert spec.order == max_migration_order(), "the new column is the registry head"
        assert spec.order > min(m.order for m in MIGRATIONS)

    def test_applying_the_registry_yields_the_column_and_stamps_the_ledger(
        self, tmp_path
    ) -> None:
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        apply_all_migrations(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(query_artifacts)").fetchall()}
        assert "duration_ms" in cols
        stamped = {
            r[0] for r in conn.execute("SELECT migration_id FROM wiki_schema_migrations").fetchall()
        }
        assert DURATION_MIGRATION_ID in stamped
        conn.close()

    def test_applying_twice_does_not_duplicate_the_column(self, tmp_path) -> None:
        # An ADD COLUMN that is not guarded raises "duplicate column name" on the second
        # pass, which the runner turns into a MigrationError and a node that will not open.
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        apply_all_migrations(conn)
        apply_all_migrations(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(query_artifacts)").fetchall()]
        assert cols.count("duration_ms") == 1
        conn.close()


@pytest.fixture()
def session_store(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    apply_all_migrations(conn)
    store = SQLiteQuerySessionStore(conn)
    store.put(
        {
            "session_id": "sess-1",
            "requester_id": "owner",
            "intent_hash": "ih",
            "envelope_json": {"scope_id": "messages:read"},
            "ttl_expires_at": "2099-01-01T00:00:00Z",
        }
    )
    yield store
    conn.close()


class TestATurnSaysWhatItCost:
    def test_a_measured_turn_reads_back_as_an_integer(self, session_store) -> None:
        session_store.append_artifact(
            "sess-1",
            {"artifact_id": "a1", "cache_key": "ck", "duration_ms": 5310},
        )
        artifact = session_store.get("sess-1")["artifacts"][0]
        assert artifact["duration_ms"] == 5310

    def test_an_unmeasured_turn_stores_absence_not_zero(self, session_store) -> None:
        # The distinction the percentile script depends on: a turn nobody timed must not
        # enter the sample as a 0 ms turn and drag p50 to the floor.
        session_store.append_artifact("sess-1", {"artifact_id": "a1", "cache_key": "ck"})
        assert session_store.get("sess-1")["artifacts"][0]["duration_ms"] is None

    def test_an_unreadable_duration_stores_absence_rather_than_text(self, session_store) -> None:
        session_store.append_artifact(
            "sess-1",
            {"artifact_id": "a1", "cache_key": "ck", "duration_ms": OWNER_SENTENCE},
        )
        artifact = session_store.get("sess-1")["artifacts"][0]
        assert artifact["duration_ms"] is None
        assert "Sarah" not in json.dumps(artifact)

    def test_a_negative_duration_clamps(self, session_store) -> None:
        session_store.append_artifact(
            "sess-1", {"artifact_id": "a1", "cache_key": "ck", "duration_ms": -1}
        )
        assert session_store.get("sess-1")["artifacts"][0]["duration_ms"] == 0

    def test_a_fractional_duration_is_stored_whole(self, session_store) -> None:
        # SQLite's INTEGER affinity would happily keep 5310.7 as a REAL; the coercion
        # is in the store so the column holds one type regardless of caller.
        session_store.append_artifact(
            "sess-1", {"artifact_id": "a1", "cache_key": "ck", "duration_ms": 5310.7}
        )
        stored = session_store.get("sess-1")["artifacts"][0]["duration_ms"]
        assert isinstance(stored, int) and stored == 5310


class TestTheGrammarTheFrontEndWrites:
    """This parser is a SECOND implementation of `next/lib/mcp/latencyTrace.ts`.

    Two codebases agreeing on a string format agree only while both are exercised. The
    TS side has a round-trip test; this is the other half, written from the format's
    definition rather than from the TS source, so a change to one that is not made to
    the other fails here rather than in a silently empty report.
    """

    def test_it_reads_the_line_the_front_end_emits(self) -> None:
        sample = PCT.parse_latency_step("[latency] leg=synthesis elapsed_ms=5310 max_tokens=2800 parts=5")
        assert sample == {
            "leg": "synthesis",
            "elapsed_ms": 5310,
            "measures": {"max_tokens": 2800, "parts": 5},
        }

    def test_it_reads_a_bare_turn_line(self) -> None:
        assert PCT.parse_latency_step("[latency] leg=turn elapsed_ms=9021")["elapsed_ms"] == 9021

    @pytest.mark.parametrize(
        "line",
        [
            "Calling tool query_scope",  # an ordinary step, not a latency line
            "[latency] leg=turn",  # no duration
            "[latency] elapsed_ms=12",  # no leg
            "[latency] leg=exfiltrate elapsed_ms=12",  # leg outside the closed set
            "[latency] leg=turn elapsed_ms=-5",  # negative
            f"[latency] leg=turn elapsed_ms={OWNER_SENTENCE}",
            None,
            42,
        ],
    )
    def test_it_refuses_anything_the_formatter_could_not_have_written(self, line) -> None:
        assert PCT.parse_latency_step(line) is None

    def test_a_measure_that_is_not_an_integer_is_dropped_not_kept(self) -> None:
        sample = PCT.parse_latency_step(f"[latency] leg=turn elapsed_ms=10 note={OWNER_SENTENCE}")
        assert sample["measures"] == {}
        assert "Sarah" not in json.dumps(sample)


class TestThePercentileItself:
    def test_an_empty_sample_reports_nothing_rather_than_zero(self) -> None:
        assert PCT.percentile([], 0.5) is None
        assert PCT.summarize([]) == {"samples": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}

    def test_every_reported_number_is_a_duration_something_actually_took(self) -> None:
        # Nearest-rank, not interpolated. An interpolated p95 of a short series is a
        # figure no request produced, and this script exists to stop such figures being
        # quoted.
        values = [10, 20, 30, 40]
        assert PCT.percentile(values, 0.95) in values
        assert PCT.percentile(values, 0.50) in values

    def test_it_matches_nearest_rank_on_a_known_series(self) -> None:
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert PCT.percentile(values, 0.50) == 5
        assert PCT.percentile(values, 0.95) == 10

    def test_a_single_sample_is_its_own_p50_and_p95(self) -> None:
        assert PCT.summarize([777]) == {"samples": 1, "p50_ms": 777, "p95_ms": 777, "max_ms": 777}


class TestTheReportAgainstADatabase:
    """Built against a temp DB. The script's default target is the owner's live node."""

    @pytest.fixture()
    def db(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        apply_all_migrations(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS home_chat_sessions (
                id TEXT PRIMARY KEY, history_json TEXT, updated_at_ms INTEGER
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS routine_runs (id TEXT PRIMARY KEY, payload_json TEXT)"
        )
        conn.commit()
        yield conn
        conn.close()

    def test_a_node_with_no_traffic_reports_zero_not_a_number(self, db) -> None:
        # The output on the day this ships. Zero samples is the correct answer, and the
        # report has to be able to say it without a p50 of `0`.
        report = PCT.build_report(db, days=7, stale_minutes=60)
        assert report["entrances"]["home_chat"]["samples"] == 0
        assert report["entrances"]["home_chat"]["p50_ms"] is None
        assert report["engine_per_scope_query"]["samples"] == 0

    def test_it_counts_turn_lines_out_of_the_step_trace(self, db) -> None:
        import time

        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO home_chat_sessions (id, history_json, updated_at_ms) VALUES (?, ?, ?)",
            (
                "s1",
                json.dumps(
                    {
                        "agentSteps": [
                            "Calling tool query_scope",
                            "[latency] leg=scope_query elapsed_ms=900",
                            "[latency] leg=synthesis elapsed_ms=5310 max_tokens=2800 parts=5",
                            "[latency] leg=turn elapsed_ms=9021",
                        ]
                    }
                ),
                now_ms,
            ),
        )
        db.commit()
        report = PCT.build_report(db, days=7, stale_minutes=60)
        assert report["entrances"]["home_chat"] == {
            "samples": 1,
            "p50_ms": 9021,
            "p95_ms": 9021,
            "max_ms": 9021,
            "source": "home_chat_sessions.history_json agentSteps '[latency] leg=turn'",
            "caveat": "agentSteps keeps the last 24 lines per session; this is a sample of recent turns, not a census",
        }
        assert report["front_end_legs"]["scope_query"]["p50_ms"] == 900
        assert report["synthesis_budget_vs_cost"] == [
            {"leg": "synthesis", "elapsed_ms": 5310, "max_tokens": 2800, "parts": 5}
        ]

    def test_a_session_outside_the_window_is_not_counted(self, db) -> None:
        db.execute(
            "INSERT INTO home_chat_sessions (id, history_json, updated_at_ms) VALUES (?, ?, ?)",
            ("s1", json.dumps({"agentSteps": ["[latency] leg=turn elapsed_ms=9021"]}), 0),
        )
        db.commit()
        assert PCT.build_report(db, days=7, stale_minutes=60)["entrances"]["home_chat"]["samples"] == 0

    def _run(self, *, started, completed, status="completed"):
        return json.dumps({"status": status, "started_at": started, "completed_at": completed})

    def test_a_routine_run_is_timed_from_started_not_created(self, db) -> None:
        # The defect this caught on the live node: `created_at` is stamped when the run
        # is SCHEDULED, so a daily routine reported a p95 of 86,429,354 ms — one day and
        # a quarter second, which is the cron interval, not the run.
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        db.execute(
            "INSERT INTO routine_runs (id, payload_json) VALUES (?, ?)",
            (
                "r1",
                json.dumps(
                    {
                        "status": "completed",
                        "created_at": (now - timedelta(days=1)).isoformat(),
                        "started_at": (now - timedelta(seconds=60)).isoformat(),
                        "completed_at": now.isoformat(),
                    }
                ),
            ),
        )
        db.commit()
        stats = PCT.build_report(db, days=7, stale_minutes=60)["entrances"]["routines"]
        assert stats["samples"] == 1
        assert 59_000 <= stats["p50_ms"] <= 61_000, "one minute, not one day"

    def test_a_reaped_run_is_counted_as_abandoned_not_as_a_slow_one(self, db) -> None:
        # A run longer than the reaper's threshold did not finish; its `completed_at` is
        # the reaper's clock. Counted and reported, because "a run was abandoned" is
        # information — and dropped from the percentile, because it is not a latency.
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        db.execute(
            "INSERT INTO routine_runs (id, payload_json) VALUES (?, ?)",
            ("r1", self._run(started=(now - timedelta(hours=24)).isoformat(), completed=now.isoformat(), status="failed")),
        )
        db.execute(
            "INSERT INTO routine_runs (id, payload_json) VALUES (?, ?)",
            ("r2", self._run(started=(now - timedelta(seconds=30)).isoformat(), completed=now.isoformat())),
        )
        db.commit()
        stats = PCT.build_report(db, days=7, stale_minutes=60)["entrances"]["routines"]
        assert stats["samples"] == 1
        assert stats["abandoned_runs"] == 1
        assert stats["max_ms"] < 60 * 60 * 1000

    def test_a_cancelled_or_running_run_is_not_a_latency(self, db) -> None:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        started = (now - timedelta(seconds=30)).isoformat()
        for i, status in enumerate(("cancelled", "running", "queued")):
            db.execute(
                "INSERT INTO routine_runs (id, payload_json) VALUES (?, ?)",
                (f"r{i}", self._run(started=started, completed=now.isoformat(), status=status)),
            )
        db.commit()
        assert PCT.build_report(db, days=7, stale_minutes=60)["entrances"]["routines"]["samples"] == 0

    def test_an_unmeasured_artifact_row_is_reported_as_absent_not_as_zero(self, db) -> None:
        store = SQLiteQuerySessionStore(db)
        store.put({"session_id": "s", "requester_id": "owner", "intent_hash": "i", "envelope_json": {}, "ttl_expires_at": "2099-01-01"})
        store.append_artifact("s", {"artifact_id": "a1", "cache_key": "c"})
        store.append_artifact("s", {"artifact_id": "a2", "cache_key": "c", "duration_ms": 400})
        engine = PCT.build_report(db, days=7, stale_minutes=60)["engine_per_scope_query"]
        assert engine["samples"] == 1
        assert engine["unmeasured_rows"] == 1
        assert engine["p50_ms"] == 400

    def test_test_suite_turns_are_not_reported_as_this_person_s_latency(self, db) -> None:
        """The pollution that made this series meaningless.

        Until the test lane was made hermetic (docs/testing/TEST_LANES.md) a plain
        `pytest tests/` ran the owner-database evals against ~/.topos. On
        2026-08-19 that left 100% of the rows carrying a duration_ms written by
        the harness, so this number described the eval suite while reading as a
        statement about the owner's node. Those rows are permanent, so the
        filter is too.
        """
        store = SQLiteQuerySessionStore(db)
        for session_id, duration in (
            ("qq-eval-D4-abc123", 9000),
            ("d13-live-abc123", 9000),
            ("sel-live-abc123", 9000),
            ("adv-iter2-raw-open", 9000),
            ("qs_real-session", 400),
        ):
            store.put({"session_id": session_id, "requester_id": "owner", "intent_hash": "i", "envelope_json": {}, "ttl_expires_at": "2099-01-01"})
            store.append_artifact(session_id, {"artifact_id": f"a-{session_id}", "cache_key": "c", "duration_ms": duration})

        engine = PCT.build_report(db, days=7, stale_minutes=60)["engine_per_scope_query"]
        assert engine["samples"] == 1, "only the real session counts"
        assert engine["p50_ms"] == 400, "harness turns must not move the percentile"
        assert engine["harness_rows_excluded"] == 4

    def test_excluded_harness_rows_are_counted_not_silently_dropped(self, db) -> None:
        """A filter nobody can see is indistinguishable from missing data."""
        store = SQLiteQuerySessionStore(db)
        store.put({"session_id": "qq-eval-x", "requester_id": "owner", "intent_hash": "i", "envelope_json": {}, "ttl_expires_at": "2099-01-01"})
        store.append_artifact("qq-eval-x", {"artifact_id": "a1", "cache_key": "c", "duration_ms": 100})
        engine = PCT.build_report(db, days=7, stale_minutes=60)["engine_per_scope_query"]
        assert engine["samples"] == 0
        assert engine["harness_rows_excluded"] == 1
        assert engine["unmeasured_rows"] == 0, "an excluded row is not an untimed one"

    def test_a_database_without_the_tables_reports_zero_rather_than_raising(self, tmp_path) -> None:
        # A node that has never opened home chat, or an older DB. Observability may not
        # be the thing that fails.
        conn = sqlite3.connect(str(tmp_path / "bare.db"))
        report = PCT.build_report(conn, days=7, stale_minutes=60)
        assert report["entrances"]["home_chat"]["samples"] == 0
        assert report["entrances"]["routines"]["samples"] == 0
        assert report["engine_per_scope_query"]["samples"] == 0
        conn.close()

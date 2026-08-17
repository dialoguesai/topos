"""Scope of the cooperative stop signal: run-scoped, and per-batch for cancels.

The bug these pin: `runtime_shutdown` was ONE process-lifetime boolean doing
three jobs at three scopes — "the process got a signal", "this app run is
tearing down", and "this fact-extraction batch was cancelled". The last one was
wrong by the width of the process: `FactExtractionJob` answered
`asyncio.CancelledError` with `request_shutdown("fact_extraction_cancelled")`,
which only an app startup could clear, so ONE cancelled batch left every later
batch returning 0 facts — job still reporting success, healthcheck still green —
until the node was restarted.

Covered here:
  * a retired generation stops its OWN workers and nobody else's;
  * ending a run leaves the process not-shutting-down;
  * a cancelled batch does not touch the next batch, or the run;
  * the stop is reported, not silent (the `stats` out-dict).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading

import pytest

from topos.features.facts.llm_extract import extract_owner_facts_llm
from topos.runtime_shutdown import (
    begin_runtime,
    clear_shutdown,
    current_generation,
    end_runtime,
    is_shutdown_requested,
    request_shutdown,
    stop_checker,
)
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture(autouse=True)
def _fresh_runtime():
    """Every test here starts and ends on a live, un-stopped generation."""
    begin_runtime("test")
    yield
    end_runtime("test_teardown")
    clear_shutdown()


@pytest.fixture()
def conn(tmp_path):
    db = sqlite3.connect(str(tmp_path / "facts.db"))
    db.row_factory = sqlite3.Row
    apply_all_migrations(db)
    db.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
        " VALUES ('ent-owner', 'person', 'Owner', 'owner', 1)"
    )
    db.commit()
    yield db
    db.close()


def _owner_row(content, mid):
    return {
        "message_id": mid,
        "conversation_id": "chatgpt:conv-1",
        "sender_type": "human",
        "sender_id": None,
        "content": content,
        "event_at": "2026-06-01T10:00:00+00:00",
        "_table": "ai_chat_messages",
    }


def _stub(prompt, row):
    return [{"predicate": "practices", "object": f"yoga-{row.get('message_id')}"}]


def _active_facts(conn):
    rows = conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


class TestGenerationScope:
    def test_retired_generation_stops_only_its_own_workers(self):
        mine = current_generation()
        retired = end_runtime("app_shutdown")

        assert retired is mine
        assert stop_checker(mine)() is True, "my run was told to stop"
        # The process is NOT shutting down: the run that was is over.
        assert is_shutdown_requested() is False
        assert stop_checker(current_generation())() is False

    def test_a_finished_run_cannot_speak_for_the_next_one(self):
        first = current_generation()
        end_runtime("app_shutdown")
        second = begin_runtime("app_startup")

        assert second is not first
        assert stop_checker(first)() is True, "the old run stays stopped forever"
        assert stop_checker(second)() is False, "the new run starts clean"

    def test_beginning_a_run_retires_one_that_never_ended(self):
        # A run that dies without end_runtime (crash, killed lifespan) must not
        # leave workers polling a flag nobody will set.
        orphan = current_generation()
        fresh = begin_runtime("app_startup")

        assert stop_checker(orphan)() is True
        assert stop_checker(fresh)() is False

    def test_request_shutdown_still_stops_the_current_run(self):
        # The signal path: SIGINT/SIGTERM with or without an app running.
        gen = current_generation()
        request_shutdown("signal:SIGINT")

        assert is_shutdown_requested() is True
        assert stop_checker(gen)() is True
        assert gen.reason == "signal:SIGINT"


class TestBatchCancelScope:
    def test_cancelling_one_batch_leaves_the_next_batch_working(self, conn):
        """The headline regression: one cancel used to darken the lane for good."""
        cancel = threading.Event()
        cancel.set()

        first = extract_owner_facts_llm(
            conn, [_owner_row("I have practiced yoga for years", "r1")],
            extractor=_stub, cancel=cancel,
        )
        assert first == 0

        # A *different* batch, with its own (unset) cancel, is unaffected.
        second = extract_owner_facts_llm(
            conn, [_owner_row("I have practiced yoga for years", "r2")],
            extractor=_stub,
        )
        assert second == 1
        assert [f["object_value"] for f in _active_facts(conn)] == ["yoga-r2"]

    def test_cancelling_a_batch_does_not_stop_the_run(self, conn):
        cancel = threading.Event()
        cancel.set()
        gen = current_generation()

        extract_owner_facts_llm(
            conn, [_owner_row("I have practiced yoga for years", "r1")],
            extractor=_stub, cancel=cancel,
        )

        assert is_shutdown_requested() is False
        assert stop_checker(gen)() is False

    def test_shutdown_still_stops_a_batch(self, conn):
        """Run-scoped stop keeps working — this is the behaviour worth keeping."""
        request_shutdown("app_shutdown")

        written = extract_owner_facts_llm(
            conn, [_owner_row("I have practiced yoga for years", "r1")],
            extractor=_stub,
        )
        assert written == 0
        assert _active_facts(conn) == []

    def test_a_batch_stops_when_the_run_it_started_under_ends(self, conn):
        """The capture point is load-bearing.

        `end_runtime` installs a fresh, LIVE generation. A batch that polled
        "the current run" would see that fresh one and keep working straight
        through the shutdown it was supposed to honour, so the batch captures
        its generation at entry and polls that.
        """
        seen = []

        def _ends_the_run(prompt, row):
            seen.append(row.get("message_id"))
            if len(seen) == 1:
                end_runtime("app_shutdown")
            return _stub(prompt, row)

        rows = [_owner_row("I have practiced yoga for years", f"r{i}") for i in range(6)]
        written = extract_owner_facts_llm(
            conn, rows, extractor=_ends_the_run, concurrency=1
        )

        assert is_shutdown_requested() is False, "the process itself is fine"
        assert len(seen) < len(rows), "the batch stopped with rows left"
        assert written < len(rows)


class TestStopIsReported:
    def test_stats_name_the_reason_and_the_unprocessed_rows(self, conn):
        cancel = threading.Event()
        cancel.set()
        stats = {}

        extract_owner_facts_llm(
            conn, [_owner_row("I have practiced yoga for years", "r1")],
            extractor=_stub, cancel=cancel, stats=stats,
        )

        assert stats["stopped"] is True
        assert stats["stop_reason"] == "cancelled"
        assert stats["unprocessed"] == 1
        assert stats["written"] == 0

    def test_stats_on_a_clean_pass_report_no_stop(self, conn):
        stats = {}
        written = extract_owner_facts_llm(
            conn, [_owner_row("I have practiced yoga for years", "r1")],
            extractor=_stub, stats=stats,
        )

        assert written == 1
        assert stats["stopped"] is False
        assert stats["stop_reason"] == ""
        assert stats["unprocessed"] == 0
        assert stats["eligible"] == 1


class TestJobCancelDoesNotPoisonTheProcess:
    @pytest.mark.asyncio
    async def test_cancelled_enrich_leaves_the_run_alive(self, conn, tmp_path, monkeypatch):
        """FactExtractionJob's cancel path must stay inside the batch.

        Before: `request_shutdown("fact_extraction_cancelled")`, which no later
        batch could recover from without a node restart.
        """
        import topos.enrichment.jobs.canonical.fact_extraction_job as job_mod
        from topos.enrichment.jobs.canonical.fact_extraction_job import FactExtractionJob

        # The job binds get_db_connection by value at import; patch it there.
        monkeypatch.setattr(job_mod, "get_db_connection", lambda: conn)

        started = threading.Event()
        release = threading.Event()

        def _blocking_extract(_conn, _rows, **_kwargs):
            started.set()
            release.wait(timeout=5)
            return 0

        monkeypatch.setattr(
            "topos.features.facts.extract.extract_facts_from_batch", _blocking_extract
        )

        gen = current_generation()
        task = asyncio.create_task(
            FactExtractionJob().enrich([_owner_row("I practice yoga daily", "r1")])
        )
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The cancel stayed in the batch: the run is untouched, so the next
        # batch in this process still extracts. Its own connection, opened with
        # check_same_thread=False, because inside an async test
        # _run_coro_blocking drives the fan-out on a worker thread.
        assert is_shutdown_requested() is False
        assert stop_checker(gen)() is False

        next_batch = sqlite3.connect(str(tmp_path / "next.db"), check_same_thread=False)
        next_batch.row_factory = sqlite3.Row
        apply_all_migrations(next_batch)
        next_batch.execute(
            "INSERT INTO entities (entity_id, entity_type, canonical_name,"
            " normalized_name, is_self) VALUES ('ent-owner','person','Owner','owner',1)"
        )
        next_batch.commit()
        try:
            assert (
                extract_owner_facts_llm(
                    next_batch,
                    [_owner_row("I have practiced yoga for years", "r2")],
                    extractor=_stub,
                )
                == 1
            )
        finally:
            next_batch.close()

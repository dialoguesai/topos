"""The enrichment and signal lanes must never take the write gate on the loop.

The gate is a blocking OS lock, so a hold taken on the event-loop thread stalls
every coroutine behind whatever writer owns it — including the control-plane
keepalive. dc2178b cleared the ingest chain; the lanes reached from
``run_post_canonical_pipeline`` still acquired it at 23 sites (dev node,
2026-08-08): stats fold/promotion, attention triage, dimension briefs, the
scope materializer, url-classification writes, and the runtime migrations that
``AdapterFactory`` runs when the orchestrator resolves its adapter bundle.
"""

from __future__ import annotations

import logging
import sqlite3
import threading

import pytest

from topos.enrichment.jobs.canonical import attention_triage_job as triage_mod
from topos.enrichment.jobs.canonical import statistics_job as stats_mod
from topos.enrichment.orchestrator import (
    EnrichmentOrchestrator,
    SignalDerivationOrchestrator,
)
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.write_gate import reset_loop_warning_state


@pytest.mark.asyncio
async def test_statistics_job_resolves_its_connection_off_the_loop(monkeypatch) -> None:
    seen: dict[str, int] = {}

    def _fake_conn():
        seen["thread"] = threading.get_ident()
        return None

    monkeypatch.setattr(stats_mod, "get_db_connection", _fake_conn)
    out = await stats_mod.StatisticsJob().enrich([{"record_id": "r1"}])

    assert out == [{"_deferred": True, "error": "database_unavailable"}]
    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_attention_triage_runs_each_day_off_the_loop(monkeypatch) -> None:
    days: list[int] = []

    monkeypatch.setattr(triage_mod, "get_db_connection", lambda: object())
    monkeypatch.setattr(
        triage_mod,
        "run_daily_triage",
        lambda _conn, _day: days.append(threading.get_ident()) or {"quadrants": {}},
    )

    await triage_mod.AttentionTriageJob().enrich(
        [{"record_id": "r1", "event_at": "2026-08-08T20:17:00Z"}]
    )

    assert days and all(t != threading.get_ident() for t in days)


@pytest.mark.asyncio
async def test_run_canonical_writes_its_batch_off_the_loop() -> None:
    """``write_enrichment_batch`` holds the gate per batch.

    It also carries the connection of whichever thread built the manager — the
    post-canonical pipeline builds it in a ``to_thread`` worker — so driving it
    from the loop was both a gate-on-loop hold and a cross-thread connection.
    """

    class _Job:
        def get_job_name(self) -> str:
            return "fake"

        def get_derived_table(self) -> str:
            return "message_emotions"

        def should_run(self, _messages) -> bool:
            return True

        async def enrich(self, _messages, progress_callback=None):
            return [{"message_id": "m1"}]

    class _Manager:
        thread: int | None = None

        def write_enrichment_batch(self, records, _table) -> int:
            self.thread = threading.get_ident()
            return len(records)

    manager = _Manager()
    orchestrator = EnrichmentOrchestrator(tables_manager=manager)
    orchestrator.canonical_jobs = [_Job()]

    results = await orchestrator.run_canonical([{"id": "m1"}])

    assert results["records_created"]["message_emotions"] == 1
    assert manager.thread is not None
    assert manager.thread != threading.get_ident()


@pytest.mark.asyncio
async def test_signal_derivation_never_builds_adapters_on_the_loop() -> None:
    """``AdapterFactory.create`` runs migrations and canonical DDL under the gate.

    Resolving the bundle eagerly took that gate on the loop, and cached a
    loop-bound bundle that the next batch would treat as injected and use
    inline. On the runtime path ``_offload_write`` builds it in the worker.
    """
    orchestrator = SignalDerivationOrchestrator()
    called: list[int] = []

    def _boom():
        called.append(threading.get_ident())
        raise AssertionError("adapters must not be resolved on the event loop")

    orchestrator._get_adapters = _boom  # type: ignore[method-assign]

    await orchestrator.run_signal_derivation(
        [{"record_id": "r1"}], source_id="unknown_source_with_no_jobs"
    )

    assert called == []


@pytest.mark.asyncio
async def test_signal_lane_takes_no_write_gate_on_event_loop(
    monkeypatch, tmp_path, caplog
) -> None:
    """End to end over a real database: the stats and triage jobs must log no
    loop-thread gate acquisitions (they accounted for 8 of the 23 live sites)."""
    conn = sqlite3.connect(str(tmp_path / "lane.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)

    # These jobs bind get_db_connection at import, so patch the module
    # attributes; the orchestrator itself imports it late.
    for mod in (stats_mod, triage_mod):
        monkeypatch.setattr(mod, "get_db_connection", lambda: conn)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)

    reset_loop_warning_state()
    caplog.set_level(logging.WARNING, logger="topos.storage.db.write_gate")
    # This test's own apply_all_migrations ran on the loop thread and warned.
    # Drop it, or the assertion below reports the setup rather than the lane —
    # and only intermittently, since the gate suppresses a repeat from the same
    # site for 300s, so it depended on whether another test warned there first.
    caplog.clear()

    await SignalDerivationOrchestrator().run_signal_derivation(
        [
            {
                "record_id": "r1",
                "source_id": "browser_visits",
                "event_at": "2026-08-08T20:17:00Z",
                "content": "https://example.com/loop-gate",
            }
        ],
        source_id="browser_visits",
        job_names=["statistics", "attention_triage"],
        sync_batch_id="batch-loop-gate",
    )

    loop_warnings = [
        rec.getMessage()
        for rec in caplog.records
        if "acquired on the event-loop thread" in rec.getMessage()
    ]
    assert not loop_warnings, loop_warnings
    conn.close()

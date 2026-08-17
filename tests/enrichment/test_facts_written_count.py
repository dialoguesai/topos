"""The facts job writes via FactStore inside enrich(), not write_signal_records.

records_created must surface that count (the 2026-07-27 backfill reported
{'facts': 0} while FactStore wrote 61 facts) via the _written sentinel.
"""

from __future__ import annotations

import pytest

import topos.enrichment.jobs.canonical.fact_extraction_job as fact_job_module
from topos.enrichment.orchestrator import SignalDerivationOrchestrator


@pytest.mark.asyncio
async def test_signal_derivation_reports_facts_written(monkeypatch):
    monkeypatch.setattr(fact_job_module, "get_db_connection", lambda: object())
    monkeypatch.setattr(
        "topos.features.facts.extract.extract_facts_from_batch",
        lambda conn, rows, **_: 7,
    )

    orchestrator = SignalDerivationOrchestrator()
    result = await orchestrator.run_signal_derivation(
        [{"message_id": "m1", "content": "I live in Austin"}],
        source_id="grow_journal",
        job_names=["facts"],
        sync_batch_id="batch-1",
    )

    assert result["errors"] == []
    assert result["records_created"]["facts"] == 7
    assert result["jobs_run"] == 1


@pytest.mark.asyncio
async def test_signal_derivation_facts_zero_written_stays_zero(monkeypatch):
    monkeypatch.setattr(fact_job_module, "get_db_connection", lambda: object())
    monkeypatch.setattr(
        "topos.features.facts.extract.extract_facts_from_batch",
        lambda conn, rows, **_: 0,
    )

    orchestrator = SignalDerivationOrchestrator()
    result = await orchestrator.run_signal_derivation(
        [{"message_id": "m1", "content": "hello"}],
        source_id="grow_journal",
        job_names=["facts"],
        sync_batch_id="batch-2",
    )

    assert result["records_created"]["facts"] == 0

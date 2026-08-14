"""A deferred derivation job is recorded debt, not silence.

Jobs report an unreachable provider by RETURNING ``[{"_deferred": True, ...}]``
rather than raising, and only the raise path recorded durable debt. So the case
this mechanism exists for — ingest with no model installed, install one later —
wrote nothing to ``pipeline_jobs``: the retry endpoint and the worker had
nothing to find, and the only durable trace was an ``ingest_audit`` row that
names the batch but not the job.

The retry path had the mirror-image bug: ``retry_single_derivation`` checked
only ``results["errors"]``, so a retry that deferred again reported "recovered"
with zero rows created — discharging the debt and marking the queue row done
while the data stayed missing.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from topos.core.state import get_db_connection
from topos.enrichment.derivation_recovery import (
    list_pending_derivation_retries,
    retry_single_derivation,
)
from topos.enrichment.orchestrator import SignalDerivationOrchestrator


class _DeferringJob:
    """Stands in for topics/goal_extraction with ollama down."""

    def __init__(self, name: str = "topics", error: str = "ollama_unreachable") -> None:
        self._name = name
        self._error = error
        self.enrich_calls = 0

    def get_job_name(self) -> str:
        return self._name

    def should_run(self, _messages: List[Dict[str, Any]]) -> bool:
        return True

    async def enrich(self, _messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self.enrich_calls += 1
        return [{"_deferred": True, "error": self._error}]


async def _run_deferred_batch(batch_id: str, job: _DeferringJob) -> Dict[str, Any]:
    orch = SignalDerivationOrchestrator()
    orch._signal_jobs = {job.get_job_name(): job}
    return await orch.run_signal_derivation(
        [{"message_id": "m1", "content": "hello"}],
        source_id="grow_journal",
        job_names=[job.get_job_name()],
        sync_batch_id=batch_id,
    )


@pytest.mark.asyncio
async def test_deferred_job_records_durable_debt() -> None:
    job = _DeferringJob()
    result = await _run_deferred_batch("batch-offline", job)

    assert result["deferred_jobs"] == ["topics"]
    assert result["jobs_run"] == 0

    pending = list_pending_derivation_retries(get_db_connection())
    debts = [p for p in pending if p["sync_batch_id"] == "batch-offline"]
    assert len(debts) == 1, "a deferral must leave something to re-run"
    assert debts[0]["job_name"] == "topics"
    assert debts[0]["source_id"] == "grow_journal"
    assert "ollama_unreachable" in debts[0]["error"]
    assert debts[0]["record_count"] == 1


@pytest.mark.asyncio
async def test_repeated_deferral_does_not_stack_debt() -> None:
    """Re-ingesting the same batch while still offline is one debt, not N."""
    job = _DeferringJob()
    await _run_deferred_batch("batch-repeat", job)
    await _run_deferred_batch("batch-repeat", job)

    pending = list_pending_derivation_retries(get_db_connection())
    debts = [p for p in pending if p["sync_batch_id"] == "batch-repeat"]
    assert len(debts) == 1


@pytest.mark.asyncio
async def test_retry_that_defers_again_is_not_recovered(monkeypatch) -> None:
    """The provider is still down; claiming recovery would discharge the debt."""

    monkeypatch.setattr(
        "topos.ingestion.reprocess._resolve_source_def",
        lambda source_id: object(),
    )
    monkeypatch.setattr(
        "topos.ingestion.canonical_pipeline.load_canonical_records_for_signal",
        lambda conn, source_def: [{"message_id": "m1", "content": "hello"}],
    )

    async def _deferring_run(self, records, source_id, **kwargs):
        return {
            "jobs_run": 0,
            "records_created": {},
            "errors": [],
            "deferred_jobs": ["topics"],
            "envelopes": [
                {
                    "provenance": {"job_name": "topics", "status": "deferred"},
                    "error": "ollama_unreachable",
                }
            ],
        }

    monkeypatch.setattr(
        SignalDerivationOrchestrator, "run_signal_derivation", _deferring_run
    )

    outcome = await retry_single_derivation(
        get_db_connection(),
        source_id="grow_journal",
        sync_batch_id="batch-offline",
        job_name="topics",
        record_ids=["m1"],
    )

    assert outcome["outcome"] == "still_failing"
    assert "ollama_unreachable" in outcome["error"]

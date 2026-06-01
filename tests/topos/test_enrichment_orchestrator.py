import pytest

from topos.enrichment.jobs import CANONICAL_JOBS
from topos.enrichment.orchestrator import EnrichmentOrchestrator


@pytest.mark.asyncio
async def test_enrichment_orchestrator_runs_canonical_jobs():
    orchestrator = EnrichmentOrchestrator()
    messages = [{"message_id": "m1", "content": "hello"}]
    result = await orchestrator.run_canonical(messages)

    assert result["jobs_run"] == len(CANONICAL_JOBS)
    expected_tables = {job.get_derived_table() for job in CANONICAL_JOBS}
    assert expected_tables == set(result["records_created"].keys())

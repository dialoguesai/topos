import pytest

from topos.enrichment.jobs import CANONICAL_JOBS
from topos.enrichment.orchestrator import EnrichmentOrchestrator


@pytest.mark.asyncio
async def test_enrichment_orchestrator_runs_canonical_jobs():
    orchestrator = EnrichmentOrchestrator()
    messages = [{"message_id": "m1", "content": "hello"}]

    # Not every canonical job runs on every message: the orchestrator skips any
    # job whose should_run() declines the batch (url_classification only fires
    # on browser records, for one). Expect the jobs that opted in, not all of
    # CANONICAL_JOBS.
    expected_jobs = [job for job in CANONICAL_JOBS if job.should_run(messages)]

    result = await orchestrator.run_canonical(messages)

    assert result["jobs_run"] == len(expected_jobs)
    # Jobs that write a derived table key records_created by table name; the
    # rest (statistics, facts, timeline) return "" from get_derived_table() and
    # key by job name instead.
    expected_keys = {job.get_derived_table() or job.get_job_name() for job in expected_jobs}
    assert expected_keys == set(result["records_created"].keys())

import pytest

from topos.enrichment.jobs import CANONICAL_JOBS
from topos.enrichment.orchestrator import EnrichmentOrchestrator


@pytest.mark.asyncio
async def test_enrichment_orchestrator_attempts_exactly_the_jobs_that_opted_in():
    """The orchestrator's own contract, without requiring the models to be here.

    Not every canonical job runs on every message: the orchestrator skips any
    job whose should_run() declines the batch. What it must never do is skip one
    that opted in, or swallow one that raised. Three of these jobs download a
    Hugging Face model on first use, so on a machine without them cached they
    raise and land in `errors` — which is the reported outcome, not a silent
    one. Asserting attempted-equals-opted-in holds either way; asserting
    succeeded-equals-opted-in made this test a model download in disguise, and
    it has been red on this machine for that reason.
    """
    orchestrator = EnrichmentOrchestrator()
    messages = [{"message_id": "m1", "content": "hello"}]

    expected_jobs = [job for job in CANONICAL_JOBS if job.should_run(messages)]
    expected_names = {job.get_job_name() for job in expected_jobs}

    result = await orchestrator.run_canonical(messages)

    failed = {error["job"] for error in result["errors"]}
    assert failed <= expected_names, "a job that declined the batch still ran"
    assert result["jobs_run"] + len(result["errors"]) == len(expected_jobs)
    assert len(failed) == len(result["errors"]), "a job was attempted twice"

    # Jobs that write a derived table key records_created by table name; the
    # rest (statistics, facts, timeline) return "" from get_derived_table() and
    # key by job name instead. Only the jobs that completed report a key.
    succeeded = [job for job in expected_jobs if job.get_job_name() not in failed]
    expected_keys = {job.get_derived_table() or job.get_job_name() for job in succeeded}
    assert expected_keys == set(result["records_created"].keys())


@pytest.mark.asyncio
async def test_enrichment_orchestrator_skips_jobs_that_decline_the_batch():
    """The skip path itself, on the batch almost every job declines.

    Nine of the ten canonical jobs work from the batch they are handed and
    decline an empty one. `derived_object_embeddings` works from the database
    rather than the batch, so it opts in regardless; the point of this test is
    that the other nine are skipped rather than run with nothing.
    """
    orchestrator = EnrichmentOrchestrator()
    willing = [job for job in CANONICAL_JOBS if job.should_run([])]
    assert len(willing) < len(CANONICAL_JOBS), "every job opted into an empty batch"

    result = await orchestrator.run_canonical([])

    attempted = result["jobs_run"] + len(result["errors"])
    assert attempted == len(willing)
    declined = {job.get_job_name() for job in CANONICAL_JOBS} - {job.get_job_name() for job in willing}
    assert declined & set(result["records_created"]) == set()

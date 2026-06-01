from dataclasses import replace

import pytest

from topos.ingestion.state_machine import DefaultStateMachine, IngestionJob, JobEvent, JobState


def test_state_machine_happy_path():
    sm = DefaultStateMachine()
    job = IngestionJob(job_id="job-1", dataset_id="ds", schema_id="schema")

    job = replace(job, state=sm.transition(job, JobEvent.START))
    assert job.state == JobState.RUNNING

    job = replace(job, state=sm.transition(job, JobEvent.PARSING_STARTED))
    assert job.state == JobState.PARSING

    job = replace(job, state=sm.transition(job, JobEvent.PARSING_COMPLETED))
    assert job.state == JobState.RAW_ENRICH

    job = replace(job, state=sm.transition(job, JobEvent.RAW_ENRICHED))
    assert job.state == JobState.CANONICALIZE

    job = replace(job, state=sm.transition(job, JobEvent.CANONICALIZED))
    assert job.state == JobState.CANONICAL_ENRICH

    job = replace(job, state=sm.transition(job, JobEvent.CANONICAL_ENRICHED))
    assert job.state == JobState.VECTOR_INDEX

    job = replace(job, state=sm.transition(job, JobEvent.VECTOR_INDEXED))
    assert job.state == JobState.COMPLETE


def test_state_machine_failure_and_retry():
    sm = DefaultStateMachine()
    job = IngestionJob(job_id="job-2", dataset_id="ds", schema_id="schema")

    failed_state = sm.transition(job, JobEvent.FAIL)
    assert failed_state == JobState.FAILED

    retrying_state = sm.transition(replace(job, state=failed_state), JobEvent.RETRY)
    assert retrying_state == JobState.RETRYING


def test_state_machine_invalid_transition():
    sm = DefaultStateMachine()
    job = IngestionJob(job_id="job-3", dataset_id="ds", schema_id="schema")

    with pytest.raises(ValueError):
        sm.transition(job, JobEvent.PARSING_COMPLETED)

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PARSING = "parsing"
    RAW_ENRICH = "raw_enrich"
    CANONICALIZE = "canonicalize"
    CANONICAL_ENRICH = "canonical_enrich"
    VECTOR_INDEX = "vector_index"
    COMPLETE = "complete"
    FAILED = "failed"
    RETRYING = "retrying"


class JobEvent(str, Enum):
    START = "start"
    PARSING_STARTED = "parsing_started"
    PARSING_COMPLETED = "parsing_completed"
    RAW_ENRICHED = "raw_enriched"
    CANONICALIZED = "canonicalized"
    CANONICAL_ENRICHED = "canonical_enriched"
    VECTOR_INDEXED = "vector_indexed"
    FAIL = "fail"
    RETRY = "retry"


@dataclass(frozen=True)
class IngestionJob:
    job_id: str
    dataset_id: str
    schema_id: str
    metadata: Dict[str, str] = field(default_factory=dict)
    state: JobState = JobState.QUEUED
    checkpoint_id: Optional[str] = None


class IngestionStateMachine:
    """State transition contract for ingestion jobs."""

    def transition(self, job: IngestionJob, event: JobEvent) -> JobState:
        raise NotImplementedError


class DefaultStateMachine(IngestionStateMachine):
    _transitions: Dict[JobState, Dict[JobEvent, JobState]] = {
        JobState.QUEUED: {JobEvent.START: JobState.RUNNING},
        JobState.RETRYING: {JobEvent.START: JobState.RUNNING},
        JobState.RUNNING: {JobEvent.PARSING_STARTED: JobState.PARSING},
        JobState.PARSING: {JobEvent.PARSING_COMPLETED: JobState.RAW_ENRICH},
        JobState.RAW_ENRICH: {JobEvent.RAW_ENRICHED: JobState.CANONICALIZE},
        JobState.CANONICALIZE: {JobEvent.CANONICALIZED: JobState.CANONICAL_ENRICH},
        JobState.CANONICAL_ENRICH: {JobEvent.CANONICAL_ENRICHED: JobState.VECTOR_INDEX},
        JobState.VECTOR_INDEX: {JobEvent.VECTOR_INDEXED: JobState.COMPLETE},
    }

    def transition(self, job: IngestionJob, event: JobEvent) -> JobState:
        if event == JobEvent.FAIL:
            return JobState.FAILED
        if event == JobEvent.RETRY and job.state == JobState.FAILED:
            return JobState.RETRYING
        next_state = self._transitions.get(job.state, {}).get(event)
        if not next_state:
            raise ValueError(f"Invalid transition: {job.state} + {event}")
        return next_state

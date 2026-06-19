"""Batching limits for enrichment jobs."""

from unittest.mock import MagicMock

import pytest

from topos.enrichment.jobs.canonical._batch_limits import MAX_JOB_MESSAGES
from topos.enrichment.jobs.canonical.dimension_summary_job import _truncate_records_for_summary
from topos.enrichment.jobs.canonical.topics_job import TopicsJob
from topos.engine.tasks import ProcessingResult

pytestmark = pytest.mark.gap


def test_truncate_records_for_summary_respects_caps() -> None:
    records = [{"content": "x" * 1000} for _ in range(200)]
    out = _truncate_records_for_summary(records)
    assert len(out) <= 50
    assert sum(len(str(r.get("content", ""))) for r in out) <= 12_000


@pytest.mark.asyncio
async def test_topics_job_caps_message_count(monkeypatch) -> None:
    job = TopicsJob(engine=MagicMock())
    calls = {"n": 0}

    async def _fake_run(*_args, **_kwargs):
        calls["n"] += 1
        return ProcessingResult(
            task_id="t1",
            status="completed",
            output={"topics": [{"label": "AI", "confidence": 0.8}], "model": "llama3.2:3b"},
        )

    monkeypatch.setattr("topos.enrichment.jobs.canonical.topics_job.run_engine_task", _fake_run)
    messages = [
        {"message_id": f"m{i}", "content": f"text {i}", "source_id": "chatgpt"}
        for i in range(MAX_JOB_MESSAGES + 50)
    ]
    await job.enrich(messages)
    assert calls["n"] == MAX_JOB_MESSAGES

"""
Gap: Ollama — sanitization-only → topics/summary/goals produce signal rows
Sprint: EN-P2-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

from unittest.mock import MagicMock

import pytest

from topos.enrichment.jobs.canonical.topics_job import TopicsJob
from topos.engine.tasks import ProcessingResult

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_ollama_topics_job(monkeypatch) -> None:
    job = TopicsJob(engine=MagicMock())
    mock_result = ProcessingResult(
        task_id="t1",
        status="completed",
        output={"topics": [{"label": "AI", "confidence": 0.8}], "model": "llama3.2:3b"},
    )

    async def _fake_run(*_args, **_kwargs):
        return mock_result

    monkeypatch.setattr("topos.enrichment.jobs.canonical.topics_job.run_engine_task", _fake_run)
    records = await job.enrich([{"message_id": "m1", "content": "talk about AI", "source_id": "chatgpt"}])
    assert records[0]["topic"] == "AI"


@pytest.mark.asyncio
async def test_ollama_deferred(monkeypatch) -> None:
    job = TopicsJob(engine=MagicMock())
    mock_result = ProcessingResult(task_id="t1", status="deferred", output={"error": "ollama_unreachable"})

    async def _fake_deferred(*_args, **_kwargs):
        return mock_result

    monkeypatch.setattr("topos.enrichment.jobs.canonical.topics_job.run_engine_task", _fake_deferred)
    records = await job.enrich([{"message_id": "m1", "content": "hello", "source_id": "chatgpt"}])
    assert records[0]["_deferred"] is True

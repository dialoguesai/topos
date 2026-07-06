"""
Gap: Entities — stub → HF NER rows in message_entities
Sprint: EN-P2-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

from unittest.mock import MagicMock

import pytest

from topos.enrichment.jobs.canonical.entities_job import EntitiesJob
from topos.engine.tasks import ProcessingResult

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_entities_job_produces_ner_rows(monkeypatch) -> None:
    job = EntitiesJob(engine=MagicMock())
    # Batched contract: entity_extraction_batch returns per-message items.
    mock_result = ProcessingResult(
        task_id="t1",
        status="completed",
        output={
            "items": [
                {
                    "id": "m1",
                    "entities": [
                        {"entity_text": "Alice", "entity_type": "PER", "confidence": 0.9}
                    ],
                }
            ],
            "model": "dslim/bert-base-NER",
            "provider": "huggingface",
        },
    )
    async def _fake_run(*_args, **_kwargs):
        return mock_result

    monkeypatch.setattr(
        "topos.enrichment.jobs.canonical.entities_job.run_engine_task",
        _fake_run,
    )
    records = await job.enrich([{"message_id": "m1", "content": "Alice went home", "source_id": "chatgpt"}])
    assert len(records) == 1
    assert records[0]["entity_text"] == "Alice"
    assert records[0]["provider"] == "huggingface"
    assert records[0]["model"]

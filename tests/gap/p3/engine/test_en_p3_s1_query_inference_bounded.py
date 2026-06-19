"""GT-EN-P3-S1-04: query_inference bounded context."""

from unittest.mock import MagicMock

import pytest

from topos.engine.tasks import ProcessingResult
from topos.query.inference import build_inference_context_packet, run_query_inference

pytestmark = pytest.mark.gap


def test_query_inference_truncates_context_and_calls_engine_once() -> None:
    huge = {"data": "x" * 10_000}
    bounded = build_inference_context_packet(huge, max_chars=500)
    assert len(bounded["context"]) <= 500
    assert bounded["truncated"] is True

    mock_engine = MagicMock()
    mock_engine.run.return_value = ProcessingResult(
        task_id="task-1",
        status="completed",
        output={"answer": "yes", "confidence": 0.8},
    )
    run_query_inference(
        query_text="Am I free Thursday?",
        context_packet=huge,
        scope_id="availability:read",
        engine=mock_engine,
        max_chars=500,
    )
    assert mock_engine.run.call_count == 1

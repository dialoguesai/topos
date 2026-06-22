"""Engine privacy_disclosure task tests."""

from unittest.mock import patch

import pytest

from topos.engine import Engine
from topos.engine.backends.huggingface import HuggingFaceAdapter
from topos.engine.tasks import ModelRequest, ProcessingTask


def test_hf_privacy_disclosure_subtype():
    adapter = HuggingFaceAdapter()
    with patch(
        "topos.sanitization.privacy_filter.redact_privacy_batch",
        return_value={
            "items": [{"id": "1", "text": "Contact [EMAIL]"}],
            "model": "openai/privacy-filter",
            "privacy_layer_version": "1",
            "status": "ok",
        },
    ) as mock_redact:
        out = adapter.run_inference(
            {"items": [{"id": "1", "text": "Contact alice@example.com"}]},
            config={"subtype": "privacy_disclosure", "model": "openai/privacy-filter"},
        )
    mock_redact.assert_called_once()
    assert out["items"][0]["text"] == "Contact [EMAIL]"


def test_engine_run_privacy_disclosure_task():
    engine = Engine()
    task = ProcessingTask(
        id="privacy_1",
        type="enrichment",
        subtype="privacy_disclosure",
        source_id="conversation_messages",
        record_ids=["m1"],
        input={"items": [{"id": "m1", "text": "Email me at bob@test.com"}]},
        model_request=ModelRequest(provider="huggingface", model="openai/privacy-filter"),
    )
    with patch(
        "topos.sanitization.privacy_filter.apply_text_transform_with_privacy_filter",
        return_value="Email me at [EMAIL]",
    ):
        with patch("topos.sanitization.privacy_filter.privacy_filter_available", return_value=True):
            result = engine.run(task)
    assert result.status == "completed"
    assert result.output["items"][0]["text"] == "Email me at [EMAIL]"

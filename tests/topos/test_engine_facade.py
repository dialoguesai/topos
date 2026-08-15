"""Sprint 01/02: Engine facade tests (run valid/invalid task, pipeline, no HF/torch, registry)."""

import pytest

from topos.engine import Engine, ProcessingTask, ProcessingResult, ModelRequest
from topos.enrichment.models.registry import ModelRegistry


def test_engine_run_valid_minimal_task():
    """Engine.run(task) returns ProcessingResult for a valid minimal task."""
    # Use provider that returns stub so test passes without torch/transformers
    task = ProcessingTask(
        id="task_123",
        type="enrichment",
        subtype="emotion_classification",
        input={"url": "https://example.com", "title": "Example"},
        model_request=ModelRequest(provider="stub"),
    )
    engine = Engine()
    result = engine.run(task)
    assert isinstance(result, ProcessingResult)
    assert result.task_id == "task_123"
    assert result.status == "completed"
    assert "status" in result.output
    assert result.output["status"] == "stub"


def test_engine_run_invalid_task_missing_id():
    """Engine.run(task) returns structured error result when id is missing."""
    task = ProcessingTask(
        id="",
        type="enrichment",
        input={},
        model_request=ModelRequest(provider="huggingface"),
    )
    engine = Engine()
    result = engine.run(task)
    assert result.status == "failed"
    assert result.error is not None
    assert "id" in result.error.lower() or "required" in result.error.lower()


def test_engine_run_invalid_task_missing_type():
    """Engine.run(task) returns error result when type is missing."""
    task = ProcessingTask(
        id="task_1",
        type="",
        input={},
        model_request=ModelRequest(provider="huggingface"),
    )
    engine = Engine()
    result = engine.run(task)
    assert result.status == "failed"
    assert result.error is not None


def test_engine_run_invalid_task_missing_model_request():
    """Engine.run(task) returns error when model_request is missing provider."""
    # Pydantic will require model_request; use invalid provider
    task = ProcessingTask(
        id="task_1",
        type="enrichment",
        input={},
        model_request=ModelRequest(provider=""),  # invalid
    )
    engine = Engine()
    result = engine.run(task)
    # Validator checks provider non-empty
    assert result.status == "failed"
    assert result.error is not None


def test_engine_run_triggers_intake_and_formatter():
    """Run valid task; result has provenance or execution_meta (pipeline smoke)."""
    task = ProcessingTask(
        id="t1",
        type="enrichment",
        subtype="emotion_classification",
        source_id="browser_visits",
        record_ids=["r1", "r2"],
        input={"url": "https://x.com"},
        model_request=ModelRequest(provider="stub"),
    )
    engine = Engine()
    result = engine.run(task)
    assert result.task_id == "t1"
    assert result.status == "completed"
    assert result.execution_meta is not None
    assert result.execution_meta.provider == "stub"
    assert result.execution_meta.duration_ms is not None
    assert result.provenance is not None
    assert result.provenance.source_id == "browser_visits"
    assert result.provenance.record_ids == ["r1", "r2"]


def test_registry_get_model_for_task():
    """Registry.get_model_for_task returns model spec when registered (Sprint 02)."""
    reg = ModelRegistry()
    reg.register_model(
        model_id="m1",
        model_name="Website Classifier",
        model_version="1",
        model_type="text-classification",
        task_name="emotion_classification",
        huggingface_path="SamLowe/roberta-base-go_emotions",
        is_preferred=True,
    )
    spec = reg.get_model_for_task("enrichment", "emotion_classification")
    assert spec is not None
    assert spec.get("huggingface_path") == "SamLowe/roberta-base-go_emotions"
    unknown = reg.get_model_for_task("enrichment", "unknown_subtype")
    assert unknown is None


def test_engine_submit_returns_handle():
    """Engine.submit(task) returns TaskHandle; task is enqueued (Sprint 05)."""
    from topos.engine import Engine, ProcessingTask, ModelRequest
    task = ProcessingTask(
        id="sub_1",
        type="enrichment",
        input={},
        model_request=ModelRequest(provider="stub"),
    )
    engine = Engine()
    handle = engine.submit(task)
    assert handle is not None
    assert handle.task_id == "sub_1"
    assert handle.get_status() == "pending"
    ran = engine.run_worker_once()
    assert ran is True
    assert handle.get_status() == "completed"
    result = handle.get_result()
    assert result is not None
    assert result.task_id == "sub_1"


def test_engine_run_unchanged_after_submit():
    """Engine.run() still works synchronously (regression)."""
    from topos.engine import Engine, ProcessingTask, ModelRequest
    task = ProcessingTask(id="r1", type="enrichment", input={}, model_request=ModelRequest(provider="stub"))
    engine = Engine()
    result = engine.run(task)
    assert result.status == "completed"
    assert result.task_id == "r1"


def test_engine_importable_without_transformers_torch():
    """Engine package imports and run() works without transformers/torch in engine core."""
    # Use provider that does not trigger HF adapter (stub path)
    from topos.engine import Engine
    from topos.engine.tasks import ProcessingTask, ModelRequest
    task = ProcessingTask(
        id="no_hf",
        type="enrichment",
        input={},
        model_request=ModelRequest(provider="stub"),
    )
    engine = Engine()
    result = engine.run(task)
    assert result.task_id == "no_hf"
    assert result.status == "completed"

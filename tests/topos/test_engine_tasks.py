"""Sprint 01: Task contract tests (ProcessingTask, ProcessingResult)."""

import json
import pytest

from topos.engine.tasks import (
    ExecutionSpec,
    ModelRequest,
    ProcessingResult,
    ProcessingTask,
    Provenance,
    build_task,
)


def test_task_roundtrip():
    """Build ProcessingTask from dict; serialize to JSON/dict; deserialize; assert key fields."""
    data = {
        "id": "task_1",
        "type": "enrichment",
        "subtype": "emotion_classification",
        "source_id": "browser_visits",
        "record_ids": ["r1"],
        "input": {"url": "https://example.com", "title": "Example"},
        "model_request": {"provider": "huggingface", "model": "SamLowe/roberta-base-go_emotions"},
        "execution": {"mode": "sync", "priority": 100},
    }
    task = ProcessingTask.model_validate(data)
    out = task.model_dump_json_roundtrip()
    assert out["id"] == "task_1"
    assert out["type"] == "enrichment"
    assert out["subtype"] == "emotion_classification"
    assert out["input"]["url"] == "https://example.com"
    assert out["model_request"]["provider"] == "huggingface"
    # JSON round-trip
    json_str = task.model_dump_json()
    task2 = ProcessingTask.model_validate_json(json_str)
    assert task2.id == task.id
    assert task2.type == task.type


def test_result_roundtrip():
    """ProcessingResult: serialize to dict/JSON and deserialize."""
    result = ProcessingResult(
        task_id="task_1",
        status="completed",
        output={"category": "news", "confidence": 0.9},
        output_type="json",
        confidence=0.9,
        provenance=Provenance(source_id="browser_visits", record_ids=["r1"]),
        execution_meta={"provider": "huggingface", "model": "roberta-base-go_emotions", "duration_ms": 100, "cache_hit": False},
    )
    out = result.model_dump_json_roundtrip()
    assert out["task_id"] == "task_1"
    assert out["status"] == "completed"
    assert out["output"]["category"] == "news"
    assert out["confidence"] == 0.9
    json_str = result.model_dump_json()
    result2 = ProcessingResult.model_validate_json(json_str)
    assert result2.task_id == result.task_id
    assert result2.status == result.status


def test_processing_task_has_prd_fields():
    """ProcessingTask includes all PRD §6.1 fields."""
    task = ProcessingTask(
        id="t1",
        type="enrichment",
        model_request=ModelRequest(provider="huggingface"),
        subtype="emotion_classification",
        source_id="messages",
        record_ids=["msg_1"],
        input={"url": "https://x.com"},
        execution=ExecutionSpec(mode="async", priority=50, batch_key="k"),
        options={"store_result": True, "apply_fisher_filter": False},
        requested_by={"user_id": "u1", "origin": "write_event"},
        created_at="2026-03-18T12:00:00Z",
    )
    d = task.model_dump()
    assert "id" in d and "type" in d and "subtype" in d and "source_id" in d
    assert "record_ids" in d and "input" in d and "model_request" in d
    assert "execution" in d and "options" in d and "requested_by" in d and "created_at" in d
    assert d["execution"]["mode"] == "async"
    assert d["execution"]["priority"] == 50


def test_processing_result_has_prd_fields():
    """ProcessingResult includes all PRD §6.2 fields."""
    result = ProcessingResult(
        task_id="t1",
        status="completed",
        output={},
        output_type="json",
        confidence=0.88,
        provenance=Provenance(source_id="messages", record_ids=["msg_1"]),
        execution_meta={"provider": "ollama", "model": "llama3.1", "duration_ms": 1920, "cache_hit": True},
        error=None,
    )
    d = result.model_dump()
    assert "task_id" in d and "status" in d and "output" in d and "output_type" in d
    assert "confidence" in d and "provenance" in d and "execution_meta" in d and "error" in d


def test_build_task_helper():
    """build_task() creates a valid task with defaults."""
    task = build_task(
        "tid",
        "enrichment",
        ModelRequest(provider="huggingface", model="x"),
        subtype="emotion_classification",
        source_id="browser_visits",
        record_ids=["r1"],
        input_data={"url": "https://y.com"},
    )
    assert task.id == "tid"
    assert task.type == "enrichment"
    assert task.subtype == "emotion_classification"
    assert task.model_request.provider == "huggingface"
    assert task.execution.mode == "sync"

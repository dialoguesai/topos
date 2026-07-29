from __future__ import annotations

import json

from topos.engine.backends.ollama import OllamaAdapter
from topos.engine.backends.openai_compatible import OpenAICompatibleAdapter
from topos.engine.engine import Engine
from topos.engine.tasks import ModelRequest, ProcessingTask, RequestedBy
from topos.engine.usage_observation import (
    emit_engine_llm_usage_observation,
    resolve_llm_usage_purpose,
)


class _FakeUrlOpen:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_resolve_llm_usage_purpose_maps_enrichment_and_query():
    assert (
        resolve_llm_usage_purpose(task_type="enrichment", subtype="topic_extraction")
        == "ingestion_pipeline"
    )
    assert resolve_llm_usage_purpose(subtype="query_inference") == "user_request"
    assert (
        resolve_llm_usage_purpose(origin="ingestion_pipeline", subtype="query_inference")
        == "ingestion_pipeline"
    )
    assert (
        resolve_llm_usage_purpose(subtype="query_inference", source_id="cluster_labeler")
        == "ingestion_pipeline"
    )
    assert (
        resolve_llm_usage_purpose(
            subtype="query_inference", source_id="conversation_context"
        )
        == "ingestion_pipeline"
    )
    assert resolve_llm_usage_purpose(subtype="goal_extraction") == "ingestion_pipeline"


def test_ollama_generate_returns_usage(monkeypatch):
    adapter = OllamaAdapter(base_url="http://localhost:11434")

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        return _FakeUrlOpen(
            {
                "response": '{"topics":[]}',
                "prompt_eval_count": 12,
                "eval_count": 8,
            }
        )

    monkeypatch.setattr("topos.engine.backends.ollama.urllib.request.urlopen", fake_urlopen)
    result = adapter._generate("llama3.2:3b", "hello")
    assert result["text"] == '{"topics":[]}'
    assert result["usage"]["prompt_tokens"] == 12
    assert result["usage"]["completion_tokens"] == 8
    assert result["usage"]["total_tokens"] == 20


def test_openai_compatible_chat_completion_returns_usage(monkeypatch):
    adapter = OpenAICompatibleAdapter(api_key="sk-test", default_model="gpt-4o-mini")

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        return _FakeUrlOpen(
            {
                "choices": [{"message": {"content": '{"topics":[]}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )

    monkeypatch.setattr(
        "topos.engine.backends.openai_compatible.urllib.request.urlopen",
        fake_urlopen,
    )
    result = adapter._chat_completion(model="gpt-4o-mini", prompt="hello")
    assert result["text"] == '{"topics":[]}'
    assert result["usage"]["total_tokens"] == 15


def test_engine_run_populates_execution_meta_tokens_and_emits(monkeypatch):
    emitted = []

    class _Adapter:
        def run_inference(self, payload, config=None):  # noqa: ARG002
            return {
                "topics": [{"label": "AI", "confidence": 0.9}],
                "model": "llama3.2:3b",
                "usage": {"prompt_tokens": 11, "completion_tokens": 9, "total_tokens": 20},
            }

    monkeypatch.setattr("topos.engine.engine.get_adapter_for_task", lambda task: _Adapter())
    monkeypatch.setattr(
        "topos.engine.usage_observation.emit_engine_llm_usage_observation",
        lambda **kwargs: emitted.append(kwargs) or {"ok": True},
    )

    engine = Engine()
    result = engine.run(
        ProcessingTask(
            id="task-1",
            type="enrichment",
            subtype="topic_extraction",
            input={"content": "hello"},
            model_request=ModelRequest(provider="ollama", model="llama3.2:3b"),
            requested_by=RequestedBy(origin="ingestion_pipeline"),
        )
    )
    assert result.status == "completed"
    assert result.execution_meta is not None
    assert result.execution_meta.total_tokens == 20
    assert result.execution_meta.prompt_tokens == 11
    assert len(emitted) == 1
    assert emitted[0]["subtype"] == "topic_extraction"
    assert emitted[0]["usage"]["total_tokens"] == 20


def test_emit_engine_llm_usage_observation_writes_local_and_enqueues(monkeypatch):
    local_writes = []
    enqueued = []

    class _Client:
        def enqueue_unsolicited_message_threadsafe(self, message):
            enqueued.append(message)

    monkeypatch.setattr(
        "topos.engine.usage_observation._record_local_usage_best_effort",
        lambda **kwargs: local_writes.append(kwargs),
    )

    class _State:
        control_plane_client = _Client()

    monkeypatch.setattr("topos.core.state.control_plane_client", _State.control_plane_client, raising=False)
    # Patch the import path used inside emit_engine_llm_usage_observation
    import topos.core.state as engine_state

    monkeypatch.setattr(engine_state, "control_plane_client", _State.control_plane_client)

    envelope = emit_engine_llm_usage_observation(
        task_id="t1",
        task_type="enrichment",
        subtype="goal_extraction",
        provider="ollama",
        model="llama3.2:3b",
        usage={"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
        origin="ingestion_pipeline",
    )
    assert envelope is not None
    assert envelope["source"] == "ingestion_pipeline"
    assert envelope["quantity"] == 10
    assert envelope["metadata"]["purpose"] == "ingestion_pipeline"
    assert len(local_writes) == 1
    assert local_writes[0]["source"] == "ingestion_pipeline"
    assert local_writes[0]["usage"]["total_tokens"] == 10
    assert len(enqueued) == 1
    assert enqueued[0]["type"] == "usage_observation"
    assert enqueued[0]["payload"]["metadata"]["subtype"] == "goal_extraction"


def test_control_plane_client_threadsafe_enqueue():
    from topos.control_plane_client import ControlPlaneClient

    async def _handler(_msg):
        return None

    client = ControlPlaneClient("ws://localhost/ws", "key", _handler)
    client.enqueue_unsolicited_message_threadsafe(
        {"id": "1", "type": "usage_observation", "payload": {"metric_key": "llm_tokens"}}
    )
    assert len(client._sync_outbox) == 1
    assert client._sync_outbox[0]["type"] == "usage_observation"

"""Tests for the Enrichment Lab HF model resolver (playground entry point)."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from topos.enrichment_lab import model_resolve


@pytest.fixture(autouse=True)
def _clear_resolver_cache():
    model_resolve.clear_cache()
    yield
    model_resolve.clear_cache()


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _hub_payload(**overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": "org/model",
        "pipeline_tag": "text-classification",
        "library_name": "transformers",
        "downloads": 1234,
        "likes": 56,
        "gated": False,
        "private": False,
        "siblings": [
            {"rfilename": "model.safetensors", "size": 250_000_000},
            {"rfilename": "config.json", "size": 900},
        ],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# normalize_model_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("org/model", "org/model"),
        ("  org/model  ", "org/model"),
        ("hf:org/model", "org/model"),
        ("https://huggingface.co/org/model", "org/model"),
        ("https://huggingface.co/org/model/tree/main", "org/model"),
        ("huggingface.co/SamLowe/roberta-base-go_emotions", "SamLowe/roberta-base-go_emotions"),
        # Legacy root-level ids (no org prefix) are valid hub ids.
        ("distilbert-base-uncased-finetuned-sst-2-english", "distilbert-base-uncased-finetuned-sst-2-english"),
        ("gpt2", "gpt2"),
        ("", None),
        ("bad id/with spaces", None),
    ],
)
def test_normalize_model_id(raw, expected):
    assert model_resolve.normalize_model_id(raw) == expected


# ---------------------------------------------------------------------------
# task -> job compatibility
# ---------------------------------------------------------------------------


def test_compatible_jobs_text_classification():
    jobs = model_resolve.compatible_jobs_for_pipeline_tag("text-classification")
    by_id = {j["job_id"]: j for j in jobs}
    assert by_id["emo_27"]["match"] == "exact"
    assert by_id["sentiment"]["match"] == "exact"
    # exact matches sort first
    assert jobs[0]["match"] == "exact"


def test_compatible_jobs_sentence_similarity_maps_to_embeddings():
    jobs = model_resolve.compatible_jobs_for_pipeline_tag("sentence-similarity")
    assert [j["job_id"] for j in jobs] == ["embeddings"]
    assert jobs[0]["match"] == "compatible"


def test_compatible_jobs_token_classification():
    jobs = model_resolve.compatible_jobs_for_pipeline_tag("token-classification")
    assert [j["job_id"] for j in jobs] == ["entities"]


def test_compatible_jobs_unknown_tag_empty():
    assert model_resolve.compatible_jobs_for_pipeline_tag("text-generation") == []
    assert model_resolve.compatible_jobs_for_pipeline_tag(None) == []


def test_task_compatibility_judgments():
    assert model_resolve.task_compatibility("emo_27", "text-classification") is True
    assert model_resolve.task_compatibility("emo_27", "token-classification") is False
    assert model_resolve.task_compatibility("embeddings", "sentence-similarity") is True
    # Unknown tag or job without hf_task -> None (never block)
    assert model_resolve.task_compatibility("emo_27", None) is None
    assert model_resolve.task_compatibility("topics", "text-classification") is None


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------


def test_resolve_model_ok(monkeypatch):
    def fake_get(url, **kwargs):
        assert "org/model" in url
        return _FakeResponse(200, _hub_payload())

    monkeypatch.setattr("httpx.get", fake_get)
    result = model_resolve.resolve_model("hf:org/model")
    assert result["status"] == "ok"
    assert result["model_id"] == "org/model"
    assert result["pipeline_tag"] == "text-classification"
    assert result["size_bytes"] == 250_000_000
    assert result["size_human"] is not None
    assert any(j["job_id"] == "emo_27" for j in result["compatible_jobs"])
    assert result["hub_reachable"] is True


def test_resolve_model_gated_warning(monkeypatch):
    monkeypatch.setattr(
        "httpx.get", lambda url, **kw: _FakeResponse(200, _hub_payload(gated="manual"))
    )
    result = model_resolve.resolve_model("org/model")
    assert result["status"] == "ok"
    assert result["gated"] is True
    assert any("gated" in w for w in result["warnings"])


def test_resolve_model_not_found(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda url, **kw: _FakeResponse(404))
    result = model_resolve.resolve_model("org/missing")
    assert result["status"] == "not_found"
    assert result["warnings"]


def test_resolve_model_unauthorized(monkeypatch):
    monkeypatch.setattr("httpx.get", lambda url, **kw: _FakeResponse(401))
    result = model_resolve.resolve_model("org/private-model")
    assert result["status"] == "unauthorized"
    assert result["gated"] is True


def test_resolve_model_network_error_degrades(monkeypatch):
    def boom(url, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr("httpx.get", boom)
    result = model_resolve.resolve_model("org/model")
    assert result["status"] == "unreachable"
    assert result["hub_reachable"] is False
    # Format was still validated
    assert result["model_id"] == "org/model"


def test_resolve_model_invalid_format_short_circuits(monkeypatch):
    def fail(url, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("hub should not be queried for invalid ids")

    monkeypatch.setattr("httpx.get", fail)
    result = model_resolve.resolve_model("bad id/with spaces")
    assert result["status"] == "invalid"
    assert result["model_id"] is None


def test_resolve_model_uses_cache(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, **kwargs):
        calls["n"] += 1
        return _FakeResponse(200, _hub_payload())

    monkeypatch.setattr("httpx.get", fake_get)
    first = model_resolve.resolve_model("org/model")
    second = model_resolve.resolve_model("org/model")
    assert calls["n"] == 1
    assert first["status"] == second["status"] == "ok"


def test_resolve_model_no_task_warns(monkeypatch):
    monkeypatch.setattr(
        "httpx.get",
        lambda url, **kw: _FakeResponse(200, _hub_payload(pipeline_tag=None)),
    )
    result = model_resolve.resolve_model("org/model")
    assert result["status"] == "ok"
    assert result["compatible_jobs"] == []
    assert any("task" in w for w in result["warnings"])


def test_resolve_model_size_prefers_primary_weight_format(monkeypatch):
    """Duplicate weight formats must not inflate the size estimate."""
    siblings = [
        {"rfilename": "model.safetensors", "size": 268_000_000},
        {"rfilename": "pytorch_model.bin", "size": 268_000_000},
        {"rfilename": "tf_model.h5", "size": 268_000_000},
        {"rfilename": "onnx/model.onnx", "size": 268_000_000},
    ]
    monkeypatch.setattr(
        "httpx.get", lambda url, **kw: _FakeResponse(200, _hub_payload(siblings=siblings))
    )
    result = model_resolve.resolve_model("org/model")
    assert result["size_bytes"] == 268_000_000

    model_resolve.clear_cache()
    # No safetensors: fall back to the torch bin size.
    monkeypatch.setattr(
        "httpx.get",
        lambda url, **kw: _FakeResponse(200, _hub_payload(siblings=siblings[1:])),
    )
    result = model_resolve.resolve_model("org/model")
    assert result["size_bytes"] == 268_000_000


def test_format_size():
    assert model_resolve.format_size(None) is None
    assert model_resolve.format_size(0) is None
    assert model_resolve.format_size(500) == "500 B"
    assert model_resolve.format_size(250_000_000) == "238.4 MB"

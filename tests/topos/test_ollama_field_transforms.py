"""Ollama-backed field transforms (mocked HTTP)."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from topos.config.sanitization_ollama import SanitizationOllamaEffective
from topos.sanitization.ollama_transforms import OLLAMA_TRANSFORM_IDS, apply_text_transform_with_ollama


def _fake_effective(*, auto_pull: bool = True) -> SanitizationOllamaEffective:
    return SanitizationOllamaEffective(
        enabled=True,
        host="http://127.0.0.1:11434",
        default_model="llama3.2",
        timeout_sec=60.0,
        auto_pull=auto_pull,
        max_input_chars=8000,
        models={tid: "llama3.2" for tid in OLLAMA_TRANSFORM_IDS},
    )


def test_apply_pii_redaction_parses_ollama_chat_response():
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {"message": {"content": "Hello [NAME]"}}

    with patch("httpx.Client") as client_cls:
        instance = MagicMock()
        instance.__enter__.return_value = instance
        instance.post.return_value = fake_resp
        client_cls.return_value = instance

        out = apply_text_transform_with_ollama("Hello Alice", "pii_redaction", {}, effective=_fake_effective())
        assert out == "Hello [NAME]"
        instance.post.assert_called_once()
        url = instance.post.call_args[0][0]
        assert url.endswith("/api/chat")
        body = instance.post.call_args[1]["json"]
        assert body["model"]
        assert body["stream"] is False
        assert len(body["messages"]) == 2


def test_raw_to_sentiment_falls_back_to_truncated_string_on_bad_json():
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {"message": {"content": "not json"}}

    with patch("httpx.Client") as client_cls:
        instance = MagicMock()
        instance.__enter__.return_value = instance
        instance.post.return_value = fake_resp
        client_cls.return_value = instance

        out = apply_text_transform_with_ollama("I love this", "raw_to_sentiment", {}, effective=_fake_effective())
        assert out == "not json"


def test_unknown_transform_raises():
    with pytest.raises(ValueError, match="not handled"):
        apply_text_transform_with_ollama("x", "rolling_window_days", {}, effective=_fake_effective())


def test_auto_pull_retries_chat_after_model_not_found():
    miss = MagicMock()
    miss.status_code = 404
    miss.json.return_value = {"error": "model 'llama3.1' not found"}

    ok = MagicMock()
    ok.status_code = 200
    ok.raise_for_status = MagicMock()
    ok.json.return_value = {"message": {"content": "Hello [NAME]"}}

    with patch("httpx.Client") as client_cls:
        instance = MagicMock()
        instance.__enter__.return_value = instance
        instance.post.side_effect = [miss, ok]
        client_cls.return_value = instance

        with patch("topos.engine.backends.ollama.OllamaAdapter") as ad_cls:
            ad_cls.return_value.ensure_model.return_value = True
            out = apply_text_transform_with_ollama("Hello Alice", "pii_redaction", {}, effective=_fake_effective())
            assert out == "Hello [NAME]"
            assert instance.post.call_count == 2
            ad_cls.return_value.ensure_model.assert_called_once_with("llama3.2")


def test_auto_pull_disabled_does_not_call_ensure_model():
    req = httpx.Request("POST", "http://127.0.0.1:11434/api/chat")
    miss_resp = httpx.Response(404, json={"error": "model 'x' not found"}, request=req)

    with patch("httpx.Client") as client_cls:
        instance = MagicMock()
        instance.__enter__.return_value = instance
        instance.post.return_value = miss_resp
        client_cls.return_value = instance

        with patch("topos.engine.backends.ollama.OllamaAdapter") as ad_cls:
            with pytest.raises(httpx.HTTPStatusError):
                apply_text_transform_with_ollama(
                    "Hello Alice",
                    "pii_redaction",
                    {},
                    effective=_fake_effective(auto_pull=False),
                )
            ad_cls.assert_not_called()

"""Prepaid-wallet probe for Topos-hosted ingest LLMs."""

from __future__ import annotations

import json
import urllib.error

import pytest

from topos.engine.backends.openai_compatible import OpenAICompatibleAdapter
from topos.engine.hosted_llm_wallet import (
    INSUFFICIENT_CREDITS,
    hosted_llm_wallet_allows,
    ingest_uses_hosted_llm,
    reset_hosted_llm_wallet_cache,
)


class _FakeUrlOpen:
    def __init__(self, payload: dict, *, raises: BaseException | None = None):
        self._payload = payload
        self._raises = raises

    def __enter__(self):
        if self._raises is not None:
            raise self._raises
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


@pytest.fixture(autouse=True)
def _reset_wallet_cache() -> None:
    reset_hosted_llm_wallet_cache()
    yield
    reset_hosted_llm_wallet_cache()


def _configure_cp(monkeypatch: pytest.MonkeyPatch) -> None:
    from topos.config.settings import settings

    monkeypatch.setattr(settings, "topos_key", "eng_test_key", raising=False)
    monkeypatch.setattr(
        settings,
        "topos_control_plane_url",
        "wss://cp.example/ws/engine",
        raising=False,
    )


def test_probe_allows_when_wallet_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_cp(monkeypatch)
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        calls["n"] += 1
        return _FakeUrlOpen({"wallet_balance_usd": 5.0})

    monkeypatch.setattr(
        "topos.engine.hosted_llm_wallet.urllib.request.urlopen", fake_urlopen
    )
    assert hosted_llm_wallet_allows() is True
    assert hosted_llm_wallet_allows() is True
    assert calls["n"] == 1, "TTL cache should skip a second probe"


def test_probe_denies_when_wallet_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_cp(monkeypatch)
    monkeypatch.setattr(
        "topos.engine.hosted_llm_wallet.urllib.request.urlopen",
        lambda req, timeout=None: _FakeUrlOpen({"wallet_balance_usd": 0}),  # noqa: ARG005
    )
    assert hosted_llm_wallet_allows() is False


def test_probe_fails_open_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from topos.config.settings import settings

    monkeypatch.setattr(settings, "topos_key", "", raising=False)
    monkeypatch.setattr(settings, "topos_control_plane_url", "", raising=False)
    called = {"n": 0}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        called["n"] += 1
        raise AssertionError("must not probe without a key")

    monkeypatch.setattr(
        "topos.engine.hosted_llm_wallet.urllib.request.urlopen", fake_urlopen
    )
    assert hosted_llm_wallet_allows() is True
    assert called["n"] == 0


def test_failed_probe_reuses_last_successful_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_cp(monkeypatch)
    responses: list[object] = [
        _FakeUrlOpen({"wallet_balance_usd": 0}),
        urllib.error.URLError("down"),
    ]

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        item = responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(
        "topos.engine.hosted_llm_wallet.urllib.request.urlopen", fake_urlopen
    )
    assert hosted_llm_wallet_allows() is False
    assert hosted_llm_wallet_allows(force=True) is False


def test_ingest_uses_hosted_llm_when_signal_extraction_is_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "topos.config.signal_extraction.get_signal_extraction_provider",
        lambda: "platform",
    )
    monkeypatch.setattr(
        "topos.core.state.get_db_connection",
        lambda: object(),
    )
    assert ingest_uses_hosted_llm() is True


def test_gated_adapter_defers_when_wallet_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "topos.engine.hosted_llm_wallet.hosted_llm_wallet_allows",
        lambda force=False: False,
    )
    called = {"n": 0}

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        called["n"] += 1
        raise AssertionError("hosted call must not reach the provider")

    monkeypatch.setattr(
        "topos.engine.backends.openai_compatible.urllib.request.urlopen",
        fake_urlopen,
    )
    adapter = OpenAICompatibleAdapter(
        api_key="sk-test", default_model="gpt-4o-mini", wallet_gated=True
    )
    result = adapter.run_inference({"text": "hello"}, {"subtype": "topic_extraction"})
    assert result["status"] == "deferred"
    assert result["error"] == INSUFFICIENT_CREDITS
    with pytest.raises(RuntimeError, match="insufficient_credits"):
        adapter._chat_completion(model="gpt-4o-mini", prompt="hello")
    assert called["n"] == 0


def test_byok_adapter_does_not_check_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "topos.engine.hosted_llm_wallet.hosted_llm_wallet_allows",
        lambda force=False: False,
    )

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        return _FakeUrlOpen(
            {
                "choices": [{"message": {"content": '{"topics":[]}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    monkeypatch.setattr(
        "topos.engine.backends.openai_compatible.urllib.request.urlopen",
        fake_urlopen,
    )
    adapter = OpenAICompatibleAdapter(
        api_key="sk-owner", default_model="gpt-4o-mini", wallet_gated=False
    )
    result = adapter._chat_completion(model="gpt-4o-mini", prompt="hello")
    assert result["text"] == '{"topics":[]}'

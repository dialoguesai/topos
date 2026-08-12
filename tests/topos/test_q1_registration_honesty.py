"""Q1 — a machine that has never had Ollama must not register as Ollama-capable.

`engine_ollama_base_url` defaults to http://localhost:11434 (settings.py:91), so
the old `if settings.engine_ollama_base_url` test advertised the capability on
every node ever started (PLAN_LOCAL_MODEL_QUICKSTART §1.2). The control plane
then routes local work to a provider that isn't there.
"""

from __future__ import annotations

import pytest

from topos.engine import registration


@pytest.fixture()
def _configured_base_url(monkeypatch):
    monkeypatch.setattr(
        registration.settings, "engine_ollama_base_url", "http://localhost:11434", raising=False
    )


def test_unreachable_ollama_is_not_advertised(monkeypatch, _configured_base_url):
    monkeypatch.setattr(registration, "ollama_is_reachable", lambda: False)
    capabilities = registration.build_engine_capabilities()
    assert "ollama" not in capabilities["providers"]
    assert "ollama" not in capabilities["signal_providers"]


def test_reachable_ollama_is_advertised(monkeypatch, _configured_base_url):
    monkeypatch.setattr(registration, "ollama_is_reachable", lambda: True)
    capabilities = registration.build_engine_capabilities()
    assert "ollama" in capabilities["providers"]
    assert "ollama" in capabilities["signal_providers"]


def test_no_base_url_configured_is_never_probed_or_advertised(monkeypatch):
    monkeypatch.setattr(registration.settings, "engine_ollama_base_url", "", raising=False)
    probed = []
    monkeypatch.setattr(
        registration, "ollama_is_reachable", lambda: probed.append(1) or True
    )
    capabilities = registration.build_engine_capabilities()
    assert "ollama" not in capabilities["providers"]
    assert not probed, "an unconfigured node must not pay for a health probe"


def test_a_probe_that_blows_up_degrades_to_not_advertised(monkeypatch, _configured_base_url):
    def _boom():
        raise OSError("no route to host")

    monkeypatch.setattr(registration, "ollama_is_reachable", _boom)
    capabilities = registration.build_engine_capabilities()
    assert "ollama" not in capabilities["providers"]

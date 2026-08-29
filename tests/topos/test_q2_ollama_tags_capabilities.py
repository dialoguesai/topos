"""Q2 — /api/tags capabilities and modified_at reach Branch A.

The FE preselect prefers tools-capable tags and shows capability chips. Both
`list_models_detailed` (adapter) and `_ollama_list_models` (WS relay path) must
forward those fields from /api/tags — size alone is not enough.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from topos.engine.backends import ollama as ollama_backend
from topos.services.llm import openai as openai_llm


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_list_models_detailed_forwards_capabilities_and_modified_at(monkeypatch):
    tags = {
        "models": [
            {
                "name": "nomic-embed-text:latest",
                "size": 274_000_000,
                "modified_at": "2026-01-01T00:00:00Z",
                "capabilities": ["embedding"],
            },
            {
                "name": "llama3.2:latest",
                "size": 2_000_000_000,
                "modified_at": "2026-08-01T12:00:00Z",
                "capabilities": ["tools"],
            },
        ]
    }
    monkeypatch.setattr(
        ollama_backend.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResp(tags),
    )
    adapter = ollama_backend.OllamaAdapter(base_url="http://localhost:11434")
    detailed = adapter.list_models_detailed()
    by_name = {row["name"]: row for row in detailed}
    assert by_name["llama3.2:latest"]["capabilities"] == ["tools"]
    assert by_name["llama3.2:latest"]["modified_at"] == "2026-08-01T12:00:00Z"
    assert by_name["nomic-embed-text:latest"]["capabilities"] == ["embedding"]


@pytest.mark.asyncio
async def test_ollama_list_models_payload_includes_capabilities_and_modified_at(monkeypatch):
    tags = {
        "models": [
            {
                "name": "llama3.2:latest",
                "size": 2_000_000_000,
                "modified_at": "2026-08-01T12:00:00Z",
                "capabilities": ["tools", "completion"],
            }
        ]
    }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            assert url.endswith("/api/tags")
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = tags
            resp.text = ""
            return resp

    monkeypatch.setattr(openai_llm.httpx, "AsyncClient", lambda **kwargs: _Client())
    monkeypatch.setattr(openai_llm.settings, "engine_ollama_base_url", "http://ollama.test")
    monkeypatch.setattr(openai_llm.settings, "ollama_list_timeout_sec", 5.0)
    monkeypatch.setattr(openai_llm, "_ensure_ollama_running", lambda _base=None: None)

    payload = await openai_llm._ollama_list_models()
    assert payload["models"] == ["llama3.2:latest"]
    assert payload["sizes"]["llama3.2:latest"] == 2_000_000_000
    assert payload["capabilities"]["llama3.2:latest"] == ["tools", "completion"]
    assert payload["modified_at"]["llama3.2:latest"] == "2026-08-01T12:00:00Z"

"""Delegated inference preserves chat roles and never calls the data pipeline."""
import pytest
import json
import httpx
from unittest.mock import AsyncMock


def test_chat_rejects_unspecified_model_and_extra_fields():
    from topos.services.llm.delegated import ChatRequest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ChatRequest(model="", messages=[{"role": "user", "content": "hello"}], max_tokens=10)
    with pytest.raises(ValidationError):
        ChatRequest(model="example", messages=[{"role": "user", "content": "hello"}],
                    max_tokens=10, tools=[{"name": "query_scope"}])


def test_cloud_alias_and_embedding_models_are_not_local_chat():
    from topos.services.llm.delegated import local_completion
    valid = {"capabilities": ["completion"], "model_info": {"general.architecture": "llama"}}
    assert local_completion(valid)
    assert not local_completion({**valid, "remote_host": "https://ollama.com"})
    assert not local_completion({**valid, "remote_model": "cloud-model"})
    assert not local_completion({**valid, "capabilities": ["embedding"]})
    assert not local_completion({"capabilities": ["completion"]})


@pytest.mark.asyncio
async def test_chat_uses_only_messages_and_fixed_local_endpoint(monkeypatch):
    from topos.services.llm import delegated
    from contextlib import asynccontextmanager
    from topos import local_serving
    monkeypatch.setattr(delegated.settings, "enable_llm", True)
    monkeypatch.setattr(delegated.settings, "engine_ollama_base_url", "http://127.0.0.1:11434")
    monkeypatch.setattr(delegated.state, "get_engine_mode", lambda: "full")
    sent = []
    def handle(request):
        body = json.loads(request.content)
        sent.append((request.url.path, body))
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"], "model_info": {"arch": "test"}})
        return httpx.Response(200, text='{"message":{"content":"ok"},"done":false}\n'
            '{"message":{"content":""},"done":true,"prompt_eval_count":8,"eval_count":1,"done_reason":"stop"}\n')
    real_client = httpx.AsyncClient
    monkeypatch.setattr(delegated.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle)))
    @asynccontextmanager
    async def admission(body, **kwargs):
        yield body
    monkeypatch.setattr(local_serving, "request_async", admission)
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "user"},
                {"role": "assistant", "content": "previous"}, {"role": "user", "content": "next"}]
    emit = AsyncMock()
    result = await delegated.chat({"model": "test", "messages": messages, "max_tokens": 10}, emit)
    assert sent[-1][1]["messages"] == messages
    assert sent[-1][1]["options"]["num_predict"] == 10
    assert result["output"] == "ok" and result["usage"]["completion_tokens"] == 1
    emit.assert_awaited_once_with("ok")


@pytest.mark.asyncio
async def test_cloud_alias_is_refused_before_prompt_is_sent(monkeypatch):
    from topos.services.llm import delegated
    from fastapi import HTTPException
    monkeypatch.setattr(delegated.settings, "enable_llm", True)
    monkeypatch.setattr(delegated.settings, "engine_ollama_base_url", "http://127.0.0.1:11434")
    monkeypatch.setattr(delegated.state, "get_engine_mode", lambda: "full")
    paths = []
    def handle(request):
        paths.append(request.url.path)
        assert "private input" not in request.content.decode()
        return httpx.Response(200, json={"remote_host": "https://ollama.com", "capabilities": ["completion"]})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(delegated.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle)))
    with pytest.raises(HTTPException):
        await delegated.chat({"model": "cloud", "messages": [{"role": "user", "content": "private input"}], "max_tokens": 10}, AsyncMock())
    assert paths == ["/api/show"]


@pytest.mark.asyncio
async def test_http_and_owner_principals_cannot_impersonate_the_relay():
    from topos.core.handlers.device import handle_model_chat
    from topos.principal import Principal, set_principal, reset_principal
    for cls in ("third_party", "owner_app", "owner_agent", "cp_relay"):
        token = set_principal(Principal(cls=cls, channel="local_http"))
        try:
            result = await handle_model_chat({"id": "r", "payload": {}})
            assert result["error"] == "relay_required"
        finally:
            reset_principal(token)

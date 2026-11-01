"""An overloaded provider must not become unreachable, a retry storm, or an answer."""
import asyncio
import io
import json
from types import SimpleNamespace
import urllib.error

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from topos.engine import client, engine
from topos.engine.backends.ollama import OllamaAdapter
from topos.engine.overload import ComputeUnavailable, provider_unavailable, retry_after_seconds
from topos.engine.tasks import ModelRequest, ProcessingTask
from topos.query import verify
from topos.services.llm import openai as llm

BUSY = 'server busy, please try again. maximum pending requests exceeded'
PRIVATE = 'DO-NOT-EXPOSE-provider-body'


@pytest.mark.parametrize('value,expected', [('3', 3), (0, 0), (86400, 86400), (True, None),
    ('-1', None), ('3.5', None), ('999999999999999999', None), (None, None), ('date', None)])
def test_retry_delay_is_only_reported_when_known(value, expected):
    assert retry_after_seconds(value) == expected


@pytest.mark.parametrize('status,body,code', [(503, BUSY, 'compute_busy'),
    (503, PRIVATE, 'compute_unavailable'), (429, PRIVATE, 'compute_rate_limited'), (500, BUSY, None)])
def test_busy_is_distinguished_from_unavailability(status, body, code):
    error = provider_unavailable(status, body, '7')
    if code is None:
        assert error is None
    else:
        assert error.code == code
        assert error.headers() == {'Retry-After': '7'}
        assert PRIVATE not in json.dumps(error.detail())


@pytest.fixture
def overloaded_engine(monkeypatch):
    """Only replace provider IO and unrelated policy/cache dependencies."""
    adapter = OllamaAdapter(base_url='http://compute.invalid:11434')
    monkeypatch.setattr(adapter, '_ensure_server', lambda: None)
    monkeypatch.setattr(adapter, 'resolve_think', lambda *a: False)
    calls = []

    def reject(request, **kwargs):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 503, 'Unavailable',
            {'Retry-After': '4'}, io.BytesIO((BUSY + PRIVATE + ' disabling thinking').encode()))

    monkeypatch.setattr('urllib.request.urlopen', reject)
    monkeypatch.setattr(engine, 'get_adapter_for_task', lambda _: adapter)
    monkeypatch.setattr(engine, 'apply_blackhole_egress_policy', lambda task: (task, None))
    monkeypatch.setattr(engine, 'get_model_cache', lambda: SimpleNamespace(touch_for_task=lambda *a: False))
    return engine.Engine(), calls


def task():
    return ProcessingTask(id='synthetic-overload', type='truth_verdict', subtype='truth_verdict',
        input={'claim': 'Invented claim', 'lanes': {'self': ['Invented evidence']}},
        model_request=ModelRequest(provider='ollama', model='fixture-model'))


@pytest.mark.parametrize('remote', [False, True])
def test_engine_transport_retains_deferred_signal_without_retry(overloaded_engine, monkeypatch, remote):
    actual_engine, calls = overloaded_engine
    if remote:
        from topos.api import engine_run
        from topos.auth import require_api_key
        monkeypatch.setattr(engine_run, 'Engine', lambda: actual_engine)
        app = FastAPI()
        app.include_router(engine_run.router)
        app.dependency_overrides[require_api_key] = lambda: None
        server = TestClient(app)
        real_http = httpx.Client
        seen = []

        def forward(request):
            seen.append(request)
            response = server.post(request.url.path, content=request.content, headers={'Content-Type': 'application/json'})
            assert response.status_code == 503
            assert response.headers['Retry-After'] == '4'
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)

        monkeypatch.setattr(client.httpx, 'Client', lambda **kw: real_http(transport=httpx.MockTransport(forward), **kw))
        selected = client.RemoteEngineClient('https://compute.invalid', api_key='synthetic-key')
    else:
        selected = client.LocalEngineClient(actual_engine)
    result = selected.run(task())
    assert result.status == 'deferred'
    assert result.output['availability'] == ComputeUnavailable('compute_busy', 4).detail()
    assert PRIVATE not in result.model_dump_json()
    assert len(calls) == 1, 'Neither transport nor think adaptation should retry overload'
    if remote:
        assert seen[0].headers['Authorization'] == 'Bearer synthetic-key'
    monkeypatch.setattr(client, 'get_engine_client_or_local', lambda _: selected)
    with pytest.raises(ComputeUnavailable) as raised:
        verify._llm_lane_stances('Invented claim', {'self': ['Invented evidence']})
    assert raised.value.code == 'compute_busy'
    assert len(calls) == 2, 'One inference attempt per verification'


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('status,body,code', [(503, BUSY, 'compute_busy'),
    (503, PRIVATE, 'compute_unavailable'), (429, PRIVATE, 'compute_rate_limited')])
def test_generation_and_cp_terminal_error_are_typed(monkeypatch, stream, status, body, code):
    from topos.core.handlers import device
    calls, deltas, frames = [], [], []
    real_client = httpx.AsyncClient

    def reply(request):
        calls.append(request)
        return httpx.Response(status, text=body + PRIVATE + ' disabling thinking', headers={'Retry-After': '6'})

    monkeypatch.setattr(llm.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(reply), **kw))
    monkeypatch.setattr(llm, '_ensure_ollama_running', lambda _: None)
    monkeypatch.setattr(llm, '_stream_ollama_adapter', lambda _: SimpleNamespace(resolve_think=lambda *a: False))
    fn = llm._ollama_stream_generate if stream else llm._ollama_generate
    payload = {'provider': 'ollama', 'stream': stream, 'model': 'fixture', 'prompt': 'Invented prompt'}

    async def run():
        kwargs = {'on_delta': deltas.append} if stream else {}
        with pytest.raises(HTTPException) as raised:
            await fn(payload, **kwargs)
        assert raised.value.status_code == status
        assert raised.value.detail == ComputeUnavailable(code, 6).detail()
        assert raised.value.headers == {'Retry-After': '6'}

        async def generate(payload, **callbacks):
            return await fn(payload, **(callbacks if stream else {}))

        async def send(frame):
            frames.append(frame)

        monkeypatch.setattr(device, 'get_services', lambda: SimpleNamespace(llm=SimpleNamespace(generate=generate)))
        monkeypatch.setattr(device.engine_state, 'control_plane_client', SimpleNamespace(send_message=send))
        result = await device.handle_llm_generation({'id': 'fixture-cp', 'payload': payload})
        assert result == ComputeUnavailable(code, 6).envelope('fixture-cp')
        assert 'fixture-cp' not in device._LLM_GENERATION_TASKS
        assert not deltas
        assert all(f['status'] == 'chunk' and f['payload']['delta'] == '' for f in frames)

    asyncio.run(run())
    assert len(calls) == 2


def test_verification_handler_and_http_door_preserve_busy(monkeypatch):
    import topos.core.handlers as hub
    from topos.core.handlers import query
    from topos.features.lifecycle import blackhole_guard
    from topos.principal import OWNER_APP, Principal, set_principal, reset_principal
    from topos.api import local_mcp

    monkeypatch.setattr(hub, 'get_db_connection', lambda: object())
    monkeypatch.setattr(blackhole_guard, 'guard_from_message', lambda *a: None)
    monkeypatch.setattr(query, '_record_verify_claim_after_answer', lambda *a: pytest.fail('Busy is not an answered claim'))

    def reject(*a, **kw):
        raise ComputeUnavailable('compute_busy', 4)

    monkeypatch.setattr(verify, 'verify_claim', reject)
    owner = Principal(cls=OWNER_APP, channel='uds')

    async def dispatch(message, **kwargs):
        token = set_principal(owner)
        try:
            return await query.handle_verify_claim(message)
        finally:
            reset_principal(token)

    monkeypatch.setattr(local_mcp, 'handle_control_plane_request', dispatch)

    async def run():
        with pytest.raises(HTTPException) as raised:
            await local_mcp.local_verify_claim({'statement': 'Invented claim', 'app_id': 'fixture'}, principal=owner)
        assert raised.value.status_code == 503
        assert raised.value.detail == ComputeUnavailable('compute_busy', 4).detail()
        assert raised.value.headers == {'Retry-After': '4'}

    asyncio.run(run())


@pytest.mark.parametrize('status,payload', [(403, {'detail': 'refused'}),
    (503, {'detail': {'code': 'blackhole_policy_unavailable'}}),
    (503, {'result': {'output': {'availability': {'code': []}}}})])
def test_remote_policy_and_unknown_errors_are_not_busy(monkeypatch, status, payload):
    real_client = httpx.Client
    monkeypatch.setattr(client.httpx, 'Client', lambda **kw: real_client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload)), **kw))
    with pytest.raises(httpx.HTTPStatusError):
        client.RemoteEngineClient('https://compute.invalid').run(task())

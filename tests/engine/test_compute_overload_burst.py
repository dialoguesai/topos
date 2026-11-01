"""A controlled provider admits two calls, rejects a burst, then recovers.

This tests overload transport, not real GPU capacity or Ollama scheduling.
"""
import asyncio
import io
import json
import threading
from types import SimpleNamespace
import urllib.error

import httpx
import pytest
from fastapi import HTTPException

from topos.engine import engine
from topos.engine.backends.ollama import OllamaAdapter
from topos.engine.tasks import ModelRequest, ProcessingTask
from topos.services.llm import openai as llm


def test_mixed_burst_has_one_attempt_each_and_recovers(monkeypatch):
    lock, release = threading.Lock(), threading.Event()
    observed = {'attempts': 0, 'inflight': 0, 'max_inflight': 0}
    successful = {'response': json.dumps({'lanes': {'self': {'stance': 'supports', 'confidence': .9}}}),
                  'model': 'fixture', 'done': True, 'prompt_eval_count': 1, 'eval_count': 1}

    def provider():
        with lock:
            observed['attempts'] += 1
            if observed['inflight'] >= 2:
                return 503, {'error': 'server busy, please try again. maximum pending requests exceeded'}
            observed['inflight'] += 1
            observed['max_inflight'] = max(observed['max_inflight'], observed['inflight'])
        try:
            if not release.wait(10):
                raise AssertionError('Test coordinator did not release the provider')
            return 200, successful
        finally:
            with lock:
                observed['inflight'] -= 1

    real_http = httpx.AsyncClient

    async def async_transport(request):
        code, body = await asyncio.to_thread(provider)
        return httpx.Response(code, text=json.dumps(body) + '\n', headers={'Retry-After': '5'})

    monkeypatch.setattr(llm.httpx, 'AsyncClient', lambda **kw: real_http(transport=httpx.MockTransport(async_transport), **kw))
    monkeypatch.setattr(llm, '_ensure_ollama_running', lambda _: None)
    monkeypatch.setattr(llm, '_stream_ollama_adapter', lambda _: SimpleNamespace(resolve_think=lambda *a: False))

    def urlopen(request, **kwargs):
        code, body = provider()
        data = io.BytesIO(json.dumps(body).encode())
        if code == 503:
            raise urllib.error.HTTPError(request.full_url, code, 'Busy', {'Retry-After': '5'}, data)
        return data

    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    adapter = OllamaAdapter(base_url='http://compute.invalid')
    monkeypatch.setattr(adapter, '_ensure_server', lambda: None)
    monkeypatch.setattr(adapter, 'resolve_think', lambda *a: False)
    monkeypatch.setattr(engine, 'get_adapter_for_task', lambda _: adapter)
    monkeypatch.setattr(engine, 'apply_blackhole_egress_policy', lambda task: (task, None))
    monkeypatch.setattr(engine, 'get_model_cache', lambda: SimpleNamespace(touch_for_task=lambda *a: False, resident_slots=lambda: []))

    async def request(index):
        if index % 3 == 0:
            task = ProcessingTask(id=f'burst-{index}', type='truth_verdict', subtype='truth_verdict',
                input={'claim': 'Invented', 'lanes': {'self': ['Invented evidence']}},
                model_request=ModelRequest(provider='ollama', model='fixture'))
            result = await asyncio.to_thread(engine.Engine().run, task)
            if result.status == 'deferred':
                assert result.output['availability']['code'] == 'compute_busy'
                assert result.output['availability']['retry_after_seconds'] == 5
                return 'busy'
            assert result.status == 'completed'
            return 'completed'
        fn = llm._ollama_stream_generate if index % 3 == 1 else llm._ollama_generate
        try:
            result = await fn({'model': 'fixture', 'prompt': 'Invented prompt'})
            assert result['output']
            return 'completed'
        except HTTPException as exc:
            assert exc.status_code == 503 and exc.detail['code'] == 'compute_busy'
            assert exc.detail['retry_after_seconds'] == 5
            return 'busy'

    async def run():
        tasks = [asyncio.create_task(request(i)) for i in range(24)]
        try:
            async with asyncio.timeout(8):
                while observed['attempts'] < 24:
                    await asyncio.sleep(.001)
            release.set()
            outcomes = await asyncio.gather(*tasks)
            assert outcomes.count('completed') == 2
            assert outcomes.count('busy') == 22
            assert observed == {'attempts': 24, 'inflight': 0, 'max_inflight': 2}
            # One recovery request per path, with no automatic replay of rejects.
            for i in range(24, 27):
                assert await request(i) == 'completed'
            assert observed['attempts'] == 27 and observed['inflight'] == 0
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())

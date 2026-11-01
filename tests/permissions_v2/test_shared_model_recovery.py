"""Frozen transport failures: no live model, data or automatic ask retry."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from topos.permissions_v2 import answer_release as ar, shadow_labeler_local as sl
from topos.permissions_v2.contract import PolicyError

pytestmark = pytest.mark.real_support_judge


@pytest.mark.asyncio
@pytest.mark.parametrize('judge', [False, True])
async def test_expired_call_never_opens_transport(monkeypatch, judge):
    def forbidden(**kwargs):
        pytest.fail('expired work opened a transport')
    monkeypatch.setattr(sl, 'open_transport', forbidden)
    with pytest.raises(PolicyError):
        if judge:
            await ar._judge_local('Synthetic sentence', ('Synthetic evidence',), deadline=time.time() - 1)
        else:
            await ar._generate_local(SimpleNamespace(), deadline=time.time() - 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('judge', [False, True])
@pytest.mark.parametrize('phase', ['verify', 'post'])
async def test_deadline_cancels_inflight_closes_and_next_call_recovers(monkeypatch, judge, phase):
    events = []
    fail = True

    class Transport:
        base_url = 'http://synthetic.invalid'
        client = None
        def __init__(self):
            self.client = self
        async def wait(self, name):
            events.append(name)
            if fail and name == phase:
                try:
                    await asyncio.Event().wait()
                finally:
                    events.append('cancelled')
        async def verify(self):
            await self.wait('verify')
        async def post(self, *args, **kwargs):
            await self.wait('post')
            content = '{"supported":"yes"}' if judge else 'Synthetic answer [1].'
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                'model': sl.MODEL, 'done': True, 'message': {'content': content}})
        async def aclose(self):
            events.append('closed')

    monkeypatch.setattr(sl, 'open_transport', lambda **kwargs: Transport())
    monkeypatch.setattr(sl, 'assessment_base_url', lambda: Transport.base_url)
    monkeypatch.setenv('TOPOS_P2A_ANSWER_SUPPORT_JUDGE_TEMPLATE', 'v1')
    monkeypatch.setattr(ar, 'judge_template', lambda: 'v1')
    async def call(deadline):
        if judge:
            return await ar._judge_local('Synthetic sentence', ('Synthetic evidence',), deadline=deadline)
        return await ar._generate_local(SimpleNamespace(system='Synthetic', user='Synthetic'), deadline=deadline)
    # Outer watchdog makes the pre-fix failure finite; the inner deadline must win.
    with pytest.raises(PolicyError):
        await asyncio.wait_for(call(time.time() + .03), timeout=.3)
    assert events == (['verify', 'cancelled', 'closed'] if phase == 'verify'
                      else ['verify', 'post', 'cancelled', 'closed'])
    fail = False
    events.clear()
    assert await call(time.time() + 5)
    assert events == ['verify', 'post', 'closed']


@pytest.mark.asyncio
@pytest.mark.parametrize('judge', [False, True])
@pytest.mark.parametrize('phase', ['verification', 'generation'])
@pytest.mark.parametrize('kind', ['timeout', 'http_status', 'transport_error', 'malformed_response', 'incomplete_response'])
async def test_model_diagnostics_classify_without_content(monkeypatch, caplog, judge, phase, kind):
    import httpx
    secret = 'PRIVATE_SYNTHETIC_CANARY'
    closed = []
    request = httpx.Request('POST', 'https://example.invalid/' + secret)
    status = httpx.Response(503, request=request, text=secret)
    failures = {
        'timeout': httpx.ReadTimeout(secret, request=request),
        'http_status': httpx.HTTPStatusError(secret, request=request, response=status),
        'transport_error': httpx.ConnectError(secret, request=request),
        'malformed_response': ValueError(secret),
        'incomplete_response': sl.ModelUnreviewed(secret),
    }
    class Response:
        def raise_for_status(self):
            if kind in ('timeout', 'http_status', 'transport_error'):
                raise failures[kind]
        def json(self):
            if kind == 'malformed_response':
                raise failures[kind]
            return {'model': sl.MODEL, 'done': False, 'message': {'content': secret}}
    class Transport:
        base_url = 'https://example.invalid/' + secret
        def __init__(self): self.client = self
        async def verify(self):
            if phase == 'verification':
                # The real verifier wraps HTTP failures in Unreachable.
                try:
                    raise failures[kind]
                except Exception as exc:
                    raise sl.Unreachable(secret) from exc
        async def post(self, *args, **kwargs): return Response()
        async def aclose(self): closed.append(True)
    monkeypatch.setattr(sl, 'open_transport', lambda **kwargs: Transport())
    monkeypatch.setattr(sl, 'assessment_base_url', lambda: Transport.base_url)
    caplog.set_level('INFO', logger=ar.__name__)
    with pytest.raises(PolicyError) as error:
        if judge:
            await ar._judge_local(secret, (secret,), question=secret, deadline=time.time() + 10)
        else:
            await ar._generate_local(SimpleNamespace(system=secret, user=secret), deadline=time.time() + 10)
    assert error.value.code == ('model_unavailable' if phase == 'verification' else 'model_error')
    events = [r.sharing_model for r in caplog.records if hasattr(r, 'sharing_model')]
    assert events
    event = events[-1]
    expected = ('unreviewed_model' if phase == 'verification' and kind == 'incomplete_response' else kind)
    assert event == {'role': 'judge' if judge else 'writer', 'phase': phase, 'outcome': expected,
                     'http_status': 503 if kind == 'http_status' else None, 'elapsed_ms': event['elapsed_ms']}
    assert isinstance(event['elapsed_ms'], int) and event['elapsed_ms'] >= 0
    assert secret not in repr([r.__dict__ for r in caplog.records])
    assert all(r.exc_info is None for r in caplog.records)
    assert closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize('judge', [False, True])
@pytest.mark.parametrize('deadline_timeout', [False, True])
@pytest.mark.parametrize('phase', ['verification', 'generation'])
async def test_diagnostics_preserve_cancellation(monkeypatch, caplog, judge, deadline_timeout, phase):
    entered = asyncio.Event()
    closed = []
    class Transport:
        base_url = 'http://synthetic.invalid'
        def __init__(self): self.client = self
        async def pause(self):
            entered.set()
            await asyncio.Event().wait()
        async def verify(self):
            if phase == 'verification': await self.pause()
        async def post(self, *args, **kwargs): await self.pause()
        async def aclose(self): closed.append(True)
    monkeypatch.setattr(sl, 'open_transport', lambda **kwargs: Transport())
    monkeypatch.setattr(sl, 'assessment_base_url', lambda: Transport.base_url)
    caplog.set_level('INFO', logger=ar.__name__)
    deadline = time.time() + (.03 if deadline_timeout else 10)
    call = (ar._judge_local('synthetic', ('synthetic',), deadline=deadline) if judge else
            ar._generate_local(SimpleNamespace(system='synthetic', user='synthetic'), deadline=deadline))
    task = asyncio.create_task(call)
    await asyncio.wait_for(entered.wait(), 1)
    if not deadline_timeout: task.cancel()
    with pytest.raises(PolicyError if deadline_timeout else asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    events = [r.sharing_model for r in caplog.records if hasattr(r, 'sharing_model')]
    failures = [e for e in events if e['outcome'] != 'ok']
    assert len(failures) == 1
    assert failures[0]['phase'] == phase
    assert failures[0]['outcome'] == ('timeout' if deadline_timeout else 'cancelled')
    assert closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize('content,verdict,outcome', [('{"supported":"no"}', False, 'ok'),
                                                    ('synthetic-invalid', None, 'invalid_verdict')])
async def test_judge_negative_verdict_is_not_provider_failure(monkeypatch, caplog, content, verdict, outcome):
    class Transport:
        base_url = 'http://synthetic.invalid'
        def __init__(self): self.client = self
        async def verify(self): pass
        async def post(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                'model': sl.MODEL, 'done': True, 'message': {'content': content}})
        async def aclose(self): pass
    monkeypatch.setattr(sl, 'open_transport', lambda **kwargs: Transport())
    monkeypatch.setattr(sl, 'assessment_base_url', lambda: Transport.base_url)
    monkeypatch.setattr(ar, 'judge_template', lambda: 'v1')
    caplog.set_level('INFO', logger=ar.__name__)
    assert await ar._judge_local('synthetic', ('synthetic',), deadline=time.time() + 5) is verdict
    events = [r.sharing_model for r in caplog.records if hasattr(r, 'sharing_model')]
    assert [e['outcome'] for e in events] == ['ok', outcome]

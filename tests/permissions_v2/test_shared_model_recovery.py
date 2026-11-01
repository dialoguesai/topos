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

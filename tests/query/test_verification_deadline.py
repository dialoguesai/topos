import asyncio
import threading
import time

import pytest

import topos.core.handlers as hub
from topos.core.handlers import query
from topos.features.lifecycle import blackhole_guard
from topos.principal import OWNER_APP, Principal, set_principal, reset_principal
from topos.query import verify
from topos.engine.request_budget import current_budget


def test_expiry_is_unavailable_not_a_heuristic_success_and_worker_finishes(monkeypatch):
    finished = threading.Event()
    monkeypatch.setattr(hub, 'get_db_connection', lambda: object())
    monkeypatch.setattr(blackhole_guard, 'guard_from_message', lambda *args: object())

    def blocked(*args, **kwargs):
        try:
            while True:
                current_budget().check()
                time.sleep(.005)
        finally:
            finished.set()

    monkeypatch.setattr(verify, 'verify_claim', blocked)

    async def run():
        token = set_principal(Principal(cls=OWNER_APP, channel='uds'))
        try:
            result = await query.handle_verify_claim({'id':'synthetic', 'payload':{
                'statement':'invented', 'caller_app_id':'test', 'deadline_s':.08}})
            assert result['status'] == 'error' and result['error_type'] == 'compute_unavailable'
            assert result['termination_reason'] == 'deadline_exceeded'
            assert 'payload' not in result
            assert await asyncio.to_thread(finished.wait, .5)
        finally:
            reset_principal(token)
    asyncio.run(run())


def test_cancelled_handler_signals_its_worker(monkeypatch):
    started, finished = threading.Event(), threading.Event()
    monkeypatch.setattr(hub, 'get_db_connection', lambda: object())
    monkeypatch.setattr(blackhole_guard, 'guard_from_message', lambda *args: object())

    def blocked(*args, **kwargs):
        started.set()
        try:
            while True:
                current_budget().check()
                time.sleep(.005)
        finally:
            finished.set()
    monkeypatch.setattr(verify, 'verify_claim', blocked)

    async def run():
        token = set_principal(Principal(cls=OWNER_APP, channel='uds'))
        try:
            task = asyncio.create_task(query.handle_verify_claim({'id':'synthetic', 'payload':{
                'statement':'invented', 'caller_app_id':'test'}}))
            assert await asyncio.to_thread(started.wait, .5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await asyncio.to_thread(finished.wait, .5)
        finally:
            reset_principal(token)
    asyncio.run(run())


def test_default_budget_allows_previously_observed_slow_success(monkeypatch):
    """Published stress had successful 61–110s work: default must not cut it at 60s."""
    from types import SimpleNamespace
    from topos.engine import request_budget
    clock = [1000.0]
    monkeypatch.setattr(request_budget, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(hub, 'get_db_connection', lambda: object())
    monkeypatch.setattr(blackhole_guard, 'guard_from_message', lambda *args: object())
    monkeypatch.setattr(query, '_record_verify_claim_after_answer', lambda *args: None)

    def slow_success(*args, **kwargs):
        clock[0] += 110.0
        current_budget().check()
        return {'mode': 'standard', 'lanes': {'self': {'stance': 'supports'}}}
    monkeypatch.setattr(verify, 'verify_claim', slow_success)

    async def run():
        token = set_principal(Principal(cls=OWNER_APP, channel='uds'))
        try:
            result = await query.handle_verify_claim({'id':'synthetic', 'payload':{
                'statement':'invented', 'caller_app_id':'test'}})
            assert result['status'] == 'ok'
            assert result['payload']['lanes']['self']['stance'] == 'supports'
        finally:
            reset_principal(token)
    asyncio.run(run())


@pytest.mark.parametrize('reason', ['deadline_exceeded', 'cancelled', 'PRIVATE_UNKNOWN_REASON', {'private':'value'}])
def test_remote_termination_reason_is_validated_and_preserved(monkeypatch, reason):
    from types import SimpleNamespace
    from topos.engine import client
    from topos.engine.tasks import ProcessingResult
    from topos.engine.overload import ComputeUnavailable
    from topos.engine.request_budget import RequestBudget, RequestStopped, budget_scope
    def remote(task):
        return ProcessingResult(task_id=task.id, status='deferred', output={
            'availability': {'code':'compute_unavailable','reason':reason}})
    monkeypatch.setattr(client, 'get_engine_client_or_local', lambda _: SimpleNamespace(run=remote))
    with budget_scope(RequestBudget(1)), pytest.raises(ComputeUnavailable) as error:
        verify._llm_lane_stances('synthetic', {'self':['synthetic']})
    if reason in ('deadline_exceeded', 'cancelled'):
        assert isinstance(error.value, RequestStopped)
        assert error.value.envelope('synthetic')['termination_reason'] == reason
    else:
        assert 'termination_reason' not in error.value.envelope('synthetic')
        assert 'private' not in str(error.value.detail()).lower()

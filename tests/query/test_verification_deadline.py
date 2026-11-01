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

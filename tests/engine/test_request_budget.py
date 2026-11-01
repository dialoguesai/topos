"""Controlled sockets and workers: deadline closes local transport and permits a fresh request."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import httpx
import pytest

from topos.engine.request_budget import RequestBudget, RequestStopped, budget_scope, current_budget, await_budgeted, TIMEOUT_HEADER
from topos.engine.backends.ollama import OllamaAdapter
from topos.engine.client import RemoteEngineClient
from topos.engine.tasks import ProcessingTask, ModelRequest


@pytest.mark.parametrize('value', [True, False, 0, -1, float('nan'), float('inf'), 121, 10 ** 1000, '30', None])
def test_budget_rejects_invalid_lifetimes(value):
    with pytest.raises(ValueError):
        RequestBudget(value)


def test_budget_cancel_reaches_worker_and_does_not_leak_context():
    finished = threading.Event()
    budget = RequestBudget(.08)

    def worker():
        assert current_budget() is budget
        try:
            while True:
                budget.check()
                time.sleep(.005)
        finally:
            finished.set()

    async def run():
        with budget_scope(budget):
            with pytest.raises(RequestStopped):
                await await_budgeted(asyncio.to_thread(worker), budget)
        assert current_budget() is None
        assert await asyncio.to_thread(finished.wait, .5)
    asyncio.run(run())


def test_remote_hop_receives_remaining_duration_not_fresh_timeout(monkeypatch):
    from topos.engine import request_budget
    task = ProcessingTask(id='synthetic', type='truth_verdict', model_request=ModelRequest(provider='ollama'))
    budget = RequestBudget(.5)
    seen = {}

    def request(method, url, **kwargs):
        seen.update(kwargs)
        return httpx.Response(200, request=httpx.Request(method, url), json={
            'result': {'task_id': task.id, 'status': 'completed', 'output': {}}})

    monkeypatch.setattr(request_budget, 'request', request)
    with budget_scope(budget):
        time.sleep(.03)
        assert RemoteEngineClient('https://compute.example.invalid').run(task).status == 'completed'
    assert 0 < int(seen['headers'][TIMEOUT_HEADER]) < 490


@pytest.mark.parametrize('stall', ['headers', 'trickling_body'])
def test_stalled_provider_socket_closes_on_deadline_then_next_call_succeeds(stall):
    closed = threading.Event()
    entered = threading.Event()
    count = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if self.path == '/api/show':
                body = {'capabilities': []}
            else:
                count.append(1)
                if len(count) == 1:
                    entered.set()
                    if stall == 'trickling_body':
                        self.send_response(200)
                        self.send_header('Content-Length', '10000')
                        self.end_headers()
                        try:
                            for _ in range(100):
                                self.wfile.write(b' ')
                                self.wfile.flush()
                                time.sleep(.02)
                        except (BrokenPipeError, ConnectionResetError):
                            closed.set()
                        return
                    self.connection.settimeout(2)
                    try:
                        if self.connection.recv(1) == b'':
                            closed.set()
                    except TimeoutError:
                        pass
                    return
                body = {'response': 'synthetic response', 'done': True}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    adapter = OllamaAdapter(f'http://127.0.0.1:{server.server_port}')
    try:
        started = time.monotonic()
        with budget_scope(RequestBudget(.3)), pytest.raises(RequestStopped) as caught:
            adapter._generate('synthetic', 'invented claim', think=False)
        assert caught.value.reason == 'deadline_exceeded'
        assert time.monotonic() - started < 1.5
        assert entered.is_set() and closed.wait(1)
        with budget_scope(RequestBudget(1)):
            assert adapter._generate('synthetic', 'invented claim', think=False)['text'] == 'synthetic response'
        assert len(count) == 2  # no hidden resubmission after cancellation
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)

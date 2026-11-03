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

        def do_GET(self):
            assert self.path == "/api/tags"
            body = b'{"models": []}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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


def test_default_preserves_successful_sub_120_second_verifications():
    from topos.engine.request_budget import DEFAULT_VERIFY_SECONDS
    assert DEFAULT_VERIFY_SECONDS == 120.0


def test_stalled_dns_does_not_hold_request_worker_until_native_resolver_returns(monkeypatch):
    import socket
    from topos.engine.request_budget import request
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    outcome = []
    def resolve(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        raise socket.gaierror('synthetic DNS failure')
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    def worker():
        try:
            with budget_scope(RequestBudget(.15)):
                request('GET', 'http://synthetic.invalid/', timeout=1)
        except Exception as exc:
            outcome.append(exc)
        finally:
            finished.set()
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(1)
        assert finished.wait(.5), 'request worker is waiting for native resolver shutdown'
        assert len(outcome) == 1 and isinstance(outcome[0], RequestStopped)
    finally:
        release.set()
        thread.join(3)


def test_resolver_capacity_survives_repeated_cancel_and_recovers(monkeypatch):
    import socket
    from topos.engine import budget_transport as transport
    from topos.engine.request_budget import request
    runtime = transport.BudgetTransport(requests=2, resolvers=1)
    monkeypatch.setattr(transport, '_runtime', runtime)
    release, entered = threading.Event(), threading.Event()
    native_calls=[]
    def resolve(*args, **kwargs):
        native_calls.append(1)
        entered.set()
        assert release.wait(3)
        raise socket.gaierror('synthetic DNS failure')
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    try:
        with budget_scope(RequestBudget(.1)), pytest.raises(RequestStopped):
            request('GET', 'http://synthetic.invalid/', timeout=1)
        assert entered.is_set()
        for _ in range(12):
            with budget_scope(RequestBudget(.3)), pytest.raises(Exception) as caught:
                request('GET', 'http://synthetic.invalid/', timeout=1)
            assert getattr(caught.value, 'code', None) == 'compute_busy'
        assert native_calls == [1]
        assert runtime.snapshot()['resolvers_active'] == 1
        assert runtime.snapshot()['requests_active'] == 0
        release.set()
        deadline = time.monotonic() + 1
        while runtime.snapshot()['resolvers_active'] and time.monotonic() < deadline:
            time.sleep(.005)
        assert runtime.snapshot()['resolvers_active'] == 0
        # Fresh resolver work becomes possible; a DNS failure is unavailable,
        # not saturation. No DNS lookup ever forwards to the real resolver.
        with budget_scope(RequestBudget(.5)), pytest.raises(Exception) as caught:
            request('GET', 'http://synthetic.invalid/', timeout=1)
        assert getattr(caught.value, 'code', None) == 'compute_unavailable'
        assert len(native_calls) == 2
    finally:
        release.set()
        runtime.close(1)


def test_transport_limit_retains_cancel_resistant_cleanup_and_context_isolation():
    from topos.engine.budget_transport import BudgetTransport
    runtime = BudgetTransport(requests=1, resolvers=1)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    seen=[]
    first = RequestBudget(.08)
    async def resistant():
        seen.append(current_budget())
        entered.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            while not release.is_set():
                await asyncio.sleep(.005)
        return 'fixture'
    def worker():
        try:
            with budget_scope(first), pytest.raises(RequestStopped):
                runtime.execute(first, resistant)
        finally:
            finished.set()
    thread=threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(1) and finished.wait(.5)
        assert runtime.snapshot()['requests_active'] == 1
        second = RequestBudget(.5)
        with budget_scope(second), pytest.raises(Exception) as caught:
            runtime.execute(second, resistant)
        assert getattr(caught.value, 'code', None) == 'compute_busy'
        release.set()
        deadline=time.monotonic()+1
        while runtime.snapshot()['requests_active'] and time.monotonic()<deadline:
            time.sleep(.005)
        async def fresh():
            seen.append(current_budget())
            return 'fresh'
        with budget_scope(second):
            assert runtime.execute(second, fresh) == 'fresh'
        assert seen == [first, second]
        assert current_budget() is None
    finally:
        release.set(); thread.join(1); runtime.close(1)


def test_transport_shutdown_is_bounded_and_refuses_replacement_work(monkeypatch):
    import socket
    from topos.engine import budget_transport as transport
    from topos.engine.request_budget import request
    runtime=transport.BudgetTransport(requests=1,resolvers=1)
    monkeypatch.setattr(transport,'_runtime',runtime)
    release=threading.Event()
    def resolve(*args, **kwargs):
        assert release.wait(3)
        raise socket.gaierror('synthetic')
    monkeypatch.setattr(socket,'getaddrinfo',resolve)
    try:
        with budget_scope(RequestBudget(.1)),pytest.raises(RequestStopped):
            request('GET','http://synthetic.invalid/',timeout=1)
        start=time.monotonic()
        snapshot=runtime.close(.2)
        assert time.monotonic()-start<.5
        assert snapshot['closed'] and snapshot['resolvers_active']==1
        with budget_scope(RequestBudget(.5)),pytest.raises(Exception) as caught:
            request('GET','http://synthetic.invalid/',timeout=1)
        assert getattr(caught.value,'code',None)=='compute_unavailable'
    finally:
        release.set(); runtime.close(1)


def test_explicit_cancel_retains_native_resolver_then_recovers(monkeypatch):
    import socket
    from topos.engine import budget_transport as transport
    from topos.engine.request_budget import request
    runtime=transport.BudgetTransport(requests=2,resolvers=1)
    monkeypatch.setattr(transport,'_runtime',runtime)
    entered,release,finished=threading.Event(),threading.Event(),threading.Event()
    native_threads=[]
    def resolve(*args,**kwargs):
        native_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))]
    monkeypatch.setattr(socket,'getaddrinfo',resolve)
    # Fake transport exercises the loop's resolver without opening any socket.
    class Client:
        def __init__(self,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def request(self,*args,**kwargs):
            await asyncio.get_running_loop().getaddrinfo('synthetic.invalid',80)
            return httpx.Response(200,json={'fixture':'ok'})
    monkeypatch.setattr(httpx,'AsyncClient',Client)
    budget=RequestBudget(2)
    outcomes=[]
    def worker():
        try:
            with budget_scope(budget): request('GET','http://synthetic.invalid',timeout=2)
        except Exception as exc: outcomes.append(exc)
        finally: finished.set()
    thread=threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(1)
        budget.cancel()
        assert finished.wait(.5)
        assert isinstance(outcomes[0],RequestStopped) and outcomes[0].reason=='cancelled'
        assert runtime.snapshot()['resolvers_active']==1
        release.set()
        end=time.monotonic()+1
        while runtime.snapshot()['resolvers_active'] and time.monotonic()<end: time.sleep(.005)
        for _ in range(5):
            with budget_scope(RequestBudget(.5)):
                assert request('GET','http://synthetic.invalid',timeout=1).json()=={'fixture':'ok'}
        assert len(native_threads)==6 and len(set(native_threads))==1
        assert runtime.snapshot()['resolvers_active']==0
    finally:
        release.set();thread.join(1);runtime.close(1)


def test_shutdown_before_dispatch_reclaims_scheduled_slot():
    from topos.engine.budget_transport import BudgetTransport
    runtime=BudgetTransport(requests=1,resolvers=1)
    loop_held,release,finished=threading.Event(),threading.Event(),threading.Event()
    calls=[]
    def hold_loop():
        loop_held.set();release.wait(3)
    runtime._loop.call_soon_threadsafe(hold_loop)
    assert loop_held.wait(1)
    budget=RequestBudget(1)
    async def never(): calls.append('unexpected')
    def worker():
        try:
            with budget_scope(budget),pytest.raises(RequestStopped): runtime.execute(budget,never)
        finally: finished.set()
    thread=threading.Thread(target=worker)
    thread.start()
    try:
        end=time.monotonic()+1
        while not runtime.snapshot()['requests_active'] and time.monotonic()<end: time.sleep(.005)
        assert runtime.snapshot()['requests_active']==1
        snapshot=runtime.close(.03)
        assert snapshot['requests_active']==1  # Scheduled operation not yet reaped.
        assert finished.wait(.2)
        release.set()
        snapshot=runtime.close(1)
        assert snapshot['requests_active']==0 and not snapshot['transport_thread_alive']
        assert not calls
    finally:
        release.set();thread.join(1);runtime.close(1)


def test_owned_loop_is_independent_of_application_loop_policy():
    from topos.engine.budget_transport import BudgetTransport
    class ForeignPolicy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):
            raise AssertionError('must not construct a policy-specific loop')
    prior=asyncio.get_event_loop_policy()
    runtime=None
    try:
        asyncio.set_event_loop_policy(ForeignPolicy())
        runtime=BudgetTransport(requests=1,resolvers=1)
        assert isinstance(runtime._loop,asyncio.SelectorEventLoop)
        budget=RequestBudget(.5)
        async def call():
            assert current_budget() is budget
            return 'fixture'
        with budget_scope(budget): assert runtime.execute(budget,call)=='fixture'
    finally:
        if runtime is not None: runtime.close(1)
        asyncio.set_event_loop_policy(prior)


def test_close_before_first_use_prevents_implicit_start(monkeypatch):
    from topos.engine import budget_transport as transport
    monkeypatch.setattr(transport,'_runtime',None)
    monkeypatch.setattr(transport,'_runtime_closed',False)
    assert transport.close_transport() is None
    with pytest.raises(Exception) as caught:
        transport.get_transport()
    assert getattr(caught.value,'code',None)=='compute_unavailable'
    assert transport._runtime is None

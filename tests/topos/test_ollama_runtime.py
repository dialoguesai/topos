"""Local Ollama is launched when a request needs it and :11434 is down.

No real process is spawned: every test injects reachability / app / CLI seams.
"""

from __future__ import annotations

import threading

from topos.engine import ollama_runtime


def test_already_up_does_not_launch():
    opened: list[int] = []
    served: list[int] = []
    assert ollama_runtime.ensure_running(
        base_url="http://localhost:11434",
        is_reachable=lambda: True,
        app_present=lambda: True,
        open_app=lambda: opened.append(1),
        cli_present=lambda: True,
        spawn_serve=lambda: served.append(1),
    ) is True
    assert opened == []
    assert served == []


def test_down_local_app_opens_and_waits():
    opened: list[int] = []
    served: list[int] = []

    def is_reachable() -> bool:
        return bool(opened)

    assert ollama_runtime.ensure_running(
        base_url="http://127.0.0.1:11434",
        is_reachable=is_reachable,
        app_present=lambda: True,
        open_app=lambda: opened.append(1),
        cli_present=lambda: True,
        spawn_serve=lambda: served.append(1),
        sleep=lambda _t: None,
        wait_sec=2.0,
        poll_interval=0.0,
    ) is True
    assert opened == [1]
    assert served == []


def test_down_local_no_app_spawns_serve():
    served: list[int] = []

    def is_reachable() -> bool:
        return bool(served)

    assert ollama_runtime.ensure_running(
        base_url="http://localhost:11434",
        is_reachable=is_reachable,
        app_present=lambda: False,
        open_app=lambda: None,
        cli_present=lambda: True,
        spawn_serve=lambda: served.append(1),
        sleep=lambda _t: None,
        wait_sec=2.0,
        poll_interval=0.0,
    ) is True
    assert served == [1]


def test_remote_url_does_not_launch():
    opened: list[int] = []
    probed: list[int] = []
    assert ollama_runtime.ensure_running(
        base_url="http://10.0.0.5:11434",
        is_reachable=lambda: probed.append(1) or False,
        app_present=lambda: True,
        open_app=lambda: opened.append(1),
        cli_present=lambda: True,
        spawn_serve=lambda: opened.append(2),
    ) is False
    assert opened == []
    assert probed == []


def test_nothing_installed_does_not_launch():
    assert ollama_runtime.ensure_running(
        base_url="http://localhost:11434",
        is_reachable=lambda: False,
        app_present=lambda: False,
        open_app=lambda: None,
        cli_present=lambda: False,
        spawn_serve=lambda: None,
    ) is False


def test_concurrent_callers_launch_once():
    launched: list[int] = []
    started = threading.Event()
    release = threading.Event()

    def is_reachable() -> bool:
        return bool(launched)

    def open_app() -> None:
        started.set()
        release.wait(timeout=2.0)
        launched.append(1)

    results: list[bool] = []

    def worker() -> None:
        results.append(
            ollama_runtime.ensure_running(
                base_url="http://localhost:11434",
                is_reachable=is_reachable,
                app_present=lambda: True,
                open_app=open_app,
                cli_present=lambda: False,
                spawn_serve=lambda: None,
                sleep=lambda _t: None,
                wait_sec=2.0,
                poll_interval=0.0,
            )
        )

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    assert started.wait(timeout=2.0)
    second.start()
    release.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)
    assert launched == [1]
    assert results == [True, True]


def test_is_local_base_url_accepts_loopback_only():
    assert ollama_runtime.is_local_base_url("http://localhost:11434") is True
    assert ollama_runtime.is_local_base_url("http://127.0.0.1:11434") is True
    assert ollama_runtime.is_local_base_url("http://[::1]:11434") is True
    assert ollama_runtime.is_local_base_url("http://10.0.0.5:11434") is False
    assert ollama_runtime.is_local_base_url("http://ollama.test") is False


def test_budgeted_generation_preserves_local_cold_start(monkeypatch):
    from topos.engine.backends.ollama import OllamaAdapter
    from topos.engine.request_budget import RequestBudget, budget_scope
    opened = []
    monkeypatch.setattr(ollama_runtime, 'default_is_reachable', lambda url: bool(opened))
    monkeypatch.setattr(ollama_runtime, 'default_app_present', lambda: True)
    monkeypatch.setattr(ollama_runtime, 'default_open_app', lambda: opened.append(True))
    adapter = OllamaAdapter('http://127.0.0.1:11434')
    monkeypatch.setattr(adapter, 'resolve_think', lambda *args: False)
    def generate(*args, **kwargs):
        assert opened, 'verification must retain request-triggered local startup'
        return {'text': 'synthetic'}
    monkeypatch.setattr(adapter, '_generate_once', generate)
    with budget_scope(RequestBudget(1)):
        assert adapter._generate('synthetic', 'synthetic')['text'] == 'synthetic'
    assert opened == [True]


def test_startup_lock_wait_obeys_request_budget():
    import time
    import pytest
    from topos.engine.request_budget import RequestBudget, RequestStopped, budget_scope
    # Release the competing owner on a finite watchdog even on pre-fix code.
    entered, release = threading.Event(), threading.Event()
    def holder():
        with ollama_runtime._LOCK:
            entered.set()
            release.wait(.3)
    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(1)
    try:
        started = time.monotonic()
        with budget_scope(RequestBudget(.04)), pytest.raises(RequestStopped):
            ollama_runtime.ensure_running(base_url='http://localhost:11434',
                is_reachable=lambda: False, app_present=lambda: False, cli_present=lambda: False)
        assert time.monotonic() - started < .2
    finally:
        release.set()
        thread.join(1)


def test_startup_does_not_launch_after_expiry():
    import pytest
    from topos.engine.request_budget import RequestBudget, RequestStopped, budget_scope
    budget = RequestBudget(1)
    def reachable():
        budget.cancel('deadline_exceeded')
        return False
    with budget_scope(budget), pytest.raises(RequestStopped):
        ollama_runtime.ensure_running(base_url='http://localhost:11434', is_reachable=reachable,
            app_present=lambda: True, open_app=lambda: pytest.fail('expired launch'))


def test_budget_reaches_app_launch_timeout(monkeypatch):
    from topos.engine import ollama_install
    from topos.engine.request_budget import RequestBudget, budget_scope
    calls = []
    monkeypatch.setattr(ollama_install.subprocess, 'run', lambda *args, **kwargs: calls.append(kwargs))
    with budget_scope(RequestBudget(.5)):
        ollama_runtime.default_open_app()
    assert len(calls) == 1
    assert 0 < calls[0]['timeout'] <= .5

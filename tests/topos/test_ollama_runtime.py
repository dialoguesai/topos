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

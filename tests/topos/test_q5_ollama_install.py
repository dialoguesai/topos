"""Q5 — the node can install Ollama on an explicit click (D3 revised).

Mirrors the ollama_pull job shape: start + poll over a single in-memory record.
No network and no real installer: every test injects a fake runner / platform /
app-presence seam. A guard that curled install.sh for real would download
~180 MB on CI and might prompt for sudo.
"""

from __future__ import annotations

import threading
import time

import pytest

from topos.core.handlers.registry import HANDLERS
from topos.engine import ollama_install


def _await_terminal(*, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = ollama_install.install_status()
        if record.get("state") in ("started", "error"):
            return record
        time.sleep(0.01)
    pytest.fail(
        f"install never reached a terminal state: {ollama_install.install_status()}"
    )


@pytest.fixture(autouse=True)
def _clear_job():
    ollama_install.reset_job()
    yield
    ollama_install.reset_job()


def test_status_before_anyone_asks_is_idle_not_an_exception():
    record = ollama_install.install_status()
    assert record["state"] == "idle"
    assert record.get("refused") is not True


def test_non_darwin_start_is_a_typed_refusal_and_never_runs_the_installer():
    ran = {"n": 0}

    def runner(**_kwargs):
        ran["n"] += 1
        return 0, "should not run"

    record = ollama_install.start_install(
        platform="linux",
        run_install=runner,
        app_present=lambda: False,
        open_app=lambda: None,
    )
    assert record.get("refused") is True
    assert record["state"] == "error"
    assert record["reason"] == "unsupported_platform"
    assert ran["n"] == 0
    assert ollama_install.install_status()["refused"] is True


def test_a_second_start_while_installing_is_a_noop_acknowledging_the_running_job():
    gate = threading.Event()
    starts = {"n": 0}

    def runner(*, on_status=None):
        starts["n"] += 1
        if on_status is not None:
            on_status("downloading Ollama.app")
        gate.wait(timeout=5.0)
        return 0, "ok"

    first = ollama_install.start_install(
        platform="Darwin",
        run_install=runner,
        app_present=lambda: True,
        open_app=lambda: None,
        is_reachable=lambda: True,
    )
    assert first["state"] == "installing"
    second = ollama_install.start_install(
        platform="Darwin",
        run_install=runner,
        app_present=lambda: True,
        open_app=lambda: None,
        is_reachable=lambda: True,
    )
    assert second["state"] == "installing"
    gate.set()
    final = _await_terminal()
    assert final["state"] == "started"
    assert starts["n"] == 1


def test_darwin_resilience_script_failure_with_app_present_ends_in_started():
    opened = {"n": 0}

    def runner(*, on_status=None):
        if on_status is not None:
            on_status("curl: (22) The requested URL returned error: 403")
        return 1, "curl: (22) The requested URL returned error: 403"

    record = ollama_install.start_install(
        platform="Darwin",
        run_install=runner,
        app_present=lambda: True,
        open_app=lambda: opened.__setitem__("n", opened["n"] + 1),
        is_reachable=lambda: True,
    )
    assert record["state"] == "installing"
    final = _await_terminal()
    assert final["state"] == "started", final
    assert opened["n"] == 1
    assert final.get("refused") is not True


def test_darwin_resilience_script_failure_without_app_ends_in_error_with_last_line():
    def runner(*, on_status=None):
        if on_status is not None:
            on_status("permission denied writing /usr/local/bin/ollama")
        return 1, "permission denied writing /usr/local/bin/ollama"

    ollama_install.start_install(
        platform="Darwin",
        run_install=runner,
        app_present=lambda: False,
        open_app=lambda: None,
        is_reachable=lambda: False,
    )
    final = _await_terminal()
    assert final["state"] == "error"
    assert "permission denied" in final["error"]
    assert "permission denied" in (final.get("status") or "")


def test_darwin_successful_script_ends_in_started():
    def runner(*, on_status=None):
        if on_status is not None:
            on_status(">>> Installing ollama to /usr/local/bin")
            on_status(">>> Starting ollama")
        return 0, "ok"

    ollama_install.start_install(
        platform="Darwin",
        run_install=runner,
        app_present=lambda: True,
        open_app=lambda: None,
        is_reachable=lambda: True,
    )
    final = _await_terminal()
    assert final["state"] == "started"
    assert "Starting ollama" in (final.get("status") or "")


@pytest.mark.asyncio
async def test_install_message_handlers_are_registered_and_relay_the_job_record(monkeypatch):
    def runner(*, on_status=None):
        if on_status is not None:
            on_status(">>> Installing")
        return 0, "ok"

    monkeypatch.setattr(ollama_install, "default_platform", lambda: "Darwin")
    monkeypatch.setattr(ollama_install, "default_run_install", runner)
    monkeypatch.setattr(ollama_install, "default_app_present", lambda: True)
    monkeypatch.setattr(ollama_install, "default_open_app", lambda: None)
    monkeypatch.setattr(ollama_install, "default_is_reachable", lambda: True)

    assert "ollama_install" in HANDLERS
    assert "ollama_install_status" in HANDLERS

    started = await HANDLERS["ollama_install"](
        {"id": "req-1", "type": "ollama_install", "payload": {}}
    )
    assert started["id"] == "req-1"
    assert started["status"] == "ok"
    assert started["payload"]["state"] in ("installing", "started")

    _await_terminal()
    polled = await HANDLERS["ollama_install_status"](
        {"id": "req-2", "type": "ollama_install_status", "payload": {}}
    )
    assert polled["status"] == "ok"
    assert polled["payload"]["state"] == "started"


def test_install_operations_are_advertised_for_the_local_engine_profile():
    from topos.engine.registration import RUNTIME_PROFILE_OPERATIONS

    local = set(RUNTIME_PROFILE_OPERATIONS["local_engine"])
    assert {"ollama_install", "ollama_install_status"} <= local

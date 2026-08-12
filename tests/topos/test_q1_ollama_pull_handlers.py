"""Q1 — the node can pull a model on request, and says how far it got.

The adapter has been able to download models since forever
(`engine/backends/ollama.py:151`) but nothing on the websocket could ask it to,
so the product shipped packs naming tags the machine did not have. These guards
cover the two new messages and the in-memory progress they read from.

No network: every test drives a fake adapter. A guard that pulled for real
would download gigabytes on CI.
"""

from __future__ import annotations

import threading
import time

import pytest

from topos.core.handlers.registry import HANDLERS
from topos.engine import ollama_pull


class _FakeAdapter:
    """Streams a few /api/pull frames, then owns the tag."""

    def __init__(self, *, frames=None, error=None, gate=None):
        self._frames = frames if frames is not None else [(25, 100), (60, 100), (100, 100)]
        self._error = error
        #: When set, the pull blocks here until the test releases it — the only
        #: way to hold a fake download "in flight" long enough to click twice.
        self._gate = gate
        self.installed: list[str] = []
        self.pulled: list[str] = []

    def list_models(self):
        return list(self.installed)

    def pull_model(self, model_name, *, stream=True, on_progress=None):
        self.pulled.append(model_name)
        for completed, total in self._frames:
            if on_progress is not None:
                on_progress({"status": "downloading", "completed": completed, "total": total})
        if self._gate is not None:
            self._gate.wait(timeout=5.0)
        if self._error is not None:
            raise self._error
        self.installed.append(model_name)


def _await_terminal(model: str, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = ollama_pull.pull_status(model)
        if record.get("state") in ("done", "error"):
            return record
        time.sleep(0.01)
    pytest.fail(f"pull of {model!r} never reached a terminal state: {ollama_pull.pull_status(model)}")


@pytest.fixture(autouse=True)
def _clear_progress():
    ollama_pull.reset_progress()
    yield
    ollama_pull.reset_progress()


def test_pull_streams_monotonic_progress_and_ends_with_the_tag_installed():
    adapter = _FakeAdapter()
    ollama_pull.start_pull("llama3.2:latest", adapter=adapter)

    seen = []
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        record = ollama_pull.pull_status("llama3.2:latest")
        seen.append(int(record.get("completed") or 0))
        if record.get("state") in ("done", "error"):
            break
        time.sleep(0.005)

    assert seen == sorted(seen), f"progress went backwards: {seen}"
    final = _await_terminal("llama3.2:latest")
    assert final["state"] == "done"
    assert final["completed"] == final["total"] == 100
    assert "llama3.2:latest" in adapter.list_models()


def test_a_garbage_tag_surfaces_the_ollama_error_rather_than_polling_forever():
    adapter = _FakeAdapter(frames=[], error=RuntimeError("pull model manifest: file does not exist"))
    ollama_pull.start_pull("not-a-real-model:9001b", adapter=adapter)

    final = _await_terminal("not-a-real-model:9001b")
    assert final["state"] == "error"
    assert "manifest" in final["error"]
    assert "not-a-real-model:9001b" not in adapter.list_models()


def test_status_for_a_tag_nobody_pulled_is_idle_not_an_exception():
    record = ollama_pull.pull_status("never-asked:latest")
    assert record["state"] == "idle"
    assert record["model"] == "never-asked:latest"


def test_a_second_start_for_an_in_flight_tag_does_not_start_a_second_download():
    # D4: downloads are multi-gigabyte. A double-clicked CTA must not cost the
    # owner twice the bandwidth.
    gate = threading.Event()
    adapter = _FakeAdapter(frames=[(i, 100) for i in range(0, 101, 5)], gate=gate)
    ollama_pull.start_pull("llama3.2:latest", adapter=adapter)
    second = ollama_pull.start_pull("llama3.2:latest", adapter=adapter)
    assert second["state"] == "pulling"
    gate.set()
    _await_terminal("llama3.2:latest")
    assert adapter.pulled == ["llama3.2:latest"]


@pytest.mark.asyncio
async def test_pull_message_handlers_are_registered_and_relay_the_progress_record(monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(ollama_pull, "OllamaAdapter", lambda *a, **k: adapter)

    assert "ollama_pull_model" in HANDLERS
    assert "ollama_pull_status" in HANDLERS

    started = await HANDLERS["ollama_pull_model"](
        {"id": "req-1", "type": "ollama_pull_model", "payload": {"model": "llama3.2:latest"}}
    )
    assert started["id"] == "req-1"
    assert started["status"] == "ok"
    assert started["payload"]["model"] == "llama3.2:latest"

    _await_terminal("llama3.2:latest")
    polled = await HANDLERS["ollama_pull_status"](
        {"id": "req-2", "type": "ollama_pull_status", "payload": {"model": "llama3.2:latest"}}
    )
    assert polled["status"] == "ok"
    assert polled["payload"]["state"] == "done"


@pytest.mark.asyncio
async def test_pull_without_a_model_is_rejected_rather_than_pulling_something_arbitrary():
    result = await HANDLERS["ollama_pull_model"](
        {"id": "req-3", "type": "ollama_pull_model", "payload": {}}
    )
    assert result["status"] == "error"


def test_pull_operations_are_advertised_for_the_local_engine_profile():
    from topos.engine.registration import RUNTIME_PROFILE_OPERATIONS

    local = set(RUNTIME_PROFILE_OPERATIONS["local_engine"])
    assert {"ollama_pull_model", "ollama_pull_status"} <= local

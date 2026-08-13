"""Q1 fix round — the node-side contracts the first round left unguarded.

Three of the sprint's node changes shipped with no test that could see them
disappear, and two more are defects the sprint introduced:

* §1.7's "Ollama is not running" answer (`handle_ollama_list_models`) is the
  only thing that lets the CP tell an absent server apart from an empty one.
  The whole 502 branch could be deleted and 102 node tests stayed green,
  because the only guard on that contract lives on the CP side and mocks the
  relay away.
* The monotonic clamp in `ollama_pull._apply_frame` exists because /api/pull
  reports per-layer byte counts and restarts at zero on each new layer. Every
  existing pull test feeds an already-monotonic frame sequence, so the clamp
  could be replaced by a plain assignment with nothing to show for it.
* `build_engine_capabilities()` runs once, at registration. A node started
  before Ollama never re-advertises the provider for the rest of its process
  life, which is exactly journey Branch C ("owner installs Ollama, comes
  back") failing silently.
* The reachability probe is blocking urllib. It is built on the asyncio
  presence loop, so a hung Ollama socket stalls every other engine task for
  the timeout.
* `_PROGRESS` keeps one record per tag ever asked for, forever.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time

import pytest
from fastapi import HTTPException

from topos.core.handlers import device
from topos.core.handlers.registry import HANDLERS
from topos.engine import ollama_pull, registration


# --------------------------------------------------------------------------
# M19b — an unreachable Ollama is data, not a transport failure
# --------------------------------------------------------------------------


class _LLMService:
    def __init__(self, *, raises=None, models=None):
        self._raises = raises
        self._models = models or []

    async def list_ollama_models(self):
        if self._raises is not None:
            raise self._raises
        return {"models": list(self._models)}


class _Services:
    def __init__(self, llm):
        self.llm = llm


def _services(monkeypatch, llm) -> None:
    monkeypatch.setattr(device, "get_services", lambda: _Services(llm))


@pytest.mark.asyncio
async def test_an_ollama_that_is_not_running_answers_ok_with_reachable_false(monkeypatch):
    # The CP reads this payload to decide what the machine has installed. An
    # `error` envelope is indistinguishable from a dropped relay, and the CP
    # would have to guess; `reachable: False` is the fact itself.
    _services(
        monkeypatch,
        _LLMService(raises=HTTPException(status_code=502, detail="Ollama unreachable at http://x")),
    )

    result = await HANDLERS["ollama_list_models"](
        {"id": "req-1", "type": "ollama_list_models"}
    )

    assert result["status"] == "ok", (
        "an absent Ollama came back as a transport error, so the CP cannot tell "
        f"it apart from a node that never answered: {result}"
    )
    assert result["payload"]["reachable"] is False
    assert result["payload"]["models"] == []
    assert result["payload"]["ollama_models_schema"] == 1
    assert "unreachable" in result["payload"]["detail"]


@pytest.mark.asyncio
async def test_a_running_ollama_with_nothing_pulled_is_reachable_with_no_models(monkeypatch):
    # The other half of the §1.7 distinction: empty is not absent.
    _services(monkeypatch, _LLMService(models=[]))

    result = await HANDLERS["ollama_list_models"](
        {"id": "req-2", "type": "ollama_list_models"}
    )

    assert result["status"] == "ok"
    assert result["payload"]["reachable"] is True
    assert result["payload"]["models"] == []
    assert result["payload"]["ollama_models_schema"] == 1


@pytest.mark.asyncio
async def test_a_running_ollama_reports_its_tags_and_says_so(monkeypatch):
    _services(monkeypatch, _LLMService(models=["llama3.1:8b", "llama3.2:latest"]))

    result = await HANDLERS["ollama_list_models"](
        {"id": "req-3", "type": "ollama_list_models"}
    )

    assert result["payload"]["reachable"] is True
    assert result["payload"]["models"] == ["llama3.1:8b", "llama3.2:latest"]
    assert result["payload"]["ollama_models_schema"] == 1


@pytest.mark.asyncio
async def test_llm_disabled_is_still_an_error_not_a_quiet_empty_machine(monkeypatch):
    # 403 means "you may not ask", which is not a claim about what is installed.
    # Folding it into `reachable: False` would let a permissions problem read as
    # an empty machine and silently rewrite the owner's packs.
    _services(monkeypatch, _LLMService(raises=HTTPException(status_code=403, detail="LLM is disabled")))

    result = await HANDLERS["ollama_list_models"](
        {"id": "req-4", "type": "ollama_list_models"}
    )

    assert result["status"] == "error"
    assert result["error_code"] == 403


# --------------------------------------------------------------------------
# M16 — progress never goes backwards
# --------------------------------------------------------------------------


class _LayeredAdapter:
    """Emits what /api/pull really emits: per-layer counters that restart."""

    def __init__(self, frames):
        self._frames = frames
        self.installed: list[str] = []

    def list_models(self):
        return list(self.installed)

    def pull_model(self, model_name, *, stream=True, on_progress=None):
        for completed, total in self._frames:
            if on_progress is not None:
                on_progress({"status": "downloading", "completed": completed, "total": total})
        self.installed.append(model_name)


@pytest.fixture(autouse=True)
def _clear_progress():
    ollama_pull.reset_progress()
    yield
    ollama_pull.reset_progress()


def test_a_new_layer_restarting_at_zero_does_not_rewind_the_bar():
    # Layer one finishes at 4_000_000, then layer two opens at 0. Reported
    # verbatim, the owner watches the bar snap back to the start mid-download.
    tag = "llama3.2:latest"
    frames = [
        (1_000_000, 4_000_000),
        (4_000_000, 4_000_000),
        (0, 9_000_000),
        (3_000_000, 9_000_000),
    ]
    seen: list[int] = []
    adapter = _LayeredAdapter(frames)

    original = ollama_pull._apply_frame

    def _observe(t, frame):
        original(t, frame)
        seen.append(int(ollama_pull.pull_status(t)["completed"]))

    ollama_pull._apply_frame = _observe
    try:
        ollama_pull.start_pull(tag, adapter=adapter)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if ollama_pull.pull_status(tag)["state"] in ("done", "error"):
                break
            time.sleep(0.005)
    finally:
        ollama_pull._apply_frame = original

    assert seen == sorted(seen), f"the progress bar rewound: {seen}"


def test_a_shrinking_total_does_not_rewind_either():
    tag = "mistral:7b"
    ollama_pull._apply_frame(tag, {"status": "downloading", "completed": 500, "total": 1000})
    ollama_pull._apply_frame(tag, {"status": "downloading", "completed": 10, "total": 20})

    record = ollama_pull.pull_status(tag)
    assert record["completed"] == 500, f"completed rewound to {record['completed']}"
    assert record["total"] == 1000, f"total rewound to {record['total']}"


# --------------------------------------------------------------------------
# M22 — the progress store is bounded
# --------------------------------------------------------------------------


def test_finished_pulls_do_not_accumulate_records_forever():
    # A long-lived node polled by a browser accrues one dict per tag ever
    # named, including typos. Terminal records are the ones nobody needs.
    for index in range(400):
        tag = f"junk-{index}:latest"
        ollama_pull._apply_frame(tag, {"status": "downloading", "completed": 1, "total": 1})
        with ollama_pull._LOCK:
            ollama_pull._PROGRESS[tag]["state"] = ollama_pull.STATE_DONE

    with ollama_pull._LOCK:
        held = len(ollama_pull._PROGRESS)
    assert held <= 128, f"the pull progress store grew to {held} records with no eviction"


def test_eviction_never_drops_a_download_that_is_still_running():
    gate = threading.Event()

    class _Blocking:
        def pull_model(self, model_name, *, stream=True, on_progress=None):
            gate.wait(timeout=5.0)

    ollama_pull.start_pull("real:8b", adapter=_Blocking())
    for index in range(400):
        tag = f"junk-{index}:latest"
        ollama_pull._apply_frame(tag, {"status": "downloading", "completed": 1, "total": 1})
        with ollama_pull._LOCK:
            ollama_pull._PROGRESS[tag]["state"] = ollama_pull.STATE_DONE

    assert ollama_pull.pull_status("real:8b")["state"] == "pulling", (
        "eviction forgot an in-flight download, so the CTA would offer to start it again"
    )
    gate.set()


# --------------------------------------------------------------------------
# M21 — the capability is re-probed, and probing does not block the loop
# --------------------------------------------------------------------------


def test_the_heartbeat_re_advertises_capabilities_so_ollama_can_arrive_late(monkeypatch):
    # Journey Branch C: the owner installs Ollama while the node is running.
    # Registration already happened and said "no ollama"; if the heartbeat
    # carries no capabilities the CP believes that forever.
    monkeypatch.setattr(registration, "ollama_is_reachable", lambda: True)
    monkeypatch.setattr(registration.settings, "engine_ollama_base_url", "http://127.0.0.1:11434")

    message = registration.build_engine_heartbeat_message()

    capabilities = message["payload"].get("capabilities")
    assert capabilities, (
        "the heartbeat carries no capabilities, so a node that started before "
        "Ollama stays advertised as Ollama-less for its whole process life"
    )
    assert "ollama" in capabilities["providers"]


def test_the_heartbeat_stops_advertising_ollama_once_it_goes_away(monkeypatch):
    monkeypatch.setattr(registration, "ollama_is_reachable", lambda: False)
    monkeypatch.setattr(registration.settings, "engine_ollama_base_url", "http://127.0.0.1:11434")

    capabilities = registration.build_engine_heartbeat_message()["payload"]["capabilities"]
    assert "ollama" not in capabilities["providers"]


@pytest.mark.parametrize(
    "builder_name",
    ["build_engine_register_message_async", "build_engine_heartbeat_message_async"],
)
@pytest.mark.asyncio
async def test_the_presence_messages_can_be_built_without_blocking_the_event_loop(
    monkeypatch, builder_name
):
    # `ollama_is_reachable` is blocking urllib with a socket timeout, and the
    # presence loop is asyncio. A hung Ollama must not stall the engine's other
    # tasks, so the builders the loop uses have to be awaitable.
    builder = getattr(registration, builder_name, None)
    assert builder is not None, f"{builder_name} does not exist; the presence loop still blocks"
    assert inspect.iscoroutinefunction(builder), f"{builder_name} is not awaitable"

    slept = threading.Event()
    probe_duration = 0.4

    def _slow_probe() -> bool:
        time.sleep(probe_duration)
        slept.set()
        return True

    monkeypatch.setattr(registration, "ollama_is_reachable", _slow_probe)
    monkeypatch.setattr(registration.settings, "engine_ollama_base_url", "http://127.0.0.1:11434")

    # Ticked WHILE the builder is in flight, not alongside a fixed-length
    # co-routine. A bounded `for _ in range(20)` companion always runs to
    # completion whether or not the loop was stalled — blocking moves the wall
    # clock, never the tick count — so it cannot tell `asyncio.to_thread` from a
    # builder that calls the blocking probe inline. Counting only the ticks that
    # fit inside the probe can: an inline call yields exactly one, because the
    # single `await` below is the moment the builder gets to run and it does not
    # give the loop back until the socket returns.
    builder_task = asyncio.create_task(builder())
    ticks = 0
    started = time.monotonic()
    while not builder_task.done():
        await asyncio.sleep(0.01)
        ticks += 1
        assert time.monotonic() - started < 10.0, "the presence builder never finished"
    message = await builder_task

    assert slept.is_set(), "the slow probe never ran, so this proves nothing"
    assert message["payload"]["capabilities"]["providers"], "the builder returned no capabilities"
    assert ticks >= 10, (
        f"only {ticks} co-operative ticks ran during a {probe_duration}s probe; "
        f"{builder_name} is running the blocking urllib call on the event loop, "
        "so a hung Ollama stalls every other engine task"
    )


@pytest.mark.asyncio
async def test_the_async_builders_agree_with_the_synchronous_ones(monkeypatch):
    monkeypatch.setattr(registration, "ollama_is_reachable", lambda: True)

    register = await registration.build_engine_register_message_async()
    assert register["type"] == "engine_register"
    assert register["payload"]["capabilities"]["providers"] == (
        registration.build_engine_register_message()["payload"]["capabilities"]["providers"]
    )


def test_the_presence_loop_actually_awaits_the_non_blocking_builders():
    # A builder nothing calls is a dead parameter by another name: the previous
    # round of this sprint shipped exactly that. Read the source of the loop.
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "topos" / "app.py"
    tree = ast.parse(source.read_text())

    awaited: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            if isinstance(node.value.func, ast.Name):
                awaited.add(node.value.func.id)

    assert {"build_engine_register_message_async", "build_engine_heartbeat_message_async"} <= awaited, (
        f"the presence loop still builds presence messages synchronously (awaits: {sorted(awaited)})"
    )
    assert "build_engine_capabilities" not in called

"""The node's local model serving rules (topos/local_serving.py): one context per model, and the watchdog that
unloads an idle model when a node request is stuck behind it. Scenarios are the ones measured on 2 Nov."""
import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from topos import local_serving as ls

BIG = "smtek/Qwen3.8-27B:IQ2_M"
SMALL = "llama3.2:latest"
MID = "qwen3.5:9b-mlx"
GIB = 2**30


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(ls, "CONTEXT", 32768)
    monkeypatch.setattr(ls, "STEPS", (8192, 16384))
    monkeypatch.setattr(ls, "_contexts", {})
    monkeypatch.setattr(ls, "WAIT_SECONDS", 30.0)
    monkeypatch.setattr(ls, "SLOTS", 1)
    monkeypatch.setattr(ls, "_in_flight", {})
    monkeypatch.setattr(ls, "_held", {})
    monkeypatch.setattr(ls, "_loaded", ([], float("-inf")))
    monkeypatch.setattr(ls, "_released_at", {})
    monkeypatch.setattr(ls, "GROWTH_LIMIT", 2 * GIB)
    monkeypatch.setattr(ls, "_sizes", {})
    monkeypatch.setattr(ls, "_recycling", set())
    monkeypatch.setattr(ls, "_sized_at", float("-inf"))
    monkeypatch.setattr(ls, "_stats", {"releases": 0, "last_release": None, "lowered_context": 0,
                                       "kept_larger_context": 0, "raised_context": 0, "switch_holds": 0,
                                       "held_s_max": 0.0, "recycles": 0, "last_recycle": None})


def test_every_caller_context_becomes_the_one_serving_context():
    # The five contexts the node's callers asked for on 1.5.39, for a prompt that needs a few thousand tokens.
    for asked in (None, 4096, 8192, 12288, 81920):
        assert ls.serving_context(asked, prompt_chars=12_000, num_predict=2048) == 32768


def test_a_prompt_that_needs_more_keeps_the_larger_context_it_asked_for():
    assert ls.serving_context(81920, prompt_chars=120_000, num_predict=2048) == 81920
    # Asking nothing, it gets the serving context (the host's default, which it got before).
    assert ls.serving_context(None, prompt_chars=120_000, num_predict=2048) == 32768


def _context(model, chars, num_predict=None, asked=None):
    return ls.serving_context(asked, prompt_chars=chars, num_predict=num_predict, model=model)


def test_a_model_starts_at_the_first_step_its_request_fits():
    # 3 Nov: verification's llama3.2 prompts stayed under 1,600 tokens; the 27B's reached about 9,000.
    assert _context(SMALL, 4_800, num_predict=64) == 8192
    assert _context(BIG, 27_000) == 16384     # 9,000 tokens and a 2,048-token reply
    assert ls.status()["contexts"] == {SMALL: 8192, BIG: 16384}


def test_a_model_moves_up_when_a_request_needs_more_and_never_down():
    assert _context(BIG, 3_000) == 8192
    assert _context(BIG, 60_000) == 32768    # 20,000 tokens: past the last step, so CONTEXT
    assert _context(BIG, 3_000) == 32768     # a short request after it does not reload the model
    assert _context("llama3.2", 3_000) == 8192  # each model has its own
    assert ls.status()["raised_context"] == 1


def test_a_reply_with_no_length_keeps_room_for_one():
    # 7,000 prompt tokens: with 100 reply tokens 8,192 fits, with no length given it does not.
    assert _context(SMALL, 21_000, num_predict=100) == 8192
    assert _context(BIG, 21_000) == 16384


def test_steps_off_serves_every_model_at_the_one_context(monkeypatch):
    monkeypatch.setattr(ls, "STEPS", ())
    assert _context(SMALL, 300, num_predict=10) == 32768


def test_a_request_past_the_context_keeps_its_own_without_moving_the_model():
    assert _context(BIG, 3_000) == 8192
    assert _context(BIG, 300_000, asked=81920) == 81920
    assert _context(BIG, 3_000) == 8192


def test_off_leaves_each_caller_its_own(monkeypatch):
    monkeypatch.setattr(ls, "CONTEXT", 0)
    body = {"model": SMALL, "messages": [{"role": "user", "content": "x"}], "options": {"num_ctx": 8192}}
    assert ls.prepare(body) is body


def test_prepare_sets_the_context_without_touching_the_callers_body():
    body = {"model": SMALL, "messages": [{"role": "user", "content": "x"}], "options": {"temperature": 0}}
    prepared = ls.prepare(body)
    assert prepared["options"] == {"temperature": 0, "num_ctx": 8192}  # a short prompt: the model's first step
    assert body["options"] == {"temperature": 0}


def test_an_unload_is_sent_as_it_is():
    # The engine's unload sends a single space as its prompt.
    for body in ({"model": BIG, "keep_alive": 0}, {"model": BIG, "prompt": " ", "keep_alive": "0"}):
        assert ls.prepare(body) is body
        with ls.request(body):
            assert ls.in_flight() == []


def test_lowering_home_chats_pack_context_is_counted():
    ls.prepare({"model": BIG, "prompt": "a" * 10_000, "options": {"num_ctx": 81920}})
    ls.prepare({"model": BIG, "prompt": "a" * 200_000, "options": {"num_ctx": 81920}})
    assert ls.status()["lowered_context"] == 1 and ls.status()["kept_larger_context"] == 1


def test_a_request_is_counted_until_it_ends_however_it_ends():
    with pytest.raises(RuntimeError):
        with ls.request({"model": "llama3.2", "prompt": "hi"}) as body:
            assert body["options"]["num_ctx"] == 8192
            assert [(m, c) for m, c, _ in ls.in_flight()] == [(SMALL, 8192)]
            raise RuntimeError("the host went away")
    assert ls.in_flight() == []


def _ps(name, context):
    return {"name": name, "model": name, "context_length": context}


def test_the_measured_stall_unloads_the_idle_27b():
    # 2 Nov, idle node: the 27B loaded at home chat's 81,920, a llama3.2 request waiting past the limit.
    names, waited = ls.release_blocked([_ps(BIG, 81920)], [(SMALL, 32768, 0.0)], now=31.0)
    assert names == [BIG] and waited == 31.0


def test_the_27b_at_another_context_is_waiting_too():
    names, _ = ls.release_blocked([_ps(BIG, 81920)], [(BIG, 32768, 0.0)], now=40.0)
    assert names == [BIG]


def test_nothing_is_unloaded_before_the_wait_or_when_nothing_waits_for_a_load():
    assert ls.release_blocked([_ps(BIG, 81920)], [(SMALL, 32768, 0.0)], now=29.0) == ([], 0.0)
    # A long request on a model that is loaded is generating, not waiting (home chat's 190 s reply).
    assert ls.release_blocked([_ps(BIG, 32768)], [(BIG, 32768, 0.0)], now=190.0) == ([], 0.0)


def test_a_model_a_node_request_is_generating_on_is_never_unloaded():
    loaded = [_ps(BIG, 32768), _ps("qwen3.5:9b-mlx", 32768)]
    requests = [(BIG, 32768, 0.0), (SMALL, 32768, 0.0)]
    names, _ = ls.release_blocked(loaded, requests, now=45.0)
    assert names == ["qwen3.5:9b-mlx"]


def test_a_model_just_released_is_left_alone_for_the_cooldown():
    loaded, requests = [_ps(BIG, 81920)], [(SMALL, 32768, 0.0)]
    assert ls.release_blocked(loaded, requests, now=50.0, released_at={BIG: 40.0}, cooldown=15.0)[0] == []
    assert ls.release_blocked(loaded, requests, now=56.0, released_at={BIG: 40.0}, cooldown=15.0)[0] == [BIG]


def test_the_watchdog_check_sends_one_unload_and_reports_it(monkeypatch):
    sent = []

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [_ps(BIG, 81920)]})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"done": True, "done_reason": "unload"})

    clock = iter([100.0])
    monkeypatch.setattr(ls.time, "monotonic", lambda: next(clock, 100.0))
    ls._in_flight[1] = (SMALL, 32768, 60.0, ls.LIVE)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ls._check_once(client, "http://host")

    assert asyncio.run(run()) == [BIG]
    assert sent == [{"model": BIG, "keep_alive": 0}]
    status = ls.status()
    assert status["releases"] == 1 and status["last_release"]["model"] == BIG
    assert status["last_release"]["waited_s"] == 40.0


def test_the_watchdog_does_not_ask_the_host_while_nothing_waits(monkeypatch):
    def handler(request):
        raise AssertionError("asked the host")

    ls._in_flight[1] = (SMALL, 32768, ls.time.monotonic(), ls.LIVE)
    ls._sized_at = ls.time.monotonic()  # sizes read moments ago

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ls._check_once(client, "http://host")

    assert asyncio.run(run()) == []


def _sized(name, context, gib):
    return {**_ps(name, context), "size": int(gib * GIB)}


def test_the_9b_that_grew_past_the_limit_with_nothing_on_it_is_unloaded():
    # 3 Nov: the 9B's runner loaded at 8.4 GiB and grew to 13.5 GiB at 8,192 while serving.
    sizes = {}
    assert ls.grown_models([_sized(MID, 8192, 8.4)], sizes, busy=[]) == []
    assert ls.grown_models([_sized(MID, 8192, 10.0)], sizes, busy=[]) == []      # 1.6 GiB: under the limit
    grown = ls.grown_models([_sized(MID, 8192, 10.5)], sizes, busy=[])
    assert [name for name, _ in grown] == [MID] and grown[0][1] == int(10.5 * GIB) - int(8.4 * GIB)


def test_a_model_a_node_request_is_on_is_never_unloaded_for_growth():
    sizes = {(MID, 8192): int(8.4 * GIB)}
    assert ls.grown_models([_sized(MID, 8192, 13.5)], sizes, busy=[MID]) == []
    assert [n for n, _ in ls.grown_models([_sized(MID, 8192, 13.5)], sizes, busy=[SMALL])] == [MID]


def test_moving_up_a_step_is_not_growth():
    # llama3.2 is 2.9 GiB at 8,192 and 5.6 at 32,768: a reload at the larger step is another entry.
    sizes = {}
    ls.grown_models([_sized(SMALL, 8192, 2.9)], sizes, busy=[])
    assert ls.grown_models([_sized(SMALL, 32768, 5.6)], sizes, busy=[]) == []
    assert sizes == {(SMALL, 32768): int(5.6 * GIB)}


def test_a_model_the_host_unloaded_starts_again_from_its_new_size():
    sizes = {(MID, 8192): int(8.4 * GIB)}
    ls.grown_models([_sized(BIG, 16384, 9.8)], sizes, busy=[])       # the 9B is gone
    assert (MID, 8192) not in sizes
    ls.grown_models([_sized(MID, 8192, 9.0)], sizes, busy=[])
    assert ls.grown_models([_sized(MID, 8192, 10.5)], sizes, busy=[]) == []


def test_a_host_that_reports_no_size_and_the_switch_off_unload_nothing():
    sizes = {}
    assert ls.grown_models([_ps(MID, 8192)], sizes, busy=[]) == [] and sizes == {}
    ls.grown_models([_sized(MID, 8192, 8.4)], sizes, busy=[], limit=0)
    assert ls.grown_models([_sized(MID, 8192, 20.0)], sizes, busy=[], limit=0) == []


def test_a_model_being_unloaded_for_growth_holds_new_requests_to_it_only():
    ls._recycling.add(MID)
    ls._held[1] = (MID, ls.LIVE, 0.0)
    ls._held[2] = (SMALL, ls.LIVE, 0.0)
    assert ls._admit(1, 1.0, None) == (False, False)
    assert ls._admit(2, 1.0, None) == (True, False)
    assert ls._admit(1, 1.0, 0.5) == (True, False)    # never past the caller's own limit


def test_the_watchdog_reads_sizes_with_nothing_waiting_and_unloads_the_grown_9b(monkeypatch):
    sizes = iter([8.4, 10.6])
    sent = []

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [_sized(MID, 8192, next(sizes)), _sized(BIG, 16384, 9.8)]})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"done": True, "done_reason": "unload"})

    now = [100.0]
    monkeypatch.setattr(ls, "time", type("Clock", (), {"monotonic": staticmethod(lambda: now[0]),
                                                       "time": staticmethod(ls.time.time)}))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await ls._check_once(client, "http://host")
            now[0] = 105.0
            early = await ls._check_once(client, "http://host")   # 5 s later: not due, no read
            now[0] = 111.0
            return first, early, await ls._check_once(client, "http://host")

    assert asyncio.run(run()) == ([], [], [MID])
    assert sent == [{"model": MID, "keep_alive": 0}]
    status = ls.status()
    assert status["recycles"] == 1 and status["last_recycle"]["model"] == MID
    assert status["last_recycle"]["grew_mib"] == (int(10.6 * GIB) - int(8.4 * GIB)) // 2**20
    assert status["releases"] == 0 and ls._recycling == set() and (MID, 8192) not in ls._sizes
    assert status["recycle_growth_mib"] == 2048


_POST = re.compile(r'(/api/chat|/api/generate)"')


def test_every_node_model_call_goes_through_the_serving_rules():
    """A new caller that posts to the local host on its own would bring back the reloads and hide from the
    watchdog. Every module that names a chat or generate route must use local_serving."""
    root = Path(ls.__file__).parent
    missing = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "local_serving.py" or not _POST.search(text):
            continue
        if "local_serving" not in text:
            missing.append(str(path.relative_to(root)))
    assert missing == []


# The node's own scheduling (``_admit``). Scenario of the 3 Nov candidate stress run: home chat and routines kept two
# or three 27B requests queued on a host that runs one at a time, so verification's llama3.2 never got the host.

def _hold(token, model, priority=ls.LIVE, arrived=0.0):
    ls._held[token] = (ls.model_key(model), priority, arrived)


def test_one_request_per_model_is_sent_and_the_next_is_held():
    ls._in_flight[1] = (BIG, 32768, 0.0, ls.LIVE)
    _hold(2, BIG)
    assert ls._admit(2, now=1.0, limit=120.0) == (False, False)
    # Another model is not held by it: both fit beside each other on the host.
    _hold(3, SMALL)
    assert ls._admit(3, now=1.0, limit=120.0) == (True, False)


def test_a_request_waiting_for_a_load_holds_new_requests_to_the_model_in_its_way():
    ls._loaded = ([(BIG, 32768)], 10.0)
    ls._in_flight[1] = (SMALL, 32768, 5.0, ls.LIVE)   # sent 5 s ago, its model not loaded
    _hold(2, BIG, arrived=9.0)
    assert ls._admit(2, now=10.0, limit=120.0) == (False, True)
    # Once its model is loaded it no longer waits, and the hold is gone.
    ls._loaded = ([(BIG, 32768), (SMALL, 32768)], 11.0)
    assert ls._admit(2, now=11.0, limit=120.0) == (True, False)


def test_a_load_in_its_first_seconds_holds_nothing():
    ls._loaded = ([(BIG, 32768)], 10.0)
    ls._in_flight[1] = (SMALL, 32768, 9.0, ls.LIVE)
    _hold(2, BIG, arrived=9.5)
    assert ls._admit(2, now=10.0, limit=120.0) == (True, False)


def test_a_background_request_waiting_never_holds_the_owners_or_an_answers():
    ls._loaded = ([(BIG, 32768)], 10.0)
    ls._in_flight[1] = (SMALL, 32768, 0.0, ls.BACKGROUND)
    _hold(2, BIG, priority=ls.LIVE)
    assert ls._admit(2, now=10.0, limit=120.0)[0] is True
    _hold(3, "qwen3.5:9b-mlx", priority=ls.BACKGROUND)
    assert ls._admit(3, now=10.0, limit=120.0) == (False, True)


def test_a_stale_reading_of_the_host_holds_nothing():
    ls._loaded = ([(BIG, 32768)], 0.0)
    ls._in_flight[1] = (SMALL, 32768, 0.0, ls.LIVE)
    _hold(2, BIG)
    assert ls._admit(2, now=ls._LOADED_FRESH_SECONDS + 1.0, limit=120.0)[0] is True


def test_no_request_is_held_past_its_limit():
    ls._in_flight[1] = (BIG, 32768, 0.0, ls.LIVE)
    _hold(2, BIG, arrived=0.0)
    assert ls._admit(2, now=30.0, limit=30.0) == (True, False)


def test_one_models_held_requests_go_the_owners_first_then_by_arrival():
    ls._in_flight.clear()
    _hold(1, BIG, priority=ls.BACKGROUND, arrived=0.0)
    _hold(2, BIG, priority=ls.LIVE, arrived=5.0)
    assert ls._admit(1, now=6.0, limit=120.0)[0] is False
    assert ls._admit(2, now=6.0, limit=120.0)[0] is True


def test_an_unload_is_never_held():
    ls._in_flight[1] = (BIG, 32768, 0.0, ls.LIVE)
    with ls.request({"model": BIG, "keep_alive": 0}) as body:
        assert body == {"model": BIG, "keep_alive": 0}


def test_a_blocking_call_on_an_event_loop_is_counted_but_never_held():
    ls._in_flight[1] = (BIG, 32768, ls.time.monotonic(), ls.LIVE)

    async def run():
        with ls.request({"model": BIG, "prompt": "hi"}):
            return len(ls.in_flight())

    assert asyncio.run(run()) == 2


def test_the_switch_end_to_end(monkeypatch):
    """A 27B request runs, a llama3.2 request waits for the host, and a new 27B request arrives. The new one is held
    until the llama3.2 request is served, so the 27B finishes the one it is running and can be unloaded."""
    monkeypatch.setattr(ls, "POLL_SECONDS", 0.01)
    order = []

    async def run():
        first_done = asyncio.Event()

        async def first():
            async with ls.request_async({"model": BIG, "prompt": "a"}):
                order.append("27B first sent")
                await first_done.wait()
            order.append("27B first ended")

        task = asyncio.create_task(first())
        await asyncio.sleep(0.02)
        # The llama3.2 request has been sent and is waiting for the host (watchdog's reading: only the 27B loaded).
        ls._in_flight[99] = (SMALL, 32768, ls.time.monotonic() - 5.0, ls.LIVE)
        ls._loaded = ([(BIG, 32768)], ls.time.monotonic())

        async def second():
            async with ls.request_async({"model": BIG, "prompt": "b"}):
                order.append("27B second sent")

        later = asyncio.create_task(second())
        await asyncio.sleep(0.05)
        first_done.set()
        await asyncio.sleep(0.05)
        assert "27B second sent" not in order          # still held: the llama3.2 request is not served yet
        ls._loaded = ([(SMALL, 32768)], ls.time.monotonic())   # the 27B was unloaded and llama3.2 loaded
        await asyncio.wait_for(later, 1.0)
        await task

    asyncio.run(run())
    assert order == ["27B first sent", "27B first ended", "27B second sent"]
    assert ls.status()["switch_holds"] == 1


def test_a_post_held_by_the_node_takes_the_time_held_off_its_own_timeout():
    ls._in_flight[1] = (BIG, 32768, ls.time.monotonic(), ls.LIVE)
    sent = {}

    class Http:
        async def post(self, url, *, json, timeout):
            sent["timeout"] = timeout
            return "ok"

    async def run():
        return await ls.post_async(Http(), "http://host/api/chat", json={"model": BIG, "prompt": "x"}, timeout=0.3)

    assert asyncio.run(run()) == "ok"
    # Held its whole 0.3 s behind the running 27B request, then sent at the floor of half a second.
    assert sent["timeout"] == 0.5


def test_with_diagnostics_on_a_request_is_a_content_free_span(monkeypatch):
    """docs/OBSERVABILITY.md: model, context, seconds and the hold's reason; never the prompt."""
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from topos.observability import runtime

    exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
    tp = sdk.TracerProvider(shutdown_on_exit=False)
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    instance = runtime.Telemetry(tp, MeterProvider(metric_readers=[reader], shutdown_on_exit=False))
    monkeypatch.setattr(runtime, "_runtime", instance)
    secret = "the prompt text must never be exported"

    async def run():
        async with ls.request_async({"model": "llama3.2", "prompt": secret}):
            pass
        with pytest.raises(RuntimeError):
            async with ls.request_async({"model": "llama3.2", "prompt": secret}):
                raise RuntimeError(secret)

    try:
        asyncio.run(run())
        ok, failed = exporter.get_finished_spans()
        assert ok.name == "topos.local_model.request"
        assert dict(ok.attributes) == {"topos.local_model.name": SMALL, "topos.local_model.context": 8192,
                                       "topos.local_model.background": False, "topos.local_model.priority": "live",
                                       "topos.local_model.held_s": ok.attributes[
                                           "topos.local_model.held_s"], "topos.local_model.held_for": "none"}
        assert failed.attributes["error.type"] == "RuntimeError"
        exported = repr([(s.attributes, s.events, s.status.description) for s in (ok, failed)])
        metrics = reader.get_metrics_data().to_json()
        assert secret not in exported and secret not in metrics
        assert "topos.local_model.request.duration" in metrics and "topos.local_model.hold.duration" in metrics
    finally:
        instance.shutdown()


# The second candidate's run: a routine (nobody watching it stream) held the owner's live chat on the 27B.

def test_on_one_model_the_owners_live_request_goes_before_a_routine_that_arrived_first():
    ls._in_flight.clear()
    _hold(1, BIG, priority=ls.UNWATCHED, arrived=0.0)
    _hold(2, BIG, priority=ls.LIVE, arrived=30.0)
    _hold(3, BIG, priority=ls.BACKGROUND, arrived=-10.0)
    assert [t for t in (1, 2, 3) if ls._admit(t, now=31.0, limit=None)[0]] == [2]


def test_a_routine_waiting_for_a_load_never_holds_the_owners_live_request():
    ls._loaded = ([(BIG, 32768)], 10.0)
    ls._in_flight[1] = (SMALL, 32768, 0.0, ls.UNWATCHED)
    _hold(2, BIG, priority=ls.LIVE)
    assert ls._admit(2, now=10.0, limit=None) == (True, False)
    _hold(3, "qwen3.5:9b-mlx", priority=ls.UNWATCHED)
    assert ls._admit(3, now=10.0, limit=None) == (False, True)


def test_a_slot_hold_outlasts_the_switch_limit_until_the_callers_deadline_or_the_slot_limit():
    ls._in_flight[1] = (BIG, 32768, 0.0, ls.LIVE)
    _hold(2, BIG, arrived=0.0)
    # 150 s behind the 27B's one running reply: still held, because sending it would only queue it on the host.
    assert ls._admit(2, now=150.0, limit=None) == (False, False)
    assert ls._admit(2, now=150.0, limit=100.0) == (True, False)      # its caller's own deadline ends it
    assert ls._admit(2, now=ls.SLOT_LIMIT, limit=None) == (True, False)


def test_a_switch_hold_still_ends_at_the_switch_limit():
    ls._loaded = ([(BIG, 32768)], 130.0)
    ls._in_flight[1] = (SMALL, 32768, 0.0, ls.LIVE)
    _hold(2, BIG, arrived=0.0)
    assert ls._admit(2, now=119.0, limit=None) == (False, True)
    ls._loaded = ([(BIG, 32768)], 121.0)
    assert ls._admit(2, now=121.0, limit=None) == (True, False)


def test_unwatched_marks_the_requests_inside_it():
    seen = []

    async def run():
        async with ls.request_async({"model": BIG, "prompt": "a"}):
            seen.append([p for *_, p in ls._in_flight.values()])
        with ls.unwatched():
            async with ls.request_async({"model": BIG, "prompt": "b"}):
                seen.append([p for *_, p in ls._in_flight.values()])

    asyncio.run(run())
    assert seen == [[ls.LIVE], [ls.UNWATCHED]]


def test_a_generation_that_does_not_stream_is_unwatched_and_one_that_streams_is_live(monkeypatch):
    from topos.core.handlers import device

    seen = []

    class Llm:
        async def generate(self, payload, on_delta=None, on_protocol=None):
            seen.append(ls._priority())
            return {"output": "x", "model": BIG}

    monkeypatch.setattr(device, "get_services", lambda: type("S", (), {"llm": Llm()})())
    monkeypatch.setattr(device.engine_state, "control_plane_client", None, raising=False)
    for stream in (False, True):
        asyncio.run(device.handle_llm_generation({"id": f"r{stream}", "payload": {
            "provider": "ollama", "model": BIG, "prompt": "p", "stream": stream}}))
    assert seen == [ls.UNWATCHED, ls.LIVE]

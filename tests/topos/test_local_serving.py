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


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(ls, "CONTEXT", 32768)
    monkeypatch.setattr(ls, "WAIT_SECONDS", 30.0)
    monkeypatch.setattr(ls, "_in_flight", {})
    monkeypatch.setattr(ls, "_released_at", {})
    monkeypatch.setattr(ls, "_stats", {"releases": 0, "last_release": None, "lowered_context": 0,
                                       "kept_larger_context": 0})


def test_every_caller_context_becomes_the_one_serving_context():
    # The five contexts the node's callers asked for on 1.5.39, for a prompt that needs a few thousand tokens.
    for asked in (None, 4096, 8192, 12288, 81920):
        assert ls.serving_context(asked, prompt_chars=12_000, num_predict=2048) == 32768


def test_a_prompt_that_needs_more_keeps_the_larger_context_it_asked_for():
    assert ls.serving_context(81920, prompt_chars=120_000, num_predict=2048) == 81920
    # Asking nothing, it gets the serving context (the host's default, which it got before).
    assert ls.serving_context(None, prompt_chars=120_000, num_predict=2048) == 32768


def test_off_leaves_each_caller_its_own(monkeypatch):
    monkeypatch.setattr(ls, "CONTEXT", 0)
    body = {"model": SMALL, "messages": [{"role": "user", "content": "x"}], "options": {"num_ctx": 8192}}
    assert ls.prepare(body) is body


def test_prepare_sets_the_context_without_touching_the_callers_body():
    body = {"model": SMALL, "messages": [{"role": "user", "content": "x"}], "options": {"temperature": 0}}
    prepared = ls.prepare(body)
    assert prepared["options"] == {"temperature": 0, "num_ctx": 32768}
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
            assert body["options"]["num_ctx"] == 32768
            assert [(m, c) for m, c, _ in ls.in_flight()] == [(SMALL, 32768)]
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
    assert ls.release_blocked(loaded, requests, now=50.0, released_at={BIG: 20.0})[0] == []
    assert ls.release_blocked(loaded, requests, now=90.0, released_at={BIG: 20.0})[0] == [BIG]


def test_the_watchdog_check_sends_one_unload_and_reports_it(monkeypatch):
    sent = []

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [_ps(BIG, 81920)]})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"done": True, "done_reason": "unload"})

    clock = iter([100.0])
    monkeypatch.setattr(ls.time, "monotonic", lambda: next(clock, 100.0))
    ls._in_flight[1] = (SMALL, 32768, 60.0)

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

    ls._in_flight[1] = (SMALL, 32768, ls.time.monotonic())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ls._check_once(client, "http://host")

    assert asyncio.run(run()) == []


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

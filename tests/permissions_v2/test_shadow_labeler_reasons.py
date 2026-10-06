"""The pinned transport asks the node's own model host, and every way of not answering has its own name.

Measured on the beta stack (24 Sep 2026): the node carries `ENGINE_OLLAMA_BASE_URL=http://ingress:18094` and shares
the gateway's network namespace, where nothing listens on loopback 11434. `shadow_labeler_local` hard-coded that
loopback, so the model was never asked. The transport now resolves the host from the engine's own setting.

1.5.0 removed the shadow audit's labeler that sat on this transport; the transport itself still ships (the fact
reconciliation labels through it, and the review, interest and answer paths verify the model through it). These
tests drive it directly.

Every text here is synthetic, written in this file. The one socket this suite opens is its own: a fake Ollama on a
random loopback port, started and stopped by the test, that answers what the test pinned. Nothing here probes a
model host on the machine, and nothing starts one.

  H1     the transport asks the configured host; every request arrives there and none goes to loopback 11434
  H2     the host is `ENGINE_OLLAMA_BASE_URL` as the engine's adapter reads it; a node that set nothing, set it
         blank, or whose settings will not load keeps the campaign's loopback
  R1-R3  one failure per way of not answering: unreachable, model_unreviewed, rubric_mismatch
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from topos.permissions_v2 import shadow_labeler_local as local

pytestmark = [pytest.mark.p0]

WORK_NOTE = "Moving the deploy to Thursday so the release notes land first."
WORK_LABELS = json.dumps({"domains": ["work", "plans"], "sensitivity": "none"})


class _FakeOllama:
    """A stand-in for the node's Ollama on a random loopback port.

    It lists the pinned tag at the digest the test chose, answers `/api/chat` as the test chose, and records every
    request it receives: method, path, the `Host` header the client sent, and the parsed body.
    """

    def __init__(self, *, answer=WORK_LABELS, digest=local.MODEL_REVISION, reply_model=local.MODEL, done=True,
                 status=200):
        self.answer, self.digest, self.reply_model, self.done, self.status = answer, digest, reply_model, done, status
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return None

            def _reply(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.requests.append(("GET", self.path, self.headers.get("Host"), None))
                if self.path != "/api/tags":
                    return self._reply(404, {})
                self._reply(fake.status, {"models": [{"name": local.MODEL, "digest": fake.digest}]})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(("POST", self.path, self.headers.get("Host"), body))
                if self.path != "/api/chat":
                    return self._reply(404, {})
                self._reply(fake.status, {"model": fake.reply_model, "done": fake.done,
                                          "message": {"role": "assistant", "content": fake.answer}})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    @property
    def paths(self):
        return [(method, path) for method, path, _, _ in self.requests]

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False


def _ask(host, text=WORK_NOTE, seen=None):
    """One `label` call on the real transport bound to the fake host, on a client opened and closed here."""
    async def record(request):
        if seen is not None:
            seen.append(request)

    async def run():
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False, event_hooks={"request": [record]})
        try:
            return await local._PinnedTransport(client, base_url=None if host is None else host.url).label(text)
        finally:
            await client.aclose()

    return asyncio.run(run())


class _Raising:
    """A client whose every request fails the way the test pinned. Opens nothing."""

    def __init__(self, failure):
        self.failure, self.calls = failure, 0

    async def get(self, url, **kwargs):
        self.calls += 1
        raise self.failure

    post = get


@pytest.fixture(autouse=True)
def _never_start_a_host(monkeypatch):
    # The engine's runtime must never be asked to start a host on the transport's behalf: a closed local port makes
    # `ensure_running` open the owner's Ollama app. These raise if anything here reaches them.
    from topos.engine import ollama_install, ollama_runtime

    def never(*args, **kwargs):
        raise AssertionError("the transport asked the engine to start or open a model host")

    monkeypatch.setattr(ollama_runtime, "ensure_running", never)
    monkeypatch.setattr(ollama_runtime, "default_open_app", never)
    monkeypatch.setattr(ollama_runtime, "default_spawn_serve", never, raising=False)
    monkeypatch.setattr(ollama_install, "default_open_app", never)


# --------------------------------------------------------------------------- H1


def test_H1_the_transport_asks_the_configured_host_and_nothing_goes_to_loopback_11434(monkeypatch):
    """The beta stack's defect: a node whose Ollama is elsewhere has nothing on loopback 11434."""
    from topos.config import settings as config

    seen = []
    with _FakeOllama() as host:
        # A trailing slash, as an operator types one; the transport normalises it the way the adapter does.
        monkeypatch.setattr(config.settings, local.BASE_URL_SETTING, host.url + "/")
        assert local.configured_base_url() == host.url
        opened = local.open_transport()
        assert opened.base_url == host.url
        asyncio.run(opened.client.aclose())
        # No `base_url`: the transport resolves the host itself, from the setting.
        assert _ask(None, seen=seen) == WORK_LABELS
        assert _ask(None, seen=seen) == WORK_LABELS
    # Every request the transport made arrived at the fake host, and the client sent nothing anywhere else.
    assert host.paths == [("GET", "/api/tags"), ("POST", "/api/chat")] * 2
    assert {hostname for _, _, hostname, _ in host.requests} == {"127.0.0.1:%d" % host.port}
    assert len(seen) == 4
    assert all(request.url.host == "127.0.0.1" and request.url.port == host.port for request in seen)
    assert not any(request.url.port == 11434 for request in seen)
    # The pinned request shape, at the pinned model, with the pinned prompt, and the record's text as the user turn.
    chat = host.requests[1][3]
    assert chat["model"] == local.MODEL and chat["stream"] is False and chat["think"] is False
    assert chat["format"] == "json"
    assert chat["messages"] == [{"role": "system", "content": local.system_prompt()},
                                {"role": "user", "content": WORK_NOTE}]


def test_H1b_a_long_text_is_cut_at_the_pinned_length_before_it_is_sent():
    with _FakeOllama() as host:
        _ask(host, "a" * (local.MAX_TEXT_CHARS + 500))
    assert len(host.requests[1][3]["messages"][1]["content"]) == local.MAX_TEXT_CHARS == 8_000


# --------------------------------------------------------------------------- H2


def test_H2_the_host_is_the_engines_own_setting_and_the_campaigns_loopback_when_the_node_set_nothing(monkeypatch):
    from topos.config import settings as config

    # The same env contract as the adapter: `ENGINE_OLLAMA_BASE_URL` reaches `settings.engine_ollama_base_url`.
    assert local.BASE_URL_SETTING == "engine_ollama_base_url"
    assert local.BASE_URL_SETTING in config.Settings.model_fields
    monkeypatch.setenv("ENGINE_OLLAMA_BASE_URL", "http://ingress:18094/")
    assert local.configured_base_url(config.Settings(_env_file=None)) == "http://ingress:18094"
    monkeypatch.delenv("ENGINE_OLLAMA_BASE_URL")
    assert local.configured_base_url(config.Settings(_env_file=None)) == local.ORIGIN

    def fake(value, *, configured=True):
        return types.SimpleNamespace(model_fields_set={local.BASE_URL_SETTING} if configured else set(),
                                     engine_ollama_base_url=value)

    # The setting's own default is the adapter's spelling of the loopback, not a configuration.
    assert local.configured_base_url(fake("http://localhost:11434", configured=False)) == local.ORIGIN
    # Blank is nothing; a value is used as given, less a trailing slash.
    for blank in ("", "   ", None):
        assert local.configured_base_url(fake(blank)) == local.ORIGIN
    assert local.configured_base_url(fake("http://ingress:18094/")) == "http://ingress:18094"
    assert local.configured_base_url(fake("http://192.0.2.10:11434")) == "http://192.0.2.10:11434"
    # Settings that will not load, or that will not answer, are a node that has set nothing.
    monkeypatch.setitem(sys.modules, "topos.config.settings", None)
    assert local.configured_base_url() == local.ORIGIN

    class _Broken:
        @property
        def model_fields_set(self):
            raise RuntimeError("no settings today")

    assert local.configured_base_url(_Broken()) == local.ORIGIN
    # The campaign's own binding is still the loopback, byte for byte.
    assert local.ORIGIN == "http://127.0.0.1:11434"


# --------------------------------------------------------------------------- R1


def test_R1_a_host_that_cannot_be_asked_or_does_not_complete_is_unreachable():
    # No connection, a timeout, and whatever else a client can raise: the model was not heard from.
    for failure in (httpx.ConnectError("[Errno 61] Connection refused"), httpx.ReadTimeout("read timed out"),
                    RuntimeError("the host is not there")):
        client = _Raising(failure)
        with pytest.raises(local.Unreachable):
            asyncio.run(local._PinnedTransport(client, base_url="http://model-host.invalid").label(WORK_NOTE))
        assert client.calls == 1, failure                  # the model list was asked for once, and nothing after it
    # A host that answers with an error status.
    with _FakeOllama(status=503) as host:
        with pytest.raises(local.Unreachable):
            _ask(host)
    assert host.paths == [("GET", "/api/tags")]
    # A host that answers, at the reviewed digest, with a generation that did not complete.
    with _FakeOllama(done=False) as host:
        with pytest.raises(local.Unreachable):
            _ask(host)
    assert host.paths == [("GET", "/api/tags"), ("POST", "/api/chat")]
    assert local.Unreachable.reason == "labeler_unreachable" and issubclass(local.Unreachable, local.Unresolved)


# --------------------------------------------------------------------------- R2


def test_R2_a_model_that_is_not_the_reviewed_revision_is_model_unreviewed():
    # The installed tag is at another digest: refused before the model is asked anything.
    with _FakeOllama(digest="0" * 64) as host:
        with pytest.raises(local.ModelUnreviewed):
            _ask(host)
    assert host.paths == [("GET", "/api/tags")]
    # The tag is right and the answer came from some other model.
    with _FakeOllama(reply_model="qwen3.5:latest") as host:
        with pytest.raises(local.ModelUnreviewed):
            _ask(host)
    assert host.paths == [("GET", "/api/tags"), ("POST", "/api/chat")]
    # The digest check is the one `test_L4c` pins, and it still raises a ValueError.
    assert issubclass(local.ModelUnreviewed, ValueError) and local.ModelUnreviewed.reason == "labeler_model_unreviewed"


# --------------------------------------------------------------------------- R3


def test_R3_a_rubric_that_is_not_the_reviewed_bytes_is_rubric_mismatch_before_the_model_is_asked(monkeypatch, tmp_path):
    edited = tmp_path / "rubric.md"
    edited.write_text("## A rubric somebody edited\n")
    for path in (edited, tmp_path / "missing.md"):
        monkeypatch.setattr(local, "RUBRIC_PATH", path)
        with _FakeOllama() as host:
            with pytest.raises(local.RubricMismatch):
                _ask(host)
        # The model's tag was checked and the text was never sent.
        assert host.paths == [("GET", "/api/tags")], path

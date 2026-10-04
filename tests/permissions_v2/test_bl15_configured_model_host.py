"""BL-15: every sharing model call goes to the node's configured model host when it is this machine.

The machine assessment, the interest label and its second try, and the native message ceiling each opened their
transport at ``shadow_labeler_local.ORIGIN`` (127.0.0.1:11434) whatever the node set in ``ENGINE_OLLAMA_BASE_URL``, so
a node whose model listens on another local address or port could never assess anything (T3; the rig was safe only
because its guard refuses 11434 and its labeller is stubbed). They now ask the configured host
(``assessment_base_url``), but a configured REMOTE host still never receives the owner's words and Off-limits terms:
those calls keep the loopback, as the original protection test pins
(``test_automatic_review_cannot_send_protected_context_to_configured_remote_model``).

protects: each of the four calls asks a configured local host; none sends to a configured remote one. Two local fake
hosts stand in, on ephemeral ports: one is the configured host, the other takes the fixed loopback's place, so neither
a pass nor a regression ever reaches port 11434. Each fake answers the tag list with no model, so the call stops at
its first request: no model is asked.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from topos.permissions_v2 import (automatic_message_review, interest_relabel, interest_review, reconciliation_facts,
                                  shadow_labeler_local)


class Recorder:
    def __init__(self):
        self.paths: list = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                recorder.paths.append(self.path)
                body = json.dumps({"models": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def hosts(monkeypatch):
    configured, loopback = Recorder(), Recorder()
    monkeypatch.setattr(shadow_labeler_local, "configured_base_url", lambda settings=None: configured.url)
    # The fixed loopback's stand-in, wherever a caller could still read it.
    monkeypatch.setattr(shadow_labeler_local, "ORIGIN", loopback.url)
    for module in (automatic_message_review, reconciliation_facts):
        monkeypatch.setattr(module, "ORIGIN", loopback.url, raising=False)
    yield configured, loopback
    configured.close()
    loopback.close()


CALLS = {
    "machine_assessment": lambda: automatic_message_review.assess({"input": {}}),
    "interest_label": lambda: interest_review.assess({"input": {}}),
    "interest_second_label": lambda: interest_relabel.ask({"input": {}}),
    "native_message_ceiling": lambda: reconciliation_facts.prepare_facts([]),
}


@pytest.mark.parametrize("call", sorted(CALLS))
def test_each_sharing_model_call_asks_the_configured_host(hosts, call):
    configured, loopback = hosts
    try:
        asyncio.run(CALLS[call]())
    except Exception:  # noqa: BLE001 -- the fake lists no model: the call stops at its first request, by design
        pass
    assert configured.paths and configured.paths[0] == "/api/tags", configured.paths
    assert loopback.paths == []


@pytest.mark.parametrize("call", sorted(CALLS))
def test_a_configured_remote_host_never_receives_the_owners_words(hosts, monkeypatch, call):
    configured, loopback = hosts
    monkeypatch.setattr(shadow_labeler_local, "configured_base_url",
                        lambda settings=None: "http://remote-model-host.invalid:11434")
    try:
        asyncio.run(CALLS[call]())
    except Exception:  # noqa: BLE001
        pass
    assert configured.paths == []
    assert loopback.paths and loopback.paths[0] == "/api/tags", loopback.paths

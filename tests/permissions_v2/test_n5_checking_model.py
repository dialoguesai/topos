"""The checking model's status and download (any-to-any N5; A2A-3 §7.5, decision D6; test obligation "Node (N4, N5)" 6).

No model is ever downloaded or called: the model host is a local fake server on an ephemeral port of 127.0.0.1 that
speaks the two calls the node makes (the tag list and the streamed pull) and serves a fake digest generated at run
time. The local model host's launchers are replaced with ones that fail the test if anything tries to start it.

protects: each status (unsupported, missing, downloading, ready, failed) as A2A-5 §5.5 shows it; a download starts
only after a yes, once (a second call while it runs answers the same status and pulls nothing more); the digest is
checked when it ends (a build that is not the pinned one is never "ready"); ``disk_low`` refuses before a byte moves;
``unsupported`` refuses on a machine that cannot run the build; the status read never starts the model host; the
pinned size is what the owner sees, and what the disk check holds, until a download reports its own.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from topos.permissions_v2 import checking_model, shadow_labeler_local

FAKE_SIZE = 7_000_000
PINNED = hashlib.sha256(b"n5 test: the reviewed build").hexdigest()
OTHER = hashlib.sha256(b"n5 test: a build nobody reviewed").hexdigest()


class FakeHost:
    """The two Ollama calls, in memory. A pull streams half the bytes, waits for ``release``, then finishes."""

    def __init__(self):
        self.models: list = []
        self.digest_after_pull = PINNED
        self.fail = False
        self.pulls = 0
        self.release = threading.Event()
        host = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path != "/api/tags":
                    self.send_error(404)
                    return
                body = json.dumps({"models": host.models}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path != "/api/pull":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                asked = json.loads(self.rfile.read(length) or b"{}")
                host.pulls += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()

                def frame(value):
                    self.wfile.write((json.dumps(value) + "\n").encode())
                    self.wfile.flush()
                layer = "sha256:" + hashlib.sha256(b"n5 test layer").hexdigest()
                frame({"status": "pulling manifest"})
                frame({"status": "pulling", "digest": layer, "total": FAKE_SIZE, "completed": FAKE_SIZE // 2})
                host.release.wait(20)
                if host.fail:
                    frame({"error": "the fake host could not finish"})
                    return
                frame({"status": "pulling", "digest": layer, "total": FAKE_SIZE, "completed": FAKE_SIZE})
                frame({"status": "verifying sha256 digest"})
                host.models = [{"name": asked.get("model"), "digest": host.digest_after_pull, "size": FAKE_SIZE}]
                frame({"status": "success"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, name="n5-fake-model-host", daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def _never_start(*_args, **_kwargs):
    raise AssertionError("the test tried to start a model host")


@pytest.fixture()
def fake(monkeypatch, tmp_path):
    from topos.config import local_model_builds
    from topos.engine import disk_space, ollama_pull, ollama_runtime
    host = FakeHost()
    monkeypatch.setattr(shadow_labeler_local, "configured_base_url", lambda settings=None: host.url)
    monkeypatch.setattr(shadow_labeler_local, "MODEL_REVISION", PINNED)
    monkeypatch.setattr(local_model_builds, "current_platform", lambda: local_model_builds.PLATFORM_MACOS_ARM64)
    monkeypatch.setattr(ollama_runtime, "default_open_app", _never_start)
    monkeypatch.setattr(ollama_runtime, "default_spawn_serve", _never_start)
    monkeypatch.setattr(disk_space, "ollama_models_dir", lambda: tmp_path / "models")
    host.free = 50_000_000_000
    monkeypatch.setattr(disk_space, "free_bytes", lambda path=None: host.free)
    monkeypatch.setattr(disk_space, "min_free_bytes", lambda conn=None: 1_000_000_000)
    ollama_pull.reset_progress()
    yield host
    host.close()
    ollama_pull.reset_progress()


def wait_for(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_missing_then_downloading_once_then_ready_with_the_digest_checked(fake, tmp_path):
    assert checking_model.status() == {"status": "missing", "size_bytes": checking_model.SIZE_BYTES,
                                       "downloaded_bytes": 0, "free_bytes": 50_000_000_000}
    started = checking_model.download(served=None)
    assert started["status"] == "downloading"
    assert wait_for(lambda: checking_model.status()["downloaded_bytes"] == FAKE_SIZE // 2)
    during = checking_model.status()
    assert (during["status"], during["size_bytes"], during["downloaded_bytes"]) == ("downloading", FAKE_SIZE,
                                                                                   FAKE_SIZE // 2)
    assert checking_model.download(served=None)["status"] == "downloading"      # asked again: the same, no new pull
    assert fake.pulls == 1
    fake.release.set()
    assert wait_for(lambda: checking_model.status()["status"] != "downloading")
    assert checking_model.status() == {"status": "ready", "size_bytes": FAKE_SIZE, "downloaded_bytes": FAKE_SIZE,
                                       "free_bytes": 50_000_000_000}
    assert checking_model.download(served=None)["status"] == "ready" and fake.pulls == 1


def test_a_build_that_is_not_the_pinned_one_is_never_ready(fake):
    fake.digest_after_pull = OTHER
    fake.release.set()
    checking_model.download(served=None)
    assert wait_for(lambda: checking_model.status()["status"] != "downloading")
    assert checking_model.status()["status"] == "failed"
    # Already listed at another digest: still not ready, and a new download is allowed.
    assert checking_model.download(served=None)["status"] in ("downloading", "failed")
    assert wait_for(lambda: fake.pulls == 2)


def test_a_download_that_ends_in_error_is_failed_and_may_start_again(fake):
    fake.fail = True
    fake.release.set()
    checking_model.download(served=None)
    assert wait_for(lambda: checking_model.status()["status"] == "failed")
    fake.fail = False
    checking_model.download(served=None)
    assert wait_for(lambda: checking_model.status()["status"] == "ready")
    assert fake.pulls == 2


def test_disk_low_refuses_before_a_byte_moves(fake, monkeypatch):
    monkeypatch.setattr(checking_model, "SIZE_BYTES", FAKE_SIZE)
    fake.free = FAKE_SIZE + 1_000_000_000 - 1                  # one byte short of the size plus the floor
    with pytest.raises(checking_model.Refused) as refused:
        checking_model.download(served=None)
    assert refused.value.code == "disk_low" and fake.pulls == 0
    fake.free = FAKE_SIZE + 1_000_000_000
    fake.release.set()
    assert checking_model.download(served=None)["status"] in ("downloading", "ready")
    assert wait_for(lambda: fake.pulls == 1)


def test_another_machine_is_unsupported_and_downloads_nothing(fake, monkeypatch):
    from topos.config import local_model_builds
    for platform in (local_model_builds.PLATFORM_MACOS_X86_64, local_model_builds.PLATFORM_LINUX, None):
        monkeypatch.setattr(local_model_builds, "current_platform", lambda platform=platform: platform)
        assert checking_model.status()["status"] == "unsupported"
        with pytest.raises(checking_model.Refused) as refused:
            checking_model.download(served=None)
        assert refused.value.code == "unsupported"
    assert fake.pulls == 0


def test_the_pinned_size_is_reported_while_the_host_reports_none(fake, monkeypatch):
    """The reviewed build's manifest: layers 8,903,014,479 bytes plus config 279 (``checking_model.SIZE_BYTES``)."""
    from topos.config import local_model_builds
    assert checking_model.SIZE_BYTES == 8_903_014_758
    # Nothing installed and nothing pulled: the host reports no size, the status shows the pinned one.
    assert checking_model.status()["size_bytes"] == 8_903_014_758
    # Another build under the tag: its listed size is not what a download fetches.
    fake.models = [{"name": shadow_labeler_local.MODEL, "digest": OTHER, "size": FAKE_SIZE}]
    assert (checking_model.status()["status"], checking_model.status()["size_bytes"]) == ("missing", 8_903_014_758)
    # The pinned build listed without a size: ready, at the pinned size; with one, the host's own (the ready test).
    fake.models = [{"name": shadow_labeler_local.MODEL, "digest": PINNED}]
    assert checking_model.status() == {"status": "ready", "size_bytes": 8_903_014_758,
                                       "downloaded_bytes": 8_903_014_758, "free_bytes": 50_000_000_000}
    # A machine that cannot run the build still learns its size.
    with monkeypatch.context() as patched:
        patched.setattr(local_model_builds, "current_platform", lambda: local_model_builds.PLATFORM_LINUX)
        assert checking_model.status()["size_bytes"] == 8_903_014_758
    # Before any download reports a size, the disk check holds the pinned one: one byte short of it plus the floor.
    fake.models = []
    fake.free = 8_903_014_758 + 1_000_000_000 - 1
    with pytest.raises(checking_model.Refused) as refused:
        checking_model.download(served=None)
    assert refused.value.code == "disk_low" and fake.pulls == 0


def test_an_installed_pinned_build_is_ready_and_an_unreachable_host_is_missing(fake, monkeypatch):
    fake.models = [{"name": shadow_labeler_local.MODEL, "digest": PINNED, "size": FAKE_SIZE}]
    assert checking_model.status()["status"] == "ready"
    fake.close()
    # Nobody answers: the model is missing as far as this node can tell, and nothing was started to find out.
    assert checking_model.status()["status"] == "missing"


# --- through the relay -----------------------------------------------------------------------------------------

from tests.permissions_v2.test_self_bind import node  # noqa: E402,F401

CHECKING_MODEL = "permissions_v2_checking_model"


@pytest.mark.asyncio
async def test_the_relayed_checking_model_answers_the_owner_bound_or_not(node, fake):
    def frame(payload, **stamp):
        return node.stamped({"id": f"n5-{time.monotonic_ns()}", "type": CHECKING_MODEL, "payload": payload}, **stamp)
    reply = await node.send(frame({"operation": "status", "request": {}}))
    assert reply == {"id": reply["id"], "type": CHECKING_MODEL, "status": "ok",
                     "payload": {"status": "missing", "size_bytes": checking_model.SIZE_BYTES,
                                 "downloaded_bytes": 0, "free_bytes": 50_000_000_000}}
    for payload in ({"operation": "download", "request": {}}, {"operation": "download", "request": {"confirm": 1}},
                    {"operation": "delete", "request": {}}, {"operation": "status", "request": {"x": 1}}):
        refused = await node.send(frame(payload))
        assert (refused["code"], refused["error"]) == (400, "payload_invalid"), payload
    assert fake.pulls == 0                                      # no yes, no download
    stranger = await node.send(frame({"operation": "status", "request": {}}, acting="someone-else"))
    assert (stranger["code"], stranger["error"]) == (403, "owner_authority_required")
    fake.release.set()
    started = await node.send(frame({"operation": "download", "request": {"confirm": True}}))
    assert started["status"] == "ok" and started["payload"]["status"] in ("downloading", "ready")
    assert wait_for(lambda: checking_model.status()["status"] == "ready")
    from topos.config import local_model_builds
    import unittest.mock
    with unittest.mock.patch.object(local_model_builds, "current_platform", lambda: local_model_builds.PLATFORM_LINUX):
        refused = await node.send(frame({"operation": "download", "request": {"confirm": True}}))
    assert (refused["code"], refused["error"]) == (409, "unsupported")


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["third_party", "unstamped", "auto_resync", "owner_socket", "another_binding",
                                 "malformed_binding"])
async def test_only_the_owners_app_over_the_relay_for_this_node_is_answered(node, fake, who):
    from topos.core.handlers import handle_control_plane_request
    from topos.permissions_v2 import runtime as runtime_module
    from topos.principal import OWNER_APP, Principal
    from tests.permissions_v2.test_self_bind import ACTOR, OWNER, RECIPIENT_CLIENT
    await node.bind()
    payload = {"operation": "status", "request": {}}
    message = {"id": f"n5-gate-{who}", "type": CHECKING_MODEL, "payload": payload}
    if who == "third_party":
        reply = await node.send(node.stamped(message, cls="third_party", client=RECIPIENT_CLIENT, acting=ACTOR))
    elif who == "unstamped":
        reply = await node.send(message)
    elif who == "auto_resync":
        reply = await node.send(node.stamped(message, client="permissions_v2_auto_resync"))
    elif who == "owner_socket":
        reply = await handle_control_plane_request(message, principal=Principal(cls=OWNER_APP, channel="uds",
                                                                                acting_user=OWNER))
    else:
        binding = runtime_module.get_runtime().protocol.ledger.identity.model_dump()
        binding = {**binding, "node_id": "another-node"} if who == "another_binding" else {**binding, "x": 1}
        reply = await node.send(node.stamped({**message, "payload": {**payload, "binding": binding}}))
        assert (reply["code"], reply["error"]) == (409, "binding_mismatch"), reply
        return
    assert reply["status"] == "error" and reply["code"] == 403, reply
    assert fake.pulls == 0


def test_the_checking_model_is_owner_only_in_the_handled_types_snapshot():
    from pathlib import Path
    from topos.core.handlers import OWNER_ONLY_MESSAGE_TYPES
    snapshot = json.loads((Path(__file__).resolve().parents[2] / "topos" / "protocol"
                           / "handled_message_types.json").read_text())
    assert CHECKING_MODEL in OWNER_ONLY_MESSAGE_TYPES
    assert CHECKING_MODEL in snapshot["handled_message_types"] and CHECKING_MODEL in snapshot["owner_only_message_types"]

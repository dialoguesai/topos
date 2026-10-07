"""A node that holds no stamp key tries the first pin again (review S4 follow-up, question Q8).

A node pins the control plane's stamp key once, trust on first use, on a thread at start. Until 1.5.0 that thread
tried once: a first start with the control plane unreachable left the node with no key until its next restart.
Since a stamp the node cannot check is now refused (finding M1), that node refused every stamped frame until then.

Now the start-up thread tries again while the node holds no key: the same trust decision as at start, repeated,
after 5 s and then twice as long each time, never more often than every ten minutes. It ends for good once a key is
pinned. And the one thing it must never become: a key that is pinned, in the file or in the environment, is never
asked for again, replaced or overwritten, whatever happens to verification afterwards. A control plane that was
swapped must not be able to rotate itself into trust by making stamps fail.

No network: ``httpx`` is replaced by a recorder in every test. Keys are made at run time.
"""
from __future__ import annotations

import base64
import sqlite3
import sys
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import topos.core.handlers as hub
from topos import relay_stamp as rs
from topos.core.handlers.registry import HANDLERS
from topos.relay_stamp import STAMP_FIELD, canonical_signing_payload

KEY = Ed25519PrivateKey.generate()
ANOTHER_KEY = Ed25519PrivateKey.generate()
OWNER = "owner-1"


def b64(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")


class ControlPlane:
    """What stands in for ``httpx``: every request is counted; it answers what ``answers`` says, in order, and the
    last one for good. An answer is an exception to raise, a status code, or the key to serve."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.before_answering = None

    def get(self, url, timeout):
        self.requests.append(url)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if self.before_answering is not None:
            self.before_answering()
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, int):
            return _Response(answer, {})
        return _Response(200, {"algorithm": "ed25519", "public_key_b64": b64(answer)})


class _Response:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


class Waits:
    """A stop event that records every wait and never sleeps. It says "stop" after ``limit`` waits, so that a loop
    that would go on for ever ends the test."""

    def __init__(self, limit=50):
        self.delays, self.limit = [], limit

    def wait(self, delay):
        self.delays.append(delay)
        return len(self.delays) >= self.limit

    def is_set(self):
        return len(self.delays) >= self.limit


UNREACHABLE = ConnectionError("the control plane cannot be reached")


@pytest.fixture
def node(monkeypatch, tmp_path):
    """A node with no key pinned anywhere and a control plane address; ``use(ControlPlane(...))`` installs the
    control plane it will ask."""
    pin = tmp_path / "home" / ".topos" / "cp_stamp_key.pub"
    monkeypatch.setattr(rs, "_PINNED_KEY_PATH", str(pin))
    monkeypatch.delenv("TOPOS_CP_STAMP_PUBKEY", raising=False)
    monkeypatch.setattr("topos.config.settings.settings.topos_control_plane_url", "wss://cp.example/ws/engine",
                        raising=False)

    def use(control_plane):
        monkeypatch.setitem(sys.modules, "httpx", control_plane)
        return control_plane
    yield type("Node", (), {"pin": pin, "use": staticmethod(use), "monkeypatch": monkeypatch})
    rs.stop_first_pin(timeout=5.0)


def stamped(message, *, cls="owner_app", acting=OWNER, key=KEY, exp=None):
    now = time.time()
    stamp = {"v": 1, "cls": cls, "client_id": "topos_home_chat", "acting_user": acting, "iat": now,
             "exp": now + 100 if exp is None else exp}
    stamp["sig"] = base64.b64encode(key.sign(canonical_signing_payload(
        stamp, msg_id=message["id"], msg_type=message["type"]))).decode("ascii")
    return {**message, STAMP_FIELD: stamp}


@pytest.fixture
def reached(monkeypatch, tmp_path):
    """One handler replaced by a recorder, behind the real relay dispatcher."""
    conn = sqlite3.connect(tmp_path / "node.db")
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    seen = []

    async def recorder(message):
        seen.append(message["id"])
        return {"id": message["id"], "status": "ok", "payload": {}}
    monkeypatch.setitem(HANDLERS, "get_runtime_bootstrap", recorder)
    yield seen
    conn.close()


def frame(message_id):
    return {"id": message_id, "type": "get_runtime_bootstrap", "payload": {}}


REFUSED = {"status": "error", "code": 403, "error": "owner_mode_required"}


# --- the pin is made without a restart ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_node_with_no_key_pins_once_the_control_plane_answers_with_no_restart(node, reached):
    control_plane = node.use(ControlPlane(UNREACHABLE, UNREACHABLE, UNREACHABLE, KEY))
    # While it holds no key, a stamped frame is refused (finding M1).
    assert await hub.dispatch_relay_message(stamped(frame("before"))) == {"id": "before", **REFUSED}

    waits = Waits()
    assert rs.pin_first_key(waits) is True

    # Unreachable three times, then reachable: four requests, the pin made, and the loop over.
    assert len(control_plane.requests) == 4
    assert set(control_plane.requests) == {"https://cp.example/v1/relay/stamp-public-key"}
    assert node.pin.read_text().strip() == b64(KEY)
    assert waits.delays == [5.0, 10.0, 20.0]
    # The same process, no restart: the next stamped frame verifies and reaches its handler.
    assert (await hub.dispatch_relay_message(stamped(frame("after"))))["status"] == "ok"
    assert reached == ["after"]


def test_the_start_up_thread_makes_the_pin_and_ends(node):
    control_plane = node.use(ControlPlane(KEY))

    thread = rs.start_first_pin()
    thread.join(5.0)

    assert not thread.is_alive() and thread.name == rs.FIRST_PIN_THREAD and thread.daemon
    assert node.pin.read_text().strip() == b64(KEY) and len(control_plane.requests) == 1


# --- bounded and backed off -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("answer", [UNREACHABLE, 404, 503], ids=["unreachable", "stamping_off", "unavailable"])
def test_the_wait_between_tries_doubles_and_never_exceeds_ten_minutes(node, answer):
    control_plane = node.use(ControlPlane(answer))
    waits = Waits(limit=12)

    assert rs.pin_first_key(waits) is False

    assert waits.delays == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 320.0, 600.0, 600.0, 600.0, 600.0, 600.0]
    assert max(waits.delays) == rs.FIRST_PIN_RETRY_MAX_S == 600.0
    assert len(control_plane.requests) == 12            # one request a try, never two
    assert not node.pin.exists()


def test_stopping_ends_the_tries_at_once(node):
    control_plane = node.use(ControlPlane(UNREACHABLE))

    thread = rs.start_first_pin()
    deadline = time.monotonic() + 5.0
    while not control_plane.requests and time.monotonic() < deadline:
        time.sleep(0.01)
    rs.stop_first_pin(timeout=5.0)

    assert not thread.is_alive()                         # it was waiting 5 s; it did not wait them out
    assert len(control_plane.requests) == 1 and not node.pin.exists()


def test_a_node_with_no_control_plane_asks_nobody(node):
    node.monkeypatch.setattr("topos.config.settings.settings.topos_control_plane_url", "", raising=False)
    control_plane = node.use(ControlPlane(KEY))
    waits = Waits()

    assert rs.pin_first_key(waits) is False

    assert control_plane.requests == [] and waits.delays == []


# --- a pinned key is never asked for again --------------------------------------------------------------------------

def _pin_in_the_file(node):
    node.pin.parent.mkdir(parents=True, exist_ok=True)
    node.pin.write_text(b64(KEY) + "\n")


def _pin_in_the_environment(node):
    node.monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", b64(KEY))


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [_pin_in_the_file, _pin_in_the_environment], ids=["in_the_file", "in_the_environment"])
async def test_a_pinned_key_is_never_fetched_again_even_when_verification_fails(node, reached, pinned):
    """The idea that stays rejected: fetching the key again because stamps stopped verifying. It would let a control
    plane that was swapped rotate itself into trust. Every way a stamp can fail is sent here, the retry is run, the
    start-up thread is started, and the control plane is never asked."""
    pinned(node)
    before = node.pin.read_bytes() if node.pin.exists() else None
    control_plane = node.use(ControlPlane(ANOTHER_KEY))         # it would serve another key to anyone who asked

    failing = [
        stamped(frame("another-key"), key=ANOTHER_KEY),
        stamped(frame("expired"), exp=time.time() - 10),
        stamped(frame("unknown-class"), cls="grantee"),
        {**frame("malformed"), STAMP_FIELD: "owner_app"},
    ]
    for message in failing * 3:
        assert await hub.dispatch_relay_message(message) == {"id": message["id"], **REFUSED}
    waits = Waits()
    assert rs.pin_first_key(waits) is True               # a key is held: nothing to do, and no wait
    thread = rs.start_first_pin()
    thread.join(5.0)

    assert control_plane.requests == [] and waits.delays == [] and not thread.is_alive()
    assert (node.pin.read_bytes() if node.pin.exists() else None) == before
    # The pinned key is still the one that verifies; the other one still is not.
    assert (await hub.dispatch_relay_message(stamped(frame("pinned-key"))))["status"] == "ok"
    assert await hub.dispatch_relay_message(stamped(frame("other"), key=ANOTHER_KEY)) == {"id": "other", **REFUSED}
    assert reached == ["pinned-key"]


def test_a_key_pinned_while_the_answer_was_on_its_way_is_not_replaced(node):
    """The owner (or an operator) puts a key in place between the question and its answer: theirs stays."""
    control_plane = node.use(ControlPlane(ANOTHER_KEY))
    control_plane.before_answering = lambda: _pin_in_the_file(node)

    assert rs.pin_first_key(Waits()) is True

    assert len(control_plane.requests) == 1
    assert node.pin.read_text().strip() == b64(KEY)


def test_a_key_in_the_file_is_not_replaced_behind_an_environment_value_the_node_cannot_use(node):
    """An environment value that does not decode hides the file from the verifier. It is the operator's to fix: the
    node asks nobody, and the key in the file is left as it is."""
    _pin_in_the_file(node)
    node.monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", "not base64 !")
    control_plane = node.use(ControlPlane(ANOTHER_KEY))
    waits = Waits()

    assert rs.pin_first_key(waits) is False
    assert rs.autopin_stamp_key() is False               # the start decision itself, asked directly

    assert node.pin.read_text().strip() == b64(KEY)
    assert waits.delays == []                            # no loop either: nothing here is this node's to repair
    assert len(control_plane.requests) <= 1              # (the direct call may ask; it must not write)


def test_once_a_key_is_pinned_the_tries_are_over_for_good(node):
    control_plane = node.use(ControlPlane(UNREACHABLE, KEY, ANOTHER_KEY))

    assert rs.pin_first_key(Waits()) is True
    assert rs.pin_first_key(Waits()) is True
    again = rs.start_first_pin()
    again.join(5.0)

    assert len(control_plane.requests) == 2              # one that failed, one that pinned; never a third
    assert node.pin.read_text().strip() == b64(KEY)


def test_a_late_answer_after_shutdown_writes_nothing(node):
    """The app stops while a request is out: its answer, when it comes, pins nothing."""
    import threading

    stop = threading.Event()
    control_plane = node.use(ControlPlane(KEY))
    control_plane.before_answering = stop.set

    assert rs.pin_first_key(stop) is False

    assert len(control_plane.requests) == 1 and not node.pin.exists()

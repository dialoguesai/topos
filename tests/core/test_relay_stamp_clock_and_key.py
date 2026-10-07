"""Review R1 (node), R-M2 and R-M3: five minutes either side, a wrong clock told from a wrong key, and a real pin.

R-M2  A stamp that does not verify is refused (review S4, M1). That made the stamp's time window decide whether the
      owner's own app works at all, and the window was 60 s one way and the stamp's 120 s life the other, with one
      text in the log for every cause. The owner's decision (7 Oct): a stamp is accepted up to five minutes either
      side (``iat - 300 <= now <= exp + 300``); the signature is checked before the times, so the node can tell a
      wrong clock (the stamp is the control plane's, the times are off) from a wrong key; and it says which, in
      its log and in the bind's existing ``cause``.
R-M3  A pin file that held anything decodable counted as a key for ever: an empty file, a lone newline, 31 bytes.
      A pin is a key only if it is 32 bytes, and the first pin is written beside the file and linked into place,
      so a death in the middle of the write leaves no file where a key should be.

No network (``httpx`` is the recorder of test_first_stamp_pin_retry.py); keys are made at run time; every id is
invented.
"""
from __future__ import annotations

import base64
import logging
import os
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import topos.core.handlers as hub
from tests.core.test_first_stamp_pin_retry import ControlPlane, Waits, b64
from tests.core.test_first_stamp_pin_retry import node as pin_node  # noqa: F401 (fixture)
from tests.core.test_relay_non_owner_gate import OWNER, node, refusal, sweep  # noqa: F401 (fixture)
from topos import relay_stamp as rs
from topos.core.handlers.registry import HANDLERS
from topos.principal import OWNER_APP
from topos.relay_stamp import STAMP_FIELD, canonical_signing_payload, check_relay_stamp, verify_relay_stamp

KEY = Ed25519PrivateKey.generate()
ANOTHER_KEY = Ed25519PrivateKey.generate()
#: The control plane's stamp lives this long (cp:relay_stamp.py STAMP_TTL_S).
TTL = 120
T0 = 1_790_000_000.0


def at(message, issued, *, life=TTL, key=KEY, cls=OWNER_APP, acting=OWNER):
    """`message` stamped by the control plane whose clock says `issued`."""
    stamp = {"v": 1, "cls": cls, "client_id": "topos_home_chat", "acting_user": acting, "iat": issued,
             "exp": issued + life}
    stamp["sig"] = base64.b64encode(key.sign(canonical_signing_payload(
        stamp, msg_id=message["id"], msg_type=message["type"]))).decode("ascii")
    return {**message, STAMP_FIELD: stamp}


@pytest.fixture
def clock(monkeypatch):
    """This node's clock: `clock(seconds)` sets how far it is from the control plane's T0."""
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", b64(KEY))

    def set_offset(offset):
        monkeypatch.setattr(rs, "_now", lambda: T0 + offset)
    set_offset(0)
    return set_offset


def frame(msg_type="query", msg_id="m1"):
    return {"id": msg_id, "type": msg_type, "payload": {}}


# ------------------------------------------------------------------------------------------------ R-M2: the window

@pytest.mark.parametrize("offset, served", [
    (0, True),
    (-299, True), (-301, False),                 # this node's clock behind the control plane's
    (TTL + 299, True), (TTL + 301, False),       # ahead of it: five minutes past the stamp's own end
    (-60 - 1, True), (TTL + 1, True),            # both refused before: 61 s behind, 1 s past the stamp's life
])
def test_a_stamp_is_accepted_five_minutes_either_side(clock, offset, served):
    """Rule: `iat - SKEW_S <= now <= exp + SKEW_S` with SKEW_S 300. Put the 60 s and the hard end back and the two
    last cases, an owner's app on a computer a minute off, are refused again."""
    clock(offset)
    assert (verify_relay_stamp(at(frame(), T0)) is not None) is served
    assert rs.SKEW_S == 300


def test_the_stamps_own_life_is_still_capped(clock):
    assert verify_relay_stamp(at(frame(), T0, life=rs.MAX_LIFETIME_S)) is not None
    assert verify_relay_stamp(at(frame(), T0, life=rs.MAX_LIFETIME_S + 1)) is None


@pytest.mark.parametrize("iat, exp", [(float("nan"), T0 + 60), (T0, float("nan")), (T0, float("inf")),
                                      (float("-inf"), T0 + 60), (float("nan"), float("nan"))])
def test_a_time_that_is_not_a_number_verifies_nothing(clock, iat, exp):
    """Review note N1: a NaN expiry passed every comparison and verified."""
    message = frame()
    stamp = {"v": 1, "cls": OWNER_APP, "client_id": "c", "acting_user": OWNER, "iat": iat, "exp": exp}
    stamp["sig"] = base64.b64encode(KEY.sign(canonical_signing_payload(
        stamp, msg_id=message["id"], msg_type=message["type"]))).decode("ascii")
    principal, reason, _offset = check_relay_stamp({**message, STAMP_FIELD: stamp})
    assert principal is None and reason == "malformed"


# ------------------------------------------------------------------- R-M2: a wrong clock told from a wrong key

def test_the_signature_is_checked_before_the_times(clock):
    """A stamp signed by another key AND out of time is a key problem; one signed by the pinned key and out of
    time is a clock problem, with how far. Rule: `check_relay_stamp` verifies the signature first. Check the times
    first and a stamp of another control plane on a slow clock reads as "clock"."""
    clock(900)
    assert check_relay_stamp(at(frame(), T0, key=ANOTHER_KEY)) == (None, "key", None)
    principal, reason, offset = check_relay_stamp(at(frame(), T0))
    assert (principal, reason) == (None, "clock") and offset == pytest.approx(900)      # issued 900 s ago, it says
    clock(-700)
    principal, reason, offset = check_relay_stamp(at(frame(), T0))
    assert (principal, reason) == (None, "clock") and offset == pytest.approx(-700)     # issued 700 s from now
    clock(0)
    assert check_relay_stamp(at(frame(), T0, key=ANOTHER_KEY)) == (None, "key", None)
    assert check_relay_stamp(at(frame(), T0, cls="grantee")) == (None, "class", None)
    assert check_relay_stamp(frame()) == (None, "no_stamp", None)
    assert check_relay_stamp({**frame(), STAMP_FIELD: None}) == (None, "malformed", None)
    lifted = {**frame("query", "another"), STAMP_FIELD: at(frame(), T0)[STAMP_FIELD]}
    assert check_relay_stamp(lifted) == (None, "key", None)                                    # not this frame's
    principal, reason, offset = check_relay_stamp(at(frame(), T0))
    assert principal is not None and (reason, offset) == ("verified", None)


def test_a_node_with_no_key_says_so(clock, monkeypatch):
    monkeypatch.delenv("TOPOS_CP_STAMP_PUBKEY")
    monkeypatch.setattr(rs, "_PINNED_KEY_PATH", "/nonexistent/r2n/cp_stamp_key.pub")
    assert check_relay_stamp(at(frame(), T0)) == (None, "no_key", None)


@pytest.mark.asyncio
async def test_the_log_says_clock_or_key_and_how_far(node, monkeypatch, caplog):   # noqa: F811
    """One line a minute for each cause, naming no frame. `node` pins tests.core.test_relay_non_owner_gate's key."""
    from tests.core.test_relay_non_owner_gate import KEY as PINNED

    monkeypatch.setattr(hub, "_unverified_stamp_noted", {})
    monkeypatch.setattr(rs, "_now", lambda: T0 + 1000)
    caplog.set_level(logging.WARNING, logger=hub.logger.name)
    for _ in range(3):
        await hub.dispatch_relay_message(at(frame(), T0, key=PINNED))
        await hub.dispatch_relay_message(at(frame(), T0, key=ANOTHER_KEY))
    lines = [record.getMessage() for record in caplog.records if "relay stamp not verified" in record.getMessage()]
    assert len(lines) == 2                                                  # not one a frame
    clock_line = next(line for line in lines if "clock" in line)
    key_line = next(line for line in lines if line is not clock_line)
    assert "1000 s ahead" in clock_line and "signed by the pinned control-plane stamp key" in clock_line
    assert "does not verify under the pinned control-plane stamp key" in key_line and "clock" not in key_line
    assert "m1" not in clock_line + key_line and OWNER not in clock_line + key_line


# --------------------------------------------------------------------------- R-M2: the bind's existing ``cause``

@pytest.mark.asyncio
async def test_a_bind_whose_stamp_does_not_verify_says_clock_or_key_in_its_cause(node, monkeypatch):   # noqa: F811
    """The bind's answers already carry ``cause`` (contract A2A-1, amendment 8). The refusal is the same refusal;
    for the bind, and only for it, it also names why. No other type's refusal changes."""
    from tests.core.test_relay_non_owner_gate import KEY as PINNED

    bind = {"id": "b1", "type": "permissions_v2_bind", "payload": {"bind": {}}}
    monkeypatch.setattr(rs, "_now", lambda: T0 + 1000)
    assert await hub.dispatch_relay_message(at(bind, T0, key=PINNED)) == {**refusal("b1"), "cause": "stamp_clock"}
    assert await hub.dispatch_relay_message(at(bind, T0, key=ANOTHER_KEY)) == {**refusal("b1"), "cause": "stamp_key"}
    monkeypatch.setattr(rs, "_now", lambda: T0)
    assert await hub.dispatch_relay_message(at(bind, T0, key=ANOTHER_KEY)) == {**refusal("b1"), "cause": "stamp_key"}
    assert await hub.dispatch_relay_message({**bind, STAMP_FIELD: {}}) == {**refusal("b1"), "cause": "stamp_invalid"}
    monkeypatch.setattr(rs, "_load_public_key_bytes", lambda: None)
    assert await hub.dispatch_relay_message(at(bind, T0, key=PINNED)) == {
        **refusal("b1"), "cause": "stamp_key_unavailable"}
    assert node.reached == []                                               # the bind's handler never ran


@pytest.mark.asyncio
async def test_no_other_types_refusal_gains_anything(node, monkeypatch):   # noqa: F811
    from tests.core.test_relay_non_owner_gate import KEY as PINNED, SHARE_DOORS

    monkeypatch.setattr(rs, "_now", lambda: T0 + 1000)
    _reached, replies = await sweep(node, lambda m: at(m, T0, key=PINNED))
    for name, reply in replies.items():
        if name not in SHARE_DOORS and name != "permissions_v2_bind":
            assert reply == refusal("frame-" + name), name
    assert replies["permissions_v2_bind"] == {**refusal("frame-permissions_v2_bind"), "cause": "stamp_clock"}
    assert "permissions_v2_bind" in HANDLERS


# ------------------------------------------------------------------------------- R-M3: a pin is a key of 32 bytes

NOT_A_KEY = {
    "empty": "",
    "a newline": "\n",
    "31 bytes": base64.b64encode(b"k" * 31).decode("ascii") + "\n",
    "64 bytes": base64.b64encode(b"k" * 64).decode("ascii") + "\n",
    "not base64": "not a key at all !\n",
}


@pytest.mark.parametrize("label", sorted(NOT_A_KEY))
def test_a_pin_file_that_does_not_hold_32_bytes_is_no_key_and_is_pinned_over(pin_node, label):   # noqa: F811
    """The reviewer's run: with such a file the node asked the control plane 0 times and refused every stamped
    frame for ever. Rule: `_load_public_key_bytes` returns a key only when it is 32 bytes."""
    pin_node.pin.parent.mkdir(parents=True)
    pin_node.pin.write_text(NOT_A_KEY[label])
    assert rs._load_public_key_bytes() is None
    control_plane = pin_node.use(ControlPlane(KEY))
    assert rs.pin_first_key(Waits()) is True
    assert len(control_plane.requests) == 1
    assert rs._load_public_key_bytes() == KEY.public_key().public_bytes_raw()
    assert pin_node.pin.read_text() == b64(KEY) + "\n"


def test_an_environment_value_that_is_not_32_bytes_is_no_key(pin_node):   # noqa: F811
    pin_node.monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(b"k" * 31).decode("ascii"))
    assert rs._load_public_key_bytes() is None
    pin_node.monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", b64(KEY))
    assert rs._load_public_key_bytes() == KEY.public_key().public_bytes_raw()


def test_a_death_in_the_middle_of_the_first_pin_leaves_no_file_where_a_key_should_be(pin_node):   # noqa: F811
    """Rule: the key is written beside the pin and linked into place. Write it in place again (truncate, then
    write) and a death between the two leaves an empty pin file."""
    pin_node.use(ControlPlane(KEY))

    def dies(*_a, **_k):
        raise OSError("the process died here")

    pin_node.monkeypatch.setattr(rs.os, "link", dies)
    assert rs.autopin_stamp_key() is False
    assert not pin_node.pin.exists()
    assert rs._load_public_key_bytes() is None
    assert list(pin_node.pin.parent.iterdir()) == [] if pin_node.pin.parent.exists() else True   # nothing beside it


def test_two_first_pins_at_once_the_first_stands(pin_node):   # noqa: F811
    """Review R1, R-L6(a): check then write let the later of two writers stand. The link is exclusive."""
    control_plane = pin_node.use(ControlPlane(KEY))

    def another_process_pins_first():
        pin_node.pin.parent.mkdir(parents=True, exist_ok=True)
        real_link = os.link

        def link(src, dst, *a, **k):                                        # the other writer wins the race
            pin_node.pin.write_text(b64(ANOTHER_KEY) + "\n")
            return real_link(src, dst, *a, **k)
        pin_node.monkeypatch.setattr(rs.os, "link", link)

    control_plane.before_answering = another_process_pins_first
    assert rs.autopin_stamp_key() is False
    assert pin_node.pin.read_text() == b64(ANOTHER_KEY) + "\n"              # the first pin, not replaced
    assert [p.name for p in pin_node.pin.parent.iterdir()] == [pin_node.pin.name]   # nothing left beside it

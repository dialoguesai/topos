"""Kept questions Wave 2 (lane OFF2N; ``lanes/OFF2_CONTRACT.md`` §D): ``put_away_config``, the owner's away settings
as the control plane pushes them.

Pinned here:

- the payload is exactly ``{"v": 1, "while_using": "owner_first" | "even", "others_when_idle": bool}``; anything else
  is the node's usual error reply and nothing is stored or held;
- a good one is stored in ``engine_config`` under one key, as JSON, held at once (no restart), and answered with what
  the node now holds; it is read back at start, and never set it is ``owner_first`` and ``false``;
- only the owner class reaches it: a recipient, a routine, an unstamped relay frame and a stamp that does not verify
  get the dispatcher's usual refusal, and nothing is stored;
- the capability says 1.

Keys are made at run time; every id here is invented.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

import topos.core.handlers as hub
from topos.core.handlers import dispatch_relay_message
from topos.core.handlers.registry import OFF_LOOP_MESSAGE_TYPES, OWNER_ONLY_MESSAGE_TYPES
from topos.engine import registration
from topos.permissions_v2 import away_config, switches
from tests.topos.test_nr183_owner_lane import ANOTHER_KEY, OWNER, SOMEONE_ELSE, frame, stamped
from tests.topos.test_nr183_owner_lane import pinned_key  # noqa: F401 -- the fixture: the stamp key pinned

PUT = "put_away_config"
GOOD = {"v": 1, "while_using": "even", "others_when_idle": True}


@pytest.fixture(autouse=True)
def held_defaults():
    away_config.apply(away_config.DEFAULT)
    yield
    away_config.apply(away_config.DEFAULT)


@pytest.fixture
def database(tmp_path, monkeypatch, pinned_key):  # noqa: F811 -- the fixture above, asked for by name
    conn = sqlite3.connect(tmp_path / "node.db", check_same_thread=False)
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    yield conn
    conn.close()


def stored(conn):
    from topos.core.state import get_engine_config_value
    return get_engine_config_value(conn, away_config.ENGINE_CONFIG_KEY)


def put(payload, **how):
    return asyncio.run(dispatch_relay_message(stamped(frame("away-1", PUT, **{}) | {"payload": payload}, **how)))


def test_never_set_the_node_holds_owner_first_and_no_idleness(database):
    assert away_config.DEFAULT == {"v": 1, "while_using": "owner_first", "others_when_idle": False}
    assert away_config.load(database) == away_config.DEFAULT == away_config.current()
    assert away_config.shares_evenly() is False and away_config.others_when_idle() is False


def test_a_good_payload_is_stored_held_at_once_and_answered_with_what_is_held(database):
    reply = put(GOOD)
    assert reply == {"id": "away-1", "status": "ok", "payload": GOOD}
    assert away_config.current() == GOOD and away_config.shares_evenly() and away_config.others_when_idle()
    assert json.loads(stored(database)) == GOOD
    second = {"v": 1, "while_using": "owner_first", "others_when_idle": False}
    assert put(second)["payload"] == second == away_config.current()
    assert json.loads(stored(database)) == second


@pytest.mark.parametrize("payload", [
    None, [], "even", {}, {"v": 1, "while_using": "even"},
    {**GOOD, "quiet_hours": None},                                   # a key the contract does not name
    {**GOOD, "v": 2}, {**GOOD, "v": True}, {**GOOD, "v": "1"}, {**GOOD, "v": 1.0},
    {**GOOD, "while_using": "Even"}, {**GOOD, "while_using": "me_first"}, {**GOOD, "while_using": None},
    {**GOOD, "others_when_idle": 1}, {**GOOD, "others_when_idle": "true"}, {**GOOD, "others_when_idle": None},
])
def test_a_malformed_payload_is_the_usual_error_and_nothing_is_stored_or_held(database, payload):
    away_config.apply(GOOD)
    reply = put(payload)
    assert reply == {"id": "away-1", "status": "error", "error": "away_config_invalid"}
    assert stored(database) is None and away_config.current() == GOOD


@pytest.mark.parametrize("how", [
    {"cls": "third_party", "acting": SOMEONE_ELSE}, {"cls": "third_party", "acting": OWNER},
    {"cls": "owner_automation"}, {"key": ANOTHER_KEY},
], ids=["a recipient", "a third party naming the owner", "a routine", "a stamp that does not verify"])
def test_only_the_owner_class_reaches_it(database, how):
    reply = put(GOOD, **how)
    assert reply["status"] == "error" and reply.get("code") == 403
    assert stored(database) is None and away_config.current() == away_config.DEFAULT


def test_an_unstamped_relay_frame_is_refused_too(database):
    reply = asyncio.run(dispatch_relay_message({"id": "away-2", "type": PUT, "payload": GOOD}))
    assert reply == {"id": "away-2", "status": "error", "code": 403, "error": "owner_mode_required"}
    assert stored(database) is None and away_config.current() == away_config.DEFAULT


def test_what_was_stored_is_held_again_at_start_and_an_unreadable_row_is_the_defaults(database):
    put(GOOD)
    away_config.apply(away_config.DEFAULT)                 # a restart: memory empty, the row kept
    assert away_config.load(database) == GOOD == away_config.current()
    from topos.core.state import set_engine_config_value
    set_engine_config_value(database, away_config.ENGINE_CONFIG_KEY, "{not json")
    assert away_config.load(database) == away_config.DEFAULT
    set_engine_config_value(database, away_config.ENGINE_CONFIG_KEY, json.dumps({**GOOD, "v": 9}))
    assert away_config.load(database) == away_config.DEFAULT


def test_a_write_that_fails_holds_nothing_new(database, monkeypatch):
    import topos.core.state as state

    def broken(conn, key, value):
        raise sqlite3.OperationalError("an invented failure")
    monkeypatch.setattr(state, "set_engine_config_value", broken)
    assert put(GOOD) == {"id": "away-1", "status": "error", "error": "away_config_unavailable"}
    assert away_config.current() == away_config.DEFAULT


def test_listeners_hear_every_change_and_one_that_fails_undoes_nothing():
    heard = []
    stop = away_config.on_change(lambda: heard.append(away_config.while_using()))
    away_config.on_change(lambda: 1 / 0)
    try:
        away_config.apply(GOOD)
        assert heard == ["even"] and away_config.current() == GOOD
    finally:
        stop()
        away_config._listeners.clear()
    away_config.apply(away_config.DEFAULT)
    assert heard == ["even"]


def test_it_is_owner_only_off_the_loop_and_the_capability_says_one(monkeypatch):
    assert PUT in OWNER_ONLY_MESSAGE_TYPES and PUT in OFF_LOOP_MESSAGE_TYPES
    assert registration.build_engine_capabilities()["permissions_v2_away_config"] == 1 == away_config.CAPABILITY_VERSION
    snapshot = json.loads(open("topos/protocol/handled_message_types.json", encoding="utf-8").read())
    assert PUT in snapshot["handled_message_types"] and PUT in snapshot["owner_only_message_types"]
    assert switches.OWNER_LANE is not None   # the lane reads it: tests/topos/test_off2n_owner_lane_even.py

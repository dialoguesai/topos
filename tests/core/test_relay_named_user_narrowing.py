"""Review R1 (node), R-H1 and R-M1: on a node that has sharing on, a frame that names another user is refused.

The first rule (H1) reads only a frame the control plane stamped ``third_party``. The review found what that leaves:
an ``owner_app`` stamp naming another user reached every handled type, owner-only ones included; an
``owner_automation`` stamp naming another user and a frame with no stamp at all reached everything the relay
deferral reaches. The owner's decision (7 Oct): narrow it now, close it in 1.5.1. On a BOUND node only (there the
owner's id was checked against the control plane's at the bind):

(a) a verified stamp of ANY class that names a user who is not the owner reaches only the share doors. A stamp that
    names nobody is left as it is: the control plane's routine lane stamps ``owner_automation`` with no acting user.
(b) a frame with NO stamp that names another user, in one of the identity fields the control plane forwards
    (``RELAY_IDENTITY_FIELDS``), is refused the same way. Exceptions are listed, each a frame the control plane
    sends unstamped that names someone else on purpose (``UNSTAMPED_NAMING_EXCEPTIONS``).

Both only ever refuse. Neither closes the gap: a frame with no stamp that names nobody reaches what it reached,
which the last test here counts. Recorders replace every handler, as in test_relay_non_owner_gate.py; every id is
invented.
"""
from __future__ import annotations

import json

import pytest

import topos.core.handlers as hub
from tests.core.test_relay_non_owner_gate import (OWNER, SHARE_DOORS, SOMEONE_ELSE, UNHANDLED, bind_by_hand, node,  # noqa: F401
                                                   reachable_by_a_third_party, refusal, stamped, sweep)
from topos.core.handlers.registry import HANDLERS, OWNER_ONLY_MESSAGE_TYPES
from topos.principal import OWNER_APP, THIRD_PARTY

BOUND_OWNER = "bound-owner"
EVERYTHING = sorted(HANDLERS)
#: What the relay deferral (a frame with no stamp) reaches while nothing is Off-limits: not the owner-only types and
#: not ``signal_*`` (the dispatcher's older gates).
DEFERRAL = sorted(name for name in HANDLERS if name not in OWNER_ONLY_MESSAGE_TYPES and not name.startswith("signal_"))
#: The identity fields the control plane forwards, written out here on purpose.
FIELDS = (("caller", "requester_id"), ("payload", "requester_id"), ("payload", "mcp_requester_id"),
          ("payload", "user_id"), ("payload", "requesting_user_id"))
#: The frames the control plane sends with no stamp that name someone else on purpose, written out here too.
EXCEPTIONS = {"app_ingest": frozenset({("payload", "requesting_user_id")}), "connection_info": None}


def naming(where, field, value):
    """An unstamped frame of every type that names `value` in one identity field."""
    def make(message):
        return {**message, where: {**(message.get(where) or {}), field: value}}
    return make


def only_the_doors(reached, replies, *, but=()):
    assert reached == sorted(set(SHARE_DOORS) | set(but)), sorted(set(reached) ^ set(SHARE_DOORS))
    refused = {name: reply for name, reply in replies.items() if name not in SHARE_DOORS and name not in but}
    for name, reply in refused.items():
        assert reply == refusal("frame-" + name), name
    assert len({json.dumps({**reply, "id": None}, sort_keys=True) for reply in refused.values()}) == 1


def test_the_fields_and_the_exceptions_are_exactly_these():
    assert hub.RELAY_IDENTITY_FIELDS == FIELDS
    assert hub.UNSTAMPED_NAMING_EXCEPTIONS == EXCEPTIONS
    assert set(hub.UNSTAMPED_NAMING_EXCEPTIONS) <= set(HANDLERS)


# --- (a) a verified stamp of any class that names another user ------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [OWNER_APP, "owner_automation", THIRD_PARTY])
async def test_on_a_bound_node_a_stamp_of_any_class_that_names_another_user_reaches_only_the_share_doors(node, cls):
    """Rule: `_non_owner_relay_refusal` compares the acting user of every verified stamp on a bound node. Read only
    ``third_party`` again and an ``owner_app`` stamp for another user reaches all handled types."""
    bind_by_hand(node, owner=BOUND_OWNER)
    for acting in (SOMEONE_ELSE, OWNER):                   # OWNER is the engine config's id, not the bound identity's
        reached, replies = await sweep(node, lambda m: stamped(m, cls=cls, acting=acting))
        only_the_doors(reached, replies)
    assert replies["get_home_chat_session"] == refusal("frame-get_home_chat_session")
    assert replies["signal_blackhole_entity"] == refusal("frame-signal_blackhole_entity")


@pytest.mark.asyncio
async def test_on_a_bound_node_the_owners_own_stamps_reach_what_they_reached(node):
    bind_by_hand(node, owner=BOUND_OWNER)
    app, _ = await sweep(node, lambda m: stamped(m, cls=OWNER_APP, acting=BOUND_OWNER, client="topos_home_chat"))
    routine, _ = await sweep(node, lambda m: stamped(m, cls="owner_automation", acting=BOUND_OWNER))
    outside, _ = await sweep(node, lambda m: stamped(m, cls=THIRD_PARTY, acting=BOUND_OWNER, client="chatgpt"))
    assert app == EVERYTHING and routine == DEFERRAL and outside == reachable_by_a_third_party()


@pytest.mark.asyncio
@pytest.mark.parametrize("cls, reaches", [(OWNER_APP, EVERYTHING), ("owner_automation", DEFERRAL)])
async def test_a_stamp_of_an_owner_side_class_that_names_nobody_is_left_as_it_is(node, cls, reaches):
    """The routine lane's model call and query are stamped ``owner_automation`` with no acting user
    (cp:routines_engine_bridge.py); refusing them would stop every owner's routines."""
    bind_by_hand(node, owner=BOUND_OWNER)
    reached, _ = await sweep(node, lambda m: stamped(m, cls=cls, acting=""))
    assert reached == reaches


@pytest.mark.asyncio
@pytest.mark.parametrize("cls, reaches", [(OWNER_APP, EVERYTHING), ("owner_automation", DEFERRAL)])
async def test_on_an_unbound_node_an_owner_side_stamp_is_not_compared(node, cls, reaches):
    """Not there: an unbound node's own id can differ from the control plane's (review R1, R-M7), and comparing
    would turn the owner's own app away."""
    reached, _ = await sweep(node, lambda m: stamped(m, cls=cls, acting=SOMEONE_ELSE))
    assert reached == reaches


@pytest.mark.asyncio
async def test_a_bound_node_whose_identity_cannot_be_read_serves_no_named_user(node):
    """Never the open direction: a stamp that names a user is not the owner's when the node cannot say who that is."""
    bind_by_hand(node, owner=BOUND_OWNER)
    node.monkeypatch.setattr(hub, "_bound_owner_id", lambda: None)
    reached, replies = await sweep(node, lambda m: stamped(m, cls=OWNER_APP, acting=BOUND_OWNER))
    only_the_doors(reached, replies)
    reached, replies = await sweep(node, naming("payload", "user_id", BOUND_OWNER))
    only_the_doors(reached, replies, but={"connection_info"})


# --- (b) a frame with no stamp that names another user ----------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("where, field", FIELDS)
async def test_on_a_bound_node_an_unstamped_frame_that_names_another_user_is_refused(node, where, field):
    """Rule: `_unstamped_naming_refusal`. Remove it and a frame with no stamp naming another user reaches everything
    the relay deferral reaches (the first review's own example: ``get_home_chat_session`` keyed on
    ``payload.user_id``)."""
    bind_by_hand(node, owner=BOUND_OWNER)
    reached, replies = await sweep(node, naming(where, field, SOMEONE_ELSE))
    excepted = {"connection_info"} | ({"app_ingest"} if (where, field) == ("payload", "requesting_user_id") else set())
    only_the_doors(reached, replies, but=excepted)
    assert replies["get_home_chat_session"] == refusal("frame-get_home_chat_session")
    assert replies[UNHANDLED[0]] == refusal("frame-" + UNHANDLED[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("where, field", FIELDS)
@pytest.mark.parametrize("value", [BOUND_OWNER, "", None, 7, ["someone-else"], {"id": "someone-else"}],
                         ids=["the_owner", "empty", "null", "a_number", "a_list", "an_object"])
async def test_an_unstamped_frame_that_names_the_owner_or_nobody_reaches_what_it_reached(node, where, field, value):
    """Only a non-empty string that is not the owner names another user."""
    bind_by_hand(node, owner=BOUND_OWNER)
    reached, _ = await sweep(node, naming(where, field, value))
    assert reached == DEFERRAL


@pytest.mark.asyncio
async def test_another_persons_write_into_the_owners_inbox_still_arrives(node):
    """The one unstamped frame that names someone else on purpose: ``app_ingest`` names the WRITER in
    ``requesting_user_id`` and is stamped only when the writer is the owner (cp:routes/ingestion.py,
    cp:usage_inbox_flush.py). The owner it is for is still compared."""
    bind_by_hand(node, owner=BOUND_OWNER)

    async def one(payload):
        node.reached.clear()
        reply = await hub.dispatch_relay_message({"id": "w", "type": "app_ingest", "payload": payload})
        return [name for name, _cls in node.reached], reply

    assert (await one({"user_id": BOUND_OWNER, "requesting_user_id": SOMEONE_ELSE}))[0] == ["app_ingest"]
    assert (await one({"requesting_user_id": SOMEONE_ELSE}))[0] == ["app_ingest"]
    assert await one({"user_id": SOMEONE_ELSE, "requesting_user_id": SOMEONE_ELSE}) == ([], refusal("w"))
    assert await one({"user_id": BOUND_OWNER, "requester_id": SOMEONE_ELSE}) == ([], refusal("w"))
    # the exception is for a frame with NO stamp: under a stamp naming another user it is refused like any type
    node.reached.clear()
    reply = await hub.dispatch_relay_message(stamped(
        {"id": "w", "type": "app_ingest", "payload": {"user_id": BOUND_OWNER, "requesting_user_id": SOMEONE_ELSE}},
        cls=OWNER_APP, acting=SOMEONE_ELSE))
    assert (node.reached, reply) == ([], refusal("w"))


@pytest.mark.asyncio
async def test_the_handshake_is_not_compared(node):
    """``connection_info`` names the user the engine key is registered to; its own handler compares that with the
    node's id and keeps the node's."""
    bind_by_hand(node, owner=BOUND_OWNER)
    node.reached.clear()
    await hub.dispatch_relay_message({"id": "h", "type": "connection_info", "user_id": SOMEONE_ELSE,
                                      "payload": {"user_id": SOMEONE_ELSE}})
    assert [name for name, _cls in node.reached] == ["connection_info"]


@pytest.mark.asyncio
@pytest.mark.parametrize("where, field", FIELDS)
async def test_on_an_unbound_node_an_unstamped_frame_is_not_compared(node, where, field):
    reached, _ = await sweep(node, naming(where, field, SOMEONE_ELSE))
    assert reached == DEFERRAL


# --- the counts the review asked for --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_what_reaches_a_handler_by_stamp_on_a_bound_and_on_an_unbound_node(node, capsys):
    """The reviewer's `test_counts_by_stamp`, on both kinds of node. The numbers are printed for the report; the
    sets are asserted."""
    rows = {
        "third_party for another user": lambda m: stamped(m, cls=THIRD_PARTY, acting=SOMEONE_ELSE),
        "owner_app for another user": lambda m: stamped(m, cls=OWNER_APP, acting=SOMEONE_ELSE),
        "owner_automation for another user": lambda m: stamped(m, cls="owner_automation", acting=SOMEONE_ELSE),
        "no stamp, naming another user (payload.user_id)": naming("payload", "user_id", SOMEONE_ELSE),
        "no stamp, naming nobody": lambda m: m,
    }

    async def count():
        return {label: (await sweep(node, make))[0] for label, make in rows.items()}

    unbound = await count()
    bind_by_hand(node, owner=BOUND_OWNER)
    bound = await count()
    with capsys.disabled():
        print()
        for label in rows:
            print(f"R2N reached under [{label}]: unbound {len(unbound[label])} of {len(HANDLERS)}, "
                  f"bound {len(bound[label])} of {len(HANDLERS)}")
    doors = sorted(SHARE_DOORS)
    assert unbound["third_party for another user"] == bound["third_party for another user"] == doors
    assert unbound["owner_app for another user"] == EVERYTHING and bound["owner_app for another user"] == doors
    assert unbound["owner_automation for another user"] == DEFERRAL
    assert bound["owner_automation for another user"] == doors
    assert unbound["no stamp, naming another user (payload.user_id)"] == DEFERRAL
    assert bound["no stamp, naming another user (payload.user_id)"] == sorted(set(doors) | {"connection_info"})
    # What is left open, on purpose, until the control plane stamps every frame (1.5.1): a frame with no stamp
    # that names nobody is the relay deferral on both.
    assert unbound["no stamp, naming nobody"] == bound["no stamp, naming nobody"] == DEFERRAL

"""Fourth round, the tools a routine can call besides `query`: the same answer to the same question.

The control plane forwards a routine's tools to the node by name, under the routine's stamp
(`routines_engine_bridge.forward_routine_tool`). Five of them are the legacy inspection tools, which have no filter
of their own: until this round the relay's inspection floor refused all five to a routine as soon as ANYTHING was
Off-limits, and a refused tool call fails the routine's whole run (`routines_executor`). So one contact the upgrade
carried from an older "exclude", and every routine that reads messages, a table or the analytics stopped.

The ruling for the lane (7 Oct 2026) is one: an entry that is carried and waiting closes nothing by itself and is
applied to every item. For these tools:

  - the floor is not closed to a verified routine frame by a carried entry alone, for exactly the tools the routine
    bridge sends; an entry the owner made, a record protection, and every other caller: closed as before;
  - every row is still filtered: `get_table_rows` and `get_messages` pass each row through the share boundary's own
    row veto in the handler (the row's text and ids and, for a message, its conversation, roster and replies), and
    every answer passes the lane's one filter on the way out (`test_routine_lane_answers.py`).

The frames are really stamped and verified, and the handlers here are the real ones.
Every person, handle and id here is invented.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

import topos.core.handlers as hub
from tests.core.test_relay_non_owner_gate import refusal, stamped
from tests.core.test_routine_lane_answers import OWNER, ask, carry, node  # noqa: F401 (node: fixture)
from tests.topos.test_carried_entry_outward_paths import RECIPIENT, ROUTINE
from tests.topos.test_carried_entry_owner_paths import APP, as_caller
from tests.topos.test_carry_step_review_r1 import DATASET, cid, conn  # noqa: F401 (conn: fixture)
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.principal import OWNER_APP, RELAY_PRINCIPAL, THIRD_PARTY

pytestmark = pytest.mark.public

BRIDGE_TOOLS = ("get_messages", "get_analytics", "list_database_tables", "get_table_rows", "get_oplog")
OTHER_INSPECTION_TOOLS = ("get_table_schema", "get_table_count", "graph_summary", "read_jsonl_file", "list_jsonl_files")
NOW = "2026-10-01T09:00:00Z"


def floor(principal, msg_type):
    return as_caller(principal, hub._legacy_inspection_refusal, {"id": "frame-1", "type": msg_type}, msg_type)


def step(c):
    from topos.features.lifecycle.contact_excludes import carry_contact_excludes

    carry_contact_excludes(c)
    c.commit()


# ------------------------------------------------------------------------------------------------------ the floor

def test_the_tools_the_floor_opens_are_the_ones_the_routine_bridge_sends():
    """Written out here on purpose: a tool joins the code's list only by joining this one. They are the inspection
    tools among what `routines_engine_bridge` lists for a routine's three access modes."""
    assert hub.ROUTINE_BRIDGE_INSPECTION_TOOLS == frozenset(BRIDGE_TOOLS)
    assert hub.ROUTINE_BRIDGE_INSPECTION_TOOLS < hub.LEGACY_INSPECTION_TYPES
    assert set(OTHER_INSPECTION_TOOLS) == hub.LEGACY_INSPECTION_TYPES - hub.ROUTINE_BRIDGE_INSPECTION_TOOLS


@pytest.mark.parametrize("msg_type", BRIDGE_TOOLS)
def test_a_carried_entry_alone_does_not_close_a_bridge_tool_to_a_routine(node, msg_type):
    """Rule: `_legacy_inspection_refusal` asks, for a routine, whether anything is Off-limits apart from what is
    carried. Ask the old question and one carried contact fails every routine that calls one of these."""
    closed = refusal("frame-1")
    assert floor(ROUTINE, msg_type) is None
    step(node.conn)
    assert floor(ROUTINE, msg_type) is None
    # every other caller: exactly as at 9386a335
    assert [floor(p, msg_type) for p in (RELAY_PRINCIPAL, None)] == [closed, closed]
    assert floor(RECIPIENT, msg_type) == closed and floor(APP, msg_type) is None
    for door in ("local_http", "uds", "internal"):                         # the routine's class from another door
        other = type(ROUTINE)(cls="owner_automation", channel=door)
        assert floor(other, msg_type) == closed, door


@pytest.mark.parametrize("msg_type", OTHER_INSPECTION_TOOLS)
def test_an_inspection_tool_the_bridge_does_not_send_stays_closed(node, msg_type):
    assert floor(ROUTINE, msg_type) is None
    step(node.conn)
    assert floor(ROUTINE, msg_type) == refusal("frame-1")


def test_an_entry_the_owner_made_or_a_record_protection_closes_them_as_before(node):
    step(node.conn)
    assert floor(ROUTINE, "get_table_rows") is None
    node.conn.execute("INSERT INTO owner_only_records (canonical_table, record_id) VALUES ('conversation_messages', 'm-1')")
    node.conn.commit()
    assert floor(ROUTINE, "get_table_rows") == refusal("frame-1")
    node.conn.execute("DELETE FROM owner_only_records")
    BlackholeStore(node.conn).blackhole_entity(entity_ref="Perrin Ashgrove", processing_tier="secure", note=None)
    node.conn.commit()
    assert all(floor(ROUTINE, msg_type) == refusal("frame-1") for msg_type in BRIDGE_TOOLS)


def test_with_nothing_carried_the_floor_is_what_it_was(node):
    """An entry the owner made and nothing carried: closed to a routine, as at 9386a335."""
    BlackholeStore(node.conn).blackhole_entity(entity_ref="Perrin Ashgrove", processing_tier="secure", note=None)
    node.conn.commit()
    assert all(floor(ROUTINE, msg_type) == refusal("frame-1") for msg_type in BRIDGE_TOOLS)


def test_if_the_database_cannot_say_the_floor_stays_closed(node, monkeypatch):
    step(node.conn)
    assert floor(ROUTINE, "get_table_rows") is None
    from topos.features.lifecycle import blackhole_guard

    def broken(_conn):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(blackhole_guard, "anything_is_carried", broken)
    assert floor(ROUTINE, "get_table_rows") == refusal("frame-1")


# ------------------------------------------------------------------------------------------- the real row tools

def a_home(c):
    """Two threads: one with the contact the owner had excluded (saved as "Sam", from the `node` fixture) and one
    with another person; in each a message of the owner's that names nobody, and elsewhere one that names Sam."""
    c.execute("INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self) VALUES (?,?,?,?,0)",
              (cid("zz"), DATASET, "src", "Perrin Ashgrove"))
    for thread, other in (("t-sam", cid("0a")), ("t-perrin", cid("zz"))):
        c.execute("INSERT INTO conversations (conversation_id, dataset_id, source_id) VALUES (?,?,?)", (thread, DATASET, "src"))
        c.execute("INSERT INTO conversation_participants (conversation_id, dataset_id, source_id, contact_id, role) "
                  "VALUES (?,?,?,?,'member')", (thread, DATASET, "src", other))
    messages = [("m-1", "t-sam", "owner-handle", "See you at eight then.", 1),
                ("m-2", "t-sam", cid("0a"), "Eight works.", 0),
                ("m-3", "t-perrin", "owner-handle", "The compiler finally builds.", 1),
                ("m-4", "t-perrin", "owner-handle", "Sam is bringing the ladder on Friday.", 1),
                ("m-5", "t-perrin", cid("zz"), "Same plan as before, then.", 0),
                # a message whose thread has no row of its own: the share boundary cannot read its roster
                ("m-6", "t-lost", "owner-handle", "Nothing here names anyone.", 1)]
    for message_id, thread, sender, text, mine in messages:
        c.execute("INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_id, content, "
                  "event_at, source_id, is_from_self) VALUES (?,?,?,?,?,?,?,?)",
                  (message_id, thread, DATASET, sender, text, NOW, "src", mine))
    c.commit()


async def tool(msg_type, payload, *, cls="owner_automation", stamp=True):
    message = {"id": "frame-1", "type": msg_type, "payload": payload}
    if stamp:
        message = stamped(message, cls=cls, acting=OWNER, client="routine_executor")
    return await hub.dispatch_relay_message(message)


@pytest.fixture
def home(node):  # noqa: F811
    a_home(node.conn)
    node.conn.row_factory = sqlite3.Row                                    # as the node's own connection has it
    return node


@pytest.mark.asyncio
async def test_a_routine_reads_a_table_without_the_rows_of_the_carried_person(home):
    """The real `get_table_rows`. Before the step every row; after it the routine is still answered, and what is
    gone is: the contact's own row, every message of the thread they are in (the share boundary's row veto reads the
    conversation's roster, so the owner's own "See you at eight then." goes too), and the message elsewhere that
    names them. "Same plan as before" stays."""
    async def rows(table, **more):
        reply = await tool("get_table_rows", {"table_name": table, "limit": 50}, **more)
        assert reply["status"] == "ok", reply
        return reply["payload"]["rows"]

    key = lambda found, column: sorted(row[column] for row in found)       # noqa: E731
    assert key(await rows("contacts"), "contact_id") == [cid("0a"), cid("zz")]
    assert key(await rows("conversation_messages"), "message_id") == ["m-1", "m-2", "m-3", "m-4", "m-5", "m-6"]
    await carry(home)
    assert key(await rows("contacts"), "contact_id") == [cid("zz")]
    # m-6 goes too: a row the boundary cannot judge (its conversation has no row to read a roster from) is
    # withheld, as at the share doors, never passed through
    assert key(await rows("conversation_messages"), "message_id") == ["m-3", "m-5"]
    assert key(await rows("conversation_participants"), "contact_id") == [cid("zz")]
    reply = await tool("get_table_rows", {"table_name": "conversation_messages", "limit": 50})
    assert "Sam is" not in json.dumps(reply) and cid("0a") not in json.dumps(reply)
    # a frame with no stamp is refused whole, as before; the owner's app reads every row
    assert await tool("get_table_rows", {"table_name": "contacts"}, stamp=False) == refusal("frame-1")
    assert key(await rows("contacts", cls=OWNER_APP), "contact_id") == [cid("0a"), cid("zz")]


@pytest.mark.asyncio
async def test_a_routine_reads_messages_without_the_carried_persons(home):
    """The real `get_messages`, both lanes. The messenger lane already passed each row through the share boundary's
    row veto (`apply_message_contact_pipeline`); the AI-chat lane had no filter at all and now passes the same veto."""
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager

    c = home.conn
    await asyncio.to_thread(CanonicalTablesManager, c)                     # the AI-chat tables, as the node makes them
    # This lane joins `message_emotions` by a `message_id` column when that table is there. The migrations make the
    # table with other columns (`wiki_mvp_phase0`), so on a database made by the migrations alone the join errors,
    # for every caller and before any of this: not this round's. Without the table the lane reads without it.
    c.execute("DROP TABLE IF EXISTS message_emotions")
    c.execute("INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, source_id, created_at, updated_at) "
              "VALUES ('chat-1',?,'chatgpt',?,?)", (OWNER, NOW, NOW))
    for n, text in enumerate(["How do I fix a slow compiler?", "Draft a note to Sam about the ladder.",
                              "Summarise the same plan as before."]):
        c.execute("INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, sender_id, event_at, content, "
                  "sequence, source_id) VALUES (?,?,?,?,?,?,?,?)", (f"a-{n}", "chat-1", "user", "user", NOW, text, n, "chatgpt"))
    # a chat whose TITLE names the person: no message of it says the name, and no filter on the answer could know;
    # the boundary's row veto reads the conversation each message belongs to
    c.execute("INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, updated_at) "
              "VALUES ('chat-2',?,'Ladder plans with Sam','chatgpt',?,?)", (OWNER, NOW, NOW))
    c.execute("INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, sender_id, event_at, content, "
              "sequence, source_id) VALUES ('a-9','chat-2','user','user',?,'What size do I need?',0,'chatgpt')", (NOW,))
    c.commit()

    async def messages(stream):
        reply = await tool("get_messages", {"dataset_id": DATASET, "message_stream": stream, "limit": 50})
        assert reply["status"] == "ok", reply
        return sorted(row["message_id"] for row in reply["payload"]["messages"])

    assert await messages("ai_chat") == ["a-0", "a-1", "a-2", "a-9"]
    assert await messages("conversation") == ["m-1", "m-2", "m-3", "m-4", "m-5", "m-6"]
    await carry(home)
    assert await messages("ai_chat") == ["a-0", "a-2"]
    assert await messages("conversation") == ["m-3", "m-5"]


@pytest.mark.asyncio
async def test_a_routine_reads_messenger_messages_on_a_node_that_never_made_the_ai_chat_table(home):
    """Fifth round, item 1 (second re-check, R3-M1). This home has only ever synced messages: it has no AI-chat table
    at all, which is an ordinary node. The messenger lane's row veto looks each id up in both message tables, and the
    one never made used to raise: 0 messages for a routine once anyone was carried, status `ok`. A table this
    database never made holds no row of that id (`tests/permissions_v2/test_row_veto_one_message_table.py`)."""
    c = home.conn
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'ai_chat%'").fetchone()[0] == 0

    async def messages():
        reply = await tool("get_messages", {"dataset_id": DATASET, "message_stream": "conversation", "limit": 50})
        assert reply["status"] == "ok", reply
        return sorted(row["message_id"] for row in reply["payload"]["messages"])

    assert await messages() == ["m-1", "m-2", "m-3", "m-4", "m-5", "m-6"]
    await carry(home)
    assert await messages() == ["m-3", "m-5"]                             # not []: the thread, the naming one, m-6


@pytest.mark.asyncio
async def test_the_other_bridge_tools_answer_a_routine_while_an_entry_waits(home):
    """`list_database_tables` and `get_oplog` hold no row of anybody's; before this round they were refused with the
    rest and the routine's run failed."""
    await carry(home)
    tables = await tool("list_database_tables", {})
    assert tables["status"] == "ok" and isinstance(tables["payload"].get("tables"), (dict, list))
    assert (await tool("get_oplog", {}))["payload"] == {"ops": []}
    for msg_type in ("list_database_tables", "get_oplog"):
        assert await tool(msg_type, {}, stamp=False) == refusal("frame-1")
        assert await tool(msg_type, {}, cls=THIRD_PARTY) == refusal("frame-1")


@pytest.mark.asyncio
async def test_if_the_row_veto_cannot_be_built_the_tool_gives_no_rows(home):
    """Something is carried and the boundary over it refuses: the handler errors and the dispatcher refuses; no row
    is sent either way."""
    await carry(home)
    home.conn.execute("ALTER TABLE contact_identifiers RENAME TO contact_identifiers_gone")
    reply = await tool("get_table_rows", {"table_name": "contacts", "limit": 50})
    assert reply["status"] == "error" and "rows" not in json.dumps(reply) and cid("0a") not in json.dumps(reply)


# --------------------------------------------------- what is derived from a conversation with them (fifth round, item 2)

@pytest.mark.asyncio
async def test_what_was_derived_from_their_thread_is_withheld_by_the_id_it_carries(home):
    """Second re-check, R3-M2. The row veto withholds a carried person's thread where the row IS a message. What a
    node derives from those messages is not a message row: the index row that holds their own message word for word,
    the object that summarises it, the fact derived from it, the row of the thread itself, a count kept under the
    thread's id. None of them names the person, so the item rule passed all five to a routine.

    Rule: the item rule also withholds an item that carries the id of a message the boundary withholds, or of a
    conversation it reaches (`EntityBoundary.carries_a_reached_id`). The verdict on the message is the door's own
    (`observe`: its text, its conversation, the roster, the replies). Take the lookup out and a routine reads the
    carried person's own words through the index."""
    c = home.conn
    theirs = "Eight works. Bring the deed for the Larkspur flat."
    c.execute("UPDATE conversation_messages SET content=? WHERE message_id='m-2'", (theirs,))
    for message_id, text in (("m-1", "See you at eight then."), ("m-2", theirs), ("m-3", "The compiler finally builds.")):
        c.execute("INSERT INTO signal_embeddings (embedding_id, record_id, source_id, text_preview, search_text, "
                  "vector_format, chunk_index, record_type) VALUES (?,?,?,?,?,?,?,?)",
                  (f"e-{message_id}", message_id, "src", text, text, "none", 0, "message"))
    objects = {"o-theirs": {"summary_text": theirs, "record_id": "m-2"},          # summarises THEIR message
               "o-thread": {"thread": "t-sam", "messages": 2},                    # a count kept under the thread's id
               "o-other": {"summary_text": "The compiler finally builds.", "record_id": "m-3"},
               "o-count": {"thread": "t-perrin", "messages": 3}}
    for object_id, payload in objects.items():
        c.execute("INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, "
                  "valid_from, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                  (object_id, "work", "summary", f"k:{object_id}", json.dumps(payload), NOW, NOW, NOW))
    for fact_id, evidence in (("f-theirs", ["m-2"]), ("f-other", ["m-3"])):
        c.execute("INSERT INTO signal_facts (fact_id, dimension, payload_json) VALUES (?,?,?)",
                  (fact_id, "plans", json.dumps({"text": "Collect the deed at eight.", "evidence": evidence})))
    c.commit()

    async def keys(table, column):
        reply = await tool("get_table_rows", {"table_name": table, "limit": 50})
        assert reply["status"] == "ok", reply
        return sorted(row[column] for row in reply["payload"]["rows"])

    assert await keys("signal_embeddings", "embedding_id") == ["e-m-1", "e-m-2", "e-m-3"]
    assert await keys("signal_objects", "object_id") == ["o-count", "o-other", "o-theirs", "o-thread"]
    assert await keys("signal_facts", "fact_id") == ["f-other", "f-theirs"]
    assert await keys("conversations", "conversation_id") == ["t-perrin", "t-sam"]
    await carry(home)
    assert await keys("signal_embeddings", "embedding_id") == ["e-m-3"]         # their words, and the owner's reply
    assert await keys("signal_objects", "object_id") == ["o-count", "o-other"]
    assert await keys("signal_facts", "fact_id") == ["f-other"]
    assert await keys("conversations", "conversation_id") == ["t-perrin"]
    reply = await tool("get_table_rows", {"table_name": "signal_embeddings", "limit": 50})
    assert "Larkspur" not in json.dumps(reply)

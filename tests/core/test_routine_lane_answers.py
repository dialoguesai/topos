"""Fourth round: every answer the node gives a routine passes one filter on its way out of the dispatcher.

On the routine lane an Off-limits entry that is carried and waiting is applied item by item
(`tests/topos/test_routine_lane_carried_items.py` holds the rule itself and the query pipeline's use of it). The
pipeline is not the only thing a routine can ask: the control plane forwards its tools by name under the same stamp,
and the pipeline's own direct lanes add to an answer after retrieval. So the rule is also applied in ONE place for the
whole lane: `core.handlers._withhold_what_is_carried`, after the handler, whichever handler it was.

protects, each with the fault that undoes it:
  - a routine's answer loses each item that names the carried person, from any handler;
  - it is decided by the stamp that VERIFIED: the same frame with no stamp, with the owner's app's stamp, with his
    outside client's stamp, or with a routine stamp signed by another key gets what it got before this round;
  - the query envelope: the answer is `public_result`; the turn's own bookkeeping beside it is not rewritten;
  - the routine's model call is not filtered, as before;
  - if the rule cannot be built, or the node has no database, the frame is refused: never sent unfiltered.
The frames are really stamped and really verified (the relay's own entry point); only the handlers are stand-ins.
Every person, handle and id here is invented.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from types import SimpleNamespace

import pytest

import topos.core.handlers as hub
from tests.core.test_relay_non_owner_gate import ANOTHER_KEY, KEY, refusal, stamped
from tests.topos.test_carried_entry_waits import ORDINARY, excluded
from tests.topos.test_carry_step_review_r1 import cid, conn  # noqa: F401 (conn: fixture)
from topos.core.handlers.registry import HANDLERS
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.contact_excludes import carry_contact_excludes
from topos.permissions_v2 import switches
from topos.principal import OWNER_APP, THIRD_PARTY
from topos.storage.db import paths

pytestmark = [pytest.mark.public, pytest.mark.asyncio]

OWNER = "owner-1"
NAMES = "Sam is bringing the ladder."
PLAIN = "The week went to the compiler and the bouldering trip."
#: What a tool might answer: two rows that name the carried contact (one by a word, one by the contact's id), two
#: that do not, and the answer's own fields.
ANSWER = {"rows": [{"id": "r1", "text": PLAIN}, {"id": "r2", "text": NAMES},
                   {"id": "r3", "contact_id": cid("0a")}, {"id": "r4", "text": "Same plan as before."}],
          "columns": ["id", "text", "contact_id"], "total": 4}
KEPT = {"rows": [{"id": "r1", "text": PLAIN}, {"id": "r4", "text": "Same plan as before."}],
        "columns": ["id", "text", "contact_id"], "total": 4}


@pytest.fixture
def node(monkeypatch, tmp_path, conn):  # noqa: F811
    """An unbound node on a full database (every migration), the control plane's stamp key pinned, one contact
    excluded in the older setting; `answers` replaces a handler with one that returns a fixed payload."""
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(KEY.public_key().public_bytes_raw()).decode("ascii"))
    for name in switches.BY_NAME:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **k: SimpleNamespace(path=tmp_path / "canonical.db"))
    switches.forget_bound()
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                 "updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
    conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('user_id', ?)", (OWNER,))
    excluded(conn, ORDINARY["saved as Sam"])
    conn.commit()
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    called = []

    def answers(msg_type, payload):
        async def handler(message):
            called.append(msg_type)
            return {"id": message.get("id"), "status": "ok", "payload": json.loads(json.dumps(payload))}
        monkeypatch.setitem(HANDLERS, msg_type, handler)

    yield SimpleNamespace(conn=conn, answers=answers, called=called, monkeypatch=monkeypatch)
    switches.forget_bound()


async def carry(node):
    """The upgrade step, off the event loop as the runner runs it."""
    def step():
        carry_contact_excludes(node.conn)
        node.conn.commit()
    await asyncio.to_thread(step)


async def ask(msg_type, *, cls="owner_automation", acting=OWNER, key=KEY, stamp=True):
    message = {"id": "frame-1", "type": msg_type, "payload": {}}
    if stamp:
        message = stamped(message, cls=cls, acting=acting, client="routine_executor", key=key)
    return await hub.dispatch_relay_message(message)


@pytest.mark.parametrize("msg_type", ["get_sources_overview", "tools_retrieve", "aggregate"])
async def test_a_routines_answer_loses_each_item_that_names_the_carried_person(node, msg_type):
    """Rule: `handle_control_plane_request` passes every answer through `_withhold_what_is_carried`. Return the
    handler's answer as it is and a routine is handed the two rows again, by whichever tool it asked."""
    node.answers(msg_type, ANSWER)
    assert (await ask(msg_type))["payload"] == ANSWER                     # before the step: nothing is carried
    await carry(node)
    reply = await ask(msg_type)
    assert reply == {"id": "frame-1", "status": "ok", "payload": KEPT}
    assert NAMES not in json.dumps(reply) and cid("0a") not in json.dumps(reply)
    assert await ask(msg_type, acting="") == reply                        # the stamp the routine's query carries


async def test_only_a_routine_stamp_that_verified_gets_the_rule(node):
    """For a frame the control plane stamped `owner_automation`, whose stamp verifies, and only for it. Every other
    caller of the same type gets what it got at 9386a335: the handler's answer, or the dispatcher's refusal."""
    node.answers("get_sources_overview", ANSWER)
    await carry(node)
    assert (await ask("get_sources_overview"))["payload"] == KEPT
    # no stamp (the relay deferral), the owner's app, his outside client: this handler's answer, as before
    assert (await ask("get_sources_overview", stamp=False))["payload"] == ANSWER
    assert (await ask("get_sources_overview", cls=OWNER_APP))["payload"] == ANSWER
    assert (await ask("get_sources_overview", cls=THIRD_PARTY))["payload"] == ANSWER
    # a routine stamp that does not verify is not a routine: it is the least class there is, refused
    node.called.clear()
    assert await ask("get_sources_overview", key=ANOTHER_KEY) == refusal("frame-1")
    assert node.called == []


async def test_the_owners_own_callers_are_not_filtered_here(node):
    """The owner himself reads an entry that waits as he did before the upgrade (the third round's rule): his app
    and his outside client get the handler's own answer object."""
    node.answers("tools_retrieve", ANSWER)
    await carry(node)
    for cls in (OWNER_APP, THIRD_PARTY):
        assert (await ask("tools_retrieve", cls=cls))["payload"] == ANSWER


async def test_for_a_query_the_answer_is_the_public_result_and_the_turns_bookkeeping_is_left_alone(node):
    """The pipeline's direct lanes write a sentence and a list into the answer after retrieval (`facts_direct`,
    `closeness`, `collaborators`), composed from the owner's data with no Off-limits filter of their own: the
    sentence is dropped with its key when it names the person, the list loses the item. The fields beside
    `public_result` are the turn's own bookkeeping, and a contact whose saved name is one of the node's own words
    must not take them out of the answer."""
    excluded(node.conn, dict(display="Live"), tail="0b")                   # a contact saved under a word the node uses
    envelope = {"turn_outcome": "live_query", "session_id": "qs_1", "game_layer_strategy": "direct",
                "public_result": {"scope_id": "relationships:read", "answer_type": "facts",
                                  "answer": "You work most closely with Perrin Ashgrove and Sam.",
                                  "items": ["Perrin Ashgrove", "Sam"],
                                  "facts": [{"predicate": "rel.works_with", "value": "Sam", "evidence": 3},
                                            {"predicate": "rel.works_with", "value": "Perrin Ashgrove", "evidence": 5}]},
                "audit": {"stores_touched": ["facts_store"]}}
    node.answers("query", envelope)
    await carry(node)
    reply = (await ask("query"))["payload"]
    assert reply == {**envelope, "public_result": {
        "scope_id": "relationships:read", "answer_type": "facts", "items": ["Perrin Ashgrove"],
        "facts": [{"predicate": "rel.works_with", "value": "Perrin Ashgrove", "evidence": 5}]}}
    assert "Sam" not in json.dumps(reply["public_result"])
    assert reply["turn_outcome"] == "live_query"                           # "Live" is carried; this is not an item
    denied = {"turn_outcome": "denied", "public_result": None, "deny_reason": "unknown_scope"}
    node.answers("query", denied)
    assert (await ask("query"))["payload"] == denied


async def test_the_routines_model_call_is_not_filtered(node):
    """What a model wrote from a prompt the control plane put together is not the owner's stored data read out, and
    the model gate reads the owner's own view (the third round). As before: untouched."""
    assert hub.ROUTINE_ANSWERS_NOT_READ_OUT == frozenset({"llm_generation"})
    wrote = {"output": NAMES, "model": "some-model"}
    node.answers("llm_generation", wrote)
    await carry(node)
    assert (await ask("llm_generation", acting=""))["payload"] == wrote


async def test_if_the_rule_cannot_be_built_the_frame_is_refused(node):
    """Something is carried and the boundary over it refuses: the answer is never sent unfiltered, and the refusal
    is the dispatcher's one refusal, with nothing of the node in it."""
    node.answers("get_sources_overview", ANSWER)
    await carry(node)
    assert (await ask("get_sources_overview"))["payload"] == KEPT
    node.conn.execute("ALTER TABLE contact_identifiers RENAME TO contact_identifiers_gone")
    assert await ask("get_sources_overview") == refusal("frame-1")
    assert (await ask("get_sources_overview", stamp=False))["payload"] == ANSWER   # no rule for it, as before


async def test_a_routine_on_a_node_with_no_database_is_refused(node):
    node.answers("get_sources_overview", ANSWER)
    await carry(node)
    node.monkeypatch.setattr(hub, "get_db_connection", lambda: None)
    assert await ask("get_sources_overview") == refusal("frame-1")


async def test_an_entry_the_owner_made_changes_nothing_here(node):
    """This filter is for what is carried and waiting. An entry the owner made is held where it always was (the
    floors, the pipeline's own policy): with only such an entry, a routine's answer from a handler that has no
    filter of its own is the handler's, as at 9386a335."""
    node.answers("get_sources_overview", ANSWER)
    def owner_marks():
        BlackholeStore(node.conn).blackhole_entity(entity_ref="Sam", processing_tier="secure", note=None)
        node.conn.commit()
    await asyncio.to_thread(owner_marks)
    assert (await ask("get_sources_overview"))["payload"] == ANSWER
    await carry(node)                                                            # the step adds to his entry; none is carried
    assert not [entry for entry in BlackholeStore(node.conn).list() if entry["carried_waiting"]]
    assert (await ask("get_sources_overview"))["payload"] == ANSWER


async def test_the_filter_is_only_for_what_is_carried(node):
    """An entry the owner made beside a carried one: this filter withholds the carried person and does not start
    reading his own entry the share boundary's way. His entry is held where it always was. Rule: the boundary
    behind `CarriedItems` is built over the carried entries only (`ONLY_WAITING`). Build it over every entry and a
    routine's answers lose, here, what they did not lose at 9386a335."""
    other = {"rows": [{"id": "r1", "text": NAMES}, {"id": "r2", "text": "Perrin Ashgrove sent the invoice."},
                      {"id": "r3", "text": PLAIN}]}
    node.answers("get_sources_overview", other)

    def owner_marks():
        BlackholeStore(node.conn).blackhole_entity(entity_ref="Perrin Ashgrove", processing_tier="secure", note=None)
        node.conn.commit()
    await asyncio.to_thread(owner_marks)
    assert (await ask("get_sources_overview"))["payload"] == other        # only his own entry: no filter here
    await carry(node)
    assert (await ask("get_sources_overview"))["payload"] == {"rows": other["rows"][1:]}


async def test_a_database_the_step_never_wrote_to_is_not_read_for_this(node, monkeypatch):
    """No carried column, no rule, and nothing else is read: the answer is the handler's own object."""
    bare = sqlite3.connect(":memory:", check_same_thread=False)        # the filter runs off the event loop
    bare.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    monkeypatch.setattr(hub, "get_db_connection", lambda: bare)
    node.answers("get_sources_overview", ANSWER)
    assert (await ask("get_sources_overview"))["payload"] == ANSWER

"""Third fix round, review R2-H3: what the node gives the owner's app for an entry the upgrade carried.

The note the app lane builds from is `lanes/reports/R3N_INTERFACE.md`; this file holds each thing it promises, on
BOTH doors (the relay handlers the control plane's proxy reaches, and the local routes), which share one function
each so they cannot drift:

  - the list: every row says whether it is carried and waiting, what to show it as (never a contact id), its names
    and identifiers apart, and its clean-up's state in true words;
  - "make it fully Off-limits", by the entry's own id: the owner's act, for an entry with no linked entity too; an id
    that is no entry answers 404 and makes nothing;
  - "remove", by the entry's own id, for an entry carried by a name, by a handle only and by an id only;
  - the counts before the owner confirms: asked for on the list, written nowhere;
  - the notice: one for the step, gone by itself when nobody waits.
Before this round an entry with no linked entity had no control in the app at all, and the node's notice named a
control the app did not have. Every person, handle and id here is invented.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

import topos.core.handlers as hub
import topos.core.state as state_mod
from tests.topos.test_carry_step_review_r1 import HEART, PHONE, cid, contact, entity
from topos.core.handlers.signal_features import (handle_signal_blackhole_entity, handle_signal_list_blackholes,
                                                 handle_signal_unblackhole_entity)
from topos.features.lifecycle.blackhole import OWNER, BlackholeStore
from topos.features.lifecycle.contact_excludes import NOTICE, carry_contact_excludes
from topos.home_chat.schema import ensure_home_chat_schema
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.permissions_v2.protection_clock import TOMBSTONES_SQL
from topos.storage.canonical import ConversationsTablesManager
from topos.storage.db import write_gate
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public

ADDRESS = "wilf@fernmail.example"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "canonical.db"), check_same_thread=False)
    apply_all_migrations(c)
    c.execute(TOMBSTONES_SQL)
    ConversationsTablesManager(c).ensure_tables()
    ensure_home_chat_schema(c)
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    monkeypatch.setattr(state_mod, "get_db_connection", lambda: c)
    monkeypatch.setattr(state_mod, "close_thread_db_connection", lambda: None)
    write_gate.reset_loop_warning_state()
    yield c
    c.close()


def four_carried(c):
    """A contact with a linked entity, one saved as "Will" with no entity, one with a handle only (saved under an
    emoji), one with nothing but its id."""
    contact(c, cid("0a"), "Bree V.", handles=[(PHONE, "phone")], usernames=["breev"])
    entity(c, "ent-1", "Brisa Vantongeren", cid("0a"), aliases=["Bree"])
    contact(c, cid("0b"), "Will")
    contact(c, cid("0c"), HEART, handles=[(ADDRESS, "email")])
    contact(c, cid("0d"), None)
    out = carry_contact_excludes(c)
    c.commit()
    assert out["carried"] == 4
    return {row["display_label"]: row for row in asyncio.run(relay_list())["blackholes"]}


async def relay_list(**payload):
    answer = await handle_signal_list_blackholes({"id": "l1", "type": "signal_list_blackholes", "payload": payload})
    assert answer["status"] == "ok"
    return answer["payload"]


async def relay_mark(ref, **payload):
    return await handle_signal_blackhole_entity({"id": "m1", "type": "signal_blackhole_entity",
                                                 "payload": {"entity_id": ref, "processing_tier": "secure", **payload}})


async def relay_remove(ref):
    return await handle_signal_unblackhole_entity({"id": "u1", "type": "signal_unblackhole_entity",
                                                   "payload": {"entity_id": ref}})


# ------------------------------------------------------------------------------------------------------ the list

def test_each_row_says_what_it_is_and_is_never_shown_under_a_contact_id(conn):
    rows = four_carried(conn)
    assert set(rows) == {"Bree V.", "Will", HEART, "A contact with no saved name"}
    for label, row in rows.items():
        assert row["carried_waiting"] is True and row["clean_up"]["state"] == "not_started"
        assert set(row["carried_waiting_aliases"]) == {row["normalized_name"], *row["aliases"]}
        shown = json.dumps([row["display_label"], row["names"], row["identifiers"]])
        assert ":contact:" not in shown and "default contact" not in shown, label
        assert set(row["names"]).isdisjoint(row["identifiers"])
        # everything the app reads today is still there
        assert {"blackhole_id", "entity_id", "normalized_name", "canonical_name", "aliases", "identifier_aliases",
                "processing_tier", "rebuild_state", "note", "created_at", "updated_at"} <= set(row)
    assert rows["Bree V."]["names"] == ["bree", "bree v."] and rows["Bree V."]["entity_id"] == "ent-1"
    assert rows["Bree V."]["identifiers"] == sorted(["breev", "+1 555 0142 0137"])
    assert rows[HEART]["identifiers"] == [ADDRESS] and rows[HEART]["names"] == [] and rows[HEART]["entity_id"] == ""
    assert rows["A contact with no saved name"]["identifiers"] == [] and rows["A contact with no saved name"]["names"] == []
    assert rows["Will"]["clean_up"] == {"state": "not_started", "looks_for": 1, "too_short": 0}
    whole = asyncio.run(relay_list())
    assert whole["carried"] == {"waiting": 4} and "preview" not in whole
    assert [(n["kind"], n["message"]) for n in whole["notifications"]] == [("carried_over", NOTICE.format(count=4))]


@pytest.mark.asyncio
async def test_the_local_route_gives_the_same_list(conn):
    from topos.api.signal import list_blackholes

    await asyncio.to_thread(four_carried, conn)
    local = await list_blackholes(preview=None, _api_key="test")
    assert json.dumps(local, sort_keys=True, default=str) == json.dumps(await relay_list(), sort_keys=True, default=str)


def test_an_entry_the_owner_made_is_shown_as_he_named_it_with_no_waiting(conn):
    BlackholeStore(conn).blackhole_entity(entity_ref="Perrin Ashgrove")
    (row,) = asyncio.run(relay_list())["blackholes"]
    assert (row["display_label"], row["carried_waiting"], row["carried_waiting_aliases"]) == ("Perrin Ashgrove", False, [])
    assert row["clean_up"] == {"state": "not_started", "looks_for": 1, "too_short": 0}


# ------------------------------------------------------------------------------ make it fully Off-limits, by entry id

@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Bree V.", "Will", HEART, "A contact with no saved name"],
                         ids=["a_linked_entity", "a_name_only", "a_handle_only", "an_id_only"])
async def test_the_owners_act_by_the_entrys_own_id(conn, label):
    """Rule: both doors take an entry's own id where they take an entity id. Without it an entry with no linked
    entity cannot be acted on at all (the app has no entity id to send)."""
    rows = await asyncio.to_thread(four_carried, conn)
    conn.execute("INSERT INTO user_goals (goal_id, goal_text, payload_json, created_at) VALUES ('g1',"
                 "'Ask Will to return the drill','{}','2026-10-01')")
    conn.commit()
    entry = rows[label]
    answer = await relay_mark(entry["blackhole_id"])
    assert answer["status"] == "ok", answer
    result = answer["payload"]
    assert (result["blackhole_id"], result["carried_waiting"], result["rebuild_state"]) == (entry["blackhole_id"], False, "complete")
    assert result["already_blackholed"] is True and result["rebuild"]["status"] == "complete"
    assert result["clean_up"]["state"] == "done" and result["display_label"] == label
    store = BlackholeStore(conn)
    after = store.get(entry["blackhole_id"])
    # the entry itself is the one that was there: same name, same aliases, nothing renamed to its id
    assert {key: after[key] for key in ("normalized_name", "canonical_name", "aliases", "entity_id")} == \
           {key: entry[key] for key in ("normalized_name", "canonical_name", "aliases", "entity_id")}
    assert store.get(entry["blackhole_id"], view=OWNER) is not None       # an ordinary entry now, for his own tools too
    assert len(store.list()) == 4 and sum(1 for row in store.list() if row["carried_waiting"]) == 3
    goals = conn.execute("SELECT COUNT(*) FROM user_goals").fetchone()[0]
    assert goals == (0 if label == "Will" else 1)                         # its clean-up ran, and only its own


@pytest.mark.asyncio
async def test_the_local_route_takes_the_entrys_own_id_too(conn):
    from topos.api.signal import EntityBlackholeBody, blackhole_entity, unblackhole_entity

    rows = await asyncio.to_thread(four_carried, conn)
    acted = await blackhole_entity(rows[HEART]["blackhole_id"], EntityBlackholeBody(processing_tier="local_only"),
                                   _api_key="test")
    assert (acted["carried_waiting"], acted["processing_tier"], acted["clean_up"]["state"]) == (False, "local_only", "done")
    removed = await unblackhole_entity(rows["A contact with no saved name"]["blackhole_id"], _api_key="test")
    assert removed["removed"] is True and len(BlackholeStore(conn).list()) == 3


@pytest.mark.asyncio
async def test_an_id_that_is_no_entry_answers_404_and_makes_nothing(conn):
    from fastapi import HTTPException

    from topos.api.signal import EntityBlackholeBody, blackhole_entity

    await asyncio.to_thread(four_carried, conn)
    before = conn.execute("SELECT * FROM entity_blackholes ORDER BY blackhole_id").fetchall()
    answer = await relay_mark("bh_0123456789ab")
    assert (answer["status"], answer["code"], answer["error"]) == ("error", 404, "no such off-limits entry")
    with pytest.raises(HTTPException) as refused:
        await blackhole_entity("bh_0123456789ab", EntityBlackholeBody(), _api_key="test")
    assert refused.value.status_code == 404
    assert conn.execute("SELECT * FROM entity_blackholes ORDER BY blackhole_id").fetchall() == before
    assert (await relay_remove("bh_0123456789ab"))["payload"] == {"removed": False}


NEARLY_AN_ID = ["BH_0123456789AB", "Bh_0123456789ab", "bh_0123456789abc", "bh_0123456789ag", "bh_", "bh_x",
                " bh_0123456789AB ", "bh_0123456789ab"]


@pytest.mark.asyncio
@pytest.mark.parametrize("asked", NEARLY_AN_ID)
async def test_text_that_starts_like_an_entry_id_and_is_no_entry_is_refused_and_makes_nothing(conn, asked):
    """The fifth round (second re-check, R3-L2). Rule: anything that starts `bh_`, in any letter case, and is not
    an entry is refused, never made into an entry (`BlackholeStore.blackhole_entity`). The fourth round refused the
    exact shape only: capitals, thirteen digits, a letter that is no hex digit and the bare prefix each made an
    entry under that text, so an app bug filled the list with entries that withhold nothing real."""
    from fastapi import HTTPException

    from topos.api.signal import EntityBlackholeBody, blackhole_entity

    await asyncio.to_thread(four_carried, conn)
    before = conn.execute("SELECT * FROM entity_blackholes ORDER BY blackhole_id").fetchall()
    answer = await relay_mark(asked)
    assert (answer["status"], answer["code"], answer["error"]) == ("error", 404, "no such off-limits entry")
    with pytest.raises(HTTPException) as refused:
        await blackhole_entity(asked, EntityBlackholeBody(), _api_key="test")
    assert refused.value.status_code == 404
    assert conn.execute("SELECT * FROM entity_blackholes ORDER BY blackhole_id").fetchall() == before


def test_an_entry_the_owner_already_has_under_such_a_name_is_still_his_to_act_on(conn):
    """Nothing is made, so nothing is refused: an entry made before this rule, under a name that starts `bh_`, is
    found by that name as before and is acted on. Only a NEW entry is never made under such text."""
    four_carried(conn)
    conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name, aliases_json,"
                 " processing_tier, rebuild_state) VALUES ('bh_aaaaaaaaaaaa', '', 'bh_club', 'BH_Club', '[]', "
                 "'secure', 'complete')")
    conn.commit()
    again = BlackholeStore(conn).blackhole_entity(entity_ref="BH_Club", processing_tier="local_only")
    assert (again["blackhole_id"], again["already_blackholed"], again["processing_tier"]) == (
        "bh_aaaaaaaaaaaa", True, "local_only")
    assert len(BlackholeStore(conn).list()) == 5


def test_a_contact_saved_under_such_a_name_is_carried_and_is_named_by_something_else(conn):
    """The upgrade step must not be stranded by the rule: a contact whose saved name starts `bh_` (an exact id
    shape failed the step at every start before this round) is carried, named by its handle or its id, and the
    saved name is one of the entry's names, so a message that names it is still withheld."""
    contact(conn, cid("0e"), "bh_0123456789ab", handles=[(ADDRESS, "email")])
    contact(conn, cid("0f"), "BH_Runners")
    out = carry_contact_excludes(conn)
    conn.commit()
    assert (out["carried"], out["failed"], out["boundary"]) == (2, 0, "built")
    rows = {row["display_label"]: row for row in asyncio.run(relay_list())["blackholes"]}
    assert set(rows) == {"bh_0123456789ab", "BH_Runners"}
    assert rows["bh_0123456789ab"]["normalized_name"] == ADDRESS and "bh_0123456789ab" in rows["bh_0123456789ab"]["names"]
    assert rows["BH_Runners"]["normalized_name"].endswith("contact 0f") and rows["BH_Runners"]["names"] == ["bh_runners"]
    assert EntityBoundary(conn).mentions_protected("Lunch with BH_Runners went late.")


@pytest.mark.asyncio
async def test_the_older_switch_still_works_by_entity_id_and_is_the_owners_act(conn):
    """The present app sends the entity id. Kept as it is: for a carried entry that has one, that mark is his act."""
    rows = await asyncio.to_thread(four_carried, conn)
    answer = await relay_mark("ent-1")
    assert answer["payload"]["blackhole_id"] == rows["Bree V."]["blackhole_id"]
    assert answer["payload"]["carried_waiting"] is False and answer["payload"]["rebuild"]["status"] == "complete"
    removed = await relay_remove("ent-1")
    assert removed["payload"]["removed"] is True and len(BlackholeStore(conn).list()) == 3


# ---------------------------------------------------------------------------------------------------- remove

@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Will", HEART, "A contact with no saved name"],
                         ids=["a_name_only", "a_handle_only", "an_id_only"])
async def test_any_entry_is_removed_by_its_own_id(conn, label):
    rows = await asyncio.to_thread(four_carried, conn)
    entry = rows[label]
    assert entry["entity_id"] == ""                                       # the case the app's switch is not drawn for
    answer = await relay_remove(entry["blackhole_id"])
    assert answer["payload"]["removed"] is True and answer["payload"]["blackhole_id"] == entry["blackhole_id"]
    left = {row["display_label"] for row in (await relay_list())["blackholes"]}
    assert left == set(rows) - {label}
    assert carry_contact_excludes(conn)["carried"] == 0                   # and the step never puts it back
    if label == "Will":
        assert not EntityBoundary(conn).mentions_protected("Ask Will to return the drill")   # shared again


# ---------------------------------------------------------------------------- the counts before the owner confirms

def _chat(c, session, turns):
    history = {"version": 3, "currentId": "t0", "messages": {
        f"t{i}": {"id": f"t{i}", "role": "user", "content": text, "parentId": None, "childrenIds": []}
        for i, text in enumerate(turns)}}
    c.execute("INSERT INTO home_chat_sessions (id, user_id, engine_id, title, history_json) VALUES (?,?,?,?,?)",
              (session, "owner-user-1", "engine-1", "notes", json.dumps(history)))


@pytest.mark.asyncio
async def test_the_preview_counts_what_the_clean_up_would_take_and_writes_nothing(conn):
    """R2-L3: a contact saved as "Will". The clean-up the owner starts overwrites his own chat turns and summaries
    that hold the word, and nothing but a whole-database backup brings a turn back; he is shown the numbers first.
    Rule: the preview runs the clean-up's own functions with their writes off. Let one write and this test's
    byte-for-byte comparison of every table fails."""
    rows = await asyncio.to_thread(four_carried, conn)
    _chat(conn, "s1", ["I will call the plumber.", "Will is bringing the drill.", "Nothing to add."])
    _chat(conn, "s2", ["Goodwill and willing helpers.", "That will do."])
    _chat(conn, "s3", ["Nothing here holds the word."])
    conn.execute("INSERT INTO signal_dimension_briefs (brief_id, signal_dimension, head_revision_id, structured_json, "
                 "markdown_body, revision_number, updated_at, updated_by) VALUES ('b1','plans','r1','{}',"
                 "'We will see about the fence.',1,'2026-10-01','test')")
    conn.execute("INSERT INTO user_goals (goal_id, goal_text, payload_json, created_at) VALUES ('g1',"
                 "'Ask Will to return the drill','{}','2026-10-01')")
    conn.execute("INSERT INTO signal_embeddings (embedding_id, record_id, source_id, text_preview, search_text, "
                 "vector_format, chunk_index, record_type) VALUES ('e1','r1','src','it will rain','it will rain',"
                 "'none',0,'message')")
    conn.commit()

    def everything():
        tables = ("entity_blackholes", "blackhole_notifications", "home_chat_sessions", "signal_dimension_briefs",
                  "user_goals", "signal_embeddings", "signal_objects", "topic_clusters")
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables}

    before, changes = everything(), conn.total_changes
    entry = rows["Will"]
    listed = await relay_list(preview=entry["blackhole_id"])
    assert listed["preview"] == {
        "blackhole_id": entry["blackhole_id"], "looks_for": 1, "too_short": 0,
        # three of the owner's own turns, two of which only hold the verb; "Goodwill" and "willing" are not it
        "counts": {"chat_turns": 3, "chat_sessions": 2, "briefs": 1, "derived_objects": 0, "search_rows": 1,
                   "goals": 1, "insights": 0, "topic_labels": 0, "topic_excerpts": 0, "community_names": 0}}
    assert everything() == before and conn.total_changes == changes       # nothing written, not even a timestamp
    assert BlackholeStore(conn).get(entry["blackhole_id"])["carried_waiting"] is True
    # what it counted is what the owner's act then does
    report = (await relay_mark(entry["blackhole_id"]))["payload"]["rebuild"]
    assert (report["chat_turns_withdrawn"], report["chat_sessions_withdrawn"], report["briefs_invalidated"],
            report["goals_withdrawn"], report["embeddings_withdrawn"]) == (3, 2, 1, 1, 1)


@pytest.mark.asyncio
async def test_a_preview_of_no_entry_is_no_preview(conn):
    await asyncio.to_thread(four_carried, conn)
    for asked in ("bh_0123456789ab", "ent-1", "Will", "", None, 7):
        assert "preview" not in await relay_list(preview=asked)


# ------------------------------------------------------------------------------------------------- R2-L4: true words

@pytest.mark.asyncio
async def test_a_clean_up_that_looked_for_nothing_does_not_say_fully_hidden(conn):
    """A contact saved as "J": every name is under three characters, so the clean-up looks for nothing. The state is
    still `complete` (the job ran to its end and the person is hidden from everyone else at read time); what was
    untrue was "fully hidden everywhere" and, in the app, "cleaned". The row now says how many terms were looked
    for, and the node's own notice says the summaries were not changed."""
    contact(conn, cid("0j"), "J")
    carry_contact_excludes(conn)
    conn.execute("INSERT INTO user_goals (goal_id, goal_text, payload_json, created_at) VALUES ('g1',"
                 "'Give J the spare keys','{}','2026-10-01')")
    conn.commit()
    (row,) = (await relay_list())["blackholes"]
    assert row["clean_up"] == {"state": "not_started", "looks_for": 0, "too_short": 1}   # "j"; the id is not counted
    result = (await relay_mark(row["blackhole_id"]))["payload"]
    assert result["clean_up"] == {"state": "done", "looks_for": 0, "too_short": 1}
    assert (result["rebuild"]["status"], result["rebuild"]["looked_for"], result["rebuild"]["too_short"]) == ("complete", 0, 1)
    assert conn.execute("SELECT COUNT(*) FROM user_goals").fetchone()[0] == 1          # nothing was cleaned
    (told,) = [n["message"] for n in BlackholeStore(conn).notifications(state="open") if n["kind"] == "rebuild_complete"]
    assert told == ("'J' is Off-limits and hidden from everyone else. 1 of its names is too short to look for in "
                    "your summaries, so text that names them only that way was not cleaned out.")
    assert EntityBoundary(conn).mentions_protected("Give J the spare keys")            # every share: still withheld


@pytest.mark.asyncio
async def test_a_clean_up_that_looked_for_something_says_what_it_always_said(conn):
    rows = await asyncio.to_thread(four_carried, conn)
    await relay_mark(rows["Will"]["blackhole_id"])
    told = [n["message"] for n in BlackholeStore(conn).notifications(state="open") if n["kind"] == "rebuild_complete"]
    assert told == ["'Will' is now fully hidden everywhere outside your own view."]

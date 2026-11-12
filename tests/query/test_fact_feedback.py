"""An app records the owner's reaction and rank on a pack fact (DISCLOSURE_RULES DR-72): who holds the grant, which
facts the write reaches, what is stored, and what ``pack_facts`` gives back.

Synthetic only: a migrated temp database with invented people, facts and rows; never the live node.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import topos.core.handlers as hub
from tests.truth_lane_support import add_journal, bind, build_node, connect
from topos import mcp_clients, uds
from topos.cli import agents_cmd
from topos.cli.agents_cmd import agents_group
from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
from topos.mcp_clients import AppGrant, parse_app_grant
from topos.principal import CP_RELAY, OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.query import fact_feedback

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
READS = ("moments.joy", "moments.tension", "narrative.self")
#: Reads three packs, may record on two of them.
WRITER = Principal(cls="owner_agent", channel="uds", client_id="agent-atlas",
                   app_grant=AppGrant(packs=READS, feedback=("moments.joy", "narrative.self")))
#: Another app with the same grant: what one records, the other may change.
SECOND_WRITER = Principal(cls="owner_agent", channel="uds", client_id="agent-second",
                          app_grant=AppGrant(packs=READS, feedback=("moments.joy",)))
READER = Principal(cls="owner_agent", channel="uds", client_id="agent-reader", app_grant=AppGrant(packs=READS))
CODING_AGENT = Principal(cls="owner_agent", channel="uds", client_id="agent-claude-code")
OWNER = Principal(cls=OWNER_APP, channel="uds")
NOT_AVAILABLE = {"id": "r1", "status": "error", "code": 404, "error": "fact_not_available"}
NOT_ON_ITS_LIST = {"id": "r1", "status": "error", "code": 403, "error": "owner_agent_not_allowed"}
INVALID = {"id": "r1", "status": "error", "error": "invalid_fact_feedback_request"}


@pytest.fixture
def node(tmp_path, tmp_path_factory, monkeypatch):
    path = build_node(tmp_path, tmp_path_factory.getbasetemp())
    bind(monkeypatch, path)
    conn = connect(path)
    seed_pack_registry(conn, bundled_pack_dir())
    yield SimpleNamespace(path=path, conn=conn)
    conn.close()


def day(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ref(record_id, days_ago):
    return [{"table": "journal_entries", "record_id": record_id, "role": "authored",
             "date": (NOW - timedelta(days=days_ago)).date().isoformat()}]


def add_fact(conn, pack, predicate, value, *, at, refs, sensitivity="personal"):
    """A fact as the derivation writer stores one."""
    payload = {"subject_entity_id": "owner", "predicate": predicate, "object_value": value.get("description") or predicate,
               "object_entity_id": None, "disclosure": "owner_only", "asserted_by": "owner", "actor_role": "authored",
               "pack": pack, "altitude": "stated", "value_struct": value, "sensitivity": sensitivity,
               "quote": "A line only the owner wrote."}
    oid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, confidence, "
        "source_refs_json, valid_from, created_at, updated_at, extractor_version, ontology_id, ontology_version, altitude) "
        "VALUES (?, 'profile', 'fact', ?, ?, 0.8, ?, ?, ?, ?, 'derivation:test', ?, '0.1.5', 'stated')",
        (oid, f"{predicate}:{oid[:8]}", json.dumps(payload), json.dumps(refs), at, at, at, pack))
    conn.commit()
    return oid


@pytest.fixture
def world(node):
    """An invented owner: three packs on, one switched off, and one fact of each kind the write must tell apart."""
    conn = node.conn
    for pack in ("moments.joy", "moments.tension", "narrative.self", "obligations.commitments"):
        conn.execute("UPDATE pack_registry SET enabled=1 WHERE pack_id=?", (pack,))
    conn.execute("UPDATE pack_registry SET enabled=0 WHERE pack_id='life.events'")
    add_journal(conn, "j-dinner", "Dinner ran long and nobody minded.", at=day(13))
    add_journal(conn, "j-walk", "Long walk with Juniper Example by the river.", at=day(8))
    add_journal(conn, "j-fight", "Argued about the schedule again.", at=day(5))
    conn.commit()
    ids = SimpleNamespace()
    ids.peak = add_fact(conn, "moments.joy", "moment.peak", {
        "kind": "connected", "description": "dinner that ran long", "with": []}, at=day(13), refs=ref("j-dinner", 13))
    ids.walk = add_fact(conn, "moments.joy", "moment.peak", {
        "kind": "small_pleasure", "description": "a long walk by the river", "with": []}, at=day(8),
        refs=ref("j-walk", 8))
    ids.read_only_pack = add_fact(conn, "moments.tension", "moment.tension", {
        "kind": "recurring_frustration", "with": [], "description": "argued about the schedule again",
        "status": "open"}, at=day(5), refs=ref("j-fight", 5))
    ids.special = add_fact(conn, "narrative.self", "self.attribution_pattern", {
        "domain": "setback", "attributes_to": "own_ability"}, at=day(4), refs=ref("j-fight", 4), sensitivity="special")
    ids.outside = add_fact(conn, "obligations.commitments", "commit.made", {
        "direction": "owed_by_owner", "description": "send the photos", "status": "open"}, at=day(22),
        refs=ref("j-dinner", 22))
    return SimpleNamespace(conn=conn, ids=ids)


def dispatch(principal, payload=None, msg_type="fact_feedback"):
    """Through the dispatcher, so the owner-agent type lists and the door split are what is tested."""
    return asyncio.run(hub.handle_control_plane_request({"id": "r1", "type": msg_type, "payload": payload or {}},
                                                        principal=principal))


def as_handler(principal, payload):
    """The handler alone, under a principal (what the class gate inside it decides)."""
    token = set_principal(principal)
    try:
        return asyncio.run(hub.HANDLERS["fact_feedback"]({"id": "r1", "type": "fact_feedback", "payload": payload}))
    finally:
        reset_principal(token)


def put(fact_id, principal=WRITER, **fields):
    return dispatch(principal, {"fact_id": fact_id, **fields})


def stored(conn, fact_id):
    try:
        row = conn.execute("SELECT reaction, rank, updated_by FROM fact_feedback WHERE fact_id=?", (fact_id,)).fetchone()
    except Exception:  # noqa: BLE001 -- no table yet
        return None
    return tuple(row) if row else None


def read(principal, **payload):
    return {f["id"]: f for f in dispatch(principal, payload, "pack_facts")["payload"]["facts"]}


def table_exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='fact_feedback'").fetchone() is not None


class Untouchable(dict):
    """A payload that fails the test if anything reads it."""

    def __getattribute__(self, name):
        if name in ("__class__",):
            return object.__getattribute__(self, name)
        raise AssertionError(f"payload read: {name}")


def protect(conn, entity_ref, *, complete=True):
    from topos.features.lifecycle.blackhole import BlackholeStore

    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref=entity_ref)
    if complete:
        store.mark_rebuild_complete(entity_ref)
    conn.commit()


# --- who may write ------------------------------------------------------------------------------------------------------
def test_an_app_with_the_grant_records_and_gets_the_stored_values_back(world):
    out = put(world.ids.peak, reaction="love", rank=2)
    assert out["status"] == "ok"
    assert {k: out["payload"][k] for k in ("version", "fact_id", "reaction", "rank")} == {
        "version": "fact_feedback/v1", "fact_id": world.ids.peak, "reaction": "love", "rank": 2}
    assert out["payload"]["updated_at"]
    assert stored(world.conn, world.ids.peak) == ("love", 2, "atlas")


def test_an_app_with_reads_only_is_refused_and_nothing_is_stored(world):
    assert put(world.ids.peak, READER, reaction="love") == NOT_ON_ITS_LIST
    assert not table_exists(world.conn)


def test_an_app_with_no_grant_at_all_is_refused(world):
    bare = Principal(cls="owner_agent", channel="uds", client_id="agent-older")
    assert put(world.ids.peak, bare, reaction="love") == NOT_ON_ITS_LIST


def test_a_coding_agent_is_refused(world):
    assert put(world.ids.peak, CODING_AGENT, reaction="love") == NOT_ON_ITS_LIST


@pytest.mark.parametrize("principal", [
    OWNER, READER, CODING_AGENT,
    Principal(cls=THIRD_PARTY, channel="local_http", client_id="agent-atlas", app_grant=WRITER.app_grant),
    Principal(cls=CP_RELAY, channel="cp_relay"),
    Principal(cls="owner_agent", channel="local_http", client_id="agent-atlas", app_grant=WRITER.app_grant),
    None,
])
def test_the_handler_refuses_every_other_class_before_reading_the_payload(world, principal):
    out = as_handler(principal, Untouchable())
    assert out == {"id": "r1", "status": "error", "code": 403, "error": "feedback_grant_required"}
    assert not table_exists(world.conn)


def test_the_feedback_type_is_on_the_list_only_with_the_feedback_grant():
    assert "fact_feedback" in hub.owner_agent_allowed_types(WRITER)
    assert "fact_feedback" not in hub.owner_agent_allowed_types(READER)
    assert "fact_feedback" not in hub.owner_agent_allowed_types(CODING_AGENT)
    assert AppGrant(packs=READS).write_types() == frozenset()
    assert AppGrant(packs=READS, feedback=("moments.joy",)).write_types() == frozenset({"fact_feedback"})
    assert WRITER.app_grant.message_types() - READER.app_grant.message_types() == {"fact_feedback"}


# --- set, replace and clear, each without disturbing the other ---------------------------------------------------------
def test_a_reaction_is_set_replaced_and_taken_back_and_the_rank_stays(world):
    fact = world.ids.peak
    put(fact, rank=3)
    assert put(fact, reaction="like")["payload"]["reaction"] == "like"
    assert stored(world.conn, fact) == ("like", 3, "atlas")
    assert put(fact, reaction="sad")["payload"]["reaction"] == "sad"
    assert stored(world.conn, fact) == ("sad", 3, "atlas")
    out = put(fact, reaction=None)["payload"]
    assert (out["reaction"], out["rank"]) == (None, 3)
    assert stored(world.conn, fact) == (None, 3, "atlas")


def test_a_rank_is_set_replaced_and_taken_back_and_the_reaction_stays(world):
    fact = world.ids.peak
    put(fact, reaction="down")
    assert put(fact, rank=-3)["payload"]["rank"] == -3
    assert stored(world.conn, fact) == ("down", -3, "atlas")
    assert put(fact, rank=1)["payload"]["rank"] == 1
    assert stored(world.conn, fact) == ("down", 1, "atlas")
    out = put(fact, rank=0)["payload"]                      # 0 takes the rank back
    assert (out["reaction"], out["rank"]) == ("down", None)
    assert stored(world.conn, fact) == ("down", None, "atlas")
    put(fact, rank=2)
    assert put(fact, rank=None)["payload"]["rank"] is None  # so does null
    assert stored(world.conn, fact) == ("down", None, "atlas")


def test_both_can_be_set_in_one_write_and_taking_both_back_leaves_no_row(world):
    fact = world.ids.peak
    put(fact, reaction="love", rank=3)
    assert stored(world.conn, fact) == ("love", 3, "atlas")
    out = put(fact, reaction=None, rank=0)["payload"]
    assert (out["reaction"], out["rank"], out["updated_at"]) == (None, None, None)
    assert stored(world.conn, fact) is None


@pytest.mark.parametrize("reaction", fact_feedback.REACTIONS)
def test_each_reaction_is_taken(world, reaction):
    assert put(world.ids.peak, reaction=reaction)["payload"]["reaction"] == reaction


@pytest.mark.parametrize("rank", [-3, -2, -1, 1, 2, 3])
def test_each_rank_is_taken(world, rank):
    assert put(world.ids.peak, rank=rank)["payload"]["rank"] == rank


def test_the_write_is_the_value_not_a_toggle(world):
    """A retried request must not undo itself: the app takes a reaction back by sending null."""
    first = put(world.ids.peak, reaction="love")["payload"]
    again = put(world.ids.peak, reaction="love")["payload"]
    assert again["reaction"] == "love" and again["updated_at"] == first["updated_at"]


def test_taking_back_what_was_never_set_writes_nothing(world):
    out = put(world.ids.peak, reaction=None, rank=0)
    assert out["status"] == "ok" and out["payload"]["reaction"] is None and out["payload"]["rank"] is None
    assert not table_exists(world.conn)


def test_one_fact_does_not_disturb_another(world):
    put(world.ids.peak, reaction="love", rank=3)
    put(world.ids.walk, reaction="sad", rank=-1)
    assert stored(world.conn, world.ids.peak) == ("love", 3, "atlas")
    assert stored(world.conn, world.ids.walk) == ("sad", -1, "atlas")


# --- what is refused ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fields", [
    {"rank": 4}, {"rank": -4}, {"rank": 100}, {"rank": 1.0}, {"rank": 2.5}, {"rank": "2"}, {"rank": True},
    {"reaction": "angry"}, {"reaction": "LOVE"}, {"reaction": ""}, {"reaction": 1}, {"reaction": ["love"]},
    {"reaction": "love", "rank": 9}, {"reaction": "nope", "rank": 1},
    {}, {"reaction": "love", "note": "words"}, {"reaction": "love", "packs": ["moments.joy"]},
])
def test_a_rank_out_of_range_an_unknown_reaction_and_a_bad_shape_are_refused(world, fields):
    assert put(world.ids.peak, **fields) == INVALID
    assert stored(world.conn, world.ids.peak) is None


@pytest.mark.parametrize("payload", [
    None, [], "love", {"reaction": "love"}, {"fact_id": "", "reaction": "love"}, {"fact_id": 7, "reaction": "love"},
    {"fact_id": "x" * 65, "reaction": "love"}, {"fact_id": ["a"], "rank": 1},
])
def test_a_request_without_a_usable_fact_id_is_refused(world, payload):
    assert as_handler(WRITER, payload) == INVALID


def test_a_refused_value_leaves_what_was_stored(world):
    put(world.ids.peak, reaction="like", rank=1)
    assert put(world.ids.peak, reaction="love", rank=7) == INVALID      # the whole request, not half of it
    assert stored(world.conn, world.ids.peak) == ("like", 1, "atlas")


# --- only a fact the app could read, and one refusal for the rest -------------------------------------------------------
def test_a_fact_outside_its_packs_an_off_limits_fact_and_an_unknown_id_get_the_same_refusal(world):
    protect(world.conn, "Juniper Example")                        # the walk's source record names them
    assert world.ids.walk not in read(WRITER) and world.ids.peak in read(WRITER)
    outside = put(world.ids.outside, reaction="love", rank=1)
    off_limits = put(world.ids.walk, reaction="love", rank=1)
    unknown = put(uuid.uuid4().hex, reaction="love", rank=1)
    assert outside == off_limits == unknown == NOT_AVAILABLE
    assert not table_exists(world.conn)
    assert put(world.ids.peak, reaction="love")["status"] == "ok"   # the rest is still reachable


def test_the_refusal_is_the_same_bytes_whatever_was_asked(world):
    protect(world.conn, "Juniper Example")
    answers = {json.dumps(put(fact, **fields), sort_keys=True)
               for fact in (world.ids.outside, world.ids.walk, uuid.uuid4().hex, "not-an-id")
               for fields in ({"reaction": "love"}, {"rank": -2}, {"reaction": None, "rank": 0})}
    assert answers == {json.dumps(NOT_AVAILABLE, sort_keys=True)}


def test_a_special_class_fact_gets_the_same_refusal(world):
    assert world.ids.special not in read(WRITER)                  # held back from the read, feedback grant or not
    assert put(world.ids.special, reaction="love") == NOT_AVAILABLE


def test_a_fact_of_a_pack_it_reads_but_has_no_feedback_grant_on_gets_the_same_refusal(world):
    assert world.ids.read_only_pack in read(WRITER)
    assert put(world.ids.read_only_pack, reaction="love") == NOT_AVAILABLE


def test_a_fact_of_a_pack_the_owner_switched_off_gets_the_same_refusal(world):
    world.conn.execute("UPDATE pack_registry SET enabled=0 WHERE pack_id='moments.joy'")
    world.conn.commit()
    assert put(world.ids.peak, reaction="love") == NOT_AVAILABLE


def test_while_an_off_limits_rebuild_is_pending_nothing_can_be_recorded(world):
    protect(world.conn, "Somebody Unrelated", complete=False)
    assert put(world.ids.peak, reaction="love") == NOT_AVAILABLE


def test_a_protected_record_takes_its_fact_out_of_reach(world):
    from topos.features.lifecycle.record_protection import RecordProtectionStore

    RecordProtectionStore(world.conn).protect(canonical_table="journal_entries", record_id="j-dinner")
    world.conn.commit()
    assert put(world.ids.peak, reaction="love") == NOT_AVAILABLE
    assert put(world.ids.walk, reaction="love")["status"] == "ok"


def test_a_record_that_is_not_a_pack_fact_gets_the_same_refusal(world):
    assert put("j-dinner", reaction="love") == NOT_AVAILABLE      # a journal entry's id is not a fact's


def test_a_bad_value_is_refused_the_same_way_for_a_fact_it_cannot_reach(world):
    """The value is judged before the fact, so the answer to a bad value says nothing about the id."""
    assert put(world.ids.outside, rank=9) == put(uuid.uuid4().hex, rank=9) == put(world.ids.peak, rank=9) == INVALID


# --- what is stored, and what is never touched --------------------------------------------------------------------------
def fact_rows(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM signal_objects ORDER BY object_id").fetchall()]


def test_it_never_edits_the_fact(world):
    before = fact_rows(world.conn)
    put(world.ids.peak, reaction="love", rank=3)
    put(world.ids.peak, reaction="down", rank=-3)
    put(world.ids.peak, reaction=None, rank=0)
    assert fact_rows(world.conn) == before                         # confidence, text, validity: byte for byte


def test_the_row_says_when_and_which_app(world):
    put(world.ids.peak, reaction="love")
    row = world.conn.execute("SELECT fact_id, reaction, rank, updated_at, updated_by FROM fact_feedback").fetchall()
    assert len(row) == 1 and row[0][0] == world.ids.peak and row[0][4] == "atlas"
    assert datetime.fromisoformat(row[0][3]).tzinfo is not None
    assert [c[1] for c in world.conn.execute("PRAGMA table_info(fact_feedback)").fetchall()] == [
        "fact_id", "reaction", "rank", "updated_at", "updated_by"]


def test_another_app_with_the_grant_replaces_it_and_the_row_names_the_last_writer(world):
    put(world.ids.peak, reaction="love", rank=3)
    out = put(world.ids.peak, SECOND_WRITER, reaction="sad")["payload"]
    assert (out["reaction"], out["rank"]) == ("sad", 3)            # the first app's rank is left as it was
    assert stored(world.conn, world.ids.peak) == ("sad", 3, "second")


def test_the_table_cannot_hold_a_value_the_door_refuses(world):
    import sqlite3

    put(world.ids.peak, reaction="love")
    for column, value in (("reaction", "angry"), ("rank", 0), ("rank", 4), ("rank", -4)):
        with pytest.raises(sqlite3.IntegrityError):
            world.conn.execute(f"UPDATE fact_feedback SET {column}=? WHERE fact_id=?", (value, world.ids.peak))


def test_no_table_until_the_first_value_and_a_read_never_creates_it(world):
    assert not table_exists(world.conn)
    assert read(WRITER)[world.ids.peak]["reaction"] is None and read(OWNER)[world.ids.peak]["rank"] is None
    assert not table_exists(world.conn)
    put(world.ids.peak, rank=1)
    assert table_exists(world.conn)


def test_the_table_is_owner_only_for_the_legacy_table_tools():
    from topos.data_explorer_tables import is_owner_only_table

    assert is_owner_only_table(fact_feedback.TABLE)


def test_a_superseded_fact_keeps_its_row_and_drops_out_of_reach(world):
    """A pack re-run closes the old fact and writes a new id: the row stays with the old id, is no longer shown, can no
    longer be changed, and the new fact starts with nothing."""
    put(world.ids.peak, reaction="love", rank=2)
    world.conn.execute("UPDATE signal_objects SET valid_to=? WHERE object_id=?", (day(1), world.ids.peak))
    world.conn.commit()
    successor = add_fact(world.conn, "moments.joy", "moment.peak", {
        "kind": "connected", "description": "dinner that ran long, again", "with": []}, at=day(1),
        refs=ref("j-dinner", 1))
    facts = read(WRITER)
    assert world.ids.peak not in facts
    assert (facts[successor]["reaction"], facts[successor]["rank"]) == (None, None)
    assert put(world.ids.peak, reaction="sad") == NOT_AVAILABLE
    assert stored(world.conn, world.ids.peak) == ("love", 2, "atlas")


def test_the_write_is_committed_under_the_write_gate(world, monkeypatch):
    held = []
    events = []
    real_gate, real_commit = fact_feedback.with_db_write, fact_feedback.commit_connection

    @contextmanager
    def gate():
        with real_gate():
            held.append(True)
            try:
                yield
            finally:
                held.pop()

    def commit(conn):
        events.append(("commit", bool(held), conn.in_transaction))
        real_commit(conn)

    monkeypatch.setattr(fact_feedback, "with_db_write", gate)
    monkeypatch.setattr(fact_feedback, "commit_connection", commit)
    assert put(world.ids.peak, reaction="love")["status"] == "ok"
    assert events == [("commit", True, True)]                      # one commit, inside the gate, with the row pending
    assert stored(world.conn, world.ids.peak) == ("love", None, "atlas")   # and another connection sees it


def test_a_fact_it_cannot_reach_never_takes_the_gate(world, monkeypatch):
    @contextmanager
    def gate():
        raise AssertionError("the gate was taken for a refused fact")
        yield  # pragma: no cover

    monkeypatch.setattr(fact_feedback, "with_db_write", gate)
    assert put(world.ids.outside, reaction="love") == NOT_AVAILABLE


def test_the_log_carries_the_app_the_id_and_what_changed_never_text(world, caplog):
    with caplog.at_level(logging.DEBUG):
        put(world.ids.peak, reaction="love", rank=3)
        put(world.ids.outside, reaction="love")
        put(world.ids.peak, reaction="angry")
    lines = [r.getMessage() for r in caplog.records if r.name == "topos.query.fact_feedback"]
    assert lines == [f"fact_feedback app=atlas fact={world.ids.peak} changed=reaction,rank"]
    everything = "\n".join(r.getMessage() for r in caplog.records)
    for text in ("dinner that ran long", "A line only the owner wrote", "Dinner ran long", "send the photos"):
        assert text not in everything
    assert world.ids.outside not in everything                      # a refused id is not logged either


def test_each_write_is_recorded_under_the_apps_name(world):
    put(world.ids.peak, reaction="love")
    put(world.ids.outside, reaction="love")                          # refused: not an answered request
    rows = world.conn.execute("SELECT tool_name, verified_cls, verified_client_id FROM mcp_request_log "
                              "WHERE tool_name='fact_feedback'").fetchall()
    assert [tuple(r) for r in rows] == [("fact_feedback", "owner_agent", "agent-atlas")]


def test_a_failure_inside_answers_a_fixed_code(world, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("a line that must not reach the caller")

    monkeypatch.setattr(fact_feedback, "_readable", boom)
    assert put(world.ids.peak, reaction="love") == {"id": "r1", "status": "error", "error": "fact_feedback_unavailable"}


# --- pack_facts carries both ----------------------------------------------------------------------------------------------
def test_pack_facts_carries_the_reaction_and_the_rank(world):
    put(world.ids.peak, reaction="love", rank=3)
    put(world.ids.walk, rank=-2)
    facts = read(WRITER)
    assert (facts[world.ids.peak]["reaction"], facts[world.ids.peak]["rank"]) == ("love", 3)
    assert (facts[world.ids.walk]["reaction"], facts[world.ids.walk]["rank"]) == (None, -2)
    assert (facts[world.ids.read_only_pack]["reaction"], facts[world.ids.read_only_pack]["rank"]) == (None, None)
    assert all({"reaction", "rank"} <= set(fact) for fact in facts.values())
    assert "updated_by" not in json.dumps(list(facts.values())) and "atlas" not in json.dumps(list(facts.values()))


def test_a_reads_only_app_sees_them_and_cannot_write_them(world):
    put(world.ids.peak, reaction="like", rank=1)
    seen = read(READER)[world.ids.peak]
    assert (seen["reaction"], seen["rank"]) == ("like", 1)
    assert put(world.ids.peak, READER, reaction="down", rank=-3) == NOT_ON_ITS_LIST
    assert put(world.ids.peak, READER, reaction=None, rank=0) == NOT_ON_ITS_LIST
    assert stored(world.conn, world.ids.peak) == ("like", 1, "atlas")


def test_the_owners_app_sees_them_too(world):
    put(world.ids.peak, reaction="sad", rank=-1)
    seen = read(OWNER)[world.ids.peak]
    assert (seen["reaction"], seen["rank"]) == ("sad", -1)


def test_feedback_on_a_fact_that_is_now_withheld_is_not_shown(world):
    put(world.ids.walk, reaction="love")
    protect(world.conn, "Juniper Example")
    assert world.ids.walk not in read(WRITER) and world.ids.walk not in read(READER)
    assert read(OWNER)[world.ids.walk]["reaction"] == "love"


def test_the_node_orders_nothing_by_them(world):
    before = [f["id"] for f in dispatch(WRITER, {}, "pack_facts")["payload"]["facts"]]
    put(world.ids.walk, reaction="love", rank=3)                     # not the newest fact
    put(world.ids.peak, reaction="down", rank=-3)
    assert [f["id"] for f in dispatch(WRITER, {}, "pack_facts")["payload"]["facts"]] == before


def test_the_read_looks_feedback_up_past_one_chunk(world, monkeypatch):
    monkeypatch.setattr(fact_feedback, "_CHUNK", 1)
    put(world.ids.peak, reaction="love")
    put(world.ids.walk, rank=2)
    facts = read(WRITER)
    assert facts[world.ids.peak]["reaction"] == "love" and facts[world.ids.walk]["rank"] == 2


def test_a_row_the_door_could_not_have_written_reads_as_nothing(world):
    """A table from elsewhere (no CHECK): a value outside the vocabulary never reaches an app."""
    world.conn.execute("CREATE TABLE fact_feedback (fact_id TEXT PRIMARY KEY, reaction TEXT, rank INTEGER, "
                       "updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)")
    world.conn.execute("INSERT INTO fact_feedback VALUES (?, 'a sentence someone typed', 40, 'then', 'other')",
                       (world.ids.peak,))
    world.conn.commit()
    seen = read(WRITER)[world.ids.peak]
    assert (seen["reaction"], seen["rank"]) == (None, None)


# --- the door split on the real socket ---------------------------------------------------------------------------------
UNATTESTED = uds.SocketPeer(pid=4242, executable="/usr/bin/curl")


@pytest.fixture
def real_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "1")
    monkeypatch.setenv("TOPOS_UDS_PATH", str(tmp_path / "engine.sock"))
    for name in ("TOPOS_OWNER_DOOR_TEAM_IDS", "TOPOS_UDS_TEAM_IDS", "TOPOS_DEV_PROXY_ALLOWED"):
        monkeypatch.delenv(name, raising=False)


def on_socket(principal, payload, msg_type="fact_feedback"):
    with uds.as_socket_peer(UNATTESTED):
        return dispatch(principal, payload, msg_type)


def test_the_type_is_a_dangerous_change_not_a_read():
    assert "fact_feedback" in hub.DANGEROUS_TYPES
    assert "fact_feedback" not in hub.owner_door_open_types()
    assert hub.OWNER_DOOR_SPLIT_EXEMPT == frozenset()                # the grant is a leg, not an exemption


def test_an_app_with_the_grant_writes_on_the_real_socket(world, real_socket):
    out = on_socket(WRITER, {"fact_id": world.ids.peak, "reaction": "love"})
    assert out["status"] == "ok" and stored(world.conn, world.ids.peak) == ("love", None, "atlas")


def test_the_owners_own_unattested_process_is_still_asked_for_a_confirmation(world, real_socket):
    out = on_socket(OWNER, {"fact_id": world.ids.peak, "reaction": "love"})
    assert out["code"] == 403 and out["error"] == "owner_confirmation_required"
    assert not table_exists(world.conn)


def test_a_reads_only_app_on_the_real_socket_hears_its_own_refusal(world, real_socket):
    assert on_socket(READER, {"fact_id": world.ids.peak, "reaction": "love"}) == NOT_ON_ITS_LIST


@pytest.mark.parametrize("msg_type", ["put_fact_verdict", "create_routine", "signal_owner_override_object",
                                      "delete_database_rows", "truth_seed_fact", "run_pack_backfill"])
def test_the_feedback_grant_opens_no_other_change(world, real_socket, msg_type):
    assert on_socket(WRITER, {}, msg_type) == NOT_ON_ITS_LIST


def test_the_grant_leg_reads_the_grant_and_the_type_not_the_frame():
    assert hub._app_granted_write(WRITER, "fact_feedback") is True
    for principal in (READER, CODING_AGENT, OWNER, None,
                      Principal(cls=THIRD_PARTY, channel="local_http", client_id="agent-atlas",
                                app_grant=WRITER.app_grant)):
        assert hub._app_granted_write(principal, "fact_feedback") is False
    for msg_type in ("put_fact_verdict", "create_routine", "pack_facts", ""):
        assert hub._app_granted_write(WRITER, msg_type) is False


# --- through the HTTP door an app actually calls ------------------------------------------------------------------------
@pytest.fixture
def door():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from topos.api import agent_dispatch
    from topos.auth import resolve_request_principal

    app = FastAPI()
    app.include_router(agent_dispatch.router)

    def post(principal, payload, msg_type="fact_feedback"):
        app.dependency_overrides[resolve_request_principal] = lambda: principal
        return TestClient(app).post("/api/local/agent/dispatch", json={"type": msg_type, "payload": payload})

    return post


def test_the_dispatch_door_answers_an_app_with_the_grant(world, door):
    r = door(WRITER, {"fact_id": world.ids.peak, "reaction": "like", "rank": -1})
    assert r.status_code == 200, r.text
    assert (r.json()["payload"]["reaction"], r.json()["payload"]["rank"]) == ("like", -1)
    seen = door(READER, {"fact_ids": [world.ids.peak]}, "pack_facts").json()["payload"]["facts"][0]
    assert (seen["reaction"], seen["rank"]) == ("like", -1)


def test_the_dispatch_door_refuses_a_reads_only_app_before_anything_is_dispatched(world, door):
    r = door(READER, {"fact_id": world.ids.peak, "reaction": "like"})
    assert r.status_code == 403 and r.json() == {"error": "owner_agent_not_allowed"}
    assert not table_exists(world.conn)


def test_the_dispatch_door_gives_one_status_for_every_fact_out_of_reach(world, door):
    protect(world.conn, "Juniper Example")
    answers = [door(WRITER, {"fact_id": fact, "rank": 1})
               for fact in (world.ids.outside, world.ids.walk, uuid.uuid4().hex)]
    assert {r.status_code for r in answers} == {404}
    assert {(r.json()["error"], r.json()["code"], r.json()["status"]) for r in answers} == {
        ("fact_not_available", 404, "error")}
    assert all(set(r.json()) == {"id", "status", "code", "error"} for r in answers)   # the id is the request's own


def test_the_dispatch_door_answers_a_bad_value_400(world, door):
    r = door(WRITER, {"fact_id": world.ids.peak, "rank": 5})
    assert r.status_code == 400 and r.json()["error"] == "invalid_fact_feedback_request"


# --- the grant ----------------------------------------------------------------------------------------------------------
def test_feedback_words_make_a_grant_separate_from_reads():
    grant = parse_app_grant(["facts:moments.joy", "facts:life.events", "messages"], ["facts:moments.joy"])
    assert grant == AppGrant(packs=("moments.joy", "life.events"), scopes=("messages:read",), feedback=("moments.joy",))
    assert grant.words() == ["facts:moments.joy", "facts:life.events", "messages"]
    assert grant.feedback_words() == ["facts:moments.joy"]
    assert "moments.joy" in grant.plain_feedback() and "life.events" not in grant.plain_feedback()


def test_reads_alone_never_give_feedback():
    grant = parse_app_grant(["facts:moments.joy", "messages", "journals"])
    assert grant.feedback == () and grant.plain_feedback() == "" and "fact_feedback" not in grant.message_types()
    assert mcp_clients.NO_GRANT.feedback == ()


@pytest.mark.parametrize("feedback", [["facts:life.events"], ["moments.joy"], ["messages"], ["facts:"], [""],
                                      ["facts:moments.joy", "journals"]])
def test_a_feedback_word_it_does_not_take_or_a_pack_it_does_not_read_is_refused(feedback):
    with pytest.raises(ValueError):
        parse_app_grant(["facts:moments.joy", "messages"], feedback)


def test_the_feedback_grant_is_stored_and_comes_back_on_the_label(node):
    grant = AppGrant(packs=("moments.joy", "life.events"), feedback=("moments.joy",))
    label = mcp_clients.mint_agent_label(node.conn, client="atlas", grant=grant)["label"]
    assert mcp_clients.verify_agent_label(node.conn, label)["app_grant"] == grant
    row = mcp_clients.list_agents(node.conn)[0]
    assert row["reads"] == ["facts:moments.joy", "facts:life.events"] and row["feedback"] == ["facts:moments.joy"]


def test_a_new_grant_without_feedback_takes_it_away(node):
    mcp_clients.mint_agent_label(node.conn, client="atlas",
                                 grant=AppGrant(packs=("moments.joy",), feedback=("moments.joy",)))
    label = mcp_clients.mint_agent_label(node.conn, client="atlas", grant=AppGrant(packs=("moments.joy",)))["label"]
    assert mcp_clients.verify_agent_label(node.conn, label)["app_grant"] == AppGrant(packs=("moments.joy",))


def test_a_grant_stored_before_feedback_existed_has_none(node):
    label = mcp_clients.mint_agent_label(node.conn, client="atlas", grant=AppGrant(packs=("moments.joy",)))["label"]
    node.conn.execute(f"UPDATE {mcp_clients.APP_GRANTS_TABLE} SET reads_json=?",
                      (json.dumps({"packs": ["moments.joy"], "scopes": []}),))
    node.conn.commit()
    assert mcp_clients.verify_agent_label(node.conn, label)["app_grant"] == AppGrant(packs=("moments.joy",))


def test_a_stored_feedback_pack_the_app_does_not_read_is_dropped(node):
    label = mcp_clients.mint_agent_label(node.conn, client="atlas", grant=AppGrant(packs=("moments.joy",)))["label"]
    node.conn.execute(f"UPDATE {mcp_clients.APP_GRANTS_TABLE} SET reads_json=?",
                      (json.dumps({"packs": ["moments.joy"], "scopes": [],
                                   "feedback": ["life.events", "moments.joy"]}),))
    node.conn.commit()
    assert mcp_clients.verify_agent_label(node.conn, label)["app_grant"].feedback == ("moments.joy",)


def test_a_coding_tool_takes_no_feedback_grant(node):
    with pytest.raises(ValueError):
        mcp_clients.mint_agent_label(node.conn, client="codex",
                                     grant=AppGrant(packs=("moments.joy",), feedback=("moments.joy",)))


# --- the command --------------------------------------------------------------------------------------------------------
@pytest.fixture
def cli(node, tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    monkeypatch.setattr(agents_cmd, "_database_path", lambda: Path(node.path))
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: True)
    monkeypatch.setattr(agents_cmd.mcp_server, "label_path", lambda client, agents_dir=None: agents / f"{client}.token")
    return SimpleNamespace(run=lambda *args, answer="y\n": CliRunner().invoke(agents_group, list(args), input=answer),
                           agents=agents, node=node)


def grant_of(cli, client="atlas"):
    label = (cli.agents / f"{client}.token").read_text().strip()
    return mcp_clients.verify_agent_label(cli.node.conn, label)["app_grant"]


def test_the_owner_approves_reads_and_feedback_in_one_question(cli):
    result = cli.run("add", "atlas", "--reads", "facts:moments.joy,facts:life.events", "--feedback", "facts:moments.joy")
    assert result.exit_code == 0, result.output
    assert "it can read: facts from moments.joy, life.events" in result.output
    assert "It can also record: your reaction and rank on facts from moments.joy." in result.output
    assert "It cannot change a fact." in result.output
    assert "so is each reaction or rank it records" in result.output
    assert grant_of(cli) == AppGrant(packs=("moments.joy", "life.events"), feedback=("moments.joy",))


def test_without_the_option_the_question_says_nothing_of_recording_and_no_feedback_is_given(cli):
    result = cli.run("add", "atlas", "--reads", "facts:moments.joy")
    assert result.exit_code == 0, result.output
    assert "record:" not in result.output and "Every read is recorded under its name." in result.output
    assert grant_of(cli).feedback == ()


def test_saying_no_gives_nothing(cli):
    result = cli.run("add", "atlas", "--reads", "facts:moments.joy", "--feedback", "facts:moments.joy", answer="n\n")
    assert result.exit_code == 2 and mcp_clients.list_agents(cli.node.conn) == []


@pytest.mark.parametrize("feedback,word", [("facts:life.events", "life.events"), ("messages", "messages"),
                                           ("moments.joy", "moments.joy")])
def test_feedback_it_cant_grant_is_refused_before_the_question(cli, feedback, word):
    result = cli.run("add", "atlas", "--reads", "facts:moments.joy", "--feedback", feedback)
    assert result.exit_code == 2 and word in result.output and "Connect atlas" not in result.output
    assert mcp_clients.list_agents(cli.node.conn) == []


def test_feedback_without_reads_is_refused(cli):
    result = cli.run("add", "atlas", "--feedback", "facts:moments.joy")
    assert result.exit_code == 2 and "--reads" in result.output


def test_a_coding_tool_takes_no_feedback_option(cli):
    result = cli.run("add", "codex", "--feedback", "facts:moments.joy")
    assert result.exit_code == 2 and "coding tool" in result.output


def test_enrolling_again_without_the_option_takes_feedback_away(cli):
    assert cli.run("add", "atlas", "--reads", "facts:moments.joy", "--feedback", "facts:moments.joy").exit_code == 0
    assert grant_of(cli).feedback == ("moments.joy",)
    assert cli.run("add", "atlas", "--reads", "facts:moments.joy").exit_code == 0
    assert grant_of(cli).feedback == ()


def test_the_list_shows_what_an_app_may_record(cli):
    cli.run("add", "atlas", "--reads", "facts:moments.joy", "--feedback", "facts:moments.joy")
    cli.run("add", "reader", "--reads", "facts:moments.joy")
    rows = {row["client"]: row for row in json.loads(cli.run("list", "--json").output)}
    assert rows["atlas"]["feedback"] == ["facts:moments.joy"] and rows["atlas"]["reads"] == ["facts:moments.joy"]
    assert "feedback" not in rows["reader"]

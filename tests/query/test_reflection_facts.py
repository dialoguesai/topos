"""The owner-agent reflection read (GRANT_DECISIONS G2, DISCLOSURE_RULES DR-61): who reaches ``reflection_facts``, and
what an agent gets from it: the enabled reflection packs' active facts, special-class held back, Off-limits applied by
absence, people as display names, the owner's own words only.

Synthetic only: a migrated temp database with invented people, facts and rows; never the live node.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests.truth_lane_support import add_contact, add_journal, add_message, bind, build_node, connect
from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
from topos.principal import CP_RELAY, OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.query import reflection_facts as facts_read

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
MOMENTS = Principal(cls="owner_agent", channel="uds", client_id="agent-moments")
CODING_AGENT = Principal(cls="owner_agent", channel="uds", client_id="agent-claude-code")
OWNER = Principal(cls=OWNER_APP, channel="uds")
LEAKY = ("id:", "key:", "ent_", "+1555", "@example", "_entity_id", "j-", "m-")


@pytest.fixture
def node(tmp_path, tmp_path_factory, monkeypatch):
    path = build_node(tmp_path, tmp_path_factory.getbasetemp())
    bind(monkeypatch, path)
    conn = connect(path)
    seed_pack_registry(conn, bundled_pack_dir())
    yield SimpleNamespace(path=path, conn=conn)
    conn.close()


def enable(conn, *packs):
    for pack in packs:
        conn.execute("UPDATE pack_registry SET enabled=1 WHERE pack_id=?", (pack,))
    conn.commit()


def day(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ref(table, record_id, days_ago, role="authored"):
    return [{"table": table, "record_id": record_id, "role": role,
             "date": (NOW - timedelta(days=days_ago)).date().isoformat()}]


def add_fact(conn, pack, predicate, value, *, at, refs, quote="", actor_role="authored", sensitivity="personal",
             subject="owner", object_entity_id=None, context_refs=None):
    """A fact as the derivation writer stores one."""
    payload = {"subject_entity_id": subject, "predicate": predicate, "object_value": value.get("description") or predicate,
               "object_entity_id": object_entity_id, "disclosure": "owner_only", "asserted_by": "owner",
               "actor_role": actor_role, "pack": pack, "altitude": "stated", "value_struct": value}
    if sensitivity is not None:
        payload["sensitivity"] = sensitivity
    if quote:
        payload["quote"] = quote
    if context_refs:
        payload["context_refs"] = context_refs
    oid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, confidence, "
        "source_refs_json, valid_from, created_at, updated_at, extractor_version, ontology_id, ontology_version, altitude) "
        "VALUES (?, 'profile', 'fact', ?, ?, 0.8, ?, ?, ?, ?, 'derivation:test', ?, '0.1.5', 'stated')",
        (oid, f"{predicate}:{oid[:8]}", json.dumps(payload), json.dumps(refs), at, at, at, pack))
    conn.commit()
    return oid


def add_entity(conn, entity_id, name, *, is_self=False):
    now = "2026-10-01T00:00:00Z"
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self, created_at, updated_at) "
        "VALUES (?, 'person', ?, ?, ?, ?, ?)", (entity_id, name, name.lower(), 1 if is_self else 0, now, now))
    conn.commit()


def dispatch(principal, payload=None, msg_type="reflection_facts"):
    """Through the dispatcher, so the owner-agent type lists are what is tested."""
    from topos.core.handlers import handle_control_plane_request

    return asyncio.run(handle_control_plane_request({"id": "r1", "type": msg_type, "payload": payload or {}},
                                                    principal=principal))


def as_handler(principal, payload):
    """The handler alone, under a principal (what the class gate inside it decides)."""
    from topos.core.handlers import HANDLERS

    token = set_principal(principal)
    try:
        return asyncio.run(HANDLERS["reflection_facts"]({"id": "r1", "type": "reflection_facts", "payload": payload}))
    finally:
        reset_principal(token)


class Untouchable(dict):
    def __getattribute__(self, name):
        if name in ("__class__",):
            return object.__getattribute__(self, name)
        raise AssertionError(f"payload read: {name}")


@pytest.fixture
def world(node):
    """An invented owner: two moments packs and the commitments pack on, one of each kind of person reference."""
    conn = node.conn
    enable(conn, "moments.joy", "moments.tension", "obligations.commitments", "narrative.self")
    add_entity(conn, "ent_0a1b2c3d4e5f6071", "Wren Example")
    add_entity(conn, "ent_ffff0000aaaa1111", "The Owner", is_self=True)
    add_contact(conn, "+15550000002", "Sol Example", contact_id="c2")
    add_journal(conn, "j-dinner", "Dinner with Wren turned into four hours of talking. Left feeling at home.", at=day(13))
    add_journal(conn, "j-promise", "Told Sol I'd send the lake photos this week.", at=day(22))
    add_journal(conn, "j-fight", "Argued about the schedule again.", at=day(5))
    add_message(conn, "m-thanks", "you looked so happy on sunday!", at=day(12), sender_id="+15550000002")
    conn.commit()
    ids = SimpleNamespace()
    ids.peak = add_fact(conn, "moments.joy", "moment.peak", {
        "kind": "connected", "description": "dinner that turned into four hours of talking",
        "with": ["id:ent_0a1b2c3d4e5f6071", "key:+15550000002", "key:imessage:+15550000003", "me"],
        "with_entity_ids": ["ent_0a1b2c3d4e5f6071"], "where": "the corner place"},
        at=day(13), refs=ref("journal_entries", "j-dinner", 13), quote="Left feeling at home.")
    ids.thanks = add_fact(conn, "moments.joy", "moment.gratitude", {
        "person": "key:+15550000002", "what": "said I looked happy", "acknowledged": False},
        at=day(12), refs=ref("conversation_messages", "m-thanks", 12, role="other"), quote="you looked so happy on sunday!",
        actor_role="other")
    ids.promise = add_fact(conn, "obligations.commitments", "commit.made", {
        "counterparty": "ent_0a1b2c3d4e5f6071", "counterparty_entity_id": "ent_0a1b2c3d4e5f6071",
        "direction": "owed_by_owner", "description": "send the lake photos, call +1 (555) 010-2030", "status": "open",
        "due": "2026-10-10"}, at=day(22), refs=ref("journal_entries", "j-promise", 22))
    ids.tension = add_fact(conn, "moments.tension", "moment.tension", {
        "kind": "recurring_frustration", "with": [], "description": "argued about the schedule again", "status": "open"},
        at=day(5), refs=ref("journal_entries", "j-fight", 5))
    ids.special = add_fact(conn, "narrative.self", "self.attribution_pattern", {
        "domain": "setback", "attributes_to": "own_ability"}, at=day(4), refs=ref("journal_entries", "j-fight", 4),
        sensitivity="special")
    ids.stamp_says_personal = add_fact(conn, "narrative.self", "self.attribution_pattern", {
        "domain": "success", "attributes_to": "own_effort"}, at=day(3), refs=ref("journal_entries", "j-fight", 3),
        sensitivity="personal")
    ids.retired_predicate = add_fact(conn, "moments.joy", "moment.intensity", {"level": "high"}, at=day(2),
                                     refs=ref("journal_entries", "j-dinner", 2))
    return SimpleNamespace(conn=conn, ids=ids)


def by_id(result):
    return {f["id"]: f for f in result["facts"]}


# --- who reaches the read ---------------------------------------------------------------------------------------------
def test_moments_reads_and_a_coding_agent_is_refused(world):
    out = dispatch(MOMENTS)
    assert out["status"] == "ok" and out["payload"]["version"] == "reflection_facts/v1"
    assert dispatch(CODING_AGENT) == {"id": "r1", "status": "error", "code": 403, "error": "owner_agent_not_allowed"}


@pytest.mark.parametrize("msg_type", ["query", "aggregate", "get_sources", "reflection_invitations",
                                      "reflection_conversation", "reflection_label", "signal_list_facts",
                                      "get_messages", "verify_claim"])
def test_moments_reaches_nothing_else(world, msg_type):
    assert dispatch(MOMENTS, {}, msg_type) == {"id": "r1", "status": "error", "code": 403,
                                                "error": "owner_agent_not_allowed"}


def test_the_coding_agents_list_is_unchanged():
    import topos.core.handlers as hub
    from topos.mcp_clients import APP_AGENT_TYPES

    assert hub.owner_agent_allowed_types(CODING_AGENT) == frozenset({"query", "aggregate", "get_sources"})
    assert hub.owner_agent_allowed_types(MOMENTS) == frozenset({"reflection_facts"}) == APP_AGENT_TYPES["moments"]
    # an unlisted agent slug is a coding agent's list, never an app's
    assert hub.owner_agent_allowed_types(Principal(cls="owner_agent", channel="uds", client_id="agent-other")) == \
        hub.OWNER_AGENT_ALLOWED_TYPES
    # the row id decides, never a bare client name or another class
    assert hub.owner_agent_allowed_types(Principal(cls="owner_agent", channel="uds", client_id="moments")) == \
        hub.OWNER_AGENT_ALLOWED_TYPES


@pytest.mark.parametrize("principal", [
    Principal(CP_RELAY, "cp_relay"), Principal(THIRD_PARTY, "local_http", client_id="moments"),
    Principal(THIRD_PARTY, "cp_relay"), Principal("owner_automation", "cp_relay"),
    Principal("owner_agent", "local_http", client_id="agent-moments"), None,
])
def test_every_other_class_is_refused_before_the_payload_is_read(principal):
    assert as_handler(principal, Untouchable()) == {"id": "r1", "status": "error", "code": 403,
                                                    "error": "owner_mode_required"}


def test_on_the_socket_it_is_an_open_read():
    import topos.core.handlers as hub

    assert "reflection_facts" in hub.owner_door_open_types()
    assert "reflection_facts" not in hub.DANGEROUS_TYPES


def test_an_app_reaches_only_the_dispatch_route_on_the_socket():
    from topos.uds import owner_agent_route_allowed

    def allowed(principal, method, path):
        return owner_agent_route_allowed(principal, {"method": method, "path": path})

    assert allowed(MOMENTS, "POST", "/api/local/agent/dispatch")
    for method, path in (("GET", "/sources"), ("GET", "/v1/shell/status"), ("GET", "/api/local/owner/scope"),
                         ("GET", "/api/local/owner/sources/claude_code_sessions")):
        assert not allowed(MOMENTS, method, path), path
        assert allowed(CODING_AGENT, method, path), path


def test_the_protocol_snapshot_names_the_read():
    from pathlib import Path

    snapshot = json.loads((Path(__file__).resolve().parents[2] / "topos/protocol/handled_message_types.json").read_text())
    assert "reflection_facts" in snapshot["handled_message_types"]
    assert "reflection_facts" not in snapshot["owner_only_message_types"]


def test_an_agent_read_is_recorded_under_its_name(world):
    assert dispatch(MOMENTS)["status"] == "ok"
    rows = world.conn.execute("SELECT tool_name, verified_cls, verified_client_id FROM mcp_request_log "
                              "WHERE tool_name='reflection_facts'").fetchall()
    assert rows == [("reflection_facts", "owner_agent", "agent-moments")]


# --- what an agent gets -------------------------------------------------------------------------------------------------
def test_packs_off_means_nothing(node):
    node.conn.execute("UPDATE pack_registry SET enabled=0")      # a fresh node has relationships.social on (Wave A)
    node.conn.commit()
    out = dispatch(MOMENTS)["payload"]
    assert out["facts"] == [] and out["packs_enabled"] == [] and out["truncated"] is False
    assert out["packs_missing"] == list(facts_read.REFLECTION_PACKS)


def test_people_are_display_names_and_nothing_id_shaped_leaves(world):
    out = dispatch(MOMENTS, {"include_excerpts": True})["payload"]
    found = by_id(out)
    peak = found[world.ids.peak]
    assert peak["value"]["with"] == ["Wren Example", "Sol Example", "you"]   # the unnamed key is left out
    assert peak["people"] == ["Wren Example", "Sol Example"]
    assert "with_entity_ids" not in peak["value"] and peak["value"]["where"] == "the corner place"
    promise = found[world.ids.promise]
    assert promise["value"]["counterparty"] == "Wren Example" and "counterparty_entity_id" not in promise["value"]
    assert "555" not in promise["value"]["description"] and "someone" in promise["value"]["description"]
    assert promise["value"]["due"] == "2026-10-10"
    text = json.dumps(out)
    assert not any(s in text for s in LEAKY), [s for s in LEAKY if s in text]


def test_the_owners_words_only(world):
    out = by_id(dispatch(MOMENTS, {"include_excerpts": True})["payload"])
    peak, thanks = out[world.ids.peak], out[world.ids.thanks]
    assert peak["quote"] == "Left feeling at home." and peak["authored"] is True
    assert peak["sources"] == [{"source": "journal", "date": day(13)[:10], "authored": True,
                                "excerpt": "Dinner with Wren turned into four hours of talking. Left feeling at home."}]
    # someone else's message: no quote, no excerpt, its source and date only
    assert thanks["quote"] == "" and thanks["authored"] is False
    assert thanks["sources"][0]["authored"] is False and thanks["sources"][0]["excerpt"] == ""
    assert "happy on sunday" not in json.dumps(out)
    # excerpts only when asked
    plain = by_id(dispatch(MOMENTS)["payload"])[world.ids.peak]
    assert plain["sources"][0]["excerpt"] == "" and plain["quote"] == "Left feeling at home."


def test_special_class_is_held_back_from_an_agent_and_not_from_the_owner(world):
    agent = by_id(dispatch(MOMENTS)["payload"])
    owner = by_id(dispatch(OWNER)["payload"])
    for held in (world.ids.special, world.ids.stamp_says_personal, world.ids.retired_predicate):
        assert held not in agent and held in owner
    assert {world.ids.peak, world.ids.thanks, world.ids.promise, world.ids.tension} <= set(agent)


def test_the_request_narrows_and_pages(world):
    only = dispatch(MOMENTS, {"packs": ["moments.tension"]})["payload"]
    assert [f["id"] for f in only["facts"]] == [world.ids.tension]
    picked = dispatch(MOMENTS, {"fact_ids": [world.ids.peak, "nope"]})["payload"]
    assert [f["id"] for f in picked["facts"]] == [world.ids.peak]
    recent = dispatch(MOMENTS, {"since": day(6)[:10]})["payload"]
    assert [f["id"] for f in recent["facts"]] == [world.ids.tension]
    first = dispatch(MOMENTS, {"limit": 1})["payload"]
    assert len(first["facts"]) == 1 and first["truncated"] is True and first["facts"][0]["id"] == world.ids.tension
    assert set(first) == {"version", "packs_enabled", "packs_missing", "facts", "truncated", "as_of"}


@pytest.mark.parametrize("payload", [
    "not a dict", {"weeks": 2}, {"packs": "moments.joy"}, {"packs": []}, {"packs": ["health.mental"]},
    {"fact_ids": "x"}, {"fact_ids": [1]}, {"fact_ids": ["x" * 65]}, {"fact_ids": [f"f{i}" for i in range(101)]},
    {"since": "2026-13-01"}, {"since": "yesterday"}, {"since": 20261001},
    {"limit": 0}, {"limit": 1001}, {"limit": "5"}, {"limit": True}, {"include_excerpts": "yes"},
])
def test_a_bad_request_gets_a_fixed_code(world, payload):
    out = as_handler(MOMENTS, payload)
    assert out == {"id": "r1", "status": "error", "error": "invalid_reflection_facts_request"}


# --- Off-limits, applied by absence -----------------------------------------------------------------------------------
def protect(conn, entity_ref, *, complete=True):
    from topos.features.lifecycle.blackhole import BlackholeStore

    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref=entity_ref)
    if complete:
        store.mark_rebuild_complete(entity_ref)
    conn.commit()


def test_a_protected_person_takes_their_facts_out_of_an_agents_read(world):
    protect(world.conn, "ent_0a1b2c3d4e5f6071")
    agent = by_id(dispatch(MOMENTS)["payload"])
    assert world.ids.peak not in agent and world.ids.promise not in agent      # bound by id, in a list and alone
    assert {world.ids.thanks, world.ids.tension} <= set(agent)                  # the rest stays reachable
    owner = by_id(dispatch(OWNER)["payload"])
    assert {world.ids.peak, world.ids.promise} <= set(owner)                   # the owner's app sees everything


def test_a_protected_name_takes_out_the_facts_that_show_it(world):
    protect(world.conn, "Sol Example")
    agent = by_id(dispatch(MOMENTS)["payload"])
    assert world.ids.thanks not in agent and world.ids.peak not in agent   # both resolve a key to the protected name
    assert {world.ids.promise, world.ids.tension} <= set(agent)


def test_a_source_record_that_names_a_protected_person_takes_the_fact_out(world):
    conn = world.conn
    add_journal(conn, "j-walk", "Long walk with Juniper Example by the river.", at=day(8))
    conn.commit()
    walk = add_fact(conn, "moments.joy", "moment.peak", {"kind": "small_pleasure", "description": "a long walk by the river",
                                                          "with": []}, at=day(8), refs=ref("journal_entries", "j-walk", 8))
    assert walk in by_id(dispatch(MOMENTS)["payload"])
    protect(conn, "Juniper Example")
    assert walk not in by_id(dispatch(MOMENTS)["payload"])
    assert walk in by_id(dispatch(OWNER)["payload"])


def test_a_protected_record_takes_out_only_the_facts_resting_on_it(world):
    from topos.features.lifecycle.record_protection import RecordProtectionStore

    RecordProtectionStore(world.conn).protect(canonical_table="journal_entries", record_id="j-fight")
    world.conn.commit()
    agent = by_id(dispatch(MOMENTS)["payload"])
    assert world.ids.tension not in agent
    assert {world.ids.peak, world.ids.thanks, world.ids.promise} <= set(agent)   # not a blanket: the rest is reachable


def test_while_an_off_limits_rebuild_is_pending_an_agent_gets_nothing(world):
    protect(world.conn, "ent_0a1b2c3d4e5f6071", complete=False)
    out = dispatch(MOMENTS)["payload"]
    assert out["facts"] == [] and out["truncated"] is False
    assert set(out) == {"version", "packs_enabled", "packs_missing", "facts", "truncated", "as_of"}
    assert len(dispatch(OWNER)["payload"]["facts"]) == 7


def test_a_fact_with_no_source_is_left_out_once_anything_is_off_limits(world):
    bare = add_fact(world.conn, "moments.tension", "moment.tension", {"kind": "disappointment", "with": [],
                                                                      "description": "a plan fell through", "status": "open"},
                    at=day(1), refs=[])
    assert bare in by_id(dispatch(MOMENTS)["payload"])     # nothing protected: nothing to judge it against
    protect(world.conn, "Somebody Unrelated")
    assert bare not in by_id(dispatch(MOMENTS)["payload"])
    assert bare in by_id(dispatch(OWNER)["payload"])


@pytest.fixture
def read_with_turns(world):
    """DR-62: a joy fact on the owner's DM message, read with the other person's turn before it."""
    conn = world.conn
    add_message(conn, "m-news", "Juno here: I got the Lisbon offer, starting in March", at=day(3),
                conversation_id="dm-juno", sender_id="+15550000009")
    add_message(conn, "m-proud", "So proud of you, this made my whole week", at=day(3), conversation_id="dm-juno",
                from_self=True)
    conn.commit()
    world.ids.read_with = add_fact(
        conn, "moments.joy", "moment.peak", {"kind": "accomplished", "description": "a friend's new job abroad"},
        at=day(3), refs=ref("conversation_messages", "m-proud", 3), quote="this made my whole week",
        context_refs=[{"table": "conversation_messages", "record_id": "m-news"}])
    return world


def test_the_turns_a_moment_was_read_with_are_never_a_source_or_an_excerpt(read_with_turns):
    for principal in (MOMENTS, OWNER):
        out = dispatch(principal, {"include_excerpts": True})["payload"]
        fact = by_id(out)[read_with_turns.ids.read_with]
        assert fact["sources"] == [{"source": "imessage", "date": day(3)[:10], "authored": True,
                                    "excerpt": "So proud of you, this made my whole week"}]
        text = json.dumps(out)
        assert "Lisbon" not in text and "m-news" not in text and "context_refs" not in text


@pytest.mark.parametrize("withhold", ["protected record", "off-limits name", "nsfw", "deleted"])
def test_a_turn_it_was_read_with_protects_it_from_an_agent_and_not_from_the_owner(read_with_turns, withhold):
    from topos.features.lifecycle.record_protection import RecordProtectionStore

    conn, fact = read_with_turns.conn, read_with_turns.ids.read_with
    assert fact in by_id(dispatch(MOMENTS)["payload"])
    if withhold == "protected record":
        RecordProtectionStore(conn).protect(canonical_table="conversation_messages", record_id="m-news")
    elif withhold == "off-limits name":
        protect(conn, "Juno Example")
        conn.execute("UPDATE conversation_messages SET content='Juno Example here: big news' WHERE message_id='m-news'")
    elif withhold == "nsfw":
        conn.execute("UPDATE conversation_messages SET content_nsfw=1 WHERE message_id='m-news'")
    else:
        conn.execute("DELETE FROM conversation_messages WHERE message_id='m-news'")
    conn.commit()
    agent = by_id(dispatch(MOMENTS)["payload"])
    assert fact not in agent and world_rest(read_with_turns) <= set(agent)
    assert fact in by_id(dispatch(OWNER)["payload"])


def world_rest(world):
    return {world.ids.peak, world.ids.promise, world.ids.tension}


def test_what_was_left_out_is_never_counted(world):
    before = dispatch(MOMENTS)["payload"]
    protect(world.conn, "ent_0a1b2c3d4e5f6071")
    after = dispatch(MOMENTS)["payload"]
    assert set(before) == set(after)
    assert len(after["facts"]) == len(before["facts"]) - 2 and after["truncated"] is False


# --- enrolment ----------------------------------------------------------------------------------------------------------
def test_moments_enrols_with_the_agents_command_and_is_not_an_mcp_client():
    from topos import mcp_server
    from topos.mcp_clients import APP_AGENT_TYPES

    assert set(mcp_server.APP_NAMES) == set(APP_AGENT_TYPES)
    assert mcp_server.validate_agent("Moments") == "moments" and mcp_server.validate_agent("codex") == "codex"
    with pytest.raises(ValueError):
        mcp_server.validate_client("moments")
    with pytest.raises(ValueError):
        mcp_server.validate_agent("not-an-app")


@pytest.mark.parametrize("flag", ["--approved", "--open-terminal"])
def test_an_app_is_enrolled_on_a_terminal_only(flag):
    from click.testing import CliRunner

    from topos.cli.agents_cmd import agents_group

    result = CliRunner().invoke(agents_group, ["add", "moments", flag])
    assert result.exit_code == 2
    assert "topos-node agents add moments" in result.output


def test_the_consent_question_says_an_app_reads_only_its_own_list():
    from topos.cli.agents_cmd import _consent_question

    assert "reads only what Topos lets Moments read" in _consent_question("moments", "Moments")
    assert "will read what you allow" in _consent_question("codex", "Codex")

"""The open owner grant (DISCLOSURE_RULES DR-66): any app enrols by name, what it may read is stored on the enrolment and
shown on the prompt, and the turns around a search match are one read away (``message_context``).

Synthetic only: a migrated temp database with invented people and messages; never the live node.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import topos
from tests.truth_lane_support import bind, build_node, connect
from topos import mcp_clients, mcp_server
from topos.cli import agents_cmd
from topos.cli.agents_cmd import agents_group
from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
from topos.mcp_clients import AppGrant, app_agent_of, parse_app_reads
from topos.principal import CP_RELAY, OWNER_APP, THIRD_PARTY, Principal

APP = Principal(cls="owner_agent", channel="uds", client_id="agent-reader", app_grant=AppGrant(scopes=("messages:read",)))
JOURNALS_ONLY = Principal(cls="owner_agent", channel="uds", client_id="agent-reader", app_grant=AppGrant(scopes=("health:read",)))
CODING_AGENT = Principal(cls="owner_agent", channel="uds", client_id="agent-claude-code")
OWNER = Principal(cls=OWNER_APP, channel="uds")
CONVERSATION = "conv-1"


@pytest.fixture
def node(tmp_path, tmp_path_factory, monkeypatch):
    path = build_node(tmp_path, tmp_path_factory.getbasetemp())
    bind(monkeypatch, path)
    conn = connect(path)
    yield SimpleNamespace(path=path, conn=conn)
    conn.close()


def add(conn, message_id, at, content="words", *, conversation=CONVERSATION, sender="+15550100", from_self=False):
    conn.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_id, content, event_at, source_id, "
        "is_from_self) VALUES (?, ?, 'ds', ?, ?, ?, 'imessage', ?)",
        (message_id, conversation, "self" if from_self else sender, content, at, 1 if from_self else 0))
    conn.commit()


def dispatch(principal, payload, msg_type="message_context"):
    from topos.core.handlers import handle_control_plane_request

    return asyncio.run(handle_control_plane_request({"id": "r1", "type": msg_type, "payload": payload}, principal=principal))


def ids(out):
    return [t["record_id"] for t in out["payload"]["turns"]]


@pytest.fixture
def thread(node):
    """Seven turns, one conversation, an hour apart, and one turn in another conversation between them."""
    for hour in range(1, 8):
        add(node.conn, f"m{hour}", f"2026-10-01T0{hour}:00:00+00:00", f"turn {hour}", from_self=hour % 2 == 0)
    add(node.conn, "other", "2026-10-01T04:30:00+00:00", "another thread", conversation="conv-2")
    return node


# --- the read ------------------------------------------------------------------------------------------------------------
def test_it_returns_the_turns_around_a_match_in_order(thread):
    out = dispatch(APP, {"record_id": "m4", "before": 2, "after": 2})
    assert out["status"] == "ok" and out["payload"]["version"] == "message_context/v1"
    assert ids(out) == ["m2", "m3", "m4", "m5", "m6"]
    assert [t["is_match"] for t in out["payload"]["turns"]] == [False, False, True, False, False]


def test_both_sides_come_marked_with_who_wrote_them(thread):
    turns = {t["record_id"]: t for t in dispatch(APP, {"record_id": "m4", "before": 1, "after": 1})["payload"]["turns"]}
    assert turns["m4"]["is_from_self"] is True and turns["m3"]["is_from_self"] is False
    assert turns["m3"]["sender_id"] == "+15550100" and turns["m4"]["sender_id"] == "self"
    assert turns["m3"]["content"] == "turn 3" and turns["m3"]["_table"] == "conversation_messages"


def test_it_defaults_to_five_each_side_and_stops_at_the_ends(node):
    for hour in range(10, 23):
        add(node.conn, f"m{hour}", f"2026-10-01T{hour}:00:00+00:00")
    assert ids(dispatch(APP, {"record_id": "m16"})) == [f"m{h}" for h in range(11, 22)]
    assert ids(dispatch(APP, {"record_id": "m10", "before": 3, "after": 1})) == ["m10", "m11"]


def test_there_is_no_time_limit_on_the_next_turn(node):
    add(node.conn, "asked", "2026-10-01T09:00:00+00:00")
    add(node.conn, "answered", "2026-10-09T09:00:00+00:00")
    assert ids(dispatch(APP, {"record_id": "asked"})) == ["asked", "answered"]


def test_mixed_time_formats_keep_their_real_order(node):
    """As stored text, the middle turn sorts after the last: an offset, a Z and fractional seconds are all stored."""
    add(node.conn, "first", "2026-10-01T10:00:00-05:00")   # 15:00 UTC
    add(node.conn, "second", "2026-10-01T16:00:00Z")
    add(node.conn, "third", "2026-10-01T17:00:00.123456+00:00")
    assert ids(dispatch(APP, {"record_id": "second"})) == ["first", "second", "third"]


def test_turns_with_one_timestamp_neither_repeat_nor_go_missing(node):
    for name in ("a", "b", "c", "d"):
        add(node.conn, name, "2026-10-01T10:00:00+00:00")
    assert ids(dispatch(APP, {"record_id": "b"})) == ["a", "b", "c", "d"]


def test_a_message_with_no_text_is_not_a_turn(node):
    add(node.conn, "m1", "2026-10-01T01:00:00+00:00", "said")
    add(node.conn, "m2", "2026-10-01T02:00:00+00:00", "")
    add(node.conn, "m3", "2026-10-01T03:00:00+00:00", "said too")
    assert ids(dispatch(APP, {"record_id": "m3"})) == ["m1", "m3"]


def test_an_unknown_id_answers_with_no_turns(thread):
    assert dispatch(APP, {"record_id": "nope"})["payload"]["turns"] == []


def test_a_node_with_no_messages_table_answers_with_no_turns():
    from topos.query.message_context import parse_request, read

    conn = sqlite3.connect(":memory:")
    assert read(conn, parse_request({"record_id": "m1"}), agent=False)["turns"] == []


@pytest.mark.parametrize("payload", [
    "not a dict", {}, {"record_id": ""}, {"record_id": 7}, {"record_id": "x" * 201}, {"record_id": "m1", "before": 21},
    {"record_id": "m1", "after": -1}, {"record_id": "m1", "before": True}, {"record_id": "m1", "before": "2"},
    {"record_id": "m1", "window": 2},
])
def test_a_bad_payload_gets_one_fixed_code(thread, payload):
    from topos.core.handlers import handle_control_plane_request

    out = asyncio.run(handle_control_plane_request({"id": "r1", "type": "message_context", "payload": payload}, principal=APP))
    assert out == {"id": "r1", "status": "error", "error": "invalid_message_context_request"}


def test_each_read_is_recorded_under_the_apps_name(thread):
    dispatch(APP, {"record_id": "m4"})
    rows = thread.conn.execute("SELECT tool_name, verified_cls, verified_client_id FROM mcp_request_log "
                               "WHERE tool_name='message_context'").fetchall()
    assert rows == [("message_context", "owner_agent", "agent-reader")]


# --- who reaches it ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("principal", [JOURNALS_ONLY, CODING_AGENT,
                                       Principal(cls="owner_agent", channel="uds", client_id="agent-nothing")])
def test_an_agent_without_messages_on_its_grant_is_refused(thread, principal):
    assert dispatch(principal, {"record_id": "m4"}) == {"id": "r1", "status": "error", "code": 403,
                                                        "error": "owner_agent_not_allowed"}


@pytest.mark.parametrize("principal", [
    Principal(CP_RELAY, "cp_relay"), Principal(THIRD_PARTY, "local_http", client_id="reader"),
    Principal("owner_agent", "local_http", client_id="agent-reader", app_grant=AppGrant(scopes=("messages:read",))),
])
def test_only_the_socket_reaches_it(thread, principal):
    from topos.core.handlers import HANDLERS
    from topos.principal import reset_principal, set_principal

    token = set_principal(principal)
    try:
        out = asyncio.run(HANDLERS["message_context"]({"id": "r1", "payload": {"record_id": "m4"}}))
    finally:
        reset_principal(token)
    assert out == {"id": "r1", "status": "error", "code": 403, "error": "owner_mode_required"}


def test_it_is_an_open_read_that_writes_nothing():
    import topos.core.handlers as hub

    assert "message_context" in hub.owner_door_open_types()
    assert "message_context" not in hub.DANGEROUS_TYPES


# --- Off-limits ------------------------------------------------------------------------------------------------------------
def protect(conn, entity_ref, *, complete=True):
    from topos.features.lifecycle.blackhole import BlackholeStore

    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref=entity_ref)
    if complete:
        store.mark_rebuild_complete(entity_ref)
    conn.commit()


def test_a_turn_naming_a_protected_person_is_left_out_and_the_window_still_fills(node):
    for hour, text in enumerate(("a", "b", "about Juniper Example", "match", "c"), start=1):
        add(node.conn, f"m{hour}", f"2026-10-01T0{hour}:00:00+00:00", text)
    protect(node.conn, "Juniper Example")
    assert ids(dispatch(APP, {"record_id": "m4", "before": 2, "after": 1})) == ["m1", "m2", "m4", "m5"]
    assert ids(dispatch(OWNER, {"record_id": "m4", "before": 2, "after": 1})) == ["m2", "m3", "m4", "m5"]


def test_a_protected_match_looks_like_an_unknown_id(node):
    add(node.conn, "m1", "2026-10-01T01:00:00+00:00")
    add(node.conn, "m2", "2026-10-01T02:00:00+00:00", "about Juniper Example")
    protect(node.conn, "Juniper Example")
    assert dispatch(APP, {"record_id": "m2"})["payload"] == dispatch(APP, {"record_id": "nope"})["payload"]


def test_a_protected_record_is_left_out(thread):
    from topos.features.lifecycle.record_protection import RecordProtectionStore

    RecordProtectionStore(thread.conn).protect(canonical_table="conversation_messages", record_id="m3")
    thread.conn.commit()
    assert "m3" not in ids(dispatch(APP, {"record_id": "m4", "before": 2, "after": 0}))


def test_while_an_off_limits_rebuild_is_pending_an_app_gets_nothing(thread):
    protect(thread.conn, "Juniper Example", complete=False)
    assert dispatch(APP, {"record_id": "m4"})["payload"]["turns"] == []
    assert ids(dispatch(OWNER, {"record_id": "m4", "before": 1, "after": 1})) == ["m3", "m4", "m5"]


# --- the grant -------------------------------------------------------------------------------------------------------------
def test_reads_words_make_a_grant():
    grant = parse_app_reads(["facts:life.events", "messages", "journals", "messages"])
    assert grant == AppGrant(packs=("life.events",), scopes=("messages:read", "health:read"))
    assert grant.words() == ["facts:life.events", "messages", "journals"]
    assert "life.events" in grant.plain() and "messages" in grant.plain() and "journals" in grant.plain()


@pytest.mark.parametrize("word", ["", "facts:", "places", "aggregate", "health:read", "messages:read"])
def test_a_word_the_grant_does_not_know_is_refused(word):
    with pytest.raises(ValueError):
        parse_app_reads([word])


def test_a_grant_is_stored_and_comes_back_on_the_label(node):
    grant = AppGrant(packs=("life.events",), scopes=("messages:read",))
    label = mcp_clients.mint_agent_label(node.conn, client="reader", grant=grant)["label"]
    row = mcp_clients.verify_agent_label(node.conn, label)
    assert row["client_id"] == "agent-reader" and row["app_grant"] == grant
    assert [r["reads"] for r in mcp_clients.list_agents(node.conn)] == [["facts:life.events", "messages"]]


def test_a_new_grant_replaces_the_old_one(node):
    mcp_clients.mint_agent_label(node.conn, client="reader", grant=AppGrant(scopes=("messages:read", "health:read")))
    label = mcp_clients.mint_agent_label(node.conn, client="reader", grant=AppGrant(scopes=("health:read",)))["label"]
    assert mcp_clients.verify_agent_label(node.conn, label)["app_grant"] == AppGrant(scopes=("health:read",))


def test_an_app_enrolled_before_grants_reads_nothing(node):
    label = mcp_clients.mint_agent_label(node.conn, client="older")["label"]
    assert mcp_clients.verify_agent_label(node.conn, label)["app_grant"] == mcp_clients.NO_GRANT


def test_a_coding_tool_takes_no_grant_and_its_row_is_unchanged(node):
    with pytest.raises(ValueError):
        mcp_clients.mint_agent_label(node.conn, client="codex", grant=AppGrant(scopes=("messages:read",)))
    label = mcp_clients.mint_agent_label(node.conn, client="codex")["label"]
    assert "app_grant" not in mcp_clients.verify_agent_label(node.conn, label)
    assert "reads" not in mcp_clients.list_agents(node.conn)[0]


def test_a_broken_grant_row_reads_nothing(node):
    label = mcp_clients.mint_agent_label(node.conn, client="reader", grant=AppGrant(scopes=("messages:read",)))["label"]
    node.conn.execute(f"UPDATE {mcp_clients.APP_GRANTS_TABLE} SET reads_json = 'not json'")
    node.conn.commit()
    assert mcp_clients.verify_agent_label(node.conn, label)["app_grant"] == mcp_clients.NO_GRANT


def test_the_label_mints_an_app_principal_only_on_the_socket_row():
    assert app_agent_of(APP) == "reader"
    assert app_agent_of(CODING_AGENT) is None
    assert app_agent_of(Principal(cls="third_party", channel="local_http", client_id="agent-reader")) is None


def test_the_coding_tools_are_the_same_list_in_both_modules():
    assert set(mcp_server.CLIENT_NAMES) == set(mcp_clients.AGENT_DISPLAY_NAMES) == set(mcp_server.DISPLAY_NAMES)


def test_an_app_can_be_any_slug_and_is_not_an_mcp_client():
    assert mcp_server.validate_agent("My App") == "my-app" and mcp_server.validate_agent("codex") == "codex"
    with pytest.raises(ValueError):
        mcp_server.validate_client("my-app")
    for bad in ("", "agent-my-app", "!!!"):
        with pytest.raises(ValueError):
            mcp_server.validate_agent(bad)


# --- the command ---------------------------------------------------------------------------------------------------------
@pytest.fixture
def cli(node, tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    monkeypatch.setattr(agents_cmd, "_database_path", lambda: Path(node.path))
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: True)
    monkeypatch.setattr(agents_cmd.mcp_server, "label_path", lambda client, agents_dir=None: agents / f"{client}.token")
    seed_pack_registry(node.conn, bundled_pack_dir())
    return SimpleNamespace(run=lambda *args, answer="y\n": CliRunner().invoke(agents_group, list(args), input=answer),
                           agents=agents, node=node)


def test_an_app_enrols_with_its_reads_on_the_prompt(cli):
    result = cli.run("add", "reader", "--reads", "facts:life.events,messages")
    assert result.exit_code == 0, result.output
    assert "facts from life.events" in result.output and "your messages" in result.output
    assert "Every read is recorded under its name" in result.output
    label = (cli.agents / "reader.token").read_text().strip()
    assert mcp_clients.verify_agent_label(cli.node.conn, label)["app_grant"] == \
        AppGrant(packs=("life.events",), scopes=("messages:read",))


def test_a_bundled_pack_can_be_granted_on_a_fresh_node_with_an_empty_registry(cli):
    cli.node.conn.execute("DELETE FROM pack_registry")
    cli.node.conn.commit()
    assert cli.run("add", "reader", "--reads", "facts:life.events").exit_code == 0


def test_saying_no_enrols_nothing(cli):
    assert cli.run("add", "reader", "--reads", "messages", answer="n\n").exit_code == 2
    assert mcp_clients.list_agents(cli.node.conn) == [] and not (cli.agents / "reader.token").exists()


def test_an_app_must_say_what_it_reads(cli):
    result = cli.run("add", "reader")
    assert result.exit_code == 2 and "--reads" in result.output


@pytest.mark.parametrize("reads,word", [("places", "places"), ("facts:not.a.pack", "not.a.pack")])
def test_a_read_or_pack_it_cant_grant_is_refused(cli, reads, word):
    result = cli.run("add", "reader", "--reads", reads)
    assert result.exit_code == 2 and word in result.output
    assert mcp_clients.list_agents(cli.node.conn) == []


def test_a_coding_tool_takes_no_reads(cli):
    result = cli.run("add", "codex", "--reads", "messages")
    assert result.exit_code == 2 and "--reads" in result.output


@pytest.mark.parametrize("flag", ["--approved", "--open-terminal"])
def test_an_app_is_enrolled_on_a_terminal_only(cli, flag):
    result = cli.run("add", "reader", "--reads", "messages", flag)
    assert result.exit_code == 2 and "topos-node agents add reader" in result.output


def test_an_agent_with_no_terminal_cant_enrol_an_app(cli, monkeypatch):
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: False)
    result = cli.run("add", "reader", "--reads", "messages")
    assert result.exit_code == 2 and "yes from you in a terminal" in result.output


def test_a_disconnected_app_reconnects_on_a_yes_and_its_old_label_is_dead(cli):
    assert cli.run("add", "reader", "--reads", "messages").exit_code == 0
    old = (cli.agents / "reader.token").read_text().strip()
    assert mcp_clients.revoke_agent(cli.node.conn, client="reader")
    assert mcp_clients.verify_agent_label(cli.node.conn, old) is None
    assert cli.run("add", "reader", "--reads", "journals").exit_code == 0
    new = (cli.agents / "reader.token").read_text().strip()
    assert mcp_clients.verify_agent_label(cli.node.conn, old) is None
    assert mcp_clients.verify_agent_label(cli.node.conn, new)["app_grant"] == AppGrant(scopes=("health:read",))


def test_the_list_shows_an_apps_reads(cli):
    cli.run("add", "reader", "--reads", "messages")
    assert '"reads": [\n      "messages"\n    ]' in cli.run("list", "--json").output


# --- no app is named in the node ---------------------------------------------------------------------------------------
def test_no_node_code_names_an_app():
    """The node has no special case for any app. The two packs keep their ids: facts on disk carry them."""
    named = re.compile(r"\bMoments\b|[\"'`]moments[\"'`]|agent-moments|reflection_(?:invitations|conversation|sweep)")
    root = Path(topos.__file__).parent
    hits = [str(path.relative_to(root)) for path in root.rglob("*.py") if named.search(path.read_text(encoding="utf-8"))]
    assert hits == []

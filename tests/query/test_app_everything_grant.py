"""The owner grant (DISCLOSURE_RULES DR-76): ``--reads everything`` lets an app read what a coding agent reads, plus every
pack's facts and the turns around a message. Off-limits and sensitive kinds stay held back as they are from a coding
agent; the door, the socket and the record of every read are unchanged.

Synthetic only: a migrated temp database with invented people and messages; never the live node.
"""
from __future__ import annotations

import json

import pytest

from tests.query.test_open_owner_grant import cli, dispatch, ids, node, thread  # noqa: F401  (fixtures)
from topos import mcp_clients
from topos.core.handlers import OWNER_AGENT_ALLOWED_TYPES, _app_query_in_bounds
from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
from topos.mcp_clients import AppGrant, parse_app_grant, parse_app_reads
from topos.principal import Principal

EVERYTHING = AppGrant(everything=True)
OWNER_GRANT = Principal(cls="owner_agent", channel="uds", client_id="agent-reader", app_grant=EVERYTHING)
NARROW = Principal(cls="owner_agent", channel="uds", client_id="agent-reader", app_grant=AppGrant(scopes=("messages:read",)))


def test_the_word_makes_an_owner_grant_and_says_what_stays_held_back():
    grant = parse_app_reads(["everything", "messages"])
    assert grant.everything and grant.words() == ["everything", "messages"]
    assert "everything a coding tool can read" in grant.plain() and "Off-limits people" in grant.plain()


def test_it_opens_a_coding_agents_reads_and_the_app_reads():
    assert mcp_clients.CODING_AGENT_READS == OWNER_AGENT_ALLOWED_TYPES   # the two names for one list stay equal
    assert EVERYTHING.message_types() == OWNER_AGENT_ALLOWED_TYPES | {"pack_facts", "reflection_facts", "message_context"}
    assert "fact_feedback" not in EVERYTHING.message_types()   # a write stays a grant of its own (DR-72)


@pytest.mark.parametrize("payload", [{"scope_id": "ai_conversations:read", "access_mode": "raw"},
                                     {"scope_id": "schedule:read"}, {"manifest": {"x": 1}}])
def test_its_query_is_a_coding_agents_and_a_narrow_grants_is_not(payload):
    assert _app_query_in_bounds(OWNER_GRANT, payload) is True
    assert _app_query_in_bounds(NARROW, payload) is False


def test_it_reads_the_turns_around_a_message(thread):  # noqa: F811
    assert ids(dispatch(OWNER_GRANT, {"record_id": "m4", "before": 1, "after": 1})) == ["m3", "m4", "m5"]


def test_it_reads_any_pack_a_narrow_grant_is_refused(node):  # noqa: F811
    seed_pack_registry(node.conn, bundled_pack_dir())
    facts_only = Principal(cls="owner_agent", channel="uds", client_id="agent-reader", app_grant=AppGrant(packs=("life.events",)))
    asked = {"packs": ["moments.joy"]}
    assert dispatch(facts_only, asked, "pack_facts")["status"] == "error"
    out = dispatch(OWNER_GRANT, asked, "pack_facts")
    assert out["status"] == "ok", out


def test_it_is_stored_and_only_a_stored_true_opens_it(node):  # noqa: F811
    label = mcp_clients.mint_agent_label(node.conn, client="reader", grant=EVERYTHING)["label"]
    assert mcp_clients.verify_agent_label(node.conn, label)["app_grant"] == EVERYTHING
    assert [r["reads"] for r in mcp_clients.list_agents(node.conn)] == [["everything"]]
    for stored in ("yes", 1, "true"):
        node.conn.execute(f"UPDATE {mcp_clients.APP_GRANTS_TABLE} SET reads_json = ?",
                          (json.dumps({"packs": [], "scopes": [], "everything": stored}),))
        node.conn.commit()
        assert mcp_clients.verify_agent_label(node.conn, label)["app_grant"].everything is False


def test_a_row_written_before_owner_grants_stays_narrow(node):  # noqa: F811
    label = mcp_clients.mint_agent_label(node.conn, client="reader", grant=AppGrant(scopes=("messages:read",)))["label"]
    node.conn.execute(f"UPDATE {mcp_clients.APP_GRANTS_TABLE} SET reads_json = ?",
                      (json.dumps({"packs": [], "scopes": ["messages:read"]}),))
    node.conn.commit()
    assert mcp_clients.verify_agent_label(node.conn, label)["app_grant"] == AppGrant(scopes=("messages:read",))


def test_feedback_may_name_any_pack_it_reads(node):  # noqa: F811
    grant = parse_app_grant(["everything"], ["facts:moments.joy"])
    assert grant.everything and grant.feedback == ("moments.joy",) and "fact_feedback" in grant.message_types()
    with pytest.raises(ValueError):
        parse_app_grant(["messages"], ["facts:moments.joy"])
    label = mcp_clients.mint_agent_label(node.conn, client="reader", grant=grant)["label"]
    assert mcp_clients.verify_agent_label(node.conn, label)["app_grant"].feedback == ("moments.joy",)


def test_it_counts_as_reading_every_pack(node):  # noqa: F811
    mcp_clients.mint_agent_label(node.conn, client="reader", grant=EVERYTHING)
    mcp_clients.mint_agent_label(node.conn, client="other", grant=AppGrant(packs=("life.events",)))
    assert mcp_clients.apps_granted_pack(node.conn, "moments.joy") == 1
    assert mcp_clients.apps_granted_pack(node.conn, "life.events") == 2


def test_the_owner_says_yes_to_it_on_the_terminal(cli):  # noqa: F811
    result = cli.run("add", "reader", "--reads", "everything")
    assert result.exit_code == 0, result.output
    assert "everything a coding tool can read" in result.output
    label = (cli.agents / "reader.token").read_text().strip()
    assert mcp_clients.verify_agent_label(cli.node.conn, label)["app_grant"] == EVERYTHING
    no = cli.run("add", "second", "--reads", "everything", answer="n\n")
    assert no.exit_code != 0 and not (cli.agents / "second.token").exists()

"""The owner's sharing screens over the relay (any-to-any N4; A2A-3 §7, test obligations "Node (N4, N5)" 1, 2, 4).

Every node is in-process (N2's fresh node, ``test_self_bind``): a served database with the owner's messages, bound
through the real bind when a test needs it, frames stamped with the test's own relay stamp key exactly as the control
plane stamps the owner's app. Every person, Topos, source and id is invented.

protects:
  - each message is owner-only in the handled-types snapshot and answers only the owner's app, over the relay, for the
    node's owner; another binding is refused;
  - the catalog lists bundled and installed sources with their rows, and installs under the named scopes that have
    none yet; its kinds follow the switches and never include facts; no reply carries an item's text;
  - the week sums only the named shares' receipts inside the window, including receipts whose request envelope
    retention has already emptied.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import as_principal
from tests.permissions_v2.test_message_search_refusals import Socket
from tests.permissions_v2.test_self_bind import (ACTOR, CP, CP_KID, OWNER, RECIPIENT_CLIENT, TOPOS, node,  # noqa: F401
                                                 share_with_a_recipient)
from topos.core.handlers import OWNER_ONLY_MESSAGE_TYPES, handle_control_plane_request
from topos.permissions_v2 import runtime as runtime_module, search_transport, switches
from topos.permissions_v2.share_kinds import KINDS, kinds_of, released_kinds
from topos.principal import OWNER_APP, Principal

CATALOG, WEEK = "permissions_v2_share_catalog", "permissions_v2_share_week"
TYPES = (CATALOG, WEEK)
DATASET = f"{OWNER}:topos:{TOPOS}"
SCOPE = {"user_id": OWNER, "topos_id": TOPOS, "dataset_id": DATASET}


def frame(node, kind: str, payload, **stamp) -> dict:
    return node.stamped({"id": f"n4-{kind}-{time.monotonic_ns()}", "type": kind, "payload": payload}, **stamp)


async def send(node, kind: str, payload, **stamp) -> dict:
    return await node.send(frame(node, kind, payload, **stamp))


def identity() -> dict:
    return runtime_module.get_runtime().protocol.ledger.identity.model_dump()


def catalog_payload(**extra) -> dict:
    return {"request": {"scopes": [dict(SCOPE)]}, **extra}


def week_payload(grant_ids, since, until, **extra) -> dict:
    return {"binding": identity(), "request": {"grant_ids": list(grant_ids), "since": since, "until": until}, **extra}


# --- 1. owner-only, the owner, the binding -----------------------------------------------------------------

def test_each_message_is_owner_only_in_the_handled_types_snapshot():
    from pathlib import Path
    snapshot = json.loads((Path(__file__).resolve().parents[2] / "topos" / "protocol"
                           / "handled_message_types.json").read_text())
    for kind in TYPES:
        assert kind in OWNER_ONLY_MESSAGE_TYPES
        assert kind in snapshot["handled_message_types"] and kind in snapshot["owner_only_message_types"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", TYPES)
@pytest.mark.parametrize("who", ["third_party", "unstamped", "another_user", "auto_resync"])
async def test_only_the_owners_app_over_the_relay_is_answered(node, kind, who):
    await node.bind()
    payload = catalog_payload() if kind == CATALOG else week_payload(["g-1"], 0, 10)
    if who == "unstamped":
        message = {"id": "n4-unstamped", "type": kind, "payload": payload}
        reply = await node.send(message)
    elif who == "third_party":
        reply = await send(node, kind, payload, cls="third_party", client=RECIPIENT_CLIENT, acting=ACTOR)
    elif who == "another_user":
        reply = await send(node, kind, payload, acting="someone-else")
    else:
        reply = await send(node, kind, payload, client="permissions_v2_auto_resync")
    assert reply["status"] == "error" and reply["code"] == 403, reply
    assert reply["error"] in ("owner_mode_required", "owner_authority_required")
    assert "payload" not in reply


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", TYPES)
async def test_the_owners_own_socket_is_not_the_relay(node, kind):
    await node.bind()
    payload = catalog_payload() if kind == CATALOG else week_payload(["g-1"], 0, 10)
    reply = await handle_control_plane_request({"id": "n4-socket", "type": kind, "payload": payload},
                                               principal=Principal(cls=OWNER_APP, channel="uds", acting_user=OWNER))
    assert (reply["status"], reply["code"], reply["error"]) == ("error", 403, "owner_authority_required")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", TYPES)
@pytest.mark.parametrize("change", ["node_id", "owner_id", "resource_id", "environment_id", "malformed", "missing"])
async def test_another_binding_is_refused(node, kind, change):
    await node.bind()
    binding = identity()
    if change == "malformed":
        binding = {**binding, "extra": "field"}
    elif change != "missing":
        binding[change] = binding[change] + "-other"
    payload = catalog_payload(binding=binding) if kind == CATALOG else week_payload(["g-1"], 0, 10, binding=binding)
    if change == "missing":
        payload.pop("binding", None)
    reply = await send(node, kind, payload)
    if kind == CATALOG and change == "missing":      # bound or not: the catalog may name no binding
        assert reply["status"] == "ok", reply
        return
    assert (reply["status"], reply["code"], reply["error"]) == ("error", 409, "binding_mismatch"), reply


@pytest.mark.asyncio
async def test_an_unbound_node_answers_its_owner_the_catalog_and_refuses_what_needs_a_binding(node):
    assert not switches.is_bound()
    reply = await send(node, CATALOG, catalog_payload())
    assert reply["status"] == "ok" and reply["type"] == CATALOG, reply
    refused = await send(node, CATALOG, catalog_payload(binding={"environment_id": "permissions-beta-x",
                                                                 "node_id": "n", "resource_id": TOPOS,
                                                                 "owner_id": OWNER}))
    assert (refused["code"], refused["error"]) == (409, "binding_mismatch")
    week = await send(node, WEEK, {"binding": {"environment_id": "permissions-beta-x", "node_id": "n",
                                               "resource_id": TOPOS, "owner_id": OWNER},
                                   "request": {"grant_ids": ["g-1"], "since": 0, "until": 1}})
    assert (week["code"], week["error"]) == (409, "binding_mismatch")
    stranger = await send(node, CATALOG, catalog_payload(), acting="someone-else")
    assert (stranger["code"], stranger["error"]) == (403, "owner_authority_required")


# --- 2. the catalog ------------------------------------------------------------------------------------------

def _install(node, install_id: str, source_id: str, *, scope=None, active=1, status="active", definition=None):
    with closing(sqlite3.connect(node.canonical)) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS source_runtime_installs (install_id TEXT PRIMARY KEY,
            scope_key TEXT, source_id TEXT, version_id TEXT, status TEXT, is_active INTEGER,
            source_definition_json TEXT, source_version_row_json TEXT, failure_reason TEXT, created_at TEXT,
            updated_at TEXT)""")
        conn.execute("INSERT INTO source_runtime_installs (install_id, scope_key, source_id, version_id, status, "
                     "is_active, source_definition_json) VALUES (?,?,?,?,?,?,?)",
                     (install_id, json.dumps({**(scope or SCOPE), "device_id": "*"}), source_id, "v1", status, active,
                      json.dumps(definition or {"source_id": source_id})))
        conn.commit()


def _ai_chat(node, message_id: str, source_id: str, content: str):
    with closing(sqlite3.connect(node.canonical)) as conn:
        conn.execute("INSERT INTO ai_chat_messages (message_id, conversation_id, source_id, sender_type, content, "
                     "event_at) VALUES (?,?,?,?,?,?)",
                     (message_id, "chat-1", source_id, "user", content, mc._iso(int(time.time()) - 60)))
        conn.commit()


@pytest.mark.asyncio
async def test_the_catalog_lists_bundled_and_installed_sources_with_their_rows_and_no_text(node):
    await node.bind()
    # Rows of a bundled source nobody installs (iMessage: the fixture's six messages), and of an export source.
    _ai_chat(node, "chat-a", "chatgpt_file_ingestion", "a prompt about the quarterly vendor review")
    _ai_chat(node, "chat-b", "chatgpt_file_ingestion", "another prompt about the deploy checklist")
    # An install under the named scope with no rows yet, with its own name; and three that must not be listed.
    _install(node, "i-1", "garden_journal", definition={"source_id": "garden_journal", "display_name": "Garden notes",
                                                        "canonical_group_id": "journal"})
    _install(node, "i-2", "elsewhere_journal", scope={**SCOPE, "topos_id": "topos_" + "7a" * 16},
             definition={"source_id": "elsewhere_journal", "canonical_group_id": "journal"})
    _install(node, "i-3", "retired_journal", active=0,
             definition={"source_id": "retired_journal", "canonical_group_id": "journal"})
    _install(node, "i-4", "documents_only", definition={"source_id": "documents_only", "canonical_group_id": "documents"})
    reply = await send(node, CATALOG, catalog_payload(binding=identity()))
    assert reply["status"] == "ok", reply
    body = reply["payload"]
    assert body["version"] == "topos-share-catalog/v1" and type(body["as_of"]) is int
    assert body["sources"] == [
        {"source_id": "chatgpt_file_ingestion", "label": "ChatGPT File Ingestion", "table": "ai_chat_messages",
         "installed": False, "rows": 2},
        {"source_id": "garden_journal", "label": "Garden notes", "table": "journal_entries", "installed": True,
         "rows": 0},
        {"source_id": mc.SOURCE, "label": "iMessage", "table": "conversation_messages", "installed": False,
         "rows": 6},
    ]
    text = json.dumps(reply)
    for word in ("roadmap", "vendor", "deploy", "checklist", "quarterly"):
        assert word not in text


@pytest.mark.asyncio
async def test_a_scope_that_names_another_owner_or_a_wildcard_is_refused(node):
    await node.bind()
    for scope in ({**SCOPE, "user_id": "someone-else"}, {**SCOPE, "dataset_id": "*"}, {**SCOPE, "extra": "x"},
                  {"user_id": OWNER, "topos_id": TOPOS}):
        reply = await send(node, CATALOG, {"request": {"scopes": [scope]}})
        assert (reply["code"], reply["error"]) == (400, "payload_invalid"), scope
    for payload in ({}, {"request": {}}, {"request": {"scopes": "all"}}, {"request": {"scopes": [SCOPE] * 9}},
                    {"request": {"scopes": [SCOPE]}, "other": 1}):
        reply = await send(node, CATALOG, payload)
        assert (reply["code"], reply["error"]) == (400, "payload_invalid"), payload


ALL = ["messages", "ai_chats", "journal_entries", "interests", "goals", "relationships"]


@pytest.mark.parametrize("env, expected", [
    ({}, ALL),
    ({"TOPOS_PERMISSIONS_V2_JOURNAL_SOURCES": "false"}, [k for k in ALL if k != "journal_entries"]),
    ({"TOPOS_PERMISSIONS_V2_INTEREST_SOURCES": "off"}, [k for k in ALL if k != "interests"]),
    ({"TOPOS_PERMISSIONS_V2_IDENTITY_ATTESTATIONS_ENABLED": "0"}, [k for k in ALL if k != "relationships"]),
    ({"TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED": "false"}, []),
    ({"TOPOS_PERMISSIONS_V2_ENABLED": "false"}, []),
    # Facts are never a kind this node releases, whatever its switches say about derived facts.
    ({"TOPOS_PERMISSIONS_V2_DERIVED_FACTS": "true", "TOPOS_PERMISSIONS_V2_FACT_RELEASE_ENABLED": "true"}, ALL),
])
def test_the_released_kinds_follow_the_switches_and_never_include_facts(env, expected):
    assert released_kinds(env) == expected
    assert "facts" not in released_kinds(env)


@pytest.mark.asyncio
async def test_the_catalog_names_the_kinds_setup_will_offer_bound_or_not(node, monkeypatch):
    unbound = await send(node, CATALOG, catalog_payload())
    assert unbound["payload"]["kinds"] == ALL
    await node.bind()
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_JOURNAL_SOURCES", "false")
    bound = await send(node, CATALOG, catalog_payload())
    assert bound["payload"]["kinds"] == [k for k in ALL if k != "journal_entries"]


def test_kinds_of_reads_the_signed_policy_in_the_contracts_order():
    from tests.permissions_v2.test_knowledge_search import knowledge_policy
    from topos.permissions_v2.registry import parse_policy
    raw = knowledge_policy()
    raw["search"]["tables"] = ["ai_chat_messages", "conversation_messages", "journal_entries"]
    for form in raw["rules"][0]["release"]["forms"]:
        form["tables"] = raw["search"]["tables"]
    raw["search"]["result_types"] = ["relationship", "journal_entry", "message", "goal", "fact"]
    assert kinds_of(parse_policy(raw)) == ["messages", "ai_chats", "journal_entries", "goals", "relationships",
                                           "facts"]
    raw["search"]["result_types"] = ["goal"]
    assert kinds_of(parse_policy(raw)) == ["goals"]
    assert kinds_of(parse_policy(mc.search_policy())) == ["messages"]       # message search: by its tables
    assert KINDS[-1] == "facts"


# --- 4. the week ----------------------------------------------------------------------------------------------

async def _search(node, policy, request_id: str) -> dict:
    from topos.permissions_v2.signing import parse_envelope, request_digest, sign_envelope
    now = int(time.time())
    runtime = runtime_module.get_runtime()

    def authority():
        with runtime.protocol.ledger._transaction() as conn:
            runtime.protocol._sync_protection(conn)
        with as_principal(cls=OWNER_APP, channel="uds", acting_user=OWNER):
            return runtime.protocol.ledger.authority_snapshot(policy["binding"]["grant_id"], now=now)
    snapshot = await asyncio.to_thread(authority)
    intent = {"query": "roadmap deploy", "k": 5}
    envelope = sign_envelope(parse_envelope({
        **snapshot.model_dump(), "version": "topos-grantee-envelope/v2", "kid": CP_KID, "request_id": request_id,
        "request_type": "permissions.v2.search", "request_hash": request_digest("permissions.v2.search", intent),
        "issued_at": now, "expires_at": now + 100}, signed=False), CP)
    message = node.stamped({"id": request_id, "type": search_transport.MESSAGE_TYPE,
                            "payload": {"envelope": envelope.model_dump(), "intent": intent}},
                           cls="third_party", client=RECIPIENT_CLIENT, acting=ACTOR)
    socket = Socket()
    await search_transport.dispatch_message_search(socket, message)
    [answer] = [json.loads(value) for value in socket.sent]
    return answer


def _receipt(conn, request_id: str, receipt: dict) -> None:
    conn.execute("INSERT INTO p2a_receipts VALUES (?, ?, ?)", (request_id, json.dumps(receipt), "{}"))


@pytest.mark.asyncio
async def test_the_week_sums_only_the_named_shares_inside_the_window(node, monkeypatch):
    proof, _ = await node.bind()
    policy = await share_with_a_recipient(node, proof, monkeypatch)
    grant = policy["binding"]["grant_id"]
    answered = await _search(node, policy, "n4-week-search-1")
    assert answered["status"] == "ok", answered
    released = len(answered["payload"]["output"]["records"])
    assert released >= 1
    ledger = runtime_module.get_runtime().protocol.ledger.path
    now = int(time.time())
    with closing(sqlite3.connect(ledger)) as conn:
        # The real search's own receipt names no grant; its request row is emptied as retention leaves it.
        conn.execute("UPDATE p2a_requests SET envelope_json='' WHERE request_id='n4-week-search-1'")
        policy_hash = conn.execute("SELECT json_extract(receipt_json, '$.policy_hash') FROM p2a_receipts "
                                   "WHERE request_id='n4-week-search-1'").fetchone()[0]
        v3 = {"version": "topos-local-receipt/v3", "policy_hash": policy_hash, "verdict": "permit"}
        _receipt(conn, "old", {**v3, "checked_at": now - 8 * 86_400, "record_count": 40})     # before the window
        _receipt(conn, "late", {**v3, "checked_at": now + 3_600, "record_count": 30})          # at or after until
        _receipt(conn, "denied", {**v3, "verdict": "deny", "checked_at": now, "record_count": 0})
        _receipt(conn, "other", {**v3, "policy_hash": "ab" * 32, "checked_at": now, "record_count": 50})
        answer = {"version": "topos-local-receipt/answer-v1", "grant_id": grant, "finished_at": now}
        _receipt(conn, "ask-1", {**answer, "outcome": "answered", "records_used": 4})
        _receipt(conn, "ask-2", {**answer, "outcome": "no_answer", "records_used": 2})
        _receipt(conn, "ask-3", {**answer, "outcome": "answered", "records_used": 7, "finished_at": now - 9 * 86_400})
        _receipt(conn, "ask-4", {**answer, "grant_id": "another-grant", "outcome": "answered", "records_used": 9})
        conn.commit()
    reply = await send(node, WEEK, week_payload([grant], now - 7 * 86_400, now + 3_600))
    assert reply["status"] == "ok", reply
    assert reply["payload"] == {"version": "topos-share-week/v1", "items_used": released + 4 + 2, "answered": 1,
                                "no_answer": 1}
    other = await send(node, WEEK, week_payload(["another-grant", "unknown-grant"], now - 7 * 86_400, now + 3_600))
    assert other["payload"] == {"version": "topos-share-week/v1", "items_used": 9, "answered": 1, "no_answer": 0}
    empty = await send(node, WEEK, week_payload([grant], now, now))
    assert empty["payload"] == {"version": "topos-share-week/v1", "items_used": 0, "answered": 0, "no_answer": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("request_body", [
    {"grant_ids": [], "since": 0, "until": 1},
    {"grant_ids": [f"g-{n}" for n in range(21)], "since": 0, "until": 1},
    {"grant_ids": ["g-1", "g-1"], "since": 0, "until": 1},
    {"grant_ids": [" g-1"], "since": 0, "until": 1},
    {"grant_ids": ["g-1"], "since": 5, "until": 1},
    {"grant_ids": ["g-1"], "since": -1, "until": 1},
    {"grant_ids": ["g-1"], "since": 0, "until": "1"},
    {"grant_ids": ["g-1"], "since": 0},
    {"grant_ids": ["g-1"], "since": 0, "until": 1, "extra": True},
])
async def test_a_week_request_out_of_shape_is_refused(node, request_body):
    await node.bind()
    reply = await send(node, WEEK, {"binding": identity(), "request": request_body})
    assert (reply["status"], reply["code"], reply["error"]) == ("error", 400, "payload_invalid"), reply

"""The inbound truth lane's owner side: labels and their verified_claim object, the weekly numbers, the cards, the four
owner doors (owner class only, refused before the payload is read), the CLI, and every share path skipping it all.

Synthetic only: a migrated temp database with invented rows; never the live node.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.truth_lane_support import add_contact, add_journal, add_message, ago, bind, build_node, connect
from topos.config import truth_inbound as cfg
from topos.ingestion import truth_inbound as lane
from topos.principal import CP_RELAY, OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.query import truth_checks as store

CLAIM_ME = "The owner painted the shed doors blue."
CLAIM_THEM = "Robin finished the shed roof."


@pytest.fixture
def node(tmp_path, tmp_path_factory, monkeypatch):
    path = build_node(tmp_path, tmp_path_factory.getbasetemp())
    bind(monkeypatch, path)
    conn = connect(path)
    yield SimpleNamespace(path=path, conn=conn)
    conn.close()


def _check(conn, table, record_id, source, owner, verdict="supported", claim=CLAIM_ME, category="did",
           cited=(), model="local:test", latency=1000.0):
    result = {"verdict": verdict, "reason": None if verdict != "not_checkable" else "not_checkable:fragment",
              "category": category, "claim": {"text": claim} if claim else None, "timings_ms": {"total": latency},
              "evidence": [{"ref": f"{t}:{r}", "role": role, "date": d, "cited": True} for t, r, role, d in cited]}
    if verdict == "not_checkable":
        result["verdict"] = "unable"
    record = {"table": table, "record_id": record_id, "source": source, "owner": owner}
    return store.record_check(conn, lane.check_row(record, result, model=model))


@pytest.fixture
def week(node):
    """One week of checks: two claims (one about a contact) with a cited journal entry, and one not a claim."""
    add_contact(node.conn, "+15550000001", "Robin Example")
    add_message(node.conn, "m-me", "I painted the shed doors blue yesterday afternoon.", from_self=True, at=ago(hours=4))
    add_message(node.conn, "m-them", "Robin here: I finished the shed roof this morning.", at=ago(hours=1))
    add_message(node.conn, "m-ok", "ok", at=ago(hours=2))
    add_journal(node.conn, "j-cited", "Painted both shed doors a deep blue today. " * 20, at=ago(days=1))
    node.conn.commit()
    cited = (("journal_entries", "j-cited", "authored", "2026-10-22"),)
    ids = SimpleNamespace(
        me=_check(node.conn, "conversation_messages", "m-me", "imessage", True, cited=cited, latency=900.0),
        them=_check(node.conn, "conversation_messages", "m-them", "imessage", False, verdict="no_evidence",
                    claim=CLAIM_THEM, category="people", latency=2100.0),
        ok=_check(node.conn, "conversation_messages", "m-ok", "imessage", False, verdict="not_checkable", claim=None,
                  category=None, model=None, latency=0.0),
    )
    return ids


# --- labels and the verified_claim object ---------------------------------------------------------------------------------
def _objects(conn):
    rows = conn.execute("SELECT signal_dimension, object_type, payload_json, confidence, source_refs_json, valid_from, "
                        "valid_to, created_by FROM signal_objects WHERE object_type='verified_claim'").fetchall()
    return [dict(zip(("dimension", "type", "payload", "confidence", "refs", "valid_from", "valid_to", "by"), r)) for r in rows]


def test_a_label_is_stored_and_writes_one_owner_only_verified_claim(node, week):
    out = store.label_claim(node.conn, week.them, "true", "  saw the roof   myself ")
    assert out["ok"] is True and out["label_id"]
    label = node.conn.execute("SELECT check_id, label, note FROM truth_labels").fetchall()
    assert label == [(week.them, "true", "saw the roof myself")]
    [obj] = _objects(node.conn)
    assert obj["dimension"] == "memory" and obj["confidence"] == 1.0 and obj["valid_to"] is None and obj["by"] == "owner"
    assert json.loads(obj["payload"]) == {"claim": CLAIM_THEM, "label": "true", "note": "saw the roof myself",
                                          "speaker_kind": "contact", "source": "imessage", "check_id": week.them,
                                          "disclosure": "owner_only"}
    assert json.loads(obj["refs"]) == [{"table": "conversation_messages", "record_id": "m-them"}]
    said = node.conn.execute("SELECT event_at FROM conversation_messages WHERE message_id='m-them'").fetchone()[0]
    assert datetime.fromisoformat(obj["valid_from"]) == datetime.fromisoformat(said.replace("Z", "+00:00"))


def test_a_second_submit_replaces_the_label_and_the_object(node, week):
    first = store.label_claim(node.conn, week.me, "true")
    second = store.label_claim(node.conn, week.me, "false", "it was green")
    assert first["label_id"] == second["label_id"]
    assert node.conn.execute("SELECT COUNT(*), MAX(label) FROM truth_labels").fetchone() == (1, "false")
    [obj] = _objects(node.conn)
    assert obj["confidence"] == 0.0 and json.loads(obj["payload"])["label"] == "false"
    store.label_claim(node.conn, week.me, "partly")
    assert [o["confidence"] for o in _objects(node.conn)] == [0.5]


@pytest.mark.parametrize("check_id, label, note, code", [
    ("nope", "true", None, "unknown_check"), (None, "true", None, "invalid_truth_label"),
    ("x", "maybe", None, "invalid_truth_label"), ("x", "true", 5, "invalid_truth_label"),
])
def test_a_bad_label_is_refused_with_a_fixed_code(node, week, check_id, label, note, code):
    with pytest.raises(store.TruthRequestError) as raised:
        store.label_claim(node.conn, check_id, label, note)
    assert str(raised.value) == code


def test_a_row_that_was_not_a_claim_cannot_be_labelled(node, week):
    with pytest.raises(store.TruthRequestError) as raised:
        store.label_claim(node.conn, week.ok, "true")
    assert str(raised.value) == "not_a_claim"


# --- the weekly numbers ---------------------------------------------------------------------------------------------------
def test_weekly_stats_are_counts_only(node, week):
    store.label_claim(node.conn, week.me, "false")
    result = store.weekly_stats(node.conn, 2)
    this, last = result["weeks"]
    assert this["week"] == store.iso_week() and last["week"] == store.iso_week(datetime.now(timezone.utc) - timedelta(days=7))
    assert {k: this[k] for k in ("checked", "backed", "nothing_found", "unable", "not_a_claim", "labelled", "to_confirm")} == \
        {"checked": 3, "backed": 1, "nothing_found": 1, "unable": 0, "not_a_claim": 1, "labelled": 1, "to_confirm": 1}
    assert this["by_source"] == {"imessage": 3, "signal": 0, "transcripts": 0, "journal": 0}
    assert this["by_category"] == {"did": 1, "people": 1}
    assert this["agreement"] == {"backed_true": 0, "backed_false": 1, "nothing_true": 0}
    assert this["latency_ms_p50"] == 1500.0  # the gate-refused row made no model call and is not counted
    assert last["checked"] == 0 and last["latency_ms_p50"] is None
    text = json.dumps(result)
    for secret in (CLAIM_ME, CLAIM_THEM, "Robin", "m-me", week.me, "shed"):
        assert secret not in text


@pytest.mark.parametrize("weeks", [0, 9, "2", True, None, 1.0])
def test_weekly_stats_take_one_to_eight_weeks(node, weeks):
    with pytest.raises(store.TruthRequestError):
        store.weekly_stats(node.conn, weeks)


def test_weekly_stats_on_a_node_without_the_tables_are_zeros(node):
    [entry] = store.weekly_stats(node.conn, 1)["weeks"]
    assert entry["checked"] == 0 and not store.tables_present(node.conn)


# --- the cards --------------------------------------------------------------------------------------------------------
def test_cards_are_the_claims_newest_first_with_snippets_from_the_cited_records(node, week):
    cards = store.claim_cards(node.conn, 10, True)["cards"]
    assert [c["check_id"] for c in cards] == [week.them, week.me]  # the not-a-claim row is no card
    them, me = cards
    assert them["speaker"] == "Robin Example" and me["speaker"] == "you"
    assert them["claim_text"] == CLAIM_THEM and them["verdict"] == "no_evidence" and them["category"] == "people"
    assert them["source"] == "imessage" and them["latency_ms"] == 2100.0 and them["cited"] == []
    assert datetime.fromisoformat(me["date"]).utcoffset() is not None
    [cited] = me["cited"]
    assert cited["source"] == "journal" and cited["role"] == "authored" and cited["date"] == "2026-10-22"
    assert cited["snippet"].startswith("Painted both shed doors a deep blue today.") and len(cited["snippet"]) == 400
    assert set(me) == {"check_id", "speaker", "source", "date", "claim_text", "category", "verdict", "cited",
                       "latency_ms", "label", "note"}


def test_a_labelled_claim_leaves_the_unlabelled_cards(node, week):
    store.label_claim(node.conn, week.them, "partly", "roof yes, gutters no")
    assert [c["check_id"] for c in store.claim_cards(node.conn, 10, True)["cards"]] == [week.me]
    everything = store.claim_cards(node.conn, 10, False)["cards"]
    assert [(c["check_id"], c["label"]) for c in everything] == [(week.them, "partly"), (week.me, None)]


def test_a_deleted_record_drops_its_card_and_a_deleted_cited_record_drops_its_snippet(node, week):
    node.conn.execute("DELETE FROM journal_entries WHERE entry_id='j-cited'")
    node.conn.execute("DELETE FROM conversation_messages WHERE message_id='m-them'")
    node.conn.commit()
    [card] = store.claim_cards(node.conn, 10, True)["cards"]
    assert card["check_id"] == week.me and card["cited"] == []


@pytest.mark.parametrize("limit, unlabelled_only", [(0, True), (51, True), ("5", True), (5, "yes")])
def test_cards_take_a_limit_of_fifty(node, limit, unlabelled_only):
    with pytest.raises(store.TruthRequestError):
        store.claim_cards(node.conn, limit, unlabelled_only)


# --- the four doors ---------------------------------------------------------------------------------------------------
DOORS = ("truth_inbound_settings", "truth_weekly_stats", "truth_claim_cards", "truth_claim_label")


class Untouchable(dict):
    """A payload that fails the test if anything reads it."""

    def __getattribute__(self, name):
        if name in ("__class__",):
            return object.__getattribute__(self, name)
        raise AssertionError(f"payload read: {name}")


def _handler(msg_type):
    from topos.core.handlers import HANDLERS

    return HANDLERS[msg_type]


def _as(principal, msg_type, payload):
    token = set_principal(principal)
    try:
        return asyncio.run(_handler(msg_type)({"id": "r1", "type": msg_type, "payload": payload}))
    finally:
        reset_principal(token)


@pytest.mark.parametrize("msg_type", DOORS)
@pytest.mark.parametrize("principal", [
    Principal(CP_RELAY, "cp_relay"), Principal(THIRD_PARTY, "local_http", client_id="truth-mirror"),
    Principal(THIRD_PARTY, "cp_relay"), Principal("owner_agent", "uds"), Principal("owner_automation", "cp_relay"), None,
])
def test_every_door_refuses_anyone_but_the_owner_before_reading_the_payload(monkeypatch, msg_type, principal):
    monkeypatch.setenv("TOPOS_TRUTH_CLIENT_ALLOWLIST", "truth-mirror")  # even a client allowed on verify_claim
    out = _as(principal, msg_type, Untouchable())
    assert out == {"id": "r1", "status": "error", "code": 403, "error": "owner_mode_required"}


def test_the_owner_reads_and_changes_the_settings_and_turning_on_queues_a_batch(node):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    owner = Principal(OWNER_APP, "uds")
    got = _as(owner, "truth_inbound_settings", {"get": True})
    assert got["status"] == "ok" and got["payload"] == {**cfg.defaults(), "paused_until": None}
    changed = _as(owner, "truth_inbound_settings", {"set": {"enabled": True, "sources": ["journal"], "rate_per_minute": 3}})
    assert changed["payload"] == {"enabled": True, "sources": ["journal"], "model": "local", "rate_per_minute": 3,
                                  "paused_until": None}
    assert node.conn.execute("SELECT COUNT(*) FROM pipeline_jobs WHERE kind=?", (lane.KIND,)).fetchone()[0] == 1
    for bad in ({"set": {"model": "gpt"}}, {"set": "on"}, {"get": True, "drop": 1}, "get"):
        assert _as(owner, "truth_inbound_settings", bad) == {"id": "r1", "status": "error", "error": "invalid_truth_settings"}


def test_the_owner_reads_stats_and_cards_and_labels_through_the_doors(node, week):
    owner = Principal(OWNER_APP, "uds")
    stats = _as(owner, "truth_weekly_stats", {"weeks": 1})
    assert stats["status"] == "ok" and stats["payload"]["enabled"] is False
    assert stats["payload"]["weeks"][0]["checked"] == 3
    assert _as(owner, "truth_weekly_stats", {"weeks": 9})["error"] == "invalid_truth_request"
    cards = _as(owner, "truth_claim_cards", {"limit": 50, "unlabelled_only": True})["payload"]["cards"]
    assert [c["check_id"] for c in cards] == [week.them, week.me]
    assert _as(owner, "truth_claim_cards", {"limit": 51})["error"] == "invalid_truth_request"
    labelled = _as(owner, "truth_claim_label", {"check_id": week.me, "label": "true", "note": "yes"})
    assert labelled["status"] == "ok" and labelled["payload"]["ok"] is True and labelled["payload"]["label_id"]
    assert _as(owner, "truth_claim_label", {"check_id": "nope", "label": "true"})["error"] == "unknown_check"
    assert _as(owner, "truth_claim_label", "not a dict")["error"] == "invalid_truth_label"


def test_on_the_owner_socket_the_reads_are_open_and_the_changes_need_the_app():
    import topos.core.handlers as hub

    assert {"truth_weekly_stats", "truth_claim_cards"} <= hub.owner_door_open_types()
    assert {"truth_inbound_settings", "truth_claim_label"} <= hub.DANGEROUS_TYPES


def test_the_protocol_snapshot_names_the_four_doors():
    snapshot = json.loads((Path(__file__).resolve().parents[2] / "topos/protocol/handled_message_types.json").read_text())
    assert set(DOORS) <= set(snapshot["handled_message_types"])


# --- the owner socket routes ------------------------------------------------------------------------------------------
def _client(principal):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import topos.api.local_mcp as routes
    from topos.auth import resolve_request_principal

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[resolve_request_principal] = lambda: principal
    return TestClient(app)


@pytest.mark.parametrize("msg_type", DOORS)
def test_each_route_refuses_a_non_owner_before_the_body_is_read(msg_type):
    client = _client(Principal(THIRD_PARTY, "local_http"))
    response = client.post(f"/api/local/{msg_type}", content=b"{not json")  # unreadable: a 422 would mean it was read
    assert response.status_code == 403 and response.json() == {"detail": "owner_mode_required"}


@pytest.mark.parametrize("msg_type", DOORS + ("verify_claim_owner",))
def test_each_route_hands_the_owners_body_to_its_door(monkeypatch, msg_type):
    import topos.api.local_mcp as routes

    seen = []

    async def dispatch(message, principal=None):
        seen.append((message["type"], message["payload"], principal.cls))
        return {"id": message["id"], "status": "ok", "payload": {"echo": message["payload"]}}

    monkeypatch.setattr(routes, "handle_control_plane_request", dispatch)
    response = _client(Principal(OWNER_APP, "uds")).post(f"/api/local/{msg_type}", json={"weeks": 2})
    assert response.status_code == 200 and response.json() == {"echo": {"weeks": 2}}
    assert seen == [(msg_type, {"weeks": 2}, OWNER_APP)]
    if msg_type != "verify_claim_owner":
        assert response.headers["cache-control"] == "no-store"
        empty = _client(Principal(OWNER_APP, "uds")).post(f"/api/local/{msg_type}")
        assert empty.status_code == 200 and seen[-1][1] == {}


def test_each_route_is_in_the_owner_socket_census():
    from topos import owner_door_routes as census

    for msg_type in DOORS:
        assert census.lookup("POST", f"/api/local/{msg_type}").verdict == census.DISPATCHER


# --- the CLI ----------------------------------------------------------------------------------------------------------
def test_the_cli_prints_the_weekly_numbers_without_claim_text_and_never_writes(node, week, monkeypatch):
    from click.testing import CliRunner

    from topos.cli import commands
    from topos.cli.truth_cmd import truth_group

    monkeypatch.setattr("topos.storage.db.paths.resolve_active_database", lambda *a, **k: SimpleNamespace(path=node.path))
    store.label_claim(node.conn, week.them, "true")
    node.conn.commit()
    before = node.path.read_bytes()
    result = CliRunner().invoke(truth_group, ["weekly", "--weeks", "2"])
    assert result.exit_code == 0, result.output
    assert f"{store.iso_week()}: 3 checked, 1 backed, 1 nothing found, 0 unable, 1 not a claim; 1 labelled, 1 to confirm" \
        in result.output
    assert "Truth lane: off (local model" in result.output and "nothing found but true 1" in result.output
    for secret in (CLAIM_ME, CLAIM_THEM, "Robin", "shed"):
        assert secret not in result.output
    as_json = json.loads(CliRunner().invoke(truth_group, ["weekly", "--json"]).output)
    assert as_json["weeks"][0]["checked"] == 3
    assert node.path.read_bytes() == before
    assert "truth" in commands.main.commands
    assert CliRunner().invoke(truth_group, ["weekly", "--weeks", "9"]).exit_code != 0


def test_the_cli_on_a_database_without_the_lane_prints_zeros_and_creates_nothing(node, monkeypatch):
    from click.testing import CliRunner

    from topos.cli.truth_cmd import truth_group

    monkeypatch.setattr("topos.storage.db.paths.resolve_active_database", lambda *a, **k: SimpleNamespace(path=node.path))
    result = CliRunner().invoke(truth_group, ["weekly"])
    assert result.exit_code == 0 and ": 0 checked" in result.output
    assert not store.tables_present(node.conn)


# --- every share path skips it ----------------------------------------------------------------------------------------
def test_retrieval_never_releases_a_verified_claim_below_the_owner_tier():
    from topos.query.retrieval import _OWNER_ONLY_GRANTS, _fact_disclosure_allowed, _fact_release_allowed

    every_grant = SimpleNamespace(signal_objects=sorted(set(_OWNER_ONLY_GRANTS.values()) | {"stat_insights", "verified_claim"}),
                                  fact_classes=[])
    for disclosure in ("owner_only", "scoped", None):
        claim = {"object_type": "verified_claim", "disclosure": disclosure}
        for tier in ("owner_summary", "redacted", "aggregate", "minimal", ""):
            assert not _fact_disclosure_allowed(claim, tier, every_grant)
            assert not _fact_release_allowed(claim, tier, every_grant)
        assert _fact_disclosure_allowed(claim, "owner_raw", every_grant)
    # An ordinary owner-only fact still opens with its grant, as before.
    assert _fact_disclosure_allowed({"object_type": "fact", "disclosure": "owner_only"}, "redacted", every_grant)


def test_a_verified_claim_is_never_indexed_for_search(node, week):
    from topos.features.signal.derived_index import DERIVED_RECORD_TYPES, load_indexable_objects

    store.label_claim(node.conn, week.me, "true")
    assert "verified_claim" not in DERIVED_RECORD_TYPES
    assert all(o["object_type"] != "verified_claim" for o in load_indexable_objects(node.conn))


def test_share_side_readers_of_signal_objects_read_facts_or_named_ids_only():
    """Fail closed: every query in permissions_v2 (the share and recipient code) that reads signal_objects names
    ``object_type='fact'`` (or its own type parameter) or reads one object by id. A new reader of every object type
    would carry the owner's labels into a share, and fails here until it says what it reads."""
    root = Path(__file__).resolve().parents[2] / "topos" / "permissions_v2"
    allowed = re.compile(r"object_type\s*=\s*('fact'|\?)|WHERE object_id\s*=\s*\?")
    offenders = []
    for path in sorted(root.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "FROM signal_objects" in line and not allowed.search(" ".join(lines[i:i + 2])):
                offenders.append(f"{path.name}:{i + 1}")
    assert not offenders, offenders


def test_the_truth_tables_and_verified_claims_are_withheld_from_the_table_tools_for_all_but_the_owner(node, week):
    from topos.core.handlers import handle_control_plane_request
    from topos.data_explorer_tables import OWNER_ONLY_TABLES

    store.label_claim(node.conn, week.me, "true")
    node.conn.execute("INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, "
                      "valid_from, created_at, updated_at) VALUES ('f1', 'memory', 'fact', 'k', '{}', 'x', 'x', 'x')")
    node.conn.commit()
    assert {"truth_checks", "truth_labels"} <= OWNER_ONLY_TABLES

    def rows(principal, table):
        message = {"id": "r1", "type": "get_table_rows", "payload": {"table_name": table, "limit": 100}}
        return asyncio.run(handle_control_plane_request(message, principal=principal))

    for relay in (Principal(CP_RELAY, "cp_relay"), Principal("owner_automation", "cp_relay")):
        for table in ("truth_checks", "truth_labels"):
            assert rows(relay, table)["error"] == "owner_mode_required"
        objects = rows(relay, "signal_objects")["payload"]["rows"]
        assert [o["object_type"] for o in objects] == ["fact"]
    owner = Principal(OWNER_APP, "uds")
    assert len(rows(owner, "truth_checks")["payload"]["rows"]) == 3
    assert sorted(o["object_type"] for o in rows(owner, "signal_objects")["payload"]["rows"]) == ["fact", "verified_claim"]


# --- a claim does not outlive its record --------------------------------------------------------------------------------
def test_removing_a_record_removes_its_check_label_and_verified_claim(node, week):
    from topos.features.lifecycle.derived_scrub import purge_derived_for_records

    store.label_claim(node.conn, week.me, "true")
    store.label_claim(node.conn, week.them, "false")
    node.conn.execute("DELETE FROM conversation_messages WHERE message_id='m-me'")
    node.conn.commit()
    report = purge_derived_for_records(node.conn, ["m-me"])
    assert report["truth_checks_removed"] == 1
    assert [r[0] for r in node.conn.execute("SELECT record_id FROM truth_checks ORDER BY record_id")] == ["m-ok", "m-them"]
    assert [r[0] for r in node.conn.execute("SELECT check_id FROM truth_labels")] == [week.them]
    assert [json.loads(o["payload"])["check_id"] for o in _objects(node.conn)] == [week.them]


def test_a_source_scrub_removes_the_checks_whose_records_are_gone(node, week):
    store.label_claim(node.conn, week.them, "true")
    node.conn.execute("DELETE FROM conversation_messages WHERE message_id IN ('m-them', 'm-ok')")
    node.conn.commit()
    assert store.forget_records(node.conn) == 2
    assert [r[0] for r in node.conn.execute("SELECT record_id FROM truth_checks")] == ["m-me"]
    assert node.conn.execute("SELECT COUNT(*) FROM truth_labels").fetchone()[0] == 0 and _objects(node.conn) == []
    assert store.forget_records(node.conn) == 0

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
    # The contact's claim is not "to confirm": the cards show the owner's own claims by default (step 1).
    assert {k: this[k] for k in ("checked", "backed", "nothing_found", "unable", "not_a_claim", "labelled", "dismissed",
                                 "to_confirm")} == \
        {"checked": 3, "backed": 1, "nothing_found": 1, "unable": 0, "not_a_claim": 1, "labelled": 1, "dismissed": 0,
         "to_confirm": 0}
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
    assert entry["rechecked"] == 0 and entry["attempts_exhausted"] == 0


def _set_attempts(conn, check_id, attempts, verdict=None):
    conn.execute("UPDATE truth_checks SET attempts=?, verdict=COALESCE(?, verdict) WHERE check_id=?",
                 (attempts, verdict, check_id))
    conn.commit()


def test_weekly_stats_count_rows_checked_again_and_rows_out_of_attempts(node, week):
    add_message(node.conn, "m-late", "I fixed the shed door hinges after lunch.", from_self=True, at=ago(hours=3))
    add_message(node.conn, "m-later", "I swept out the whole shed before dinner.", from_self=True, at=ago(hours=2))
    node.conn.commit()
    late = _check(node.conn, "conversation_messages", "m-late", "imessage", True, verdict="unable", claim=None)
    later = _check(node.conn, "conversation_messages", "m-later", "imessage", True, verdict="unable", claim=None)
    _set_attempts(node.conn, week.them, 2)                      # answered on its second check
    _set_attempts(node.conn, late, store.MAX_ATTEMPTS)          # still unable after the third
    _set_attempts(node.conn, later, 2)                          # unable, one check left
    [entry] = store.weekly_stats(node.conn, 1)["weeks"]
    assert entry["checked"] == 5 and entry["unable"] == 2
    assert entry["rechecked"] == 3 and entry["attempts_exhausted"] == 1
    text = json.dumps(entry)
    for secret in ("m-late", late, "hinges"):
        assert secret not in text


# --- the cards --------------------------------------------------------------------------------------------------------
def test_cards_are_the_claims_newest_first_with_snippets_from_the_cited_records(node, week):
    assert [c["check_id"] for c in store.claim_cards(node.conn, 10, True)["cards"]] == [week.me]  # the owner's, by default
    cards = store.claim_cards(node.conn, 10, True, "all")["cards"]
    assert [c["check_id"] for c in cards] == [week.them, week.me]  # the not-a-claim row is no card
    them, me = cards
    assert them["speaker"] == "Robin Example" and me["speaker"] == "you"
    assert them["claim_text"] == CLAIM_THEM and them["verdict"] == "no_evidence" and them["category"] == "people"
    assert them["source"] == "imessage" and them["latency_ms"] == 2100.0 and them["cited"] == []
    assert datetime.fromisoformat(me["date"]).utcoffset() is not None
    [cited] = me["cited"]
    assert cited["source"] == "journal" and cited["role"] == "authored" and cited["date"] == "2026-10-22"
    assert cited["snippet"].startswith("Painted both shed doors a deep blue today.") and len(cited["snippet"]) == 400
    assert set(me) == {"check_id", "speaker", "speaker_is_owner", "source", "date", "said", "claim_text", "category",
                       "verdict", "cited", "latency_ms", "label", "note", "evidence_ok", "relevance", "dismiss_reason",
                       "contradicted", "polarity", "kind", "check_ids", "restated", "due_at", "plan"}
    assert me["speaker_is_owner"] is True and them["speaker_is_owner"] is False
    assert me["polarity"] is None and me["kind"] is None and me["check_ids"] == []  # a row with no reading is its own group
    assert me["due_at"] is None and me["plan"] is None  # not a plan
    assert me["restated"] == {"count": 1, "distinct_days": 1, "first": me["date"], "last": me["date"]}
    assert me["said"] and me["said"] != me["claim_text"]  # what was said, beside the claim taken from it


def test_a_labelled_claim_leaves_the_unlabelled_cards(node, week):
    store.label_claim(node.conn, week.them, "partly", "roof yes, gutters no")
    assert [c["check_id"] for c in store.claim_cards(node.conn, 10, True, "all")["cards"]] == [week.me]
    everything = store.claim_cards(node.conn, 10, False, "all")["cards"]
    assert [(c["check_id"], c["label"]) for c in everything] == [(week.them, "partly"), (week.me, None)]


def test_a_deleted_record_drops_its_card_and_a_deleted_cited_record_drops_its_snippet(node, week):
    node.conn.execute("DELETE FROM journal_entries WHERE entry_id='j-cited'")
    node.conn.execute("DELETE FROM conversation_messages WHERE message_id='m-them'")
    node.conn.commit()
    [card] = store.claim_cards(node.conn, 10, True)["cards"]
    assert card["check_id"] == week.me and card["cited"] == []


@pytest.mark.parametrize("limit, unlabelled_only", [(0, True), (501, True), ("5", True), (5, "yes")])
def test_cards_take_a_limit_of_five_hundred(node, limit, unlabelled_only):
    with pytest.raises(store.TruthRequestError):
        store.claim_cards(node.conn, limit, unlabelled_only)


def test_every_open_claim_is_returned_and_the_app_chooses_how_many_to_show(node):
    """The node returns every open claim (the owner sees everything; the app shows 20 a week). The 50 cap this replaces
    held 105 waiting cards behind it on 27 Oct 2026."""
    for i in range(60):
        add_message(node.conn, f"m-{i}", f"I painted shed door number {i} blue.", from_self=True, at=ago(minutes=i + 1))
    node.conn.commit()
    for i in range(60):
        _check(node.conn, "conversation_messages", f"m-{i}", "imessage", True, claim=f"The owner painted shed door {i} blue.")
    assert store.MAX_CARDS == 500
    assert len(store.claim_cards(node.conn, 500, True)["cards"]) == 60
    assert len(store.claim_cards(node.conn, 20, True)["cards"]) == 20


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
                                  "window_days": 7, "paused_until": None}
    widened = _as(owner, "truth_inbound_settings", {"set": {"window_days": 365}})
    assert widened["payload"]["window_days"] == 365 and widened["payload"]["rate_per_minute"] == 3
    assert node.conn.execute("SELECT COUNT(*) FROM pipeline_jobs WHERE kind=?", (lane.KIND,)).fetchone()[0] == 1
    for bad in ({"set": {"model": "gpt"}}, {"set": {"window_days": 0}}, {"set": "on"}, {"get": True, "drop": 1}, "get"):
        assert _as(owner, "truth_inbound_settings", bad) == {"id": "r1", "status": "error", "error": "invalid_truth_settings"}


def test_the_owner_reads_stats_and_cards_and_labels_through_the_doors(node, week):
    owner = Principal(OWNER_APP, "uds")
    stats = _as(owner, "truth_weekly_stats", {"weeks": 1})
    assert stats["status"] == "ok" and stats["payload"]["enabled"] is False
    assert stats["payload"]["weeks"][0]["checked"] == 3
    assert _as(owner, "truth_weekly_stats", {"weeks": 9})["error"] == "invalid_truth_request"
    cards = _as(owner, "truth_claim_cards", {"limit": 50, "unlabelled_only": True, "speakers": "all"})["payload"]["cards"]
    assert [c["check_id"] for c in cards] == [week.them, week.me]
    assert _as(owner, "truth_claim_cards", {"speakers": "contacts"})["error"] == "invalid_truth_request"
    assert _as(owner, "truth_claim_cards", {"limit": 501})["error"] == "invalid_truth_request"
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


def test_the_cli_reads_a_table_an_earlier_build_created_without_changing_it(node, monkeypatch):
    """The CLI's connection is read-only, so it cannot add the re-check columns: it reads the 1.5.19 table as it is."""
    from click.testing import CliRunner

    from topos.cli.truth_cmd import truth_group

    node.conn.execute("CREATE TABLE truth_checks (check_id TEXT PRIMARY KEY, record_table TEXT NOT NULL, record_id TEXT "
                      "NOT NULL, conversation_id TEXT, source TEXT NOT NULL, speaker_kind TEXT NOT NULL, week TEXT NOT NULL, "
                      "category TEXT, claim_text TEXT, verdict TEXT NOT NULL, reason TEXT, cited_refs_json TEXT NOT NULL "
                      "DEFAULT '[]', effort TEXT, model TEXT, latency_ms REAL, created_at TEXT NOT NULL)")
    node.conn.execute("CREATE TABLE truth_labels (label_id TEXT PRIMARY KEY, check_id TEXT NOT NULL UNIQUE, label TEXT "
                      "NOT NULL, note TEXT, labelled_at TEXT NOT NULL)")
    node.conn.execute("INSERT INTO truth_checks (check_id, record_table, record_id, source, speaker_kind, week, verdict, "
                      "created_at) VALUES ('c1', 'journal_entries', 'j1', 'journal', 'owner', ?, 'unable', ?)",
                      (store.iso_week(), datetime.now(timezone.utc).isoformat()))
    node.conn.commit()
    monkeypatch.setattr("topos.storage.db.paths.resolve_active_database", lambda *a, **k: SimpleNamespace(path=node.path))
    before = node.path.read_bytes()
    result = CliRunner().invoke(truth_group, ["weekly", "--json"])
    assert result.exit_code == 0, result.output
    entry = json.loads(result.output)["weeks"][0]
    assert entry["unable"] == 1 and entry["rechecked"] == 0 and entry["attempts_exhausted"] == 0
    assert node.path.read_bytes() == before


def test_the_cli_says_how_many_rows_were_checked_again(node, week, monkeypatch):
    from click.testing import CliRunner

    from topos.cli.truth_cmd import truth_group

    _set_attempts(node.conn, week.them, 2)
    monkeypatch.setattr("topos.storage.db.paths.resolve_active_database", lambda *a, **k: SimpleNamespace(path=node.path))
    result = CliRunner().invoke(truth_group, ["weekly"])
    assert result.exit_code == 0 and "checked again: 1; still unable after the last check allowed: 0" in result.output


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
    from topos.data_explorer_tables import OWNER_ONLY_TABLE_PREFIXES, OWNER_ONLY_TABLES, is_owner_only_table

    store.label_claim(node.conn, week.me, "true")
    node.conn.execute("INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, "
                      "valid_from, created_at, updated_at) VALUES ('f1', 'memory', 'fact', 'k', '{}', 'x', 'x', 'x')")
    node.conn.commit()
    # Every table the lane's DDL creates is owner-only, by the ``truth_`` prefix: a table a later build adds cannot miss
    # the rule. Until 27 Oct 2026 the rule was a two-name set, and truth_dismissals (the owner's free-text note),
    # truth_skips and truth_lane_daily were served to any principal by the legacy table tools.
    assert {"truth_checks", "truth_labels"} <= OWNER_ONLY_TABLES and OWNER_ONLY_TABLE_PREFIXES == ("truth_",)
    truth_tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", "\n".join(store._DDL))
    assert set(truth_tables) == {"truth_checks", "truth_labels", "truth_dismissals", "truth_skips", "truth_lane_daily"}
    assert all(is_owner_only_table(t) for t in truth_tables) and is_owner_only_table("truth_anything_later")
    assert not is_owner_only_table("conversation_messages") and not is_owner_only_table("truthiness")
    assert all(store._present(node.conn, t) for t in truth_tables)

    def rows(principal, table):
        message = {"id": "r1", "type": "get_table_rows", "payload": {"table_name": table, "limit": 100}}
        return asyncio.run(handle_control_plane_request(message, principal=principal))

    for relay in (Principal(CP_RELAY, "cp_relay"), Principal("owner_automation", "cp_relay")):
        for table in truth_tables:
            assert rows(relay, table)["error"] == "owner_mode_required", table
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


# --- Calibrate step 1: dismissal and the evidence question ------------------------------------------------------------
def _dismissals(conn):
    return conn.execute("SELECT check_id, reason, note FROM truth_dismissals").fetchall()


def test_a_dismissal_takes_the_card_out_and_writes_no_verified_claim(node, week):
    out = store.dismiss_claim(node.conn, week.me, "trivial", "  not worth it ")
    assert out["ok"] is True and out["dismissal_id"]
    assert _dismissals(node.conn) == [(week.me, "trivial", "not worth it")] and _objects(node.conn) == []
    assert store.claim_cards(node.conn, 10, True)["cards"] == []
    [card] = store.claim_cards(node.conn, 10, False)["cards"]
    assert (card["relevance"], card["dismiss_reason"], card["label"]) == ("dismissed", "trivial", None)
    [entry] = store.weekly_stats(node.conn, 1)["weeks"]
    assert entry["dismissed"] == 1 and entry["labelled"] == 0 and entry["to_confirm"] == 0


def test_a_label_and_a_dismissal_replace_each_other(node, week):
    store.label_claim(node.conn, week.me, "true")
    assert len(_objects(node.conn)) == 1
    first = store.dismiss_claim(node.conn, week.me, "misread")
    assert node.conn.execute("SELECT COUNT(*) FROM truth_labels").fetchone()[0] == 0 and _objects(node.conn) == []
    again = store.dismiss_claim(node.conn, week.me, "other", "changed my mind")
    assert again["dismissal_id"] == first["dismissal_id"] and _dismissals(node.conn) == [(week.me, "other", "changed my mind")]
    store.label_claim(node.conn, week.me, "partly", evidence_ok=False)
    assert _dismissals(node.conn) == [] and len(_objects(node.conn)) == 1
    [card] = store.claim_cards(node.conn, 10, False)["cards"]
    assert (card["relevance"], card["label"], card["evidence_ok"]) == ("relevant", "partly", False)


@pytest.mark.parametrize("call", [
    lambda c, w: store.dismiss_claim(c, w.me, "boring"),
    lambda c, w: store.dismiss_claim(c, w.me, None),
    lambda c, w: store.label_claim(c, w.me, "true", evidence_ok="yes"),
])
def test_a_bad_dismissal_or_evidence_answer_is_refused(node, week, call):
    with pytest.raises(store.TruthRequestError) as raised:
        call(node.conn, week)
    assert str(raised.value) == "invalid_truth_label"


def test_a_table_from_before_step_1_gains_the_evidence_column_and_reads_without_dismissals(node, week):
    node.conn.execute("DROP TABLE IF EXISTS truth_dismissals")
    node.conn.execute("DROP TABLE IF EXISTS truth_skips")
    node.conn.execute("ALTER TABLE truth_labels DROP COLUMN evidence_ok")
    node.conn.commit()
    [card] = store.claim_cards(node.conn, 10, True)["cards"]  # a read creates nothing
    assert card["evidence_ok"] is None and card["relevance"] is None
    assert store.weekly_stats(node.conn, 1)["weeks"][0]["dismissed"] == 0
    assert store.weekly_stats(node.conn, 1)["weeks"][0]["skipped"] == 0
    store.label_claim(node.conn, week.me, "true", evidence_ok=True)  # the first write adds what is missing
    assert node.conn.execute("SELECT evidence_ok FROM truth_labels").fetchone() == (1,)
    assert store._present(node.conn, "truth_dismissals")


def test_the_owner_dismisses_through_the_door(node, week):
    owner = Principal(OWNER_APP, "uds")
    out = _as(owner, "truth_claim_label", {"check_id": week.me, "relevance": "dismissed", "dismiss_reason": "someone_else"})
    assert out["status"] == "ok" and out["payload"]["dismissal_id"]
    assert _as(owner, "truth_claim_label", {"check_id": week.me, "relevance": "maybe"})["error"] == "invalid_truth_label"
    assert _as(owner, "truth_claim_label", {"check_id": week.me, "relevance": "dismissed"})["error"] == "invalid_truth_label"
    back = _as(owner, "truth_claim_label", {"check_id": week.me, "relevance": "relevant", "label": "true", "evidence_ok": True})
    assert back["status"] == "ok" and back["payload"]["label_id"] and _dismissals(node.conn) == []


# --- skips are recorded (owner, 27 Oct 2026) --------------------------------------------------------------------------
def _skips(conn):
    return conn.execute("SELECT check_id, reason, evidence_opened FROM truth_skips ORDER BY skipped_at, skip_id").fetchall()


def test_a_skip_is_logged_each_time_and_the_card_stays(node, week):
    first = store.skip_claim(node.conn, week.me, "need_context", evidence_opened=True)
    store.skip_claim(node.conn, week.me)  # skipped again, without saying why
    assert first["ok"] is True and first["skip_id"]
    assert sorted(_skips(node.conn), key=str) == sorted([(week.me, "need_context", 1), (week.me, None, None)], key=str)
    assert [c["check_id"] for c in store.claim_cards(node.conn, 10, True)["cards"]] == [week.me]
    assert node.conn.execute("SELECT COUNT(*) FROM truth_labels").fetchone()[0] == 0 and _objects(node.conn) == []
    [entry] = store.weekly_stats(node.conn, 1)["weeks"]
    assert entry["skipped"] == 1 and entry["to_confirm"] == 1  # a card, not a skip count; still waiting


@pytest.mark.parametrize("reason, opened", [("bored", None), ("dont_know", "yes")])
def test_a_bad_skip_is_refused(node, week, reason, opened):
    with pytest.raises(store.TruthRequestError) as raised:
        store.skip_claim(node.conn, week.me, reason, opened)
    assert str(raised.value) == "invalid_truth_label"


def test_a_removed_record_takes_its_skips_with_it(node, week):
    store.skip_claim(node.conn, week.me, "not_now")
    assert store.forget_records(node.conn, ["m-me"]) == 1
    assert _skips(node.conn) == []


def test_the_owner_skips_through_the_door(node, week):
    owner = Principal(OWNER_APP, "uds")
    out = _as(owner, "truth_claim_label", {"check_id": week.me, "relevance": "skipped", "skip_reason": "unclear_claim",
                                           "evidence_opened": False})
    assert out["status"] == "ok" and out["payload"]["skip_id"]
    assert _skips(node.conn) == [(week.me, "unclear_claim", 0)]
    assert _as(owner, "truth_claim_label", {"check_id": week.me, "relevance": "skipped", "skip_reason": "x"})["error"] == "invalid_truth_label"


# --- a record id is unique within a table, not across them (27 Oct 2026) ----------------------------------------------
def test_a_bare_record_id_two_tables_share_forgets_only_the_record_that_is_gone(node):
    """``forget_records`` is keyed on (table, id). The owner's "remove this" flow knows ids only, so a bare id that
    two checked tables' rows carry removes the check whose record is gone from its own table and keeps the other
    (until 27 Oct 2026 it removed both). A (table, id) pair names its check outright."""
    add_journal(node.conn, "dup-1", "I painted both shed doors a deep blue today.")
    add_message(node.conn, "dup-1", "I painted the shed doors blue yesterday afternoon.", from_self=True)
    node.conn.commit()
    journal = _check(node.conn, "journal_entries", "dup-1", "journal", True)
    message = _check(node.conn, "conversation_messages", "dup-1", "imessage", True)
    store.label_claim(node.conn, message, "true")
    assert store.forget_records(node.conn, ["dup-1"]) == 0  # both records still exist: the id names neither
    node.conn.execute("DELETE FROM conversation_messages WHERE message_id='dup-1'")
    node.conn.commit()
    assert store.forget_records(node.conn, ["dup-1"]) == 1
    assert [r[0] for r in node.conn.execute("SELECT check_id FROM truth_checks")] == [journal]
    assert node.conn.execute("SELECT COUNT(*) FROM truth_labels").fetchone()[0] == 0 and _objects(node.conn) == []
    assert store.forget_records(node.conn, [("journal_entries", "dup-1")]) == 1  # a pair names its table outright
    assert node.conn.execute("SELECT COUNT(*) FROM truth_checks").fetchone()[0] == 0
    assert store.forget_records(node.conn, [("not_a_checked_table", "dup-1"), "", ("journal_entries", "")]) == 0


# --- the public reads are blind to the lane (lean v3 increment A, 27 Oct 2026) ------------------------------------------
PUBLIC_STATEMENTS = ("I love playing the mandolin", "I have never played the mandolin", "I play tennis every week",
                     "I hate cilantro", "I only listen to vinyl records", "I was never diagnosed with an allergy")


def _seed_fun_fact(conn):
    from topos.features.facts.store import FactStore

    FactStore(conn).assert_fact(subject_entity_id="self-1", predicate="enjoys", object_value="playing the mandolin",
                                dimension="interests", confidence=0.8, source_refs=[], disclosure="scoped")
    conn.commit()


def _public_reads(conn):
    """Both reads the truth door opens to the CP relay and enrolled clients, as the caller sees them (no ``_audit``)."""
    from topos.query.truth_prompts import suggest_prompts
    from topos.query.verify import verify_claim

    out = {}
    for statement in PUBLIC_STATEMENTS:
        result = verify_claim(conn, statement=statement, mode="fun", caller_app_id="tru-test-app")
        result.pop("_audit", None)
        out[statement] = result
    prompts = suggest_prompts(conn, mode="fun", caller_app_id="tru-test-app", limit=8)
    prompts.pop("_audit", None)
    out["prompts"] = prompts
    return json.dumps(out, sort_keys=True)


def test_the_public_reads_answer_the_same_bytes_whatever_the_lane_holds(node):
    """verify_claim and truth_prompts read the fact store and nothing of the lane: the owner's own utterance, its
    negation, five repeats and a label ruled true change not one byte of either answer, and neither path touches a
    truth_ table. The label writes a ``verified_claim`` into signal_objects, which the public path legitimately reads
    for ``object_type = 'fact'``, so the bytes are the property; a trace alone could not see that route."""
    _seed_fun_fact(node.conn)
    before = _public_reads(node.conn)
    assert '"stance": "contradicts"' in before  # the seeded fact is found: the reads are not trivially empty
    texts = ["I love playing the mandolin", "I have never played the mandolin"] + ["I love playing the mandolin"] * 5
    ids = [_check(node.conn, "conversation_messages", f"m-{i}", "imessage", True, claim=text, category="view")
           for i, text in enumerate(texts)]
    store.label_claim(node.conn, ids[0], "true", evidence_ok=True)
    assert node.conn.execute("SELECT COUNT(*) FROM truth_checks").fetchone()[0] == 7 and len(_objects(node.conn)) == 1
    statements = []
    node.conn.set_trace_callback(statements.append)
    try:
        after = _public_reads(node.conn)
    finally:
        node.conn.set_trace_callback(None)
    assert after == before
    assert {m for s in statements for m in re.findall(r"\btruth_\w+", s)} == set()
    assert any("signal_objects" in s for s in statements)


class _OwnerChat:
    """The owner engine's model, stood in: one claim about the shed doors, and "shows" for the one record offered."""

    name = "fake"

    def __call__(self, system, user, schema, timeout, usage):
        from topos.query import verify_v2 as v2

        usage.add(1, 1)
        if system in (v2.READ_SYSTEM, v2.READ_SYSTEM_WITH_OTHERS):
            return {"category": "did", "not_checkable_because": "", "claims": [
                {"type": "did_or_made", "subject": "owner", "reported_speech": False,
                 "claim": "The owner painted the shed doors blue.", "names": ["shed"], "date_from": "", "date_to": ""}]}
        return {"labels": [{"n": 1, "label": "shows"}]}


def test_the_request_paths_write_nothing_even_with_labels_as_evidence_on(node, week, monkeypatch):
    """verify_claim, truth_prompts and verify_claim_owner are reads. With LABELS_AS_EVIDENCE and CONSISTENCY on, the
    owner path reads the owner's labels as evidence (one label shown, cited, supported) and still issues no INSERT,
    UPDATE or DELETE; and only a label the OWNER wrote counts: a ``verified_claim`` some engine wrote with the same
    payload is not evidence (``owner_labels`` requires ``created_by = 'owner'``)."""
    from topos.query import verify_v2 as v2
    from topos.query.truth_prompts import suggest_prompts
    from topos.query.verify import verify_claim

    _seed_fun_fact(node.conn)
    store.label_claim(node.conn, week.me, "true")  # CLAIM_ME, the owner's own judgement
    node.conn.execute("INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, confidence, "
                      "source_refs_json, valid_from, created_at, updated_at, created_by) SELECT 'engine-copy', signal_dimension, "
                      "object_type, 'truth_check:engine', payload_json, confidence, source_refs_json, valid_from, created_at, "
                      "updated_at, 'derivation' FROM signal_objects WHERE object_type='verified_claim'")
    node.conn.commit()
    assert len(_objects(node.conn)) == 2 and len(v2.owner_labels(node.conn)) == 1
    monkeypatch.setattr(v2, "LABELS_AS_EVIDENCE", True)
    monkeypatch.setattr(v2, "CONSISTENCY", True)

    class NoRecords:  # retrieval stood in: the only evidence on offer is the owner's label
        def __init__(self, conn):
            pass

        def gather(self, query, needles, now):
            return [], {"candidates": 0}

    monkeypatch.setattr(v2, "NodeEvidence", NoRecords)

    async def gather(fn):
        return fn(node.conn)

    changes, statements = node.conn.total_changes, []
    node.conn.set_trace_callback(statements.append)
    try:
        verify_claim(node.conn, statement="I love playing the mandolin", mode="fun", caller_app_id="tru-test-app")
        suggest_prompts(node.conn, mode="fun", caller_app_id="tru-test-app")
        request = v2.ClaimRequest(statement="I got the shed doors painted blue earlier.", speaker="owner", speaker_is_owner=True)
        out = asyncio.run(v2.verify_owner_claim(request, chat=_OwnerChat(), gather_in_worker=gather, to_thread=asyncio.to_thread))
    finally:
        node.conn.set_trace_callback(None)
    assert out["verdict"] == "supported" and out["stages"]["retrieve"]["owner_labels"] == 1
    assert [e["source"] for e in out["evidence"] if e["cited"]] == ["verified_claim"]
    writes = [s for s in statements if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b", s, re.I)]
    assert writes == [] and node.conn.total_changes == changes and len(_objects(node.conn)) == 2


# --- said before: one card per group of the owner's utterances (lean v3 increment B, 27 Oct 2026) -------------------------
def _reading(text, names, polarity, kind="did_or_made", category="did"):
    return {"category": category, "claims": [{"type": kind, "subject": "owner", "reported_speech": False, "claim": text,
                                              "names": names, "date_from": "", "date_to": "", "as_written": "",
                                              "polarity": {1: "affirms", -1: "denies"}.get(polarity)}]}


def _said(conn, record_id, said, *, claim, names, polarity, hours_ago, verdict="no_evidence", shown=None):
    """One owner message and its check, with what the reader made of it: the lane's row for increment B."""
    add_message(conn, record_id, said, from_self=True, at=ago(hours=hours_ago))
    conn.commit()
    result = {"verdict": verdict, "reason": None, "category": "did", "claim": {"text": claim}, "evidence": [],
              "timings_ms": {"total": 900.0}, "reading": _reading(claim, names, polarity),
              **({"stages": {"retrieve": {"owner_labels": shown}}} if shown is not None else {})}
    record = {"table": "conversation_messages", "record_id": record_id, "source": "imessage", "owner": True}
    return store.record_check(conn, lane.check_row(record, result, model="local:test", said_at=store.with_offset(ago(hours=hours_ago))))


PINWHEEL_KEY = "did|did_or_made|none|day every pinwheel"  # claim_key of "The owner uses Pinwheel every day." named Pinwheel ("uses" is a stop word)


def test_repeated_statements_make_one_card_with_the_count_and_a_negation_makes_another(node):
    yes = [_said(node.conn, f"m-yes-{i}", f"I use Pinwheel every day, said again {i}.", claim="The owner uses Pinwheel every day.",
                 names=["Pinwheel"], polarity=1, hours_ago=30 * i + 1) for i in range(3)]
    no = _said(node.conn, "m-no", "I never use Pinwheel.", claim="The owner uses Pinwheel every day.", names=["Pinwheel"], polarity=-1, hours_ago=2)
    plain = _said(node.conn, "m-plain", "I watered the Monstera today.", claim="The owner watered the Monstera.", names=["Monstera"], polarity=None, hours_ago=3)
    row = node.conn.execute("SELECT claim_key, polarity, said_at, reading_json FROM truth_checks WHERE check_id=?", (yes[0],)).fetchone()
    assert row[0] == PINWHEEL_KEY and row[1] == 1 and row[2] and json.loads(row[3])["claims"][0]["polarity"] == "affirms"
    cards = store.claim_cards(node.conn, 10, True)["cards"]
    assert [c["check_id"] for c in cards] == [yes[0], no, plain]  # newest utterance of each group leads
    said, denied, single = cards
    assert said["check_ids"] == [yes[1], yes[2]] and said["polarity"] == 1 and said["kind"] == "did_or_made"
    assert said["restated"]["count"] == 3 and said["restated"]["distinct_days"] >= 2
    assert said["restated"]["first"] < said["restated"]["last"] == said["date"]
    assert denied["check_ids"] == [] and denied["polarity"] == -1 and denied["restated"]["count"] == 1
    assert single["polarity"] is None and single["check_ids"] == [] and single["restated"]["count"] == 1
    assert store.occurrence_refs(node.conn, PINWHEEL_KEY, 1, own="conversation_messages:m-yes-0") == [
        "conversation_messages:m-yes-1", "conversation_messages:m-yes-2"]
    assert store.occurrence_refs(node.conn, PINWHEEL_KEY, -1) == ["conversation_messages:m-no"]
    assert store.occurrence_refs(node.conn, PINWHEEL_KEY, None) == []
    [entry] = store.weekly_stats(node.conn, 1)["weeks"]
    assert (entry["groups"], entry["grouped"], entry["splits"]) == (3, 2, 0)  # the no-polarity row is its own group
    assert entry["restated_distribution"] == {"3": 1, "1": 2} and entry["opposite_polarity_pairs"] == 1
    assert entry["distinct_days_distribution"]["1"] == 2 and sum(entry["distinct_days_distribution"].values()) == 3


def test_a_split_leaves_the_group_and_keeps_the_label_and_the_card(node):
    first = _said(node.conn, "m-a", "I use Pinwheel daily.", claim="The owner uses Pinwheel every day.", names=["Pinwheel"], polarity=1, hours_ago=20)
    second = _said(node.conn, "m-b", "I use the Pinwheel app most days.", claim="The owner uses Pinwheel every day.", names=["Pinwheel"], polarity=1, hours_ago=1)
    store.label_claim(node.conn, first, "true")
    [card] = store.claim_cards(node.conn, 10, False)["cards"]
    assert card["check_id"] == second and card["check_ids"] == [first] and card["restated"]["count"] == 2
    assert store.split_claim(node.conn, first) == {"ok": True, "check_id": first}
    cards = store.claim_cards(node.conn, 10, False)["cards"]
    assert [(c["check_id"], c["check_ids"], c["restated"]["count"], c["label"]) for c in cards] == [(second, [], 1, None), (first, [], 1, "true")]
    assert node.conn.execute("SELECT COUNT(*) FROM truth_labels").fetchone()[0] == 1 and len(_objects(node.conn)) == 1  # nothing deleted
    assert store.occurrence_refs(node.conn, PINWHEEL_KEY, 1) == ["conversation_messages:m-b"]
    [entry] = store.weekly_stats(node.conn, 1)["weeks"]
    assert entry["splits"] == 1 and entry["groups"] == 2 and entry["grouped"] == 0
    owner = Principal(OWNER_APP, "uds")
    assert _as(owner, "truth_claim_label", {"check_id": second, "relevance": "split"})["payload"] == {"ok": True, "check_id": second}
    assert _as(owner, "truth_claim_label", {"check_id": "nope", "relevance": "split"})["error"] == "unknown_check"
    with pytest.raises(store.TruthRequestError):
        store.split_claim(node.conn, "")


def test_the_lane_row_carries_how_many_owner_labels_the_check_was_shown(node):
    """The export scores accuracy only on rows with owner_labels_shown = 0: a check that retrieved with
    LABELS_AS_EVIDENCE off reads 0, one that was shown two labels reads 2, and a row that never reached retrieval (the
    gate or the reader refused it, or a deadline came first) reads NULL."""
    shown = _said(node.conn, "m-s", "I merged the Pinwheel branch.", claim="The owner merged the Pinwheel branch.", names=["Pinwheel"], polarity=1, hours_ago=1, shown=2)
    none = _said(node.conn, "m-n", "I watered the Monstera.", claim="The owner watered the Monstera.", names=["Monstera"], polarity=1, hours_ago=2, shown=0)
    unread = _said(node.conn, "m-u", "I fixed the Orbit build.", claim="The owner fixed the Orbit build.", names=["Orbit"], polarity=1, hours_ago=3)
    rows = dict(node.conn.execute("SELECT check_id, owner_labels_shown FROM truth_checks").fetchall())
    assert rows[shown] == 2 and rows[none] == 0 and rows[unread] is None


# --- verify_claim usage on the Daily overview ---------------------------------------------------------------------
CHECK = {"statement": "I have never played chess", "mode": "fun", "caller_app_id": "truth-mirror"}


@pytest.fixture
def answering(node, monkeypatch):
    """verify_claim answers without reading anything, and the node is linked to an owner id."""
    monkeypatch.setattr("topos.query.verify.verify_claim",
                        lambda conn, **kw: {"stance": "unknown", "_audit": {"mode": kw["mode"]}})
    from topos.core.state import set_engine_config_value

    set_engine_config_value(node.conn, "user_id", "owner-1")
    return node


def _usage(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mcp_request_log'").fetchone():
        return []
    return [tuple(row) for row in conn.execute(
        "SELECT tool_name, source, requester_id, access_context, verified_cls, verified_client_id FROM mcp_request_log "
        "ORDER BY id")]


def test_an_answered_verify_claim_is_one_request_log_row_the_daily_overview_names(answering):
    from topos.dashboard.measurements import daily_measurements

    out = _as(Principal(CP_RELAY, "cp_relay"), "verify_claim", CHECK)
    assert out["status"] == "ok" and "_audit" not in out["payload"]
    assert _usage(answering.conn) == [("verify_claim", "truth-mirror", "owner-1", "owner_self", "cp_relay", None)]

    today = datetime.now(timezone.utc).date()
    report = daily_measurements(answering.conn, today.isoformat(), (today + timedelta(days=1)).isoformat())
    day = report["callers"]["buckets"][0]
    assert day["callers"] == [{"source": "truth-mirror", "access_context": "owner_self", "count": 1}]
    assert [(call["tool"], call["source"]) for call in day["calls"]] == [("verify_claim", "truth-mirror")]


def test_the_owner_class_is_the_owners_own_use(answering):
    assert _as(Principal(OWNER_APP, "uds"), "verify_claim", CHECK)["status"] == "ok"
    assert _usage(answering.conn) == [("verify_claim", "truth-mirror", "owner-1", "owner_self", "owner_app", None)]


def test_an_allowlisted_local_client_is_recorded_as_itself_beside_the_app_it_names(answering, monkeypatch):
    monkeypatch.setenv("TOPOS_TRUTH_CLIENT_ALLOWLIST", "fun-cam-dev")
    assert _as(Principal(THIRD_PARTY, "local_http", client_id="fun-cam-dev"), "verify_claim", CHECK)["status"] == "ok"
    assert _usage(answering.conn) == [("verify_claim", "truth-mirror", "fun-cam-dev", "other", "third_party",
                                       "fun-cam-dev")]


def test_a_refused_or_failed_verify_claim_writes_no_usage_row(answering, monkeypatch):
    refused = _as(Principal(THIRD_PARTY, "cp_relay"), "verify_claim", CHECK)
    assert refused["error"] == "owner_mode_required"
    monkeypatch.setattr("topos.query.verify.verify_claim", lambda conn, **kw: {"error": "caller_app_id required"})
    assert _as(Principal(CP_RELAY, "cp_relay"), "verify_claim", CHECK)["status"] == "error"
    assert _usage(answering.conn) == []


def test_a_usage_row_that_cannot_be_written_never_fails_the_answer(answering, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("topos.core.handlers.common.record_mcp_request", broken)
    assert _as(Principal(CP_RELAY, "cp_relay"), "verify_claim", CHECK)["status"] == "ok"
    assert _usage(answering.conn) == []


@pytest.fixture
def owner_door(answering, monkeypatch):
    """verify_claim_owner answers without a model or retrieval: the engine stood in, its verdict given per test."""
    from topos.query import verify_v2, verify_v2_models

    verdict = {"verdict": "no_evidence", "reason": "no_record_shows_it"}

    async def answer(request, **kwargs):
        if verdict.get("raise"):
            raise RuntimeError("model down")
        return dict(verdict)

    monkeypatch.setattr(verify_v2, "verify_owner_claim", answer)
    monkeypatch.setattr(verify_v2_models, "chat_for_setting", lambda *a, **k: None)
    monkeypatch.setattr(verify_v2_models, "judges_for_setting", lambda *a, **k: None)
    return SimpleNamespace(node=answering, verdict=verdict)


OWNER_CHECK = {"statement": "I restyled the pinwheel sidebar.", "said_at": "2026-10-07T19:47:36-05:00",
               "context": [{"speaker": "", "text": "What did you change?"}], "effort": "high", "deadline_s": 30}


def test_an_owner_door_check_is_a_verify_claim_row_under_the_name_the_caller_gives(owner_door):
    """Owner, 29 Oct 2026: runs of the labelled transcripts should count on the Home card, labelled as tests. The owner
    door names no app of itself; a run says ``caller_app_id`` and the row carries it."""
    assert _as(Principal(OWNER_APP, "uds"), "verify_claim_owner", {**OWNER_CHECK, "caller_app_id": "dev-transcripts"})["status"] == "ok"
    assert _as(Principal(OWNER_APP, "uds"), "verify_claim_owner", OWNER_CHECK)["status"] == "ok"
    owner_door.verdict.update(verdict="unable", reason="deadline")  # an answer all the same: it was asked
    assert _as(Principal(OWNER_APP, "uds"), "verify_claim_owner", {**OWNER_CHECK, "caller_app_id": " dev-transcripts "})["status"] == "ok"
    assert _usage(owner_door.node.conn) == [
        ("verify_claim", "dev-transcripts", "owner-1", "owner_self", "owner_app", None),
        ("verify_claim", "owner-door", "owner-1", "owner_self", "owner_app", None),
        ("verify_claim", "dev-transcripts", "owner-1", "owner_self", "owner_app", None),
    ]


def test_an_owner_door_check_refused_invalid_or_failed_writes_no_row(owner_door):
    assert _as(Principal(CP_RELAY, "cp_relay"), "verify_claim_owner", OWNER_CHECK)["error"] == "owner_mode_required"
    for bad in ({**OWNER_CHECK, "caller_app_id": 7}, {**OWNER_CHECK, "deadline_s": 31}, {"caller_app_id": "dev-transcripts"}):
        assert _as(Principal(OWNER_APP, "uds"), "verify_claim_owner", bad)["error"] == "invalid_truth_request"
    owner_door.verdict["raise"] = True
    assert _as(Principal(OWNER_APP, "uds"), "verify_claim_owner", OWNER_CHECK)["error"] == "verification_unavailable"
    assert _usage(owner_door.node.conn) == []


@pytest.mark.parametrize("verdict,expected", [("supported", "true"), ("contradicted", "false"),
                                              ("no_evidence", "unanswerable"), ("unable", "unanswerable")])
def test_owner_verification_records_only_outcome_on_same_usage_row(owner_door, verdict, expected):
    owner_door.verdict.update(verdict=verdict)
    out = _as(Principal(OWNER_APP, "uds"), "verify_claim_owner", {**OWNER_CHECK, "caller_app_id": "dev-transcripts"})
    assert out["payload"]["verdict"] == verdict
    rows = owner_door.node.conn.execute("SELECT source, verification_outcome FROM mcp_request_log").fetchall()
    assert [tuple(r) for r in rows] == [("dev-transcripts", expected)]


def test_app_verification_records_lane_result_on_same_usage_row(answering, monkeypatch):
    monkeypatch.setattr("topos.query.verify.verify_claim", lambda conn, **kw: {
        "lanes": {"work": {"stance": "supports"}, "social": {"stance": "no_evidence"}}})
    assert _as(Principal(CP_RELAY, "cp_relay"), "verify_claim", CHECK)["status"] == "ok"
    assert [r[0] for r in answering.conn.execute("SELECT verification_outcome FROM mcp_request_log")] == ["true"]

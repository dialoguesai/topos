"""The reflection doors: candidates from the enabled packs' facts, the balanced reads, the composed conversation, the
labels and their owner-only object, and the three doors (owner class only, refused before the payload is read).

Synthetic only: a migrated temp database with invented facts and rows; never the live node.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.truth_lane_support import add_contact, add_journal, add_message, ago, bind, build_node, connect
from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
from topos.principal import CP_RELAY, OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.query import reflection as store
from topos.query.reflection_stances import STARTERS

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


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


def add_fact(conn, pack, predicate, value, *, at, refs, quote="", actor_role="authored", key=None):
    """A fact as the derivation writer stores one (the columns the sweep reads)."""
    payload = {"subject_entity_id": "owner", "predicate": predicate, "object_value": value.get("description") or predicate,
               "disclosure": "owner_only", "asserted_by": "owner", "actor_role": actor_role, "pack": pack,
               "altitude": "stated", "value_struct": value}
    if quote:
        payload["quote"] = quote
    oid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, confidence, "
        "source_refs_json, valid_from, created_at, updated_at, extractor_version, ontology_id, ontology_version, altitude) "
        "VALUES (?, 'profile', 'fact', ?, ?, 0.8, ?, ?, ?, ?, 'derivation:test', ?, '0.1.0', 'stated')",
        (oid, key or f"{predicate}:{oid[:8]}", json.dumps(payload), json.dumps(refs), at, at, at, pack))
    conn.commit()
    return oid


def day(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def world(node):
    """An invented owner with every reflection pack on and one fact per rule family."""
    conn = node.conn
    enable(conn, *store.REFLECTION_PACKS)
    add_contact(conn, "+15550000001", "Nadia Example")
    add_journal(conn, "j-dinner", "Dinner at Nadia's turned into four hours of talking. Left feeling completely at home.", at=day(13))
    add_journal(conn, "j-band", "Last night with the band was one of those nights. Haven't felt that alive in months.", at=day(27))
    add_journal(conn, "j-paper", "Paper accepted. Proud of this one.", at=day(18))
    add_journal(conn, "j-mark", "Mark and I got into it again about the schedule. Third time this month.", at=day(5))
    add_journal(conn, "j-mark2", "Schedule fight with Mark again, this time about the weekend.", at=day(40))
    add_journal(conn, "j-dad", "Still haven't told dad about the move. Every call I almost do.", at=day(9))
    add_journal(conn, "j-promise", "Told Wren I'd send the lake photos this week.", at=day(22))
    add_journal(conn, "j-theme", "The pottery course again. One day.", at=day(30))
    add_message(conn, "m-nadia", "you looked so happy on sunday!", at=day(12), sender_id="+15550000001")
    conn.commit()
    r = lambda table, rid, days: [{"table": table, "record_id": rid, "role": "authored", "date": (NOW - timedelta(days=days)).date().isoformat()}]
    add_fact(conn, "moments.joy", "moment.peak", {"kind": "connected", "description": "dinner at Nadia's", "with": ["Nadia"],
             "intensity": "strong", "where": "Nadia's kitchen"}, at=day(13), refs=r("journal_entries", "j-dinner", 13),
             quote="Left feeling completely at home", key="moment.peak:dinner")
    add_fact(conn, "moments.joy", "moment.peak", {"kind": "alive", "description": "the band night", "with": ["Nadia"],
             "intensity": "peak", "where": "Nadia's kitchen"}, at=day(27), refs=r("journal_entries", "j-band", 27),
             quote="Haven't felt that alive in months")
    add_fact(conn, "moments.joy", "moment.peak", {"kind": "accomplished", "description": "paper accepted", "intensity": "strong"},
             at=day(18), refs=r("journal_entries", "j-paper", 18), quote="Paper accepted. Proud of this one.")
    add_fact(conn, "moments.joy", "moment.gratitude", {"person": "Dana", "what": "getting me through the move", "acknowledged": False},
             at=day(60), refs=r("journal_entries", "j-dinner", 60))
    add_fact(conn, "moments.tension", "moment.tension", {"kind": "recurring_frustration", "with": "Mark", "context": "work",
             "description": "the schedule fight", "status": "open"}, at=day(5), refs=r("journal_entries", "j-mark", 5),
             quote="Mark and I got into it again about the schedule")
    add_fact(conn, "moments.tension", "moment.tension", {"kind": "recurring_frustration", "with": "Mark", "context": "family",
             "description": "the weekend fight", "status": "open"}, at=day(40), refs=r("journal_entries", "j-mark2", 40))
    add_fact(conn, "moments.tension", "moment.tension", {"kind": "avoided_conversation", "with": "dad", "context": "family",
             "description": "telling dad about the move", "status": "open"}, at=day(9), refs=r("journal_entries", "j-dad", 9),
             quote="Still haven't told dad about the move")
    add_fact(conn, "moments.tension", "moment.tension", {"kind": "misunderstanding", "with": "Nadia", "context": "relationship",
             "description": "the mix-up about the reading", "status": "processed"}, at=day(80), refs=r("journal_entries", "j-theme", 80))
    add_fact(conn, "obligations.commitments", "commit.made", {"counterparty": "Wren", "direction": "owed_by_owner",
             "description": "send the lake photos", "due": "this week", "status": "open"}, at=day(22),
             refs=r("journal_entries", "j-promise", 22), quote="I'd send the lake photos this week")
    add_fact(conn, "narrative.self", "self.recurring_theme", {"theme": "the pottery course", "evidence_kind": "returns_to_topic"},
             at=day(30), refs=r("journal_entries", "j-theme", 30))
    add_fact(conn, "narrative.self", "self.stated_change", {"domain": "writing", "from_state": "doubting the draft", "to_state": "working on it"},
             at=day(21), refs=r("journal_entries", "j-paper", 21))
    add_fact(conn, "life.events", "life.event", {"kind": "relocation", "description": "moved to the new flat"}, at=day(70),
             refs=r("journal_entries", "j-dad", 70))
    add_fact(conn, "relationships.social", "rel.relationship_event", {"person": "Priya", "event": "drifted",
             "description": "haven't talked since August"}, at=day(50), refs=r("journal_entries", "j-theme", 50))
    return node


# --- the sweep ---------------------------------------------------------------------------------------------------------
def test_the_sweep_builds_candidates_for_every_starter_from_the_enabled_packs(world):
    result = store.sweep(world.conn, now=NOW)
    assert result["packs"] == list(store.REFLECTION_PACKS) and result["facts"] == 13
    assert result["candidates"] >= 12 and result["retired"] == 0
    for starter in STARTERS:
        cards = store.invitations(world.conn, starter, 5, now=NOW)["cards"]
        assert cards, starter
        assert all(c["section_id"] in STARTERS[starter] for c in cards)


def test_the_rules_find_the_catalog_shapes(world):
    store.sweep(world.conn, now=NOW)
    rules = {r[0]: r for r in world.conn.execute("SELECT rule, section_id, valence, person, text FROM reflection_candidates")}
    assert rules["peak"][1:3] == ("S4", "positive")
    assert rules["uncelebrated"][1:3] == ("S4", "positive") and "Proud of this one" in rules["uncelebrated"][4]
    assert rules["gratitude"][3] == "Dana" and rules["gratitude"][1] == "S2"
    assert rules["company"][3] == "Nadia" and "2 of the moments" in rules["company"][4]
    assert rules["avoided"][1:4] == ("S2", "difficult", "dad")
    assert rules["recurring_tension"][1:3] == ("S3", "difficult") and "across 2 contexts" in rules["recurring_tension"][4]
    assert rules["open_promise"][3] == "Wren" and rules["open_promise"][1] == "S1"
    assert rules["theme"][1] == "S1" and rules["stated_change"][1] == "S5"
    assert rules["change"][1] == "S1" and rules["rel_open"][1:4] == ("S2", "neutral", "Priya")
    assert rules["both_ends"][1] == "S7" and rules["energy"][1] == "S7"


def test_a_sweep_keeps_ids_and_labels_and_retires_what_the_facts_no_longer_support(world):
    store.sweep(world.conn, now=NOW)
    before = {r[0]: r[1] for r in world.conn.execute("SELECT candidate_key, candidate_id FROM reflection_candidates")}
    store.label(world.conn, next(v for k, v in before.items() if k.startswith("theme:")), "worth_it", now=NOW)
    world.conn.execute("UPDATE signal_objects SET valid_to=? WHERE payload_json LIKE '%pottery course%'", (NOW.isoformat(),))
    world.conn.commit()
    second = store.sweep(world.conn, now=NOW + timedelta(hours=7))
    after = {r[0]: (r[1], r[2], r[3]) for r in world.conn.execute(
        "SELECT candidate_key, candidate_id, retired, retire_reason FROM reflection_candidates")}
    assert second["retired"] == 1
    theme_key = next(k for k in before if k.startswith("theme:"))
    assert after[theme_key][0] == before[theme_key] and after[theme_key][1:] == (1, "gone")
    assert all(after[k][0] == before[k] for k in before)
    assert world.conn.execute("SELECT COUNT(*) FROM reflection_labels").fetchone()[0] == 1


def test_no_pack_on_means_no_candidates_and_the_missing_packs_named(node):
    node.conn.execute("UPDATE pack_registry SET enabled=0")  # relationships.social is Wave A: on by default
    node.conn.commit()
    assert store.sweep(node.conn, now=NOW) == {"packs": [], "facts": 0, "candidates": 0, "retired": 0, "by_section": {},
                                               "built_at": NOW.isoformat()}
    out = store.invitations(node.conn, "wonderful", 5, now=NOW)
    assert out["cards"] == [] and out["packs_enabled"] == [] and out["packs_missing"] == list(store.REFLECTION_PACKS)


def test_a_fact_of_a_pack_that_is_off_is_never_read(world):
    world.conn.execute("UPDATE pack_registry SET enabled=0 WHERE pack_id='moments.tension'")
    world.conn.commit()
    result = store.sweep(world.conn, now=NOW)
    assert "moments.tension" not in result["packs"]
    assert not world.conn.execute("SELECT 1 FROM reflection_candidates WHERE rule IN ('tension','recurring_tension','avoided')").fetchone()


def test_sweep_due_and_enqueue(world):
    assert store.sweep_due(world.conn, now=NOW)
    job = store.enqueue_sweep(world.conn, now=NOW)
    assert job and store.enqueue_sweep(world.conn, now=NOW) is None  # one in flight
    store.sweep(world.conn, now=NOW)
    assert not store.sweep_due(world.conn, now=NOW + timedelta(hours=1))
    assert store.sweep_due(world.conn, now=NOW + timedelta(hours=7))


# --- invitations ---------------------------------------------------------------------------------------------------------
def test_invitations_are_balanced_and_the_home_list_spans_sections(world):
    store.sweep(world.conn, now=NOW)
    home = store.invitations(world.conn, None, 3, now=NOW)
    assert len(home["cards"]) == 3 and home["cards"][0]["valence"] == "positive"
    assert {c["valence"] for c in home["cards"]} != {"difficult"}
    difficult = store.invitations(world.conn, "difficult", 5, now=NOW)["cards"]
    assert difficult and all(c["section_id"] == "S3" for c in difficult)
    for card in home["cards"]:
        assert card["evidence"] and all(e["date"] for e in card["evidence"])
        assert "snippet" not in card["evidence"][0] and "excerpt" not in card["evidence"][0]


def test_invitations_honour_exclude_and_fall_back_when_nothing_fresh_is_left(world):
    store.sweep(world.conn, now=NOW)
    first = store.invitations(world.conn, "relationship", 2, now=NOW)["cards"]
    second = store.invitations(world.conn, "relationship", 2, exclude=[c["candidate_id"] for c in first], now=NOW)["cards"]
    assert {c["candidate_id"] for c in first}.isdisjoint({c["candidate_id"] for c in second})
    everything = [c["candidate_id"] for c in store.invitations(world.conn, "relationship", 6, now=NOW)["cards"]]
    assert store.invitations(world.conn, "relationship", 2, exclude=everything, now=NOW)["cards"]


@pytest.mark.parametrize("starter,limit,exclude", [("nope", 5, None), ("wonderful", 0, None), ("wonderful", 7, None),
                                                   ("wonderful", 5, "x"), ("wonderful", 5, [1])])
def test_bad_invitation_requests_are_refused_with_a_fixed_code(world, starter, limit, exclude):
    with pytest.raises(store.ReflectionRequestError, match="invalid_reflection_request"):
        store.invitations(world.conn, starter, limit, exclude, now=NOW)


# --- conversation --------------------------------------------------------------------------------------------------------
def test_the_conversation_carries_the_owners_words_and_only_the_date_of_a_contacts_row(world):
    store.sweep(world.conn, now=NOW)
    dinner = world.conn.execute("SELECT candidate_id FROM reflection_candidates WHERE candidate_key='peak:moment.peak:dinner'").fetchone()[0]
    world.conn.execute("UPDATE reflection_candidates SET evidence_refs_json=? WHERE candidate_id=?", (json.dumps([
        {"table": "journal_entries", "record_id": "j-dinner", "role": "authored", "date": "2026-10-11"},
        {"table": "conversation_messages", "record_id": "m-nadia", "role": "addressed", "date": "2026-10-12"}]), dinner))
    world.conn.commit()
    out = store.conversation(world.conn, dinner)
    assert out["section_id"] == "S4" and out["character"].startswith("someone who helps you stay")
    assert out["evidence"][0]["excerpt"].startswith("Dinner at Nadia's") and out["evidence"][0]["role"] == "authored"
    assert out["evidence"][1] == {"source": "imessage", "date": "2026-10-12", "role": "other", "excerpt": ""}
    assert "you looked so happy" not in out["system_prompt"]
    assert "Dinner at Nadia's turned into four hours" in out["system_prompt"]
    assert "never attribute it to their records" in out["system_prompt"]
    assert len(out["suggested_questions"]) == 3 and out["provider_note"] and out["opening_message"].startswith("Let's go back")


def test_an_unknown_candidate_is_a_fixed_code(world):
    with pytest.raises(store.ReflectionRequestError, match="unknown_candidate"):
        store.conversation(world.conn, "nope")
    with pytest.raises(store.ReflectionRequestError, match="invalid_reflection_request"):
        store.conversation(world.conn, 7)


# --- labels -------------------------------------------------------------------------------------------------------------
def _objects(conn):
    return conn.execute("SELECT signal_dimension, payload_json, confidence, created_by, valid_to FROM signal_objects "
                        "WHERE object_type='reflection_label'").fetchall()


def test_labels_apply_their_effects_and_write_one_owner_only_object(world):
    store.sweep(world.conn, now=NOW)
    ids = {r[0]: r[1] for r in world.conn.execute("SELECT rule, candidate_id FROM reflection_candidates")}
    assert store.label(world.conn, ids["peak"], "worth_it", "lovely", now=NOW)["ok"]
    rows = _objects(world.conn)
    assert len(rows) == 1 and rows[0][0] == "memory" and rows[0][2] == 1.0 and rows[0][3] == "owner" and rows[0][4] is None
    payload = json.loads(rows[0][1])
    assert payload["label"] == "worth_it" and payload["disclosure"] == "owner_only" and payload["note"] == "lovely"

    first = store.label(world.conn, ids["peak"], "not_now", now=NOW)
    assert first["ok"] and len(_objects(world.conn)) == 1 and json.loads(_objects(world.conn)[0][1])["label"] == "not_now"
    assert ids["peak"] not in {c["candidate_id"] for c in store.invitations(world.conn, "wonderful", 6, now=NOW)["cards"]}
    assert ids["peak"] in {c["candidate_id"] for c in store.invitations(world.conn, "wonderful", 6, now=NOW + timedelta(days=31))["cards"]}

    store.label(world.conn, ids["recurring_tension"], "not_true", now=NOW)
    assert world.conn.execute("SELECT retired, retire_reason FROM reflection_candidates WHERE candidate_id=?",
                              (ids["recurring_tension"],)).fetchone() == (1, "not_true")

    store.label(world.conn, ids["company"], "too_personal", now=NOW)  # Nadia
    retired = {r[0] for r in world.conn.execute("SELECT rule FROM reflection_candidates WHERE retire_reason='too_personal'")}
    assert {"company", "peak", "both_ends", "energy"} <= retired  # her name or her moments' facts
    assert not any(c["person"] == "Nadia" for s in STARTERS for c in store.invitations(world.conn, s, 6, now=NOW)["cards"])

    opened = store.label(world.conn, ids["avoided"], "opened", now=NOW)
    assert opened == {"ok": True, "label_id": None, "opened_count": 1}
    assert world.conn.execute("SELECT COUNT(*) FROM reflection_labels WHERE candidate_id=?", (ids["avoided"],)).fetchone()[0] == 0


@pytest.mark.parametrize("label,note,code", [("meh", None, "invalid_reflection_label"), ("worth_it", 3, "invalid_reflection_label")])
def test_a_bad_label_is_refused(world, label, note, code):
    store.sweep(world.conn, now=NOW)
    cid = world.conn.execute("SELECT candidate_id FROM reflection_candidates LIMIT 1").fetchone()[0]
    with pytest.raises(store.ReflectionRequestError, match=code):
        store.label(world.conn, cid, label, note, now=NOW)


def test_a_reflection_label_never_leaves_the_owner_tier():
    from topos import data_explorer_tables as tables
    from topos.features.signal.derived_index import DERIVED_RECORD_TYPES
    from topos.query.retrieval import _NEVER_BELOW_OWNER

    assert "reflection_label" in _NEVER_BELOW_OWNER
    assert "reflection_label" in tables.OWNER_ONLY_SIGNAL_OBJECT_TYPES
    assert {"reflection_candidates", "reflection_labels"} <= tables.OWNER_ONLY_TABLES
    assert "reflection_label" not in DERIVED_RECORD_TYPES and "reflection_label" not in DERIVED_RECORD_TYPES.values()


# --- the doors -----------------------------------------------------------------------------------------------------------
DOORS = ("reflection_invitations", "reflection_conversation", "reflection_label")
owner = Principal(OWNER_APP, "uds")


class Untouchable(dict):
    def __getattribute__(self, name):
        if name in ("__class__",):
            return object.__getattribute__(self, name)
        raise AssertionError(f"payload read: {name}")


def _as(principal, msg_type, payload):
    from topos.core.handlers import HANDLERS

    token = set_principal(principal)
    try:
        return asyncio.run(HANDLERS[msg_type]({"id": "r1", "type": msg_type, "payload": payload}))
    finally:
        reset_principal(token)


@pytest.mark.parametrize("msg_type", DOORS)
@pytest.mark.parametrize("principal", [
    Principal(CP_RELAY, "cp_relay"), Principal(THIRD_PARTY, "local_http", client_id="moments"),
    Principal(THIRD_PARTY, "cp_relay"), Principal("owner_agent", "uds"), Principal("owner_automation", "cp_relay"), None,
])
def test_every_door_refuses_anyone_but_the_owner_before_reading_the_payload(monkeypatch, msg_type, principal):
    monkeypatch.setenv("TOPOS_TRUTH_CLIENT_ALLOWLIST", "moments")
    assert _as(principal, msg_type, Untouchable()) == {"id": "r1", "status": "error", "code": 403, "error": "owner_mode_required"}


def test_the_owner_walks_the_three_doors(world):
    store.sweep(world.conn, now=NOW)
    home = _as(owner, "reflection_invitations", {"limit": 3})
    assert home["status"] == "ok" and len(home["payload"]["cards"]) == 3 and home["payload"]["packs_missing"] == []
    options = _as(owner, "reflection_invitations", {"starter": "wonderful", "limit": 5})["payload"]["cards"]
    assert options and all(c["section_id"] == "S4" for c in options)
    convo = _as(owner, "reflection_conversation", {"candidate_id": options[0]["candidate_id"]})
    assert convo["status"] == "ok" and convo["payload"]["system_prompt"] and convo["payload"]["character"]
    labelled = _as(owner, "reflection_label", {"candidate_id": options[0]["candidate_id"], "label": "worth_it"})
    assert labelled["status"] == "ok" and labelled["payload"]["ok"]
    assert _as(owner, "reflection_invitations", {"weeks": 2})["error"] == "invalid_reflection_request"
    assert _as(owner, "reflection_invitations", {"starter": "nope"})["error"] == "invalid_reflection_request"
    assert _as(owner, "reflection_conversation", {"candidate_id": "nope"})["error"] == "unknown_candidate"
    assert _as(owner, "reflection_label", "not a dict")["error"] == "invalid_reflection_label"
    assert _as(owner, "reflection_label", {"candidate_id": options[0]["candidate_id"], "label": "meh"})["error"] == "invalid_reflection_label"


def test_on_the_owner_socket_the_reads_are_open_and_the_label_needs_the_app():
    import topos.core.handlers as hub

    assert {"reflection_invitations", "reflection_conversation"} <= hub.owner_door_open_types()
    assert "reflection_label" in hub.DANGEROUS_TYPES


def test_the_protocol_snapshot_names_the_three_doors():
    snapshot = json.loads((Path(__file__).resolve().parents[2] / "topos/protocol/handled_message_types.json").read_text())
    assert set(DOORS) <= set(snapshot["handled_message_types"])


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
    response = _client(Principal(THIRD_PARTY, "local_http")).post(f"/api/local/{msg_type}", content=b"{not json")
    assert response.status_code == 403 and response.json() == {"detail": "owner_mode_required"}


@pytest.mark.parametrize("msg_type", DOORS)
def test_each_route_hands_the_owners_body_to_its_door(monkeypatch, msg_type):
    import topos.api.local_mcp as routes

    seen = []

    async def dispatch(message, principal=None):
        seen.append((message["type"], message["payload"], principal.cls))
        return {"id": message["id"], "status": "ok", "payload": {"echo": message["payload"]}}

    monkeypatch.setattr(routes, "handle_control_plane_request", dispatch)
    response = _client(owner).post(f"/api/local/{msg_type}", json={"limit": 2})
    assert response.status_code == 200 and response.json() == {"echo": {"limit": 2}}
    assert seen == [(msg_type, {"limit": 2}, OWNER_APP)] and response.headers["cache-control"] == "no-store"


def test_each_route_is_in_the_owner_socket_census():
    from topos import owner_door_routes as census

    for msg_type in DOORS:
        assert census.lookup("POST", f"/api/local/{msg_type}").verdict == census.DISPATCHER


def test_the_sweep_runs_on_the_catch_up_lane():
    from topos.pipeline import job_runner

    assert store.JOB_KIND in job_runner.EXECUTORS and store.JOB_KIND in job_runner._CATCHUP_KINDS


def test_the_two_packs_ship_disabled_and_load(node):
    from topos.features.derivation.packs import load_packs

    packs = load_packs(bundled_pack_dir())
    assert {"moments.joy", "moments.tension"} <= set(packs)
    assert packs["moments.joy"].role_policy == "authored_only" and packs["moments.tension"].net_subject == "deny"
    rows = dict(node.conn.execute("SELECT pack_id, enabled FROM pack_registry WHERE pack_id LIKE 'moments.%'").fetchall())
    assert rows == {"moments.joy": 0, "moments.tension": 0}

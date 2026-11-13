"""The owner socket's two doors for a pack from a terminal: the switch and the trial.

protects: `PUT /v1/signal/derivation/packs/{id}/switch` flips a pack only for the app or a live owner
confirmation on the socket, for off as for on; it is refused over TCP with the owner's key and a live token, and
with the socket gate switched off; it never switches on a pack that writes about other people and records no
consent; for a pack the owner added it switches on only the definition whose digest the request carries, so a
file that changed after the yes stays off; and the row it leaves is the row the app's own switch leaves.
`POST /v1/signal/derivation/packs/try` needs no confirmation, is served on the socket only, calls no model for a
body or a file it refuses, and writes nothing.

The routes are the real ones behind the real socket wrapper, in process. The model is a stub. Synthetic only.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import Any, Dict

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from topos import owner_door_routes as census, uds
from topos.features.derivation import custom_packs, pack_trial, surfaces
from topos.features.derivation.packs import load_packs
from topos.features.derivation.registry import (bundled_pack_dir, definition_sha, enabled_packs,
                                                runs_as_switched_on, seed_pack_registry)
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public

PACK = "custom.gardening"
SHIPPED = "work.career"
OUTWARD = "net.character"
BASE = "/v1/signal/derivation/packs"
TRY = BASE + "/try"
REFUSAL = {"error": "owner_confirmation_required", "message": "This change needs the Topos app."}
ROW = ("SELECT pack_id, version, enabled, origin, definition_sha, consented_at, consent_note, disclosure_default,"
       " active_since IS NOT NULL FROM pack_registry WHERE pack_id=?")


def definition(**changes) -> Dict[str, Any]:
    data = {
        "pack": PACK, "version": "1.0.0", "title": "My gardening",
        "sensitivity_class": "personal", "role_policy": "authored_only", "net_subject": "deny",
        "routing": {"descriptors": ["Plants the owner grows"]},
        "guidance": {"definitions": "Plants the owner says they grow."},
        "predicates": [{"name": "custom.plant", "value_type": "structured", "cardinality": "multi",
                        "temporal": "interval", "altitude": "stated", "value_schema": {"name": "string"}}],
        "eval": {"gold": [{"text": "I grow tomatoes in my garden.",
                           "expect": [{"predicate": "custom.plant", "value": {"name": "tomatoes"}}]}],
                 "negative_controls": [{"text": "The film had a beautiful garden.", "expect": []}]},
        "consumers": ["assistant_context"],
    }
    data.update(changes)
    return data


def text(**changes) -> str:
    return yaml.safe_dump(definition(**changes))


def switch(pack_id: str) -> str:
    return f"{BASE}/{pack_id}/switch"


def digest(pack_id: str = PACK) -> str:
    """The digest of the pack as it is on disk now: what a person would be shown."""
    return definition_sha(load_packs(bundled_pack_dir(), only=[pack_id])[pack_id], stamped=False)


@pytest.fixture
def node(tmp_path, monkeypatch):
    home = tmp_path / "home" / "fact-packs"
    monkeypatch.setattr(custom_packs, "custom_pack_dir", lambda: home)
    custom_packs._SAID.clear()
    conn = sqlite3.connect(tmp_path / "node.db", check_same_thread=False)
    apply_all_migrations(conn)
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_owner','person','Owner','owner','[]',1)")
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)")
    seed_pack_registry(conn, bundled_pack_dir())
    conn.execute("UPDATE pack_registry SET enabled=0")
    conn.commit()
    yield SimpleNamespace(conn=conn, home=home)
    conn.close()


@pytest.fixture
def model(monkeypatch):
    from topos.engine.backends import ollama

    calls = []

    def generate(self, name, prompt, **kwargs):
        calls.append(prompt)
        found = [{"predicate": "custom.plant", "value": {"name": "tomatoes"}, "about": "owner",
                  "occurrence_date": None, "confidence": 0.9, "quote": "I grow tomatoes"}]
        return {"text": json.dumps({"assertions": found if "I grow tomatoes" in prompt else []})}

    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", generate)
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c=None: "stub-model")
    return calls


@pytest.fixture
def door(node, tmp_path, monkeypatch):
    """The signal router as the node mounts it, behind the owner socket's wrapper, in process."""
    monkeypatch.setenv("TOPOS_UDS_PATH", str(tmp_path / "engine.sock"))
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "1")
    for name in ("TOPOS_OWNER_DOOR_TEAM_IDS", "TOPOS_UDS_TEAM_IDS", "TOPOS_DEV_PROXY_ALLOWED"):
        monkeypatch.delenv(name, raising=False)
    from topos.api import signal

    monkeypatch.setattr(signal, "_entities_conn", lambda: node.conn)
    app = FastAPI()
    app.include_router(signal.router, prefix="/v1")
    return SimpleNamespace(app=app, socket=lambda: TestClient(uds.UDSChannelApp(app)))


def _yes() -> Dict[str, str]:
    return {"X-Topos-Owner-Confirm": uds.write_owner_confirmation()}


def _enabled(conn: sqlite3.Connection, pack_id: str) -> bool:
    row = conn.execute("SELECT enabled FROM pack_registry WHERE pack_id=?", (pack_id,)).fetchone()
    return bool(row and row[0])


def _tcp(door):
    """The same app as a TCP caller sees it: no socket wrapper, the owner's key accepted."""
    from topos.auth import require_api_key, resolve_request_principal
    from topos.principal import OWNER_APP, Principal

    door.app.dependency_overrides[require_api_key] = lambda: "key"
    door.app.dependency_overrides[resolve_request_principal] = lambda: Principal(OWNER_APP, "local_http", acting_user="o")
    return TestClient(door.app)


# ----------------------------------------------------------------------------- the declarations
def test_the_two_doors_are_in_the_census_with_the_verdicts_the_gate_reads() -> None:
    assert census.lookup("PUT", switch(PACK)).verdict == census.DANGEROUS
    assert census.lookup("POST", TRY).verdict == census.OPEN
    assert census.lookup("GET", TRY).verdict == census.READ
    for method, path in (("PUT", switch(PACK)), ("POST", TRY), ("GET", TRY)):
        assert not census.owner_agent_route_allowed(method, path), "a coding agent's label reaches neither"
        assert not census.app_agent_route_allowed(method, path), "nor does an app's"


# ----------------------------------------------------------------------------- the switch is the owner's
def test_on_is_refused_without_the_owners_yes_and_served_once_with_it(door, node) -> None:
    with door.socket() as client:
        for body in ({"enabled": True}, {"enabled": False}):
            bare = client.put(switch(SHIPPED), json=body)
            wrong = client.put(switch(SHIPPED), json=body, headers={"X-Topos-Owner-Confirm": "a" * 64})
            for response in (bare, wrong):
                assert response.status_code == 403 and response.json() == REFUSAL
        assert not _enabled(node.conn, SHIPPED)

        header = _yes()
        on = client.put(switch(SHIPPED), json={"enabled": True}, headers=header)
        assert on.status_code == 200, on.text
        assert on.json() == {"pack_id": SHIPPED, "enabled": True, "changed": True,
                             "version": load_packs(bundled_pack_dir(), only=[SHIPPED])[SHIPPED].version}
        assert _enabled(node.conn, SHIPPED)
        assert not uds.owner_confirmation_path().exists(), "the confirmation is consumed by the change"
        again = client.put(switch(SHIPPED), json={"enabled": False}, headers=header)
        assert again.status_code == 403 and again.json() == REFUSAL, "one yes, one change"
        assert _enabled(node.conn, SHIPPED)


def test_off_stops_the_pack_and_needs_the_yes_too(door, node) -> None:
    surfaces.set_pack_enabled(node.conn, SHIPPED, True)
    with door.socket() as client:
        assert client.put(switch(SHIPPED), json={"enabled": False}).status_code == 403
        assert _enabled(node.conn, SHIPPED)
        off = client.put(switch(SHIPPED), json={"enabled": False}, headers=_yes())
        assert off.status_code == 200 and off.json() == {"pack_id": SHIPPED, "enabled": False, "changed": True}
        assert not _enabled(node.conn, SHIPPED)
        assert SHIPPED not in enabled_packs(node.conn, bundled_pack_dir())
        again = client.put(switch(SHIPPED), json={"enabled": False}, headers=_yes())
        assert again.status_code == 200 and again.json()["changed"] is False


def test_the_switch_is_refused_over_tcp_and_with_the_socket_gate_switched_off(door, node, monkeypatch) -> None:
    try:
        with _tcp(door) as tcp:
            _yes()                                   # a live token on disk changes nothing for a TCP caller
            token = json.loads(uds.owner_confirmation_path().read_text())["token"]
            response = tcp.put(switch(SHIPPED), json={"enabled": True}, headers={"X-Topos-Owner-Confirm": token})
            assert response.status_code == 403 and response.json() == REFUSAL
            assert not _enabled(node.conn, SHIPPED)
    finally:
        door.app.dependency_overrides.clear()
    uds.clear_owner_confirmation()

    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "0")
    with door.socket() as client:
        bare = client.put(switch(SHIPPED), json={"enabled": True})
        assert bare.status_code == 403 and bare.json() == REFUSAL, "the route asks for the yes itself"
        assert not _enabled(node.conn, SHIPPED)
        assert client.put(switch(SHIPPED), json={"enabled": True}, headers=_yes()).status_code == 200
        assert _enabled(node.conn, SHIPPED)


def test_a_pack_that_writes_about_other_people_is_not_switched_on_here(door, node) -> None:
    assert surfaces.consent_terms(node.conn, OUTWARD) is not None, "the fixture pack needs consent"
    with door.socket() as client:
        refused = client.put(switch(OUTWARD), json={"enabled": True}, headers=_yes())
        assert refused.status_code == 409 and refused.json()["error"] == "consent_in_app"
        smuggled = client.put(switch(OUTWARD), json={"enabled": True, "consent": True}, headers=_yes())
        assert smuggled.status_code == 400 and smuggled.json()["error"] == "pack_switch_invalid"
    row = node.conn.execute("SELECT enabled, consented_at, consent_note FROM pack_registry WHERE pack_id=?",
                            (OUTWARD,)).fetchone()
    assert tuple(row) == (0, None, None), "nothing switched on and no consent recorded"


def test_a_body_that_is_not_the_switch_and_a_pack_that_is_not_there_are_refused(door, node) -> None:
    with door.socket() as client:
        for body in ({}, {"enabled": "yes"}, {"enabled": True, "definition": 5}, {"enabled": True, "extra": 1},
                     {"enabled": True, "definition": ""}, {"enabled": True, "definition": "x" * 129}):
            response = client.put(switch(SHIPPED), json=body, headers=_yes())
            assert response.status_code == 400 and response.json()["error"] == "pack_switch_invalid", body
        for body in ({"enabled": True}, {"enabled": False}):
            missing = client.put(switch("custom.nothing_here"), json=body, headers=_yes())
            assert missing.status_code == 404 and missing.json()["error"] == "unknown_pack"
    assert not _enabled(node.conn, SHIPPED)


# ----------------------------------------------------------------------------- the stamp is the app's
def test_on_records_what_the_apps_own_switch_records(door, node) -> None:
    """The app reads the pack list (which seeds the registry) and then flips the row. The row the terminal's
    switch leaves is that row, column for column, for a pack that ships and for one the owner added."""
    custom_packs.install_content(text().encode())
    for pack_id in (SHIPPED, PACK):
        surfaces.list_packs(node.conn)                                   # the page's read
        assert surfaces.set_pack_enabled(node.conn, pack_id, True)       # the page's switch
        by_app = tuple(node.conn.execute(ROW, (pack_id,)).fetchone())
        assert by_app[2] == 1
        surfaces.set_pack_enabled(node.conn, pack_id, False)

        body = {"enabled": True, **({"definition": digest(pack_id)} if pack_id == PACK else {})}
        with door.socket() as client:
            assert client.put(switch(pack_id), json=body, headers=_yes()).status_code == 200
        by_terminal = tuple(node.conn.execute(ROW, (pack_id,)).fetchone())
        assert by_terminal == by_app, pack_id
        pack = load_packs(bundled_pack_dir(), only=[pack_id])[pack_id]
        assert runs_as_switched_on(node.conn, pack) and pack_id in enabled_packs(node.conn, bundled_pack_dir())
    added = node.conn.execute(ROW, (PACK,)).fetchone()
    assert added[3] == "third_party" and added[4] == definition_sha(load_packs(bundled_pack_dir(), only=[PACK])[PACK])
    assert added[5] is None, "no consent is recorded for a pack that needs none"


# ----------------------------------------------------------------------------- the yes is to a definition
def test_a_pack_changed_after_the_yes_is_not_switched_on(door, node) -> None:
    custom_packs.install_content(text().encode())
    approved = digest()                                    # what the person was shown before the yes
    # ...and then the file in the node's own directory is replaced by one that reads more.
    (node.home / f"{PACK}.yaml").write_text(text(role_policy="any_with_label", sensitivity_class="special"))
    with door.socket() as client:
        refused = client.put(switch(PACK), json={"enabled": True, "definition": approved}, headers=_yes())
        assert refused.status_code == 409 and refused.json()["error"] == "pack_changed"
        assert not _enabled(node.conn, PACK)
        assert PACK not in enabled_packs(node.conn, bundled_pack_dir())

        nameless = client.put(switch(PACK), json={"enabled": True}, headers=_yes())
        assert nameless.status_code == 400 and nameless.json()["error"] == "definition_required"
        assert not _enabled(node.conn, PACK)

        # The person looks again, is shown the pack as it is now, and says yes to that.
        again = client.put(switch(PACK), json={"enabled": True, "definition": digest()}, headers=_yes())
        assert again.status_code == 200 and _enabled(node.conn, PACK)
        assert enabled_packs(node.conn, bundled_pack_dir())[PACK].role_policy == "any_with_label"


def test_a_file_that_changes_between_the_seeding_and_the_flip_is_switched_off_again(node, monkeypatch) -> None:
    """The race the route cannot see from outside: the file is right when it is checked and changed by the time
    the row is flipped. No reader would honour that row, and the switch does not report it as on."""
    custom_packs.install_content(text().encode())
    approved = digest()
    flip = surfaces.set_pack_enabled

    def flip_after_a_change(conn, pack_id, enabled, **kw):
        if enabled:
            (node.home / f"{PACK}.yaml").write_text(text(title="Swapped"))
        return flip(conn, pack_id, enabled, **kw)

    monkeypatch.setattr(surfaces, "set_pack_enabled", flip_after_a_change)
    with pytest.raises(surfaces.PackSwitchRefused) as refused:
        surfaces.switch_pack(node.conn, PACK, True, definition=approved)
    assert refused.value.code == "pack_changed" and not _enabled(node.conn, PACK)


# ----------------------------------------------------------------------------- the trial
def test_a_trial_needs_no_yes_runs_the_files_own_examples_and_writes_nothing(door, node, model) -> None:
    before = node.conn.total_changes
    rows = node.conn.execute("SELECT COUNT(*) FROM pack_registry").fetchone()[0]
    with door.socket() as client:
        response = client.post(TRY, json={"content": text()})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["pack_id"] == PACK and report["model"] == "stub-model"
    assert [(row["kind"], row["outcome"]) for row in report["examples"]] == [("gold", "pass"),
                                                                            ("negative_control", "pass")]
    assert report["totals"]["passed"] == 2 and len(model) == 2
    assert node.conn.total_changes == before and not node.conn.in_transaction
    assert node.conn.execute("SELECT COUNT(*) FROM pack_registry").fetchone()[0] == rows
    assert node.conn.execute("SELECT COUNT(*) FROM pack_registry WHERE pack_id=?", (PACK,)).fetchone()[0] == 0
    assert node.conn.execute("SELECT COUNT(*) FROM derivation_processing").fetchone()[0] == 0
    assert not node.home.exists(), "a trial puts no file in the pack directory"
    assert not uds.owner_confirmation_path().exists(), "and mints no confirmation"


def test_a_trial_is_the_sockets_only_and_refuses_what_it_cannot_run_before_any_model_call(door, node, model) -> None:
    try:
        with _tcp(door) as tcp:
            over_tcp = tcp.post(TRY, json={"content": text()})
            assert over_tcp.status_code == 403 and over_tcp.json()["error"] == "owner_socket_only"
    finally:
        door.app.dependency_overrides.clear()
    many = [{"text": f"I grow plant number {n}."} for n in range(pack_trial.MAX_EXAMPLES + 1)]
    with door.socket() as client:
        for body, status, code in (
                ({}, 400, "pack_trial_invalid"),
                ({"content": 5}, 400, "pack_trial_invalid"),
                ({"content": text(), "limit": 500}, 400, "pack_trial_invalid"),
                ({"content": "pack: [unclosed\n"}, 400, "pack_invalid"),
                ({"content": text(pack=SHIPPED)}, 400, "pack_invalid"),
                ({"content": text(eval={"gold": many, "negative_controls": [{"text": "A film."}]})}, 400,
                 "too_many_examples")):
            response = client.post(TRY, json=body)
            assert (response.status_code, response.json()["error"]) == (status, code), body
    assert model == [], "no model call for anything refused"


def test_a_trial_on_a_node_with_no_local_model_says_so(door, node, model, monkeypatch) -> None:
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c=None: "")
    with door.socket() as client:
        response = client.post(TRY, json={"content": text()})
        assert response.status_code == 409 and response.json()["error"] == "no_local_model"
        monkeypatch.setattr(pack_trial, "model_status", lambda conn: {"model": None, "available": "no"})
        look = client.get(TRY)
    assert look.status_code == 200 and look.json() == {"model": None, "available": "no", "max_examples": 20}
    assert model == []


# ----------------------------------------------------------------------------- the older doors close the same way
# `PUT /derivation/packs/{id}` and the single-batch `POST /derivation/packs/{id}/backfill` predate the switch above.
# Over TCP they asked for the API key and nothing else, so any local process holding the key switched a pack on (an
# outward one too, with `consent: true` in the body) or read history with no person asked. They now ask what the
# switch asks. The web app's own switch is a relay message (`put_derivation_pack`), not these routes.
@pytest.mark.parametrize("split", ["1", "0"])
def test_the_older_switch_and_batch_are_refused_without_a_confirmed_socket_caller(door, node, monkeypatch, split):
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", split)
    batches = []
    monkeypatch.setattr(surfaces, "run_pack_backfill", lambda conn, pack_id, limit, **kw: batches.append(pack_id) or {})
    try:
        with _tcp(door) as tcp:
            for pack_id, body in ((SHIPPED, {"enabled": True}), (OUTWARD, {"enabled": True, "consent": True})):
                response = tcp.put(f"{BASE}/{pack_id}", json=body)
                assert response.status_code == 403 and response.json() == REFUSAL, pack_id
                assert not _enabled(node.conn, pack_id)
            surfaces.set_pack_enabled(node.conn, SHIPPED, True)
            off = tcp.put(f"{BASE}/{SHIPPED}", json={"enabled": False})
            assert off.status_code == 403 and _enabled(node.conn, SHIPPED), "off is the owner's too"
            batch = tcp.post(f"{BASE}/{SHIPPED}/backfill", json={"limit": 5})
            assert batch.status_code == 403 and batch.json() == REFUSAL
    finally:
        door.app.dependency_overrides.clear()
    with door.socket() as client:
        assert client.post(f"{BASE}/{SHIPPED}/backfill", json={"limit": 5}).status_code == 403
        assert client.put(f"{BASE}/{SHIPPED}", json={"enabled": False}).status_code == 403
        assert batches == [] and _enabled(node.conn, SHIPPED)


def test_the_older_switch_and_batch_still_serve_the_owners_yes_on_the_socket(door, node, monkeypatch) -> None:
    batches = []
    monkeypatch.setattr(surfaces, "run_pack_backfill",
                        lambda conn, pack_id, limit, **kw: batches.append((pack_id, limit)) or {"read": 0})
    with door.socket() as client:
        on = client.put(f"{BASE}/{SHIPPED}", json={"enabled": True}, headers=_yes())
        assert on.status_code == 200, on.text
        assert on.json() == {"pack_id": SHIPPED, "enabled": True, "consented": False}
        assert _enabled(node.conn, SHIPPED)
        batch = client.post(f"{BASE}/{SHIPPED}/backfill", json={"limit": 5}, headers=_yes())
        assert batch.status_code == 200, batch.text
        assert batches == [(SHIPPED, 5)]

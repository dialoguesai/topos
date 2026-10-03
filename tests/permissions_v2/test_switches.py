"""One switch module (any-to-any N1, decision D5): every sharing switch, its two defaults, its overrides, and bound.

The expectations below are written out by hand from the 1.4.4 inventory (E1 §4) and from D5, not read from the
module's table, so the table cannot grade itself. Each switch is observed where its feature reads it: the door
(does the request get past the switch to the relay-stamp check), the runtime accessor, the refresh loop's
settings, the family or the flag function the feature calls. The forced cases use each switch's one name, which
is the name 1.4.4 read: none was ever renamed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_message_search_refusals import Socket
from topos.permissions_v2 import (ai_chat_capture, entailment_grounding, fact_release_transport, inferred_facts,
                                  interest_index, interest_relabel, journal_goal_field, permitted_derivation,
                                  protection_doorbell, release_transport, search_timing, search_transport,
                                  shadow_index, shadow_rescore, switches)
from topos.permissions_v2 import runtime as runtime_module
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence_families import enabled_tables, family
from topos.permissions_v2.refresh_loop import RefreshSettings
from topos.storage.db import paths

P = "TOPOS_PERMISSIONS_V2_"
REAL_RESOLVE = paths.resolve_active_database
#: Every name 1.4.4 read (inventory E1 §4, 28 names in 19 files) and the related one sharing code reads, with what
#: an unset value meant there.
V144 = {
    P + "ENABLED": False, P + "CONFIG_PATH": None, P + "EVIDENCE_REVIEWS_ENABLED": True,
    P + "PROJECTION_REVIEWS_ENABLED": False, P + "IDENTITY_ATTESTATIONS_ENABLED": False,
    P + "INGEST_SNAPSHOTS_ENABLED": False, P + "INGEST_SNAPSHOT_ROOT": None, P + "MESSAGE_SEARCH_ENABLED": False,
    P + "MESSAGE_SEARCH_BATCH_ENABLED": False, P + "SOURCE_RELEASE_ENABLED": False, P + "FACT_RELEASE_ENABLED": False,
    P + "INDEX_RESTORE_ENABLED": False, P + "ASSESSMENT_CATCHUP_ENABLED": False,
    P + "INDEX_RESTORE_MIN_INTERVAL_SECONDS": 300, P + "ASSESSMENT_CATCHUP_MAX_PER_PASS": 500,
    P + "AUTO_RESYNC": True, P + "JOURNAL_SOURCES": False, P + "JOURNAL_GOAL_FIELD": False,
    P + "INTEREST_SOURCES": False, P + "INTEREST_RELABEL": True, P + "DERIVED_FACTS": False,
    P + "PERMITTED_DERIVATION": False, P + "ENTAILMENT_GROUNDING": False, P + "ENTAILMENT_MODEL_JUDGE": False,
    P + "ENTAILMENT_SENTENCE_REPORTING": False, P + "SEARCH_TIMINGS": False, P + "SHADOW_INDEX_ENABLED": False,
    P + "SHADOW_LABELER": None, "TOPOS_OWNER_CAPTURE_APP_IDS": ("chatgpt-shadow-extension",),
}
#: D5: on a bound node with an empty environment, search, its batch form, the refresh loop and the kinds messages,
#: AI chats, journal entries, browsing interests, goals and relationships are on (with what they need: the owner's
#: "this is me" for relationships, the iMessage proof lane for messages); facts, entailment and every experiment-,
#: shadow- or lab-only switch stay off.
BOUND = {
    **V144,
    P + "ENABLED": True, P + "CONFIG_PATH": "permissions-v2/config.json",
    P + "IDENTITY_ATTESTATIONS_ENABLED": True, P + "INGEST_SNAPSHOTS_ENABLED": True,
    P + "INGEST_SNAPSHOT_ROOT": "permissions-v2/ingest-snapshots",
    P + "MESSAGE_SEARCH_ENABLED": True, P + "MESSAGE_SEARCH_BATCH_ENABLED": True,
    P + "INDEX_RESTORE_ENABLED": True, P + "ASSESSMENT_CATCHUP_ENABLED": True,
    P + "JOURNAL_SOURCES": True, P + "JOURNAL_GOAL_FIELD": True, P + "INTEREST_SOURCES": True,
}
BOOLEANS = sorted(name for name, value in V144.items() if isinstance(value, bool))


@pytest.fixture(autouse=True)
def empty_environment(monkeypatch):
    """No switch set, nothing remembered about bound, and no warning remembered as already given."""
    for name in switches.BY_NAME:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(switches, "_unreadable_logged", set())
    switches.forget_bound()
    yield
    switches.forget_bound()


@pytest.fixture
def state(monkeypatch):
    """Pin the node's state for the table: state(True) is bound, state(False) is not."""
    def pin(bound: bool):
        monkeypatch.setattr(switches, "is_bound", lambda env=None: bound)
    return pin


# --- where each switch is observed ---------------------------------------------------------------------------------

def _gate(call) -> str:
    """The code a runtime accessor refuses with, or "past" once it got beyond its switches."""
    try:
        call()
    except PolicyError as exc:
        return exc.code
    except Exception:  # noqa: BLE001 -- it went on to work the bare runtime below cannot do: past the switch
        return "past"
    return "past"


def _bare_runtime(tmp_path) -> runtime_module.Runtime:
    canonical = tmp_path / "bare" / "canonical.db"
    canonical.parent.mkdir(exist_ok=True)
    rt = object.__new__(runtime_module.Runtime)
    rt.__dict__.update(pid=os.getpid(), evidence_review_store_path=None, projection_review_store_path=None,
                       _evidence_review_runtime=None, _projection_review_runtime=None, _ingestion_service=None,
                       _ingestion_snapshot_root=None, _identity_service=None, _canonical_floor=None,
                       _message_search_index=None, _sweeper=None, _refresh=None,
                       protocol=SimpleNamespace(canonical_database=canonical, canonical_floor=None))
    return rt


def _past_door(monkeypatch, module, dispatch, message_type) -> bool:
    """Whether a frame gets past the door's switch: the relay stamp is checked only after it."""
    stamped = []
    monkeypatch.setattr(module, "verify_relay_stamp", lambda message: stamped.append(message) and None)
    socket = Socket()
    asyncio.run(dispatch(socket, {"id": "n1-probe", "type": message_type, "payload": {}}))
    assert [json.loads(frame)["status"] for frame in socket.sent] == ["error"]   # the uniform refusal either way
    return bool(stamped)


def observe(name, monkeypatch, tmp_path):
    """The value the feature reading `name` sees now. Prerequisites a feature has are set on by hand."""
    def need(*names):
        for needed in names:
            monkeypatch.setenv(needed, "true")

    if name == P + "ENABLED":
        return _gate(runtime_module.get_runtime) != "permissions_v2_disabled"
    if name == P + "CONFIG_PATH":
        located = switches.explicit(switches.CONFIG_PATH)
        return located if located is not None else switches.default(switches.CONFIG_PATH) if switches.is_bound() \
            else None
    if name == P + "EVIDENCE_REVIEWS_ENABLED":
        need(P + "ENABLED")
        return _gate(_bare_runtime(tmp_path).evidence_reviews) != "evidence_reviews_disabled"
    if name == P + "PROJECTION_REVIEWS_ENABLED":
        return _gate(_bare_runtime(tmp_path).projection_reviews) != "projection_reviews_disabled"
    if name == P + "IDENTITY_ATTESTATIONS_ENABLED":
        need(P + "ENABLED")
        return _gate(_bare_runtime(tmp_path).identity_attestations) != "identity_attestations_disabled"
    if name == P + "INGEST_SNAPSHOTS_ENABLED":
        need(P + "ENABLED")
        return _gate(_bare_runtime(tmp_path).ingestion) != "ingest_snapshots_disabled"
    if name == P + "INGEST_SNAPSHOT_ROOT":
        need(P + "ENABLED", P + "INGEST_SNAPSHOTS_ENABLED")
        rt = _bare_runtime(tmp_path)
        if _gate(rt.ingestion) == "ingest_snapshots_not_configured":
            return None
        return str(Path(switches.snapshot_root(rt.protocol.canonical_database))
                   .relative_to(rt.protocol.canonical_database.parent))
    if name == P + "MESSAGE_SEARCH_ENABLED":
        return _past_door(monkeypatch, search_transport, search_transport.dispatch_message_search,
                          search_transport.MESSAGE_TYPE)
    if name == P + "MESSAGE_SEARCH_BATCH_ENABLED":
        need(P + "MESSAGE_SEARCH_ENABLED")
        door = _past_door(monkeypatch, search_transport, search_transport.dispatch_message_search_batch,
                          search_transport.BATCH_MESSAGE_TYPE)
        assert search_transport.batch_capability_version() == (1 if door else 0)   # the heartbeat says the same
        return door
    if name == P + "SOURCE_RELEASE_ENABLED":
        return _past_door(monkeypatch, release_transport, release_transport.dispatch_source_message,
                          release_transport.MESSAGE_TYPE)
    if name == P + "FACT_RELEASE_ENABLED":
        return _past_door(monkeypatch, fact_release_transport, fact_release_transport.dispatch_fact_message,
                          fact_release_transport.MESSAGE_TYPE)
    if name == P + "INDEX_RESTORE_ENABLED":
        return RefreshSettings.from_env().restore
    if name == P + "ASSESSMENT_CATCHUP_ENABLED":
        need(P + "INDEX_RESTORE_ENABLED")
        return RefreshSettings.from_env().catchup
    if name == P + "INDEX_RESTORE_MIN_INTERVAL_SECONDS":
        return int(RefreshSettings.from_env().min_interval)
    if name == P + "ASSESSMENT_CATCHUP_MAX_PER_PASS":
        return RefreshSettings.from_env().max_assessed
    if name == P + "AUTO_RESYNC":
        return protection_doorbell.enabled()
    if name == P + "JOURNAL_SOURCES":
        return family("journal_entries").enabled()
    if name == P + "JOURNAL_GOAL_FIELD":
        return journal_goal_field.enabled()
    if name == P + "INTEREST_SOURCES":
        return interest_index.enabled()
    if name == P + "INTEREST_RELABEL":
        return interest_relabel.enabled()
    if name == P + "DERIVED_FACTS":
        need(P + "JOURNAL_SOURCES")
        return inferred_facts.enabled()
    if name == P + "PERMITTED_DERIVATION":
        return permitted_derivation.enabled()
    if name == P + "ENTAILMENT_GROUNDING":
        return entailment_grounding.enabled()
    if name == P + "ENTAILMENT_MODEL_JUDGE":
        need(P + "ENTAILMENT_GROUNDING")
        return entailment_grounding.model_judge_enabled()
    if name == P + "ENTAILMENT_SENTENCE_REPORTING":
        return entailment_grounding.sentence_scoped_reporting()
    if name == P + "SEARCH_TIMINGS":
        return search_timing.enabled()
    if name == P + "SHADOW_INDEX_ENABLED":
        return shadow_index.enabled()
    if name == P + "SHADOW_LABELER":
        return None if shadow_rescore.configured_labeler("local") is None else "local"
    if name == "TOPOS_OWNER_CAPTURE_APP_IDS":
        return tuple(sorted(ai_chat_capture._od39_app_ids()))
    raise AssertionError(f"no observation for {name}")


# --- the table -----------------------------------------------------------------------------------------------------

def test_the_table_holds_every_name_1_4_4_read_and_no_other():
    assert set(switches.BY_NAME) == set(V144)
    assert len(switches.SWITCHES) == len(V144) == 29
    assert sum(name.startswith(P) for name in switches.BY_NAME) == 28
    for item in switches.SWITCHES:
        assert item.purpose and item.kind in ("bool", "int", "choice", "path", "list"), item.name


@pytest.mark.parametrize("name", sorted(V144))
def test_each_switch_reads_as_1_4_4_on_a_node_that_is_not_bound(name, state, monkeypatch, tmp_path):
    state(False)
    item = switches.lookup(name)
    assert item.unbound == V144[name] or item.kind == "path"
    assert observe(name, monkeypatch, tmp_path) == V144[name]


@pytest.mark.parametrize("name", sorted(V144))
def test_each_switch_takes_its_d5_default_on_a_bound_node(name, state, monkeypatch, tmp_path):
    state(True)
    assert switches.lookup(name).bound == BOUND[name]
    assert observe(name, monkeypatch, tmp_path) == BOUND[name]


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
@pytest.mark.parametrize("name", BOOLEANS)
def test_each_old_name_forces_its_switch_on_and_off(name, bound, state, monkeypatch, tmp_path):
    state(bound)
    for written, expected in (("true", True), ("false", False), ("TRUE", True), (" false ", False)):
        monkeypatch.setenv(name, written)
        assert observe(name, monkeypatch, tmp_path) is expected, (name, written)
        monkeypatch.delenv(name)
        for dependency in [n for n in switches.BY_NAME if n != name]:
            monkeypatch.delenv(dependency, raising=False)


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
def test_the_numbers_and_the_choice_take_their_old_names(bound, state, monkeypatch, tmp_path):
    state(bound)
    monkeypatch.setenv(P + "INDEX_RESTORE_MIN_INTERVAL_SECONDS", "120")
    monkeypatch.setenv(P + "ASSESSMENT_CATCHUP_MAX_PER_PASS", "40")
    assert (RefreshSettings.from_env().min_interval, RefreshSettings.from_env().max_assessed) == (120.0, 40)
    monkeypatch.setenv(P + "INDEX_RESTORE_MIN_INTERVAL_SECONDS", "5")         # raised to its floor
    monkeypatch.setenv(P + "ASSESSMENT_CATCHUP_MAX_PER_PASS", "0")
    assert (RefreshSettings.from_env().min_interval, RefreshSettings.from_env().max_assessed) == (60.0, 1)
    monkeypatch.setenv(P + "SHADOW_LABELER", " Local ")
    assert observe(P + "SHADOW_LABELER", monkeypatch, tmp_path) == "local"
    monkeypatch.setenv("TOPOS_OWNER_CAPTURE_APP_IDS", "some-other-app")
    assert observe("TOPOS_OWNER_CAPTURE_APP_IDS", monkeypatch, tmp_path) == ("some-other-app",)
    monkeypatch.setenv("TOPOS_OWNER_CAPTURE_APP_IDS", "")                       # blank names no app, bound or not
    assert observe("TOPOS_OWNER_CAPTURE_APP_IDS", monkeypatch, tmp_path) == ()


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
def test_the_paths_take_their_old_names(bound, state, monkeypatch, tmp_path):
    state(bound)
    rt = _bare_runtime(tmp_path)
    expected = rt.protocol.canonical_database.parent / "permissions-v2" / "ingest-snapshots"
    monkeypatch.setenv(P + "ENABLED", "true")
    monkeypatch.setenv(P + "INGEST_SNAPSHOTS_ENABLED", "true")
    monkeypatch.setenv(P + "INGEST_SNAPSHOT_ROOT", str(expected))
    assert _gate(rt.ingestion) == "past"
    monkeypatch.setenv(P + "INGEST_SNAPSHOT_ROOT", str(tmp_path / "elsewhere"))     # set, and wrong, even when bound
    assert _gate(rt.ingestion) == "ingest_snapshots_not_configured"
    monkeypatch.setenv(P + "CONFIG_PATH", "relative/config.json")
    assert _gate(runtime_module.get_runtime) == "beta_configuration_required"
    missing = tmp_path / "missing" / "config.json"
    monkeypatch.setenv(P + "CONFIG_PATH", str(missing))
    with pytest.raises(FileNotFoundError):                                        # as 1.4.4: the path is the path
        runtime_module.get_runtime()


# --- the one parser ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, reading", [
    (None, None), ("", None), ("   ", None),
    ("true", True), ("TRUE", True), (" True ", True), ("1", True), ("yes", True), ("on", True), ("ON", True),
    ("false", False), ("0", False), ("no", False), ("off", False), (" OFF ", False),
    ("enabled", False), ("2", False), ("maybe", False), ("tru", False),
])
def test_one_reading_for_every_on_off_switch(raw, reading):
    for item in switches.SWITCHES:
        if item.kind == "bool":
            assert switches.parse(item, raw) is reading, (item.name, raw)


@pytest.mark.parametrize("raw, reading", [
    (None, None), ("", None), ("120", 120), (" 120 ", 120), ("0300", 300), ("5", 60),
    ("-5", None), ("+300", None), ("1e3", None), ("300.0", None), ("３００", None), ("²", None), ("ten", None),
])
def test_one_reading_for_numbers(raw, reading):
    assert switches.parse(switches.INDEX_RESTORE_MIN_INTERVAL, raw) == reading


def test_one_reading_for_choices_paths_and_lists():
    assert [switches.parse(switches.SHADOW_LABELER, raw) for raw in (None, "", "local", " LOCAL ", "true", "qwen")] \
        == [None, None, "local", "local", None, None]
    assert [switches.parse(switches.CONFIG_PATH, raw) for raw in (None, "", "  ", "/a/b c.json", " /a")] \
        == [None, None, None, "/a/b c.json", " /a"]                       # a path is taken as written
    assert [switches.parse(switches.OWNER_CAPTURE_APP_IDS, raw) for raw in (None, "", " a , b:src ,, ")] \
        == [None, (), ("a", "b:src")]


def test_a_value_the_node_cannot_read_is_off_and_logged_by_name_only(monkeypatch, caplog):
    odd_value = "maybe-" + secrets.token_hex(4)
    with caplog.at_level(logging.WARNING, logger="topos.permissions_v2.switches"):
        monkeypatch.setenv(P + "MESSAGE_SEARCH_ENABLED", odd_value)
        assert search_transport._enabled() is False
        assert search_transport._enabled() is False
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1 and P + "MESSAGE_SEARCH_ENABLED" in lines[0] and odd_value not in lines[0]


def test_an_unreadable_value_never_shares_more_even_on_a_bound_node(state, monkeypatch):
    state(True)
    for item in switches.SWITCHES:
        if item.kind == "bool":
            monkeypatch.setenv(item.name, "maybe")
            assert switches.on(item) is False, item.name


def test_the_1_4_4_parsers_disagreed_and_these_are_the_differences(state):
    """Every input on which some 1.4.4 parser read differently from the one parser now (listed in the N1 report)."""
    state(False)
    exact = [P + n for n in ("ENABLED", "PROJECTION_REVIEWS_ENABLED", "IDENTITY_ATTESTATIONS_ENABLED",
                             "INGEST_SNAPSHOTS_ENABLED", "MESSAGE_SEARCH_ENABLED", "MESSAGE_SEARCH_BATCH_ENABLED",
                             "SOURCE_RELEASE_ENABLED", "FACT_RELEASE_ENABLED", "INDEX_RESTORE_ENABLED",
                             "ASSESSMENT_CATCHUP_ENABLED", "ENTAILMENT_GROUNDING", "ENTAILMENT_MODEL_JUDGE",
                             "ENTAILMENT_SENTENCE_REPORTING", "SEARCH_TIMINGS", "SHADOW_INDEX_ENABLED",
                             "JOURNAL_GOAL_FIELD", "PERMITTED_DERIVATION")]
    for name in exact:                                       # 1.4.4: only "true" (any case) was on
        for raw in ("1", "yes", "on"):
            assert switches.on(name, {name: raw}) is True    # was off
    for name in exact[:15]:                                  # these did not strip: " true" was off
        assert switches.on(name, {name: " true "}) is True
    reviews = P + "EVIDENCE_REVIEWS_ENABLED"                 # 1.4.4: unset on, anything but "true" off
    assert [switches.on(reviews, {reviews: raw}) for raw in ("1", "yes", "on", "", " true ")] == [True] * 5
    for name in (P + "AUTO_RESYNC", P + "INTEREST_RELABEL"):  # 1.4.4: on unless 0/false/off/no
        assert switches.on(name, {name: "maybe"}) is False   # was on
        assert switches.on(name, {name: ""}) is True         # unchanged: blank keeps the default
    minimum = P + "INDEX_RESTORE_MIN_INTERVAL_SECONDS"       # 1.4.4: str.isdecimal() or the default
    assert switches.number(minimum, {minimum: " 120 "}) == 120   # was the default
    assert switches.number(minimum, {minimum: "１２０"}) == 300    # 1.4.4 read these fullwidth digits as 120


# --- bound -------------------------------------------------------------------------------------------------------

def _config(directory: Path, canonical: Path, *, environment="permissions-beta-n1-test", mode=0o600, **changes):
    """A node config, written where a bound node keeps it. Invented ids; keys generated here, never written down."""
    durable = directory / "permissions-v2"
    durable.mkdir(mode=0o700, exist_ok=True)
    config = {"version": "topos-policy-node-config/v1",
              "identity": {"environment_id": environment, "node_id": "node-n1", "resource_id": "topos-n1",
                           "owner_id": "owner-n1"},
              "cp_issuer_id": "cp-n1", "frontend_client_id": "web-n1",
              "trusted_cp_keys": {"cp-key": secrets.token_hex(32)}, "node_signing_kid": "node-key",
              "node_signing_key_path": str(durable / "node.key"), "canonical_database_path": str(canonical),
              "ledger_path": str(durable / "ledger.db"), **changes}
    path = durable / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(mode)
    return path


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A database this node serves, in its own folder, with no config beside it yet."""
    canonical = tmp_path / "node" / "database.db"
    canonical.parent.mkdir()
    canonical.write_bytes(b"")
    monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **k: SimpleNamespace(path=canonical))
    switches.forget_bound()
    return canonical


def test_a_node_with_no_config_is_not_bound_and_never_parses_anything(served, monkeypatch):
    monkeypatch.setattr(switches, "_refusal", lambda *a: pytest.fail("parsed a config that is not there"))
    assert switches.default_config_path() == served.parent / "permissions-v2" / "config.json"
    assert [switches.is_bound() for _ in range(3)] == [False] * 3


def test_a_config_beside_the_served_database_binds_and_a_bind_is_seen_without_a_restart(served, monkeypatch):
    assert switches.is_bound() is False
    path = _config(served.parent, served)                       # what N2 will write at the owner's first share
    assert switches.is_bound() is True                           # the next call, no restart and no forget
    calls = []
    real = switches._refusal
    monkeypatch.setattr(switches, "_refusal", lambda *a: calls.append(a) or real(*a))
    assert all(switches.is_bound() for _ in range(50)) and calls == []   # parsed once per version of the file
    path.unlink()
    assert switches.is_bound() is False
    _config(served.parent, served, cp_issuer_id="cp-n1-rewritten")   # a new version of the file
    assert switches.is_bound() is True and len(calls) == 1


@pytest.mark.parametrize("change", ["group_readable", "production_environment", "no_cp_key", "another_database",
                                    "relative_database", "not_json", "not_the_node_config"])
def test_a_config_that_would_not_load_does_not_bind(served, change, tmp_path):
    other = tmp_path / "other.db"
    other.write_bytes(b"")
    options = {"group_readable": {"mode": 0o640}, "production_environment": {"environment": "production"},
               "no_cp_key": {"trusted_cp_keys": {}}, "another_database": {"canonical_database_path": str(other)},
               "relative_database": {"canonical_database_path": "database.db"}}.get(change, {})
    path = _config(served.parent, served, **options)
    if change == "not_json":
        path.write_text("{")
    if change == "not_the_node_config":
        path.write_text(json.dumps({"version": "topos-policy-node-config/v1"}))
    assert switches.is_bound() is False


def test_an_explicit_config_path_is_where_the_node_looks(served, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = _config(elsewhere, served)
    assert switches.is_bound() is False                          # not beside the database
    monkeypatch.setenv(P + "CONFIG_PATH", str(path))
    assert switches.is_bound() is True
    monkeypatch.setenv(P + "CONFIG_PATH", "permissions-v2/config.json")
    assert switches.is_bound() is False                          # relative: refused, as 1.4.4 refused it
    _config(served.parent, served)
    monkeypatch.setenv(P + "CONFIG_PATH", str(tmp_path / "missing.json"))
    assert switches.is_bound() is False                          # the named file wins over the one beside the db


def test_the_master_switch_set_off_unbinds_the_node(served, monkeypatch, tmp_path):
    _config(served.parent, served)
    assert switches.is_bound() is True
    for off in ("false", "0", "off", "garbage"):
        monkeypatch.setenv(P + "ENABLED", off)
        assert switches.is_bound() is False
        for name, expected in V144.items():
            if name not in (P + "CONFIG_PATH", P + "INGEST_SNAPSHOT_ROOT", P + "ENABLED"):
                assert switches.value(name) == expected, (off, name)
        assert _gate(runtime_module.get_runtime) == "permissions_v2_disabled"


def test_a_change_of_the_served_database_is_seen_at_once(served, tmp_path, monkeypatch):
    """Where to look is remembered for a moment, but against everything it is derived from: a resolver or a
    settings path that changes with no environment change is seen at the next call, with no forget."""
    _config(served.parent, served)
    assert switches.is_bound() is True
    other = tmp_path / "other-topos" / "database.db"
    other.parent.mkdir()
    other.write_bytes(b"")
    monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **k: SimpleNamespace(path=other))
    assert switches.is_bound() is False and switches.default_config_path() == other.parent / "permissions-v2" / \
        "config.json"
    monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **k: SimpleNamespace(path=served))
    assert switches.is_bound() is True
    from topos.config.settings import settings
    monkeypatch.setattr(paths, "resolve_active_database", REAL_RESOLVE)      # the node's own resolver again
    monkeypatch.setattr(settings, "topos_database_path", str(served), raising=False)
    assert switches.is_bound() is True
    monkeypatch.setattr(settings, "topos_database_path", str(other), raising=False)   # settings only, env as it was
    assert switches.is_bound() is False


def test_a_node_serving_no_database_is_not_bound(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **k: SimpleNamespace(path=None))
    switches.forget_bound()
    assert switches.default_config_path() is None and switches.is_bound() is False


# --- the two nodes D5 is about -----------------------------------------------------------------------------------

def test_an_unbound_node_with_an_empty_environment_is_1_4_4(served, monkeypatch, tmp_path):
    """Nothing new starts and nothing new is served: every switch reads as 1.4.4 read it unset."""
    from topos.engine import registration
    from topos.permissions_v2 import refresh_loop
    assert switches.is_bound() is False
    for name, expected in V144.items():
        assert observe(name, monkeypatch, tmp_path) == expected, name
        for dependency in switches.BY_NAME:
            monkeypatch.delenv(dependency, raising=False)
    assert enabled_tables() == ("conversation_messages", "ai_chat_messages")
    assert registration._search_batch_version() == 0
    assert RefreshSettings.from_env().enabled is False
    assert refresh_loop.start_at_startup(delay=0) is False      # no thread
    assert _gate(runtime_module.get_runtime) == "permissions_v2_disabled"


@pytest.fixture
def bound_node(served):
    """A bound node: its private config beside the database it serves, and an environment with no switch in it."""
    _config(served.parent, served)
    assert not any(name in os.environ for name in switches.BY_NAME)
    return served


def test_a_bound_node_with_an_empty_environment_has_d5s_defaults(bound_node, monkeypatch, tmp_path):
    from topos.engine import registration
    assert switches.is_bound() is True
    for name, expected in BOUND.items():
        if name in (P + "CONFIG_PATH",):
            continue
        assert observe(name, monkeypatch, tmp_path) == expected, name
        for dependency in switches.BY_NAME:
            monkeypatch.delenv(dependency, raising=False)
    # search and its batch form, and the heartbeat says so
    assert search_transport._enabled() and search_transport._batch_enabled()
    monkeypatch.setattr(registration, "ollama_is_reachable", lambda: False)
    assert registration.build_engine_capabilities()["permissions_v2_search_batch_version"] == 1
    # the refresh loop, with interests and their second labels, and no derived facts
    settings = RefreshSettings.from_env()
    assert settings.enabled and settings.restore and settings.catchup and settings.interests and settings.relabels
    assert settings.facts is False
    # the six kinds: messages, AI chats and journal entries are families; interests; goals (the journal's goal
    # field; a stated goal needs no switch); relationships need the owner's confirmed self
    assert enabled_tables() == ("conversation_messages", "ai_chat_messages", "journal_entries")
    assert interest_index.enabled() and journal_goal_field.enabled()
    assert switches.on(switches.IDENTITY_ATTESTATIONS)
    # facts and entailment off; experiment-, shadow- and lab-only off
    assert not inferred_facts.enabled() and not permitted_derivation.enabled()
    assert not switches.on(switches.FACT_RELEASE) and not switches.on(switches.PROJECTION_REVIEWS)
    assert not entailment_grounding.enabled() and not entailment_grounding.model_judge_enabled()
    assert not entailment_grounding.sentence_scoped_reporting()
    assert not switches.on(switches.SOURCE_RELEASE) and not shadow_index.enabled()
    assert shadow_rescore.configured_labeler("local") is None and not search_timing.enabled()


def test_a_bound_node_loads_its_runtime_and_serves_search_with_no_switch_set(configured, monkeypatch):
    """The real runtime, from the config beside the database it serves, with ENABLED and CONFIG_PATH unset."""
    config, path, fixture = configured
    canonical = fixture[0].canonical_database
    assert path == canonical.parent / "permissions-v2" / "config.json"      # where a bound node keeps it
    monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **k: SimpleNamespace(path=canonical))
    monkeypatch.setattr(runtime_module, "_runtime", None)
    switches.forget_bound()
    assert switches.is_bound() is True
    runtime = runtime_module.get_runtime()
    try:
        assert runtime.config_path == path.resolve()
        index = runtime.message_search_index()            # the search door's own accessor
        assert index is runtime.message_search_index() and runtime.message_search() is not None
        assert RefreshSettings.from_env().enabled              # the loop is not started here: it calls the model
        assert switches.snapshot_root(canonical) == str(canonical.parent / "permissions-v2" / "ingest-snapshots")
    finally:
        runtime.close()
        monkeypatch.setattr(runtime_module, "_runtime", None)


@pytest.mark.asyncio
async def test_a_bound_node_with_an_empty_environment_answers_a_signed_search_at_the_door(node, monkeypatch):
    """The search door end to end: a recipient's signed search through the relay, with no switch in the env."""
    from tests.permissions_v2.test_message_search_refusals import PAYLOAD, relay_message, signed
    canonical = node.corpus.path
    _config(canonical.parent, canonical)
    monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **k: SimpleNamespace(path=canonical))
    switches.forget_bound()
    message = relay_message(node, signed(node, request_id="n1-bound"), PAYLOAD, monkeypatch, request_id="n1-bound")
    monkeypatch.delenv(search_transport.FLAG)              # relay_message sets it; a bound node needs no such line
    assert not any(name in os.environ for name in switches.BY_NAME) and switches.is_bound()
    socket = Socket()
    await search_transport.dispatch_message_search(socket, message)
    [frame] = [json.loads(value) for value in socket.sent]
    assert frame["status"] == "ok" and frame["id"] == "n1-bound"
    assert set(frame["payload"]) == {"result", "output"}
    message = relay_message(node, signed(node, request_id="n1-off"), PAYLOAD, monkeypatch, request_id="n1-off")
    monkeypatch.setenv(search_transport.FLAG, "false")      # and the old name still turns it off
    socket = Socket()
    await search_transport.dispatch_message_search(socket, message)
    assert [json.loads(value)["status"] for value in socket.sent] == ["error"]


from tests.permissions_v2.test_entity_boundary_search import node  # noqa: E402,F401 -- the door test's node
from tests.permissions_v2.test_node_protocol import protocol  # noqa: E402,F401
from tests.permissions_v2.test_protocol_runtime import configured  # noqa: E402,F401

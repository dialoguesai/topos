"""The upgrade matrix proves the 1.5.0 carry step on data that needs it (T9 B6).

protects: `carry-contact-excludes-to-off-limits` turns every explicit per-person "exclude" of the older sharing model
into an Off-limits entry, without asking, on every node that upgrades. The upgrade matrix is what a release trusts,
and it could not see this step at all: the step is filed under the manifest's `unreleased` entry, which is never
planned, and the fixture held no excluded contact, so even once stamped it would have ledgered "done" having done
nothing. These tests pin, on the matrix's own fixture:
  - the fixture stores one invented contact for each thing the step must tell apart, with every naming branch;
  - the matrix passes when the step does its work, from the release before it and from the 1.1.0 floor;
  - the matrix FAILS, naming the step, when the step's body does nothing, carries only half, loses the note, reaches
    a contact that never chose exclude, is not idempotent, disturbs an entry the owner made, or has a ledger row
    without a start and a finish; and it refuses a fixture that holds nothing for the step to act on;
  - the staging entry is rehearsed as a release in a scratch copy, and the repository's manifest is not written;
  - the second from-version is read from the manifest ladder (1.4.4 for 1.5.0), not worked out from the number.

Only the carry step runs for real here. Every other step's executor is a stub, so no test loads a model, whatever a
later release adds to the plan; the older steps' own assertions are the matrix job's to make, on a real run.
Every person, handle and id is invented (scripts/build_upgrade_fixture.py declares them).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

import topos.upgrades as upgrades
from topos.features.lifecycle import contact_excludes
from topos.features.lifecycle.contact_excludes import ENDPOINT, STEP_ID

pytestmark = pytest.mark.public

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name):
    """The matrix scripts import each other by name from their own directory, as they do when run."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


matrix = _load("run_upgrade_matrix")
fixture = _load("build_upgrade_fixture")
cut_release = _load("cut_release")

CARRIED = [choice for choice in fixture.CONTACT_CHOICES if choice.case == fixture.CARRIED]
OTHERS = [choice for choice in fixture.CONTACT_CHOICES if choice.case != fixture.CARRIED]
FLOOR = "1.1.0"


@pytest.fixture(autouse=True)
def _environment_and_manifest_restored():
    """`run_matrix` sets process environment for the run, and two tests point the package at a manifest of their
    own; nothing of either may reach another test."""
    saved, manifest = dict(os.environ), upgrades._MANIFESTS_PATH
    yield
    os.environ.clear()
    os.environ.update(saved)
    upgrades._MANIFESTS_PATH = manifest


def _rehearsal():
    """(from-version, stage) that put the carry step in the plan on this tree, before the cut and after it.

    Before the cut the step waits under `unreleased`, the staging flag rehearses it as the next version, and the
    release before that is the package version. After the cut the step is declared by a release, the same flag
    changes nothing, and the from-version is the release before the one that declares it."""
    declared_in = upgrades.declaring_versions().get(STEP_ID)
    return matrix.previous_release(declared_in or matrix.shipped_for_run(matrix.NEXT)[0]), matrix.NEXT


def _built(tmp_path, version, name="fixture.db"):
    path = tmp_path / name
    fixture.build_from_current(version, path)
    return path


def _what_a_graph_rebuild_does_to_contacts(conn):
    """The model-free part of a real graph rebuild that the carry step can see (features/entities/maintenance.py):
    mention counts are recounted from the mentions on disk, mention-less entities with no contact go, and every
    contact that has a name, a handle or a username gets a linked person entity. The rest of a rebuild (and every
    extraction before it) can call a model, so it stays a stub here."""
    from topos.features.entities.resolver import EntityResolver
    from topos.features.lifecycle.derived_scrub import _delete_orphan_entities, _recount_entity_mentions
    from topos.storage.db.write_gate import with_db_write

    with with_db_write():
        _recount_entity_mentions(conn)
        _delete_orphan_entities(conn)
        conn.commit()
    with with_db_write():
        EntityResolver(conn).seed_from_contacts()


def _only_the_carry_step_is_real():
    """Executors for a test run: the carry step through the runner's own dispatch, a stub for everything else."""
    from topos.upgrades.runner import DEFAULT_EXECUTORS, _real_source_ids

    def walked(step, conn):
        return {"sources": {source: "ok" for source in _real_source_ids(conn)}, "stubbed": True}

    def endpoint(step, conn):
        path = (step.get("params") or {}).get("path")
        if path == ENDPOINT:
            return DEFAULT_EXECUTORS["engine_endpoint"](step, conn)
        if path == "/v1/signal/entities/graph/rebuild":
            _what_a_graph_rebuild_does_to_contacts(conn)
        return {"stubbed": True}

    def rebuilt(step, conn):
        params = step.get("params") or {}
        if "entity_graph" in (params.get("targets") or params.get("layers") or ["entity_graph"]):
            _what_a_graph_rebuild_does_to_contacts(conn)
        return {"targets": {}, "stubbed": True}

    return {"enrichment_reprocess": walked, "canonical_reprocess": walked, "reembed": walked,
            "derived_rebuild": rebuilt, "engine_endpoint": endpoint, "none": DEFAULT_EXECUTORS["none"]}


def _run(path, monkeypatch=None, *, from_floor=False):
    """The matrix over `path`, with the carry step in the plan. From the floor the older steps are stubs, so their
    own effect assertions (rows a real extraction wrote) are switched off for the run and said so."""
    _from, stage = _rehearsal()
    if from_floor:
        monkeypatch.setattr(matrix, "_assert_steps_did_work", lambda *args: print("older steps stubbed"))
    manifest = upgrades._MANIFESTS_PATH
    try:
        matrix.run_matrix(path, stage_unreleased=stage, executors=_only_the_carry_step_is_real())
    finally:
        assert upgrades._MANIFESTS_PATH == manifest        # staging put the manifest back, pass or fail


def _off_limits(path):
    conn = sqlite3.connect(str(path))
    try:
        return matrix._off_limits(conn)
    finally:
        conn.close()


def _fails(path, monkeypatch=None, **how):
    with pytest.raises(AssertionError) as failure:
        _run(path, monkeypatch, **how)
    message = str(failure.value)
    assert message.startswith(f"step {STEP_ID!r} "), message          # every failure names the step
    return message


# --- the fixture ----------------------------------------------------------------------------------------------

def test_the_fixture_stores_one_contact_for_each_thing_the_step_must_tell_apart(tmp_path):
    path = _built(tmp_path, _rehearsal()[0])
    conn = sqlite3.connect(str(path))
    try:
        stored = dict(conn.execute("SELECT contact_id, sharing_policy_json FROM contacts"))
        assert stored == {choice.contact_id: choice.stored_choice for choice in fixture.CONTACT_CHOICES}
        assert {choice.case for choice in OTHERS} == set(fixture.NOT_CARRIED)
        assert {choice.named_by for choice in CARRIED} == set(fixture.NAMING_BRANCHES)
        assert all(choice.named_by is None for choice in OTHERS)
        # The step's own reading of the fixture, read-only: what it counts and how it would name each entry.
        preview = contact_excludes.carry_contact_excludes(conn, dry_run=True)
        cases = [choice.case for choice in fixture.CONTACT_CHOICES]
        hidden = sum(1 for choice in fixture.CONTACT_CHOICES
                     if choice.case not in ("no_stored_choice", "unreadable")
                     and json.loads(choice.stored_choice).get("name_visibility") == "hidden")
        assert preview["counts"] == {
            "contacts": len(cases), "no_stored_choice": cases.count("no_stored_choice"),
            "unreadable": cases.count("unreadable"), "stored_without_row_choice": cases.count("no_row_choice"),
            "explicit_excludes": len(CARRIED),
            "explicit_includes": cases.count("explicit_include") + cases.count("hidden_name"),
            "hidden_names": hidden}
        assert len(CARRIED) == 8 and len(OTHERS) == 7
        assert preview["named_by"] == {branch: sum(1 for choice in CARRIED if choice.named_by == branch)
                                       for branch in fixture.NAMING_BRANCHES}
        assert conn.execute("SELECT COUNT(*) FROM entity_blackholes").fetchone()[0] == 0     # a dry run writes nothing
        assert conn.execute("SELECT value FROM engine_config WHERE key='engine.upgrade.baseline'").fetchone()[0] \
            == _rehearsal()[0]
    finally:
        conn.close()


def test_no_fixture_value_looks_like_a_phone_number_or_a_real_address():
    import re
    for choice in fixture.CONTACT_CHOICES:
        for value in (choice.contact_id, choice.display_name or "", *choice.usernames,
                      *(identifier for identifier, _kind in choice.handles)):
            assert not re.search(r"\d{4,}", value), choice.contact_id
            assert "@" not in value or value.startswith("@") or value.endswith("@example.invalid"), choice.contact_id


# --- the matrix passes when the step does its work ------------------------------------------------------------

def test_the_matrix_passes_from_the_release_before_the_step(tmp_path, capsys):
    from_version, _stage = _rehearsal()
    path = _built(tmp_path, from_version)
    _run(path)
    printed = capsys.readouterr().out
    assert f"ok: {STEP_ID} carried {len(CARRIED)} explicit excludes into Off-limits (0 -> {len(CARRIED)} entries" \
        in printed
    assert f"no entry for the {len(OTHERS)} contacts with another stored value or none" in printed
    assert "second run added 0 and changed 0" in printed and "upgrade_matrix_ok" in printed
    entries = _off_limits(path)
    assert len(entries) == len(CARRIED)
    assert {entry["note"] for entry in entries.values()} == {contact_excludes.NOTE}
    assert {entry["rebuild_state"] for entry in entries.values()} == {"complete"}


def test_the_matrix_passes_from_the_floor_with_the_step_last_in_a_long_plan(tmp_path, monkeypatch, capsys):
    path = _built(tmp_path, FLOOR)
    _run(path, monkeypatch, from_floor=True)
    printed = capsys.readouterr().out
    assert f"plan: baseline={FLOOR!r}" in printed and "older steps stubbed" in printed
    assert f"ok: {STEP_ID} carried {len(CARRIED)} explicit excludes into Off-limits" in printed
    # Graph rebuilds ran first and linked an entity to every contact with a name, a handle or a username, so only
    # the contact with nothing at all is still named by its id. The matrix says so and judges the total.
    assert (f"named by {{'contact_id_only': 1, 'linked_entity': {len(CARRIED) - 1}}}, "
            "after older steps that link entities to contacts") in printed
    # Steps that ask the owner first rest at pending_consent, so the baseline stays; the carry step never asks.
    assert f"ok: baseline={FLOOR!r} with pending_consent=" in printed and "upgrade_matrix_ok" in printed
    conn = sqlite3.connect(str(path))
    try:
        steps = [row[0] for row in conn.execute("SELECT step_id FROM derivation_ledger WHERE status='done'")]
        assert STEP_ID in steps and len(steps) > 10
        # Each of the eight still has exactly its own entry: the contact id is an alias whatever names the entry.
        aliases = [json.loads(row[0]) for row in conn.execute("SELECT aliases_json FROM entity_blackholes")]
        assert sorted(choice.contact_id for choice in CARRIED) == sorted(
            alias for entry in aliases for alias in entry if alias.startswith("upgrade-fixture-contact-"))
        # The contacts that never chose exclude were linked to entities too, and none of them has an entry.
        linked = {row[0] for row in conn.execute("SELECT contact_id FROM entities WHERE contact_id IS NOT NULL")}
        assert {choice.contact_id for choice in OTHERS if choice.display_name or choice.handles} <= linked
    finally:
        conn.close()


def test_an_entry_the_owner_made_before_is_counted_apart_and_left_alone(tmp_path, capsys):
    path = _built(tmp_path, _rehearsal()[0])
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO entity_blackholes (blackhole_id, normalized_name, canonical_name, rebuild_state, note) "
                 "VALUES ('bh_owner_made', 'halcyon verity-marsh', 'Halcyon Verity-Marsh', 'complete', 'mine')")
    conn.commit()
    conn.close()
    before = _off_limits(path)["bh_owner_made"]
    _run(path)
    assert f"(1 -> {len(CARRIED) + 1} entries" in capsys.readouterr().out
    assert _off_limits(path)["bh_owner_made"] == before


# --- the matrix fails, naming the step, when the step does not --------------------------------------------------

def _body_does_nothing(monkeypatch):
    monkeypatch.setattr(contact_excludes, "carry_contact_excludes", lambda conn, *, dry_run=False: {
        "step": STEP_ID, "dry_run": dry_run, "counts": {"contacts": 0}, "carried": 0, "already_off_limits": 0,
        "named_by": {}, "rebuilds_failed": 0})


def _carries_only_half(monkeypatch):
    real = contact_excludes.explicit_choices

    def half(conn):
        found = real(conn)
        return {"counts": found["counts"], "excludes": found["excludes"][::2]}
    monkeypatch.setattr(contact_excludes, "explicit_choices", half)


@pytest.mark.parametrize("from_floor", [False, True], ids=["from the release before", "from the floor"])
def test_the_matrix_fails_when_the_step_does_nothing(tmp_path, monkeypatch, from_floor):
    path = _built(tmp_path, FLOOR if from_floor else _rehearsal()[0])
    _body_does_nothing(monkeypatch)
    message = _fails(path, monkeypatch, from_floor=from_floor)
    assert f"added 0 Off-limits entries, expected {len(CARRIED)}" in message
    # The runner saw nothing wrong: the step is 'done' in the ledger, which is all the matrix used to look at.
    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT status FROM derivation_ledger WHERE step_id=?", (STEP_ID,)).fetchone()[0] == "done"
    conn.close()


@pytest.mark.parametrize("from_floor", [False, True], ids=["from the release before", "from the floor"])
def test_the_matrix_fails_when_the_step_carries_only_half(tmp_path, monkeypatch, from_floor):
    path = _built(tmp_path, FLOOR if from_floor else _rehearsal()[0])
    _carries_only_half(monkeypatch)
    message = _fails(path, monkeypatch, from_floor=from_floor)
    assert f"added {len(CARRIED) // 2} Off-limits entries, expected {len(CARRIED)}" in message


def test_the_matrix_fails_when_an_entry_lacks_the_steps_note(tmp_path, monkeypatch):
    from topos.features.lifecycle.blackhole import BlackholeStore
    real = BlackholeStore.blackhole_entity
    monkeypatch.setattr(BlackholeStore, "blackhole_entity",
                        lambda self, **kwargs: real(self, **{**kwargs, "note": None}))
    assert f"wrote {len(CARRIED)} of its {len(CARRIED)} Off-limits entries without the step's note" \
        in _fails(_built(tmp_path, _rehearsal()[0]))


def test_the_matrix_fails_when_an_entry_reaches_a_contact_that_never_chose_exclude(tmp_path, monkeypatch):
    """The count is right and every excluded contact has its entry, but one entry also names someone else."""
    included = next(choice for choice in OTHERS if choice.case == "explicit_include")
    real = contact_excludes._identity

    def wide(conn, contact):
        identity = real(conn, contact)
        if contact["contact_id"] == CARRIED[0].contact_id:
            identity["handles"] = [*identity["handles"], included.handles[0][0]]
        return identity
    monkeypatch.setattr(contact_excludes, "_identity", wide)
    message = _fails(_built(tmp_path, _rehearsal()[0]))
    assert f"made an Off-limits entry that reaches {included.contact_id!r}" in message
    assert "'explicit_include' and was never an exclude" in message


def test_the_matrix_fails_when_a_non_exclude_is_carried_in_place_of_an_exclude(tmp_path, monkeypatch):
    """Still eight entries, so a count alone would pass."""
    real = contact_excludes.explicit_choices

    def swapped(conn):
        found = real(conn)
        stray = dict(found["excludes"][0], contact_id=OTHERS[0].contact_id, display_name=OTHERS[0].display_name)
        return {"counts": found["counts"], "excludes": [stray, *found["excludes"][1:]]}
    monkeypatch.setattr(contact_excludes, "explicit_choices", swapped)
    message = _fails(_built(tmp_path, _rehearsal()[0]))
    assert "with 0 new Off-limits entries, expected 1" in message


def test_the_matrix_fails_when_a_declared_naming_branch_did_not_run(tmp_path, monkeypatch):
    """From the release before, the step is the first thing the plan runs and sees the contacts as built: every
    naming branch the fixture declares must show in what the step reports, as often as declared."""
    real = contact_excludes._entry
    monkeypatch.setattr(contact_excludes, "_entry", lambda identity: {**real(identity), "named_by": "contact_id_only"})
    message = _fails(_built(tmp_path, _rehearsal()[0]))
    assert f"named its entries by {{'contact_id_only': {len(CARRIED)}}}, expected the fixture's" in message


def test_the_matrix_fails_when_a_second_run_adds_an_entry(tmp_path, monkeypatch):
    from topos.features.lifecycle.blackhole import BlackholeStore
    real, runs = contact_excludes.carry_contact_excludes, []

    def restless(conn, *, dry_run=False):
        out = real(conn, dry_run=dry_run)
        if not dry_run:
            runs.append(1)
            if len(runs) > 1:
                BlackholeStore(conn).blackhole_entity(entity_ref="Second Run Stray")
        return out
    monkeypatch.setattr(contact_excludes, "carry_contact_excludes", restless)
    assert "is not idempotent: a second run added 1 Off-limits entries" in _fails(_built(tmp_path, _rehearsal()[0]))


def test_the_matrix_fails_when_a_second_run_changes_an_entry(tmp_path, monkeypatch):
    real, runs = contact_excludes.carry_contact_excludes, []

    def restless(conn, *, dry_run=False):
        out = real(conn, dry_run=dry_run)
        if not dry_run:
            runs.append(1)
            if len(runs) > 1:
                conn.execute("UPDATE entity_blackholes SET aliases_json='[]' WHERE entity_id=''")
                conn.commit()
        return out
    monkeypatch.setattr(contact_excludes, "carry_contact_excludes", restless)
    message = _fails(_built(tmp_path, _rehearsal()[0]))
    assert "is not idempotent: a second run added 0 Off-limits entries and changed or removed" in message


def test_the_matrix_fails_when_the_step_disturbs_an_entry_the_owner_made(tmp_path, monkeypatch):
    path = _built(tmp_path, _rehearsal()[0])
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO entity_blackholes (blackhole_id, normalized_name, canonical_name, rebuild_state, note) "
                 "VALUES ('bh_owner_made', 'halcyon verity-marsh', 'Halcyon Verity-Marsh', 'complete', 'mine')")
    conn.commit()
    conn.close()
    real = contact_excludes.carry_contact_excludes

    def overwrites(conn, *, dry_run=False):
        out = real(conn, dry_run=dry_run)
        if not dry_run:
            conn.execute("UPDATE entity_blackholes SET note=? WHERE blackhole_id='bh_owner_made'",
                         (contact_excludes.NOTE,))
            conn.commit()
        return out
    monkeypatch.setattr(contact_excludes, "carry_contact_excludes", overwrites)
    assert "changed or removed 1 Off-limits entry that were there before it ran" in _fails(path)


def test_the_matrix_fails_when_a_dry_run_writes(tmp_path, monkeypatch):
    real = contact_excludes.carry_contact_excludes
    monkeypatch.setattr(contact_excludes, "carry_contact_excludes", lambda conn, *, dry_run=False: real(conn))
    assert "wrote to the Off-limits list in a dry run" in _fails(_built(tmp_path, _rehearsal()[0]))


def _ran_but_not_yet_judged(path, stack):
    """The matrix's own sequence up to the carry assertions, so a test can damage what they read."""
    from topos.storage.db.migrations import ensure_migrations_applied
    from topos.upgrades.runner import plan_upgrade, run_pending_upgrades
    _from, stage = _rehearsal()
    shipped, staged = matrix.shipped_for_run(stage)
    if staged:
        assert stack.enter_context(matrix.staged_unreleased(stage)) == shipped
    conn = sqlite3.connect(str(path))
    stack.callback(conn.close)
    ensure_migrations_applied(conn, skip_backup=True)
    steps = plan_upgrade(conn, shipped=shipped)["steps"]
    step = next(step for step in steps if step["id"] == STEP_ID)
    state = matrix._carry_before(conn, step, steps)
    run_pending_upgrades(conn, shipped=shipped, executors=_only_the_carry_step_is_real())
    return conn, step, state, shipped


@pytest.mark.parametrize("damage,said", [
    ("UPDATE derivation_ledger SET started_at=NULL WHERE step_id=?", "ledger row is 'done' without a real duration"),
    ("UPDATE derivation_ledger SET finished_at=NULL WHERE step_id=?", "ledger row is 'done' without a real duration"),
    ("UPDATE derivation_ledger SET finished_at=datetime(started_at, '-5 seconds') WHERE step_id=?",
     "ledger row is 'done' without a real duration"),
    ("UPDATE derivation_ledger SET status='running' WHERE step_id=?", "ledger row is 'running' under"),
    ("UPDATE derivation_ledger SET version='0.0.1' WHERE step_id=?", "ledger row is 'done' under '0.0.1'"),
    ("UPDATE derivation_ledger SET detail_json='{}' WHERE step_id=?", "ledger row reports"),
    ("DELETE FROM derivation_ledger WHERE step_id=?", "has 0 ledger rows, expected 1"),
])
def test_the_matrix_fails_on_a_ledger_row_that_does_not_show_the_work(tmp_path, damage, said):
    import contextlib
    os.environ["TOPOS_UPGRADE_RUNNER"] = "on"
    with contextlib.ExitStack() as stack:
        conn, step, state, shipped = _ran_but_not_yet_judged(_built(tmp_path, _rehearsal()[0]), stack)
        matrix._assert_carry_step(conn, step, dict(state), shipped)          # sound as it stands
        # The second run inside the assertions wrote nothing, so the row is as the runner left it.
        conn.execute(damage, (STEP_ID,))
        conn.commit()
        with pytest.raises(AssertionError) as failure:
            matrix._assert_carry_step(conn, step, dict(state), shipped)
        assert str(failure.value).startswith(f"step {STEP_ID!r} ") and said in str(failure.value)


# --- a fixture with nothing for the step to act on is refused, not passed ------------------------------------

@pytest.mark.parametrize("damage,said", [
    ("DROP TABLE contacts", "has nothing to act on"),
    ("UPDATE contacts SET sharing_policy_json=NULL", "does not hold the contacts this job declares"),
    ("DELETE FROM contacts", "does not hold the contacts this job declares"),
    ("DELETE FROM entities WHERE contact_id IS NOT NULL", "linked entities are missing"),
    ("INSERT INTO contacts (contact_id, dataset_id, source_id, sharing_policy_json) VALUES "
     "('undeclared-contact', 'default', 'upgrade_fixture_contacts', '{\"row_visibility\": \"exclude_from_grants\"}')",
     "1 undeclared with a stored choice"),
])
def test_a_fixture_the_step_cannot_be_judged_on_is_refused_before_anything_runs(tmp_path, damage, said):
    path = _built(tmp_path, _rehearsal()[0])
    conn = sqlite3.connect(str(path))
    conn.execute(damage)
    conn.commit()
    conn.close()
    assert said in _fails(path)
    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT COUNT(*) FROM derivation_ledger").fetchone()[0] == 0      # refused before the run
    conn.close()


# --- staging: the cut's own stamping, in a scratch copy ------------------------------------------------------

def _manifest(tmp_path, *releases):
    """Point the package at a manifest of the test's own (the autouse fixture points it back)."""
    path = tmp_path / "manifests.json"
    path.write_text(json.dumps({"releases": list(releases)}), encoding="utf-8")
    upgrades._MANIFESTS_PATH = path
    return path


STAGED_STEP = {"id": "an-invented-step", "kind": "engine_endpoint", "why": "a test"}


def test_staging_runs_the_unreleased_entry_as_a_release_and_writes_nothing_back(tmp_path):
    path = _manifest(tmp_path, {"version": "7.0.0", "steps": []}, {"version": "7.0.1", "steps": []},
                     {"version": "unreleased", "summary": "Staging entry", "steps": [STAGED_STEP], "notes": []})
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert upgrades.steps_between("7.0.1", "7.1.0") == []                  # never planned while it is staged
    with matrix.staged_unreleased("7.1.0") as version:
        assert version == "7.1.0" and upgrades._MANIFESTS_PATH != path
        assert [step["id"] for step in upgrades.steps_between("7.0.1", "7.1.0")] == ["an-invented-step"]
        assert upgrades.declaring_versions()["an-invented-step"] == "7.1.0"
        assert upgrades.load_unreleased()["steps"] == []                   # as cut_release leaves it
        scratch = upgrades._MANIFESTS_PATH
    assert upgrades._MANIFESTS_PATH == path and not scratch.exists()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert cut_release.MANIFESTS == SCRIPTS.parent / "topos" / "upgrades" / "manifests.json"


def test_with_nothing_staged_the_flag_changes_nothing_and_says_so(tmp_path, capsys):
    """A tagged checkout: the staging entry is empty, the manifest is the release, and one command line serves."""
    ladder = _manifest(tmp_path, {"version": "7.0.0", "steps": []}, {"version": "7.0.1", "steps": [STAGED_STEP]},
                       {"version": "unreleased", "steps": [], "notes": []})
    assert matrix._staged_steps(ladder) == []
    current = cut_release._read_current_version()
    assert matrix.shipped_for_run(matrix.NEXT, ladder) == (current, False)
    assert matrix.shipped_for_run(None, ladder) == (current, False)
    staged = _manifest(tmp_path, {"version": "7.0.1", "steps": []}, {"version": "unreleased", "steps": [STAGED_STEP]})
    assert matrix._staged_steps(staged) == ["an-invented-step"]
    assert matrix.shipped_for_run("7.1.0", staged) == ("7.1.0", True)
    assert matrix.shipped_for_run(None, staged) == (current, False)         # never staged unless asked


def test_staging_refuses_a_version_that_is_not_above_the_newest_release(tmp_path):
    path = _manifest(tmp_path, {"version": "7.0.1", "steps": []},
                     {"version": "unreleased", "steps": [STAGED_STEP]})
    for version in ("7.0.1", "7.0.0"):
        with pytest.raises(SystemExit) as refused:
            with matrix.staged_unreleased(version):
                pass
        assert "not above the newest release" in str(refused.value)
    assert upgrades._MANIFESTS_PATH == path


def test_the_repository_manifest_and_version_are_not_written_by_a_staged_run(tmp_path):
    version_file = SCRIPTS.parent / "topos" / "__version__.py"
    manifest = SCRIPTS.parent / "topos" / "upgrades" / "manifests.json"
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (version_file, manifest)]
    _run(_built(tmp_path, _rehearsal()[0]))
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in (version_file, manifest)] == before


def test_an_unstaged_run_says_the_step_did_not_run(tmp_path, monkeypatch, capsys):
    """Before the cut the step is under `unreleased`: a run without staging must not read as having checked it."""
    if upgrades.declaring_versions().get(STEP_ID):
        pytest.skip("the step is stamped on this tree: every run plans it")
    monkeypatch.setattr(matrix, "_assert_steps_did_work", lambda *args: None)
    matrix.run_matrix(_built(tmp_path, FLOOR), executors=_only_the_carry_step_is_real())
    printed = capsys.readouterr().out
    assert f"note: {STEP_ID} is not in this plan, so its assertions did not run" in printed
    assert "--stage-unreleased rehearses it" in printed


# --- the second from-version --------------------------------------------------------------------------------

def test_the_previous_release_is_read_from_the_ladder_not_worked_out_from_the_number(tmp_path):
    # For 1.5.0 the tag build's arithmetic gave 1.4.0; nodes run 1.4.4.
    assert matrix.previous_release("1.5.0") == "1.4.4"
    ladder = _manifest(tmp_path, {"version": "7.0.0", "steps": []}, {"version": "7.0.9", "steps": []},
                       {"version": "7.0.10", "steps": []}, {"version": "unreleased", "steps": []})
    assert matrix.previous_release("7.1.0", ladder) == "7.0.10"            # by number, not by text
    assert matrix.previous_release("7.0.10", ladder) == "7.0.9"
    with pytest.raises(SystemExit):
        matrix.previous_release("7.0.0", ladder)


def test_the_command_line_prints_the_from_version_of_the_second_fixture(capsys):
    """What the workflows build their second fixture from: the release before the one the run treats as shipped.
    With a step staged that is the package version (1.4.4 while the carry step waits for 1.5.0); on a tagged
    checkout, and without the flag, it is the release before the package version."""
    current = cut_release._read_current_version()
    assert matrix.main(["--print-previous-release", "--stage-unreleased"]) == 0
    staged = capsys.readouterr().out.strip()
    assert matrix.main(["--print-previous-release"]) == 0
    unstaged = capsys.readouterr().out.strip()
    assert unstaged == matrix.previous_release(current)
    assert staged == (current if matrix._staged_steps() else unstaged)

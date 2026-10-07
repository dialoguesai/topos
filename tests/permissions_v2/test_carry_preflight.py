"""The exclude carry rehearsed on a copy of a home (scripts/permissions_v2/carry_preflight.py; fifth round, R3-M3 c).

protects: the upgrade step that carries the older per-person excludes into Off-limits can leave a node where every
share refuses, when one contact holds a value the share boundary cannot read, and the step's own dry run cannot see
it (it writes nothing, so the boundary has nothing to read). The rehearsal is the REAL step on a stopped COPY of the
home, then the boundary built over the copy. It reads a copy of a real person's home, so it prints counts and fixed
words only, never a name, a handle or an id; and it writes, so it must refuse the live home, a home a node may be
running in, and any folder it was not told is a copy.

Every home here is built in the test's own temporary folder from invented rows; the second re-check's unreadable
values (its `test_s5n_r3_hold.py`) are among them. Every person, handle and id is invented.
"""
from __future__ import annotations

import fcntl
import importlib
import json
import socket
import sqlite3
import sys
from pathlib import Path

import pytest

from tests.topos.test_carry_step_names_what_it_cannot_read import ODD, SAVED, UNREADABLE, odd
from tests.topos.test_carry_step_review_r1 import HEART, PHONE, cid, contact, entity
from topos.features.lifecycle import carry_diagnosis
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.permissions_v2.protection_clock import TOMBSTONES_SQL
from topos.storage.canonical import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations, max_migration_order, read_user_version

pytestmark = pytest.mark.public

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
preflight = importlib.import_module("carry_preflight")
cs = importlib.import_module("census_support")

OWNERS_OWN = "Halcyon Verity-Marsh"
ADDRESS = "wilf@fernmail.example"
#: Everything invented that a home here holds and no output may show.
HELD = [SAVED, "Brisa", "Vantongeren", "Bree V.", "Briony", "Valcourt", "Will", "Mine", OWNERS_OWN, "Halcyon", ADDRESS,
        "fernmail", "breev", "brisavt", "0142", ":contact:", "ent-1", "ent-odd", "bh_"]


@pytest.fixture(autouse=True)
def _scratch_environment(tmp_path, monkeypatch):
    """What the script requires before it imports engine code; and a stand-in for the live home, so no test names
    the real one."""
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(tmp_path / "scratch" / "throwaway.db"))
    monkeypatch.setenv("TOPOS_ENV_FILE", str(tmp_path / "scratch" / "topos.env"))
    monkeypatch.setattr(cs, "LIVE_HOME", tmp_path / "the-live-home")


def make_home(root: Path, *, fill=None) -> Path:
    """One invented node home, closed: `database.db` beside `permissions-v2/`, as a real one is laid out. Four
    excluded contacts (one for each way an entry is named), the owner's own card, a contact with no stored choice,
    and one entry the owner had made himself."""
    (root / "permissions-v2").mkdir(parents=True, mode=0o700)
    conn = sqlite3.connect(root / "database.db")
    apply_all_migrations(conn)
    conn.execute(TOMBSTONES_SQL)
    ConversationsTablesManager(conn).ensure_tables()
    contact(conn, cid("0a"), "Bree V.", handles=[(PHONE, "phone")], usernames=["breev"])
    entity(conn, "ent-1", "Briony Valcourt", cid("0a"), aliases=["Bree"])
    contact(conn, cid("0c"), "Will")
    contact(conn, cid("0d"), HEART, handles=[(ADDRESS, "email")])
    contact(conn, cid("0e"), None)
    contact(conn, cid("0f"), "Mine", is_self=1)
    contact(conn, cid("10"), "Perrin Ashgrove", policy=None)
    BlackholeStore(conn).blackhole_entity(entity_ref=OWNERS_OWN)
    if fill is not None:
        fill(conn)
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    return root


@pytest.fixture
def home(tmp_path):
    return make_home(tmp_path / "a-copy")


def run(root, capsys, *flags):
    code = preflight.main(["--copy-root", str(root), *flags])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def nothing_held_is_shown(text: str) -> None:
    lowered = text.lower()
    for value in HELD:
        assert value.lower() not in lowered, f"an invented value of the home is in the output ({len(value)} chars)"


def test_a_home_the_step_carries_cleanly_is_ready(home, capsys):
    code, out, err = run(home, capsys, "--this-is-a-copy")
    report = json.loads(out)
    assert code == 0 and err == ""
    assert report == {
        "schema": "carry-preflight/v1", "verdict": "ready", "boundary": "built", "reaches_contacts": 4,
        "schema_version": {"before": max_migration_order(), "after": max_migration_order()},
        "step": {"explicit_excludes": 5, "carried": 4, "added_to_existing": 0, "already_off_limits": 0,
                 "carried_before": 0, "own_card_skipped": 1, "failed": 0, "waiting": 4,
                 "named_by": {"linked_entity": 1, "name": 1, "handle": 1, "contact_id_only": 1}},
        "second_run": {"carried": 0, "added_to_existing": 0, "failed": 0},
    }
    nothing_held_is_shown(out)
    # the step ran for real, on the copy: the entries are there, carried and waiting, beside the owner's own
    conn = sqlite3.connect(home / "database.db")
    assert sorted(bool(e["carried_waiting"]) for e in BlackholeStore(conn).list()) == [False, True, True, True, True]
    conn.close()
    assert not any((home / ("database.db" + suffix)).exists() for suffix in cs.SIDECARS)     # and it is closed again


@pytest.mark.parametrize("label", list(UNREADABLE))
def test_a_value_the_boundary_cannot_read_is_found_before_the_upgrade(tmp_path, capsys, label):
    """What the dry run cannot see: the same home with one of the re-check's unreadable values on one more excluded
    contact. The report gives the boundary's code, the kind of value, the entry by its position and whether
    removing it would be enough; and nothing else about it."""
    shape, kind, enough = UNREADABLE[label]
    root = make_home(tmp_path / "a-copy", fill=lambda conn: odd(conn, **shape))
    code, out, _err = run(root, capsys, "--this-is-a-copy")
    report = json.loads(out)
    assert code == 1 and report["verdict"] == "boundary_refuses"
    assert report["boundary"] == "entity_protection_lineage_unavailable" and "reaches_contacts" not in report
    assert (report["step"]["carried"], report["step"]["failed"], report["second_run"]) == (
        5, 0, {"carried": 0, "added_to_existing": 0, "failed": 0})
    conn = sqlite3.connect(root / "database.db")
    order = [entry["blackhole_id"] for entry in BlackholeStore(conn).list()]
    made = conn.execute("SELECT blackhole_id FROM off_limits_contact_carries WHERE contact_id=?", (ODD,)).fetchone()[0]
    conn.close()
    assert report["refusal"] == {"entries": [{"position": order.index(made) + 1, "kinds": [kind]}], "of": 6,
                                 "kinds": [kind], "removing_them_is_enough": enough}
    assert set(report["refusal"]["kinds"]) <= set(carry_diagnosis.KINDS)
    nothing_held_is_shown(out)


def test_the_dry_run_of_the_same_home_sees_nothing(tmp_path):
    """The reason this script exists, pinned: the step's dry run reports no boundary at all."""
    from topos.features.lifecycle.contact_excludes import carry_contact_excludes

    root = make_home(tmp_path / "a-copy", fill=lambda conn: odd(conn, usernames="[null]"))
    conn = sqlite3.connect(root / "database.db")
    dry = carry_contact_excludes(conn, dry_run=True)
    conn.close()
    assert (dry["counts"]["explicit_excludes"], dry["failed"], dry["boundary"]) == (6, 0, "not_built")


def test_it_runs_only_on_what_it_is_told_is_a_copy(home, capsys):
    before = (home / "database.db").read_bytes()
    code, out, err = run(home, capsys)
    assert (code, out, json.loads(err)) == (2, "", {"refused": "not_told_it_is_a_copy"})
    assert (home / "database.db").read_bytes() == before
    with pytest.raises(cs.CensusRefused):
        preflight.preflight(home, this_is_a_copy="yes")                   # only the flag itself, never a truthy word


def test_the_live_home_is_refused_by_path_and_through_a_link(tmp_path, capsys):
    live = make_home(tmp_path / "the-live-home")
    before = (live / "database.db").read_bytes()
    assert json.loads(run(live, capsys, "--this-is-a-copy")[2]) == {"refused": "live_store_refused"}
    (tmp_path / "a-link-to-it").symlink_to(live)
    assert json.loads(run(tmp_path / "a-link-to-it", capsys, "--this-is-a-copy")[2]) == {"refused": "live_store_refused"}
    (live / "inside").mkdir()
    make_home(live / "inside" / "a-copy")
    assert json.loads(run(live / "inside" / "a-copy", capsys, "--this-is-a-copy")[2]) == {"refused": "live_store_refused"}
    assert (live / "database.db").read_bytes() == before


def test_a_folder_a_node_may_be_running_in_is_refused(home, tmp_path, capsys, monkeypatch):
    before = (home / "database.db").read_bytes()
    held = open(home / "permissions-v2" / "protocol.lock", "a+")
    try:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)        # as the sharing runtime holds it
        assert json.loads(run(home, capsys, "--this-is-a-copy")[2]) == {"refused": "node_lock_held"}
    finally:
        held.close()
    (home / "permissions-v2" / "protocol.lock").unlink()
    monkeypatch.chdir(home)                             # a socket path is short: bind by a relative name
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind("engine.sock")
        assert json.loads(run(home, capsys, "--this-is-a-copy")[2]) == {"refused": "node_socket_present"}
    finally:
        listener.close()
    (home / "engine.sock").unlink()
    for sidecar in ("database.db-wal", "database.db-shm", "database.db-journal"):
        (home / sidecar).write_bytes(b"")
        assert json.loads(run(home, capsys, "--this-is-a-copy")[2]) == {"refused": "copy_not_closed"}
        (home / sidecar).unlink()
    assert (home / "database.db").read_bytes() == before                 # nothing was written by any refusal
    assert run(home, capsys, "--this-is-a-copy")[0] == 0                  # and with none of them it runs


def test_a_folder_that_is_not_a_home_and_an_environment_that_is_not_scratch_are_refused(tmp_path, capsys, monkeypatch):
    (tmp_path / "empty").mkdir()
    assert json.loads(run(tmp_path / "empty", capsys, "--this-is-a-copy")[2]) == {"refused": "canonical_database_missing"}
    assert json.loads(run(tmp_path / "absent", capsys, "--this-is-a-copy")[2]) == {"refused": "source_root_missing"}
    home = make_home(tmp_path / "a-copy")
    monkeypatch.delenv("TOPOS_DATABASE_PATH")
    assert json.loads(run(home, capsys, "--this-is-a-copy")[2]) == {"refused": "scratch_environment_required"}


def test_a_copy_from_a_newer_build_is_refused_and_not_written(home, capsys):
    conn = sqlite3.connect(home / "database.db")
    conn.execute(f"PRAGMA user_version = {max_migration_order() + 1}")
    conn.commit()
    conn.close()
    before = (home / "database.db").read_bytes()
    assert json.loads(run(home, capsys, "--this-is-a-copy")[2]) == {"refused": "copy_is_from_a_newer_build"}
    assert (home / "database.db").read_bytes() == before


def test_a_copy_of_an_older_schema_is_brought_to_this_builds_first_and_no_backup_is_written(home, capsys):
    conn = sqlite3.connect(home / "database.db")
    conn.execute(f"PRAGMA user_version = {max_migration_order() - 1}")   # as a home of the release before
    conn.commit()
    conn.close()
    code, out, _err = run(home, capsys, "--this-is-a-copy")
    assert code == 0 and json.loads(out)["schema_version"] == {"before": max_migration_order() - 1,
                                                              "after": max_migration_order()}
    assert sorted(path.name for path in home.iterdir()) == ["database.db", "permissions-v2"]   # no backups folder
    conn = sqlite3.connect(home / "database.db")
    assert read_user_version(conn) == max_migration_order()
    conn.close()

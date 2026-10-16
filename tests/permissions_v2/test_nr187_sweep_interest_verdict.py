"""NR187: the daemon sweep reuses an index's last True interest verdict while nothing it reads has moved.

protects: the browsing-interest members of a share's index are still decided at every sweep that could see a change.
The sweep (runtime.py, a 10 s gap after each pass) re-checked each index's interest members with one
`interest_family.build` of their clusters, about 4.6 s of CPU per index on the owner's node (R12). Behind
`TOPOS_PERMISSIONS_V2_SWEEP_INTEREST_VERDICT` (off by default) a True verdict is kept per index file with the state it
was proven under: the file's stat identity, the key's digest, the canonical database's token (read before the sweep's
snapshot and again at the check), the Off-limits closure's revision and the interest switches and revisions. These tests
pin that:
  - each part of that state, moved alone, forces the verdict to be computed again, and a token that moved between the
    sweep's snapshot and the check is never kept;
  - every change that drops an index on main (an Off-limits entry, an exclusion, a grant change, a label reassessment,
    an opt-out, a second-label or family switch flip, a relabel or changed visit) drops it with the verdict warm, and
    the sweep decides exactly as with the switch off, sweep for sweep;
  - a False verdict is never kept; the memo is bounded and forgets indexes that are gone;
  - with the switch off nothing is kept or read: the sweep is main's;
  - the memo holds a boolean and change tokens only: no name, label, member, Build or Snapshot (DR-19).

All browsing here is synthetic: invented labels, hosts, titles and names.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.permissions_v2.interest_fixtures import open_db
from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_interest_door import (  # noqa: F401 -- `flag` is a fixture
    DATASET, LABEL, NOW, OWNER, SECOND, SITE_HOST, SITE_LABEL, _assess, _attest, _db, _interests, _node, _rebuild,
    _second_label, _visits, flag,
)
from tests.permissions_v2.interest_fixtures import at, cluster, install, visit
from topos.permissions_v2 import interest_family as fam
from topos.permissions_v2 import interest_index
from topos.permissions_v2 import interest_relabel as rl
from topos.permissions_v2 import search_index as si
from topos.permissions_v2 import switches
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.permissions_v2.search_index import SearchIndexService, index_path, root_for

pytestmark = pytest.mark.public

SWITCH = switches.SWEEP_INTEREST_VERDICT.name
BLACKHOLE = ("INSERT INTO entity_blackholes (blackhole_id, entity_id, canonical_name, normalized_name, rebuild_state) "
             "VALUES (?, '', ?, ?, 'complete')")


# --- invented nodes ----------------------------------------------------------------------------------------------

def _canonical(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "canonical.db"
    conn = open_db(path)
    conn.execute("PRAGMA journal_mode=WAL")          # as a node's is: a writer commits while the sweep reads
    conn.execute("UPDATE engine_config SET value=? WHERE key='user_id'", (OWNER,))
    install(conn, user=OWNER, dataset=DATASET)
    conn.commit()
    conn.close()
    ensure_protection_clock(path, owner_id=OWNER)
    return path


def browsing(root: Path) -> Path:
    """test_interest_door's `browsing`: one cluster, 5 visits on 3 days in August, 16 on 3 days in September. Also an
    Off-limits entry, a person and an excluded entity that touch no visit and no label, so the names exist on the
    node (and in the process) while the verdict is kept."""
    path = _canonical(root)
    with _db(path) as conn:
        _attest(conn)
        cluster(conn, "tc_hobby", LABEL)
        _visits(conn, 0, 5, [3, 9, 17])
        _visits(conn, 100, 16, [1, 5, 19], month=9)
        conn.execute(BLACKHOLE, ("bh-1", "Pemberly Hollis", "pemberly hollis"))
        conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json) "
                     "VALUES ('p-1','person','Orsolya Brandtvik','orsolya brandtvik','[\"Ossiwyn\"]')")
        conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name) "
                     "VALUES ('e-9','organization','Quillon Vantress Guild','quillon vantress guild')")
        conn.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
                     "VALUES ('x-9','entity','e-9')")
    _assess(path)
    return path


def site(root: Path) -> Path:
    """test_interest_door's `site`: a label that names its pages' site, standing in for by an assessed second label."""
    path = _canonical(root)
    with _db(path) as conn:
        _attest(conn)
        cluster(conn, "tc_hobby", SITE_LABEL)
        _visits(conn, 0, 5, [3, 9, 17], host=SITE_HOST)
    _second_label(path, SECOND)
    _assess(path)
    return path


class Arm:
    """One node, its index built, and its sweeps with the NR187 switch on or off."""

    def __init__(self, root: Path, monkeypatch, *, on: bool, build=browsing, members: int = 2):
        self.on, self.monkeypatch = on, monkeypatch
        self.path = build(root / "node")
        self.node = _node(self.path, root, monkeypatch)
        assert _rebuild(self.node) == {"state": "ready", "member_count": members}
        self.file = index_path(root_for(self.path), "grant-search")

    @property
    def kept(self) -> dict:
        return self.node.index._interest_kept

    def sweep(self, now: int = NOW) -> int:
        self.monkeypatch.setenv(SWITCH, "true" if self.on else "false")
        with owner():
            return self.node.index.sweep(now=now)


@pytest.fixture
def computed(monkeypatch):
    """The sweep's full interest decisions, per service (the ones made without a policy: a rebuild's gated recheck
    passes its grant's), with the opt-outs each was given and its verdict."""
    seen: dict = {}
    real = SearchIndexService._interests_current

    def counted(self, conn, sealed_members, policy, boundary, opt_outs=frozenset()):
        verdict = real(self, conn, sealed_members, policy, boundary, opt_outs)
        if policy is None:
            seen.setdefault(id(self), []).append((opt_outs, verdict))
        return verdict
    monkeypatch.setattr(SearchIndexService, "_interests_current", counted)

    def count(arm) -> int:
        return len(seen.get(id(arm.node.index), []))
    count.seen = seen
    return count


@contextmanager
def stale_stages():
    """The `stale (<stage>)` warnings the search index logs inside the block, in order."""
    found: list = []

    class Catch(logging.Handler):
        def emit(self, record):
            match = re.fullmatch(r"message search index stale \((\w+)\)", record.getMessage())
            if match:
                found.append(match.group(1))
    handler, logger = Catch(), logging.getLogger("topos.permissions_v2.search_index")
    logger.addHandler(handler)
    try:
        yield found
    finally:
        logger.removeHandler(handler)


def _warm(arm, computed) -> tuple:
    """Two sweeps: the first proves the verdict, the second reuses it. Returns the kept state."""
    before = computed(arm)
    assert arm.sweep() == 0 and computed(arm) == before + 1
    assert arm.sweep() == 0 and computed(arm) == before + 1, "a quiet node's second sweep rebuilt the interests"
    (state,) = arm.kept.values()
    return state


# --- reuse, and each part of the state ---------------------------------------------------------------------------

def test_a_true_verdict_is_reused_while_nothing_moves(flag, tmp_path, monkeypatch, computed):
    arm = Arm(tmp_path, monkeypatch, on=True)
    _warm(arm, computed)
    for _ in range(3):
        assert arm.sweep() == 0
    assert computed(arm) == 1 and list(arm.kept) == [arm.file.name] and arm.file.exists()


@pytest.mark.parametrize("part", si._INTEREST_STATE_PARTS)
def test_each_part_of_the_state_moved_alone_forces_a_recompute(flag, tmp_path, monkeypatch, computed, part):
    """The kept state differs from the current one in exactly this part: the verdict is computed again and the state
    it is now proven under is kept (the index is still current, so nothing goes)."""
    arm = Arm(tmp_path, monkeypatch, on=True)
    state = _warm(arm, computed)
    position = si._INTEREST_STATE_PARTS.index(part)
    arm.kept[arm.file.name] = tuple(("moved",) if i == position else value for i, value in enumerate(state))
    assert arm.sweep() == 0
    assert computed(arm) == 2 and arm.kept[arm.file.name] == state
    assert arm.sweep() == 0 and computed(arm) == 2


def _real_change(arm, change):
    if change == "index_file_touched":           # the same bytes, another stat identity
        info = arm.file.stat()
        os.utime(arm.file, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
    elif change == "unrelated_commit":           # a commit no interest reads
        with _db(arm.path) as conn:
            conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('nr187-probe', 'x')")
    elif change == "unrelated_visit_elsewhere":  # a visit of no cluster
        with _db(arm.path) as conn:
            visit(conn, 900, at(8, 25), dataset=DATASET, cluster_id=None)
    elif change == "second_label_switch_off":    # no second label here, so the verdict stands
        arm.monkeypatch.setenv(rl.FLAG, "off")


@pytest.mark.parametrize("change, part", [("index_file_touched", "file"), ("unrelated_commit", "canonical"),
                                          ("unrelated_visit_elsewhere", "canonical"),
                                          ("second_label_switch_off", "inputs")])
def test_a_real_change_of_one_part_forces_a_recompute(flag, tmp_path, monkeypatch, computed, change, part):
    arm = Arm(tmp_path, monkeypatch, on=True)
    state = _warm(arm, computed)
    _real_change(arm, change)
    assert arm.sweep() == 0 and computed(arm) == 2
    moved = [name for name, old, new in zip(si._INTEREST_STATE_PARTS, state, arm.kept[arm.file.name]) if old != new]
    assert moved == [part]
    assert arm.sweep() == 0 and computed(arm) == 2           # proven again under the new state, then reused


def test_a_token_that_moved_since_before_the_snapshot_is_neither_reused_nor_kept(flag, tmp_path, monkeypatch, computed):
    """The canonical token is read before the sweep's snapshot and again at the check. When they differ a commit may
    have landed in between, so the snapshot is not known to hold the rows either token names."""
    arm = Arm(tmp_path, monkeypatch, on=True)
    _warm(arm, computed)
    real = SearchIndexService._canonical_token
    calls = []

    def first_moved(self):
        calls.append(1)
        token = real(self)
        return (*token, "moved") if len(calls) == 1 else token   # the sweep's read before its snapshot
    monkeypatch.setattr(SearchIndexService, "_canonical_token", first_moved)
    assert arm.sweep() == 0 and computed(arm) == 2 and arm.kept == {}
    assert arm.sweep() == 0 and computed(arm) == 3 and list(arm.kept) == [arm.file.name]
    assert arm.sweep() == 0 and computed(arm) == 3


@pytest.mark.parametrize("unreadable", ["canonical_token", "inputs"])
def test_an_unreadable_part_never_reuses_or_keeps(flag, tmp_path, monkeypatch, computed, unreadable):
    arm = Arm(tmp_path, monkeypatch, on=True)
    _warm(arm, computed)
    if unreadable == "canonical_token":
        monkeypatch.setattr(SearchIndexService, "_canonical_token", lambda self: None)
    else:
        monkeypatch.setattr(si, "_interest_inputs", lambda: None)
    for count in (2, 3):
        assert arm.sweep() == 0 and computed(arm) == count and arm.kept == {}


@pytest.mark.parametrize("when", ["once_the_snapshot_is_established", "while_the_index_file_is_read"])
def test_a_commit_between_the_snapshot_and_the_check_is_never_kept(flag, tmp_path, monkeypatch, computed, when):
    """A relabel commits after the sweep's snapshot is established: right after its first read (`clock_state`), or
    while the index file is read (`_open`). The sweep decides on rows from before it, True, and must not keep that
    verdict under the token read after it. On the next sweep the relabel is visible and the index goes, exactly as
    with the switch off."""
    outcomes = {}
    for on in (True, False):
        arm = Arm(tmp_path / str(on), monkeypatch, on=on)
        assert arm.sweep() == 0

        def relabel(arm=arm):
            with _db(arm.path) as conn:
                cluster(conn, "tc_hobby", "sourdough / bread")
        real_open, real_clock = SearchIndexService._open, si.clock_state

        def opening(path):
            relabel()
            return real_open(path)

        def clocking(conn):
            state = real_clock(conn)
            relabel()
            return state
        if when == "while_the_index_file_is_read":
            monkeypatch.setattr(SearchIndexService, "_open", staticmethod(opening))
        else:
            monkeypatch.setattr(si, "clock_state", clocking)
        first = arm.sweep()
        monkeypatch.setattr(SearchIndexService, "_open", staticmethod(real_open))
        monkeypatch.setattr(si, "clock_state", real_clock)
        outcomes[on] = (first, arm.sweep(), arm.file.exists())
    assert outcomes[True] == outcomes[False] == (0, 1, False)


def test_a_check_outside_an_open_read_transaction_is_never_kept(flag, tmp_path, monkeypatch, computed):
    """The kept state says what one snapshot held. A connection with no open read transaction has no snapshot (each
    read sees the newest rows), so its True verdict is returned and not kept; on an open one it is kept."""
    arm = Arm(tmp_path, monkeypatch, on=True)
    service = arm.node.index
    key = service.keys.get("grant-search", create=False)
    sealed = SearchIndexService._open(arm.file)["sealed"]
    interests = [member for member in (si.unseal(key, opaque, blob) for opaque, blob in sealed)
                 if member.get("table") == "activity_events"]
    assert len(interests) == 2
    for in_transaction in (False, True):
        before = service._canonical_token()
        conn = sqlite3.connect(arm.path.as_uri() + "?mode=ro", uri=True)
        try:
            if in_transaction:
                conn.execute("BEGIN")
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            verdict = service._interests_current_kept(conn, interests, EntityBoundary(conn), key, arm.file,
                                                      si._file_state(arm.file), before)
        finally:
            conn.close()
        assert verdict is True and (arm.file.name in arm.kept) is in_transaction
    assert computed(arm) == 2


def test_a_file_replaced_while_it_was_read_is_decided_in_full(flag, tmp_path, monkeypatch, computed):
    """A publish (os.replace) between the stat taken before `_open` and the read: the verdict kept for the old file
    is not used for what was read, and nothing is kept under either identity (BL-171's R-N154 M1)."""
    arm = Arm(tmp_path, monkeypatch, on=True)
    _warm(arm, computed)
    real_open = SearchIndexService._open

    def replaced(path):
        copy = Path(str(path) + ".copy")
        shutil.copy2(path, copy)
        os.replace(copy, path)                               # the same bytes, a new inode
        return real_open(path)
    monkeypatch.setattr(SearchIndexService, "_open", staticmethod(replaced))
    assert arm.sweep() == 0 and computed(arm) == 2 and arm.kept == {}
    monkeypatch.setattr(SearchIndexService, "_open", staticmethod(real_open))
    assert arm.sweep() == 0 and computed(arm) == 3 and list(arm.kept) == [arm.file.name]


# --- every change that drops an index on main still drops it, decided alike ------------------------------------

def _change(arm, change):
    """One owner-side or ingest-side change between two sweeps (invented rows)."""
    if change in ("second_label_switch_off", "interest_switch_off"):
        arm.monkeypatch.setenv(rl.FLAG if change == "second_label_switch_off" else switches.INTEREST_SOURCES.name,
                               "off")
        return
    if change == "grant_changed":
        arm.node.activate(arm.node.search_raw, generation=2)
        return
    if change == "opt_out":
        with owner():
            arm.node.index.reviews.opt_out(fam.opt_out_key("tc_hobby"), now=NOW)
        return
    if change == "reassessed":
        _assess(arm.path, {"domains": ["hobbies", "home"], "sensitivity": "none", "protected_content": "none"})
        return
    with _db(arm.path) as conn:
        if change == "off_limits_added":
            conn.execute(BLACKHOLE, ("bh-2", "Tamsin Okonkwo", "tamsin okonkwo"))
        elif change == "excluded_entity_named_by_the_label":
            conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name) "
                         "VALUES ('e-10','organization','Starter Supply','starter supply')")
            conn.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
                         "VALUES ('x-10','entity','e-10')")
        elif change == "excluded_visit":
            conn.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
                         "VALUES ('x-11','record','browser:v100')")
        elif change == "excluded_cluster":
            conn.execute("INSERT INTO intelligence_exclusions (exclusion_id, artifact_type, artifact_key) "
                         "VALUES ('x-12','record','tc_hobby')")
        elif change == "relabel":
            cluster(conn, "tc_hobby", "sourdough / bread")
        elif change == "edited_visit":
            conn.execute("UPDATE activity_events SET url='https://example.test/moved' WHERE event_id='browser:v100'")
        elif change == "backfilled_visit":
            visit(conn, 300, at(9, 19, hour=11), dataset=DATASET)
        elif change == "new_person_named_by_the_label":
            conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name) "
                         "VALUES ('p-2','person','Sourdough Baking','sourdough baking')")
        elif change == "visit_after_the_build":     # seen only at the read's clock: the index stays on main too
            visit(conn, 301, "2026-09-20T12:01:00.000Z", dataset=DATASET)
        else:
            raise AssertionError(change)


DROPPED = ["off_limits_added", "excluded_entity_named_by_the_label", "excluded_visit", "excluded_cluster",
           "grant_changed", "reassessed", "opt_out", "interest_switch_off", "relabel", "edited_visit",
           "backfilled_visit", "new_person_named_by_the_label"]


@pytest.mark.parametrize("change", DROPPED + ["visit_after_the_build"])
def test_a_change_drops_the_index_with_the_verdict_warm_as_it_does_with_the_switch_off(flag, tmp_path, monkeypatch,
                                                                                         computed, change):
    decided = {}
    for on in (True, False):
        arm = Arm(tmp_path / str(on), monkeypatch, on=on)
        if on:
            _warm(arm, computed)
        else:
            assert (arm.sweep(), arm.sweep()) == (0, 0)
        _change(arm, change)
        with stale_stages() as stages:
            removed = arm.sweep()
        decided[on] = (removed, arm.file.exists(), tuple(stages))
        if on and change in DROPPED:
            assert arm.kept == {}
        monkeypatch.delenv(rl.FLAG, raising=False)
        monkeypatch.setenv(switches.INTEREST_SOURCES.name, "true")
    assert decided[True] == decided[False]
    assert decided[True][:2] == ((1, False) if change in DROPPED else (0, True))


def test_a_second_label_switch_flip_drops_the_index_it_made_a_member(flag, tmp_path, monkeypatch, computed):
    """The verdict reads the second-label switch (`interest_family.build`), and no index basis carries it: a member
    admitted under a stored second label stops being one when the switch goes off, warm verdict or not."""
    decided = {}
    for on in (True, False):
        arm = Arm(tmp_path / str(on), monkeypatch, on=on, build=site, members=1)
        assert (arm.sweep(), arm.sweep()) == (0, 0)
        if on:
            assert computed(arm) == 1 and list(arm.kept) == [arm.file.name]
        monkeypatch.setenv(rl.FLAG, "off")
        with stale_stages() as stages:
            decided[on] = (arm.sweep(), arm.file.exists(), tuple(stages))
        monkeypatch.delenv(rl.FLAG)
    assert decided[True] == decided[False] == (1, False, ("interest",))


# --- a False verdict, the bound, the switch -----------------------------------------------------------------------

def test_a_false_verdict_is_never_kept_and_is_computed_every_sweep(flag, tmp_path, monkeypatch, computed):
    """With the removal held off, a stale index stays on disk: every sweep decides its interests again, False each
    time, and nothing is kept."""
    arm = Arm(tmp_path, monkeypatch, on=True)
    _warm(arm, computed)
    with _db(arm.path) as conn:
        cluster(conn, "tc_hobby", "sourdough / bread")
    monkeypatch.setattr(si, "_shred", lambda path: None)
    for count in (2, 3, 4):
        with stale_stages() as stages:
            assert arm.sweep() == 1
        assert stages == ["interest"] and computed(arm) == count and arm.kept == {}
        assert computed.seen[id(arm.node.index)][-1][1] is False
    assert arm.file.exists()


@pytest.mark.parametrize("warm", [True, False])
def test_the_interest_step_itself_keeps_no_false_verdict(flag, tmp_path, monkeypatch, computed, warm):
    """Read right after the interest step, before the sweep drops the stale index's entry: a False verdict computed
    on a provable state (nothing moved since before the snapshot) leaves no entry, whether or not one was kept."""
    arm = Arm(tmp_path, monkeypatch, on=True)
    if warm:
        _warm(arm, computed)
    with _db(arm.path) as conn:
        cluster(conn, "tc_hobby", "sourdough / bread")
    after_step = []
    real = SearchIndexService._interests_current_kept

    def observed(self, *args, **kwargs):
        verdict = real(self, *args, **kwargs)
        after_step.append((verdict, dict(self._interest_kept)))
        return verdict
    monkeypatch.setattr(SearchIndexService, "_interests_current_kept", observed)
    monkeypatch.setattr(si, "_shred", lambda path: None)
    for _ in range(2):
        assert arm.sweep() == 1
    assert after_step == [(False, {}), (False, {})]


def test_an_error_in_the_decision_keeps_nothing(flag, tmp_path, monkeypatch, computed):
    from topos.permissions_v2.canonical import PolicyError
    arm = Arm(tmp_path, monkeypatch, on=True)
    _warm(arm, computed)
    arm.kept[arm.file.name] = ("moved",) * len(si._INTEREST_STATE_PARTS)

    def refuses(self, *args, **kwargs):
        raise PolicyError("message_protection_too_large")
    monkeypatch.setattr(SearchIndexService, "_interests_current", refuses)
    monkeypatch.setattr(si, "_shred", lambda path: None)
    with stale_stages() as stages:
        assert arm.sweep() == 1
    assert stages == ["member_unavailable"] and arm.kept == {}


def test_the_memo_forgets_an_index_that_is_gone_and_is_bounded(flag, tmp_path, monkeypatch, computed):
    arm = Arm(tmp_path, monkeypatch, on=True)
    _warm(arm, computed)
    arm.node.index._keep_interest_verdict("grant-" + "0" * 32 + ".db", ("moved",))   # a file that does not exist
    assert len(arm.kept) == 2
    assert arm.sweep() == 0 and list(arm.kept) == [arm.file.name]
    si._shred(arm.file)                                      # gone (a purge, a forget, a lifecycle scrub)
    with owner():
        arm.node.index.sweep(now=NOW)
    assert arm.kept == {}
    monkeypatch.setattr(si, "INTEREST_KEPT_MAX", 3)
    for n in range(10):
        arm.node.index._keep_interest_verdict(f"grant-{n:032x}.db", ("moved",))
    assert list(arm.kept) == [f"grant-{n:032x}.db" for n in (7, 8, 9)]


def test_switching_off_forgets_every_kept_verdict(flag, tmp_path, monkeypatch, computed):
    arm = Arm(tmp_path, monkeypatch, on=True)
    _warm(arm, computed)
    arm.on = False
    assert arm.sweep() == 0 and arm.kept == {} and computed(arm) == 2
    arm.on = True
    assert arm.sweep() == 0 and computed(arm) == 3 and list(arm.kept) == [arm.file.name]


def test_with_nothing_set_the_switch_is_off_and_the_sweep_is_mains(flag, tmp_path, monkeypatch, computed):
    """Off by default, bound or not: no token is read, nothing is kept, and every sweep decides the interests with
    exactly main's arguments (no policy, no opt-outs)."""
    monkeypatch.delenv(SWITCH, raising=False)
    assert switches.on(switches.SWEEP_INTEREST_VERDICT) is False
    real_bound = switches.is_bound
    for bound in (True, False):
        monkeypatch.setattr(switches, "is_bound", lambda env=None, bound=bound: bound)
        assert switches.on(switches.SWEEP_INTEREST_VERDICT) is False
    monkeypatch.setattr(switches, "is_bound", real_bound)
    touched = []
    for name in ("_canonical_token", "_interests_current_kept", "_keep_interest_verdict"):
        real = getattr(SearchIndexService, name)
        monkeypatch.setattr(SearchIndexService, name,
                            lambda self, *a, real=real, name=name, **kw: touched.append(name) or real(self, *a, **kw))
    monkeypatch.setattr(si, "_interest_inputs", lambda: touched.append("inputs"))
    arm = Arm(tmp_path, monkeypatch, on=False)
    monkeypatch.delenv(SWITCH, raising=False)
    for count in (1, 2, 3):
        with owner():
            assert arm.node.index.sweep(now=NOW) == 0
        assert computed(arm) == count
    assert touched == [] and arm.kept == {}
    assert {opt_outs for opt_outs, _verdict in computed.seen[id(arm.node.index)]} == {frozenset()}


# --- what the memo holds ---------------------------------------------------------------------------------------

WORDS = ("pemberly", "hollis", "orsolya", "brandtvik", "ossiwyn", "quillon", "vantress", "sourdough", "baking",
         "starter", "example.test", "synthetic page", "tc_hobby", "browser:v")
_HEX = re.compile(r"[0-9a-f]{64}")
_FILE = re.compile(r"grant-[0-9a-f]{32}\.db")


def _tokens_only(value, depth=0) -> list:
    """Every leaf of the memo that is not a change token: anything but tuples of ints, booleans, None, hex digests and
    the one tag word of the canonical token."""
    if depth > 8:
        return [value]
    if isinstance(value, dict):
        return [bad for key, item in value.items() for bad in
                ((([] if isinstance(key, str) and _FILE.fullmatch(key) else [key])) + _tokens_only(item, depth + 1))]
    if isinstance(value, tuple):
        return [bad for item in value for bad in _tokens_only(item, depth + 1)]
    if value is None or type(value) in (bool, int):
        return []
    if type(value) is str and (_HEX.fullmatch(value) or value == "canonical"):
        return []
    return [value]


def _walk_holds(value, word: str, seen=None, depth=0) -> bool:
    """Whether a string (or bytes) holding `word` is reachable from `value`: through containers, an object's attributes
    and slots, a class's dict, and a function's attributes, default arguments and closure. Never into another module,
    a logger or a lock. `seen` keeps what it walked alive, so a freed copy cannot hand its id to the next."""
    seen = {} if seen is None else seen
    if id(value) in seen or depth > 10:
        return False
    seen[id(value)] = value
    if isinstance(value, (str, bytes)):
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        return word in text.casefold()
    if isinstance(value, (types.ModuleType, logging.Logger, logging.Handler)) or value is None:
        return False
    if isinstance(value, dict) or type(value).__name__ == "mappingproxy":
        return any(_walk_holds(k, word, seen, depth + 1) or _walk_holds(v, word, seen, depth + 1)
                   for k, v in list(value.items()))
    if isinstance(value, (set, frozenset, tuple, list)):
        return any(_walk_holds(item, word, seen, depth + 1) for item in list(value))
    if isinstance(value, (staticmethod, classmethod)):
        return _walk_holds(value.__func__, word, seen, depth + 1)
    if isinstance(value, property):
        return any(_walk_holds(part, word, seen, depth + 1) for part in (value.fget, value.fset, value.fdel))
    if isinstance(value, type):
        return _walk_holds(vars(value), word, seen, depth + 1)
    if isinstance(value, types.FunctionType):
        cells = []
        for cell in value.__closure__ or ():
            try:
                cells.append(cell.cell_contents)
            except ValueError:
                pass
        return any(_walk_holds(part, word, seen, depth + 1) for part in
                   (value.__dict__, value.__defaults__ or (), value.__kwdefaults__ or {}, tuple(cells)))
    if isinstance(value, types.MethodType):
        return _walk_holds(value.__self__, word, seen, depth + 1)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict) and _walk_holds(attributes, word, seen, depth + 1):
        return True
    slots = [name for klass in type(value).__mro__ for name in getattr(klass, "__slots__", ())]
    return any(_walk_holds(getattr(value, name, None), word, seen, depth + 1) for name in slots
               if isinstance(name, str))


def _module_state_holds(module, word: str) -> bool:
    """Whether the module's own state holds `word`: every global but other modules and names imported from them, each
    walked as `_walk_holds` walks (a class's dict, its functions' defaults and closures, any object's attributes)."""
    for name, value in list(vars(module).items()):
        if name.startswith("__") or isinstance(value, types.ModuleType):
            continue
        if (isinstance(value, (type, types.FunctionType, types.BuiltinFunctionType))
                and getattr(value, "__module__", None) != module.__name__):
            continue
        if _walk_holds(value, word):
            return True
    return False


def test_the_memo_holds_a_boolean_and_change_tokens_only(flag, tmp_path, monkeypatch, computed):
    """DR-19: nothing derived from the Off-limits list or excluded entities outlives a pass. With an Off-limits entry,
    an excluded entity and a person on the node and the verdict kept: the memo is change tokens only (no bytes, no
    text but hex digests, no Build, Snapshot, boundary or member), and neither it, the module, its classes (their
    functions' defaults, closures and attributes included) nor the service's NR187 state holds any of their words."""
    from tests.permissions_v2.test_interest_family import _module_holds
    arm = Arm(tmp_path, monkeypatch, on=True)
    _warm(arm, computed)
    conn = sqlite3.connect(arm.path)
    try:                                                     # the names really are what the sweep's builds read
        boundary = EntityBoundary(conn)
        built = fam.build(conn, owner_id=OWNER, now_us=NOW * 1_000_000, boundary=boundary)
        seen = (boundary.active and any("pemberly" in term for term in boundary.terms),
                {obj.label for obj in built.objects} == {LABEL},
                _walk_holds(built, "sourdough"), _walk_holds(boundary, "pemberly"))   # the walker sees them
        boundary = built = None
    finally:
        conn.close()
    assert seen == (True, True, True, True)
    memo = arm.kept
    assert memo and _tokens_only(memo) == []
    assert all(len(state) == len(si._INTEREST_STATE_PARTS) for state in memo.values())
    nr187_state = {name: value for name, value in vars(arm.node.index).items() if name.startswith("_interest")}
    assert sorted(nr187_state) == ["_interest_kept", "_interest_reuses"]
    reuses = nr187_state["_interest_reuses"]
    assert set(reuses) <= set(memo) and all(type(count) is int for count in reuses.values())
    for word in WORDS:
        assert not _walk_holds(memo, word), word
        assert not _module_holds(si, word), word
        assert not _module_state_holds(si, word), word
        assert not _walk_holds(vars(arm.node.index), word), word
    assert all(not isinstance(value, (fam.Build, fam.Snapshot, fam.InterestObject, EntityBoundary, types.FunctionType))
               for state in memo.values() for value in state)


def test_the_kept_state_is_the_file_the_key_the_token_the_closure_and_the_inputs(flag, tmp_path, monkeypatch, computed):
    """What each part is, read here by other means: the index file's stat identity, the key's sha256, the canonical
    token on the service's probe, the Off-limits closure's revision and the interest switches and revisions."""
    import hashlib
    from topos.permissions_v2 import interest_review as ir
    arm = Arm(tmp_path, monkeypatch, on=True)
    state = _warm(arm, computed)
    key = arm.node.index.keys.get("grant-search", create=False)
    conn = sqlite3.connect(arm.path.as_uri() + "?mode=ro", uri=True)
    try:
        revision = EntityBoundary(conn).revision
    finally:
        conn.close()
    token = arm.node.index._canonical_token()
    assert token[0] == "canonical" and type(token[1]) is int
    assert state == (si._file_state(arm.file), hashlib.sha256(key).hexdigest(), token, revision,
                     (True, True, ir.rubric_revision(), ir._model_revision()))


# --- equivalence over a scripted sequence ------------------------------------------------------------------------

def _step(arm, step):
    if step == "rebuild":
        return ("rebuild", _rebuild(arm.node))
    if step.startswith("change:"):
        name = step.split(":", 1)[1]
        if name in ("index_file_touched", "unrelated_commit", "unrelated_visit_elsewhere"):
            _real_change(arm, name)
        elif name == "second_label_switch_back":
            arm.monkeypatch.delenv(rl.FLAG, raising=False)
        elif name == "second_label_switch_off":
            arm.monkeypatch.setenv(rl.FLAG, "off")
        else:
            _change(arm, name)
        return ("change", name)
    with stale_stages() as stages:
        removed = arm.sweep()
    return ("sweep", removed, arm.file.exists(), tuple(stages))


SEQUENCE = ["sweep", "sweep", "sweep", "change:unrelated_commit", "sweep", "sweep", "change:index_file_touched",
            "sweep", "change:second_label_switch_off", "sweep", "sweep", "change:second_label_switch_back", "sweep",
            "change:visit_after_the_build", "sweep", "sweep", "change:unrelated_visit_elsewhere", "sweep",
            "change:relabel", "sweep", "sweep", "rebuild", "sweep", "rebuild", "sweep",
            "change:reassessed", "sweep", "rebuild", "sweep", "sweep", "change:off_limits_added", "sweep", "rebuild",
            "sweep", "sweep"]


def test_the_sweeps_decide_alike_with_the_switch_on_and_off_over_a_scripted_sequence(flag, tmp_path, monkeypatch,
                                                                                    computed):
    """The same invented node twice, one change at a time between sweeps: every sweep removes, keeps and logs the same
    with the switch on as off. Not vacuous: both arms hold interest members for most sweeps, the off arm decides them
    at every sweep, the on arm reuses at least five of those decisions, and some sweeps drop the index."""
    decided = {}
    for on in (True, False):
        arm = Arm(tmp_path / str(on), monkeypatch, on=on)
        decided[on] = [_step(arm, step) for step in SEQUENCE]
        decided[on, "computed"] = computed(arm)
        monkeypatch.delenv(rl.FLAG, raising=False)
    assert decided[True] == decided[False]
    sweeps = [entry for entry in decided[False] if entry[0] == "sweep"]
    assert sum(entry[1] for entry in sweeps) >= 2 and sum(entry[2] for entry in sweeps) >= 10
    assert any(entry[0] == "rebuild" and entry[1]["member_count"] == 2 for entry in decided[False])
    assert decided[False, "computed"] >= 10 and decided[False, "computed"] - decided[True, "computed"] >= 5


# --- WS0's security review (16 Oct 2026): the inputs, both halves of the token, the cap -------------------------------

def test_a_switch_flipped_and_flipped_back_during_the_decision_is_not_kept(flag, tmp_path, monkeypatch, computed):
    """Review finding 1 (the reviewer's mid-build flip). The inputs are read before the decision, and the build reads
    the second-label switch again while it runs. Off when the inputs are read, on while the build runs, off again after
    it: equal before and after, yet the True was decided with the switch on. It must not be reused while it is off."""
    decided = {}
    for on in (True, False):
        arm = Arm(tmp_path / str(on), monkeypatch, on=on, build=site, members=1)
        assert (arm.sweep(), arm.sweep()) == (0, 0)
        monkeypatch.setenv(rl.FLAG, "off")
        real = interest_index.indexed_current

        def flipping(*args, **kwargs):
            monkeypatch.delenv(rl.FLAG)                      # on (its default) while the build runs
            try:
                return real(*args, **kwargs)
            finally:
                monkeypatch.setenv(rl.FLAG, "off")
        monkeypatch.setattr(interest_index, "indexed_current", flipping)
        first = arm.sweep()                                  # decided with the switch on: True
        if on:
            assert arm.kept == {}
        monkeypatch.setattr(interest_index, "indexed_current", real)
        with stale_stages() as stages:
            decided[on] = (first, arm.sweep(), arm.file.exists(), tuple(stages))
        monkeypatch.delenv(rl.FLAG, raising=False)
    assert decided[False] == (0, 1, False, ("interest",))
    assert decided[True] == decided[False]


def test_the_decision_reads_exactly_the_two_interest_switches(flag, tmp_path, monkeypatch, computed):
    """What `_interest_inputs_held` relies on, read from a real decision: every switch the interest step reads is the
    family's or the second-label switch, so a kept verdict is not refused for a switch the state does not hold."""
    arm = Arm(tmp_path, monkeypatch, on=True)
    seen = []
    real = SearchIndexService._interests_current

    def watched(self, *args, **kwargs):
        with switches.observed() as reads:
            verdict = real(self, *args, **kwargs)
        seen.append({name for name, _value in reads})
        return verdict
    monkeypatch.setattr(SearchIndexService, "_interests_current", watched)
    assert arm.sweep() == 0 and list(arm.kept) == [arm.file.name]
    assert seen and set().union(*seen) == {switches.INTEREST_SOURCES.name, switches.INTEREST_RELABEL.name}


def test_a_switch_the_state_does_not_hold_read_during_the_decision_is_not_kept(flag, tmp_path, monkeypatch, computed):
    """Should the build ever read another switch, the state does not cover it: the verdict is returned, not kept."""
    arm = Arm(tmp_path, monkeypatch, on=True)
    real = interest_index.indexed_current

    def reading_another(*args, **kwargs):
        switches.on(switches.JOURNAL_SOURCES)
        return real(*args, **kwargs)
    monkeypatch.setattr(interest_index, "indexed_current", reading_another)
    for count in (1, 2):
        assert arm.sweep() == 0 and computed(arm) == count and arm.kept == {}


def _checkpoint(path):
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        busy, log, done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert busy == 0 and log == done
    finally:
        conn.close()


def _relabelled_copy(path, folder):
    """The canonical file's bytes with one label changed, made without touching the canonical file."""
    folder.mkdir(parents=True, exist_ok=True)
    copy = folder / "restore.db"
    shutil.copyfile(path, copy)
    with _db(copy) as conn:
        cluster(conn, "tc_hobby", "sourdough / bread")
    _checkpoint(copy)
    for extra in ("-wal", "-shm"):
        if os.path.exists(str(copy) + extra):
            os.remove(str(copy) + extra)
    return copy


def test_an_in_place_restore_that_no_probe_sees_is_caught_by_the_file_states(flag, tmp_path, monkeypatch, computed):
    """Review W1. An older copy written over the canonical file in place (same inode): no commit, so the probe's
    data_version does not move, but a fresh connection reads the restored rows. Only the token's file states see it,
    and the sweep decides as with the switch off (the relabelled cluster drops the index)."""
    decided = {}
    for on in (True, False):
        arm = Arm(tmp_path / str(on), monkeypatch, on=on)
        assert (arm.sweep(), arm.sweep()) == (0, 0)
        _checkpoint(arm.path)
        assert (arm.sweep(), arm.sweep()) == (0, 0)          # warm again under the token after the checkpoint
        restore = _relabelled_copy(arm.path, tmp_path / f"restore-{on}")
        inode = os.stat(arm.path).st_ino
        before = arm.node.index._canonical_token()
        shutil.copyfile(restore, arm.path)
        assert os.stat(arm.path).st_ino == inode
        after = arm.node.index._canonical_token()
        assert before[1] == after[1] and before[2] != after[2]   # data_version still; the file states moved
        with stale_stages() as stages:
            decided[on] = (arm.sweep(), arm.file.exists(), tuple(stages))
    assert decided[True] == decided[False] == (1, False, ("interest",))


def test_with_the_file_states_held_still_the_probes_data_version_catches_a_commit(flag, tmp_path, monkeypatch,
                                                                                 computed):
    """Review W2 and W8. On a file system whose timestamps are coarser than two commits, data_version alone must
    catch a commit, and it does only on the service's one long-lived probe."""
    arm = Arm(tmp_path, monkeypatch, on=True)
    _warm(arm, computed)
    monkeypatch.setattr(si.SearchVerification, "_files", staticmethod(lambda path: ("still",)))
    assert (arm.sweep(), arm.sweep()) == (0, 0) and computed(arm) == 2      # proven again under the new shape
    with _db(arm.path) as conn:
        cluster(conn, "tc_hobby", "sourdough / bread")
    with stale_stages() as stages:
        assert arm.sweep() == 1
    assert stages == ["interest"] and computed(arm) == 3 and arm.kept == {}


def test_a_fresh_connection_starts_at_the_same_data_version_so_the_probe_is_kept(flag, tmp_path, monkeypatch):
    """Why the token reads one long-lived probe: `PRAGMA data_version` compares only on one connection. A connection
    opened after a commit reads the same number a connection opened before it did, so a probe opened per read would
    never see a commit; the service's probe does, and it is the same probe from one read to the next."""
    arm = Arm(tmp_path, monkeypatch, on=True)

    def fresh():
        conn = sqlite3.connect(arm.path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
        try:
            return conn.execute("PRAGMA data_version").fetchone()[0]
        finally:
            conn.close()
    service = arm.node.index
    before, fresh_before = service._canonical_token(), fresh()
    probe = service._probe()
    with _db(arm.path) as conn:
        conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('nr187-review', 'x')")
    after = service._canonical_token()
    assert fresh() == fresh_before                           # a fresh connection cannot tell
    assert after[1] != before[1]                             # the kept probe can
    assert service._probe() is probe


def test_a_token_unreadable_at_the_check_never_falls_back_to_the_one_read_before(flag, tmp_path, monkeypatch, computed):
    """Review W3. A commit lands after the sweep's token read and before its snapshot, and the token cannot be read at
    the check: the kept True must not be reused on the strength of the read before the snapshot."""
    decided = {}
    for on in (True, False):
        arm = Arm(tmp_path / str(on), monkeypatch, on=on)
        assert (arm.sweep(), arm.sweep()) == (0, 0)
        real_token, real_clock = SearchIndexService._canonical_token, si.clock_state
        calls = []

        def token(self):
            calls.append(1)
            return real_token(self) if len(calls) == 1 else None   # read before the snapshot; unreadable after

        def clocking(conn, arm=arm):
            with _db(arm.path) as writer:                    # before the snapshot's first read
                cluster(writer, "tc_hobby", "sourdough / bread")
            return real_clock(conn)
        monkeypatch.setattr(SearchIndexService, "_canonical_token", token)
        monkeypatch.setattr(si, "clock_state", clocking)
        with stale_stages() as stages:
            decided[on] = (arm.sweep(), arm.file.exists(), tuple(stages))
        monkeypatch.setattr(SearchIndexService, "_canonical_token", real_token)
        monkeypatch.setattr(si, "clock_state", real_clock)
    assert decided[True] == decided[False] == (1, False, ("interest",))


def test_a_sweep_that_could_not_run_keeps_no_verdict(flag, tmp_path, monkeypatch, computed):
    """Review W5. The files stay (the purge is held off), so the next listing would keep their names."""
    arm = Arm(tmp_path, monkeypatch, on=True)
    _warm(arm, computed)
    monkeypatch.setattr(si, "purge_all", lambda root: 0)

    def broken(self, now):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(SearchIndexService, "_authorities", broken)
    assert arm.sweep() == 0
    assert arm.kept == {} and arm.node.index._interest_reuses == {}


@pytest.mark.parametrize("setting, every", [(None, 30), ("3", 3), ("1", 1), ("0", 1), ("100000", 360),
                                            ("thirty", 30)])
def test_every_nth_sweep_decides_in_full(flag, tmp_path, monkeypatch, computed, setting, every):
    """Review Q3: a verdict is reused at most every-1 sweeps in a row; the every-th decides in full whatever the tokens
    say (default 30; at least 1; at most 360; unreadable is the default)."""
    if setting is None:
        monkeypatch.delenv(switches.SWEEP_INTEREST_VERDICT_FULL_EVERY.name, raising=False)
    else:
        monkeypatch.setenv(switches.SWEEP_INTEREST_VERDICT_FULL_EVERY.name, setting)
    assert si._interest_full_every() == every
    if every > 3:
        return
    arm = Arm(tmp_path, monkeypatch, on=True)
    full = []
    for sweep in range(1, 10):
        before = computed(arm)
        assert arm.sweep() == 0
        if computed(arm) > before:
            full.append(sweep)
    assert full == list(range(1, 10, every))
    assert list(arm.kept) == [arm.file.name]


def test_a_read_error_a_warm_verdict_skips_waits_at_most_until_the_next_full_decision(flag, tmp_path, monkeypatch,
                                                                                    computed):
    """Review Q3, as ruled: a read error inside the interest decision does not invalidate a kept verdict (with every
    token unchanged it cannot be a committed change), so a sweep that reuses it keeps the index where main drops it
    (`member_unavailable`); the next full decision, at most every-1 sweeps later, meets the error and drops it."""
    monkeypatch.setenv(switches.SWEEP_INTEREST_VERDICT_FULL_EVERY.name, "3")
    decided = {}
    for on in (True, False):
        arm = Arm(tmp_path / str(on), monkeypatch, on=on)
        assert (arm.sweep(), arm.sweep()) == (0, 0)          # on: a full decision, then a reuse
        real = interest_index.indexed_current

        def failing(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(interest_index, "indexed_current", failing)
        monkeypatch.setattr(si, "_shred", lambda path: None)
        outcome = []
        for _ in range(2):
            with stale_stages() as stages:
                outcome.append((arm.sweep(), tuple(stages)))
        decided[on] = outcome
        monkeypatch.setattr(interest_index, "indexed_current", real)
    assert decided[False] == [(1, ("member_unavailable",)), (1, ("member_unavailable",))]
    assert decided[True] == [(0, ()), (1, ("member_unavailable",))]

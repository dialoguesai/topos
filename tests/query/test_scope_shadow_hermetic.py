"""The suite must not be able to reach the operator's real shadow log.

~/.topos/scope_shadow.jsonl is written by the RUNNING node, and `ShadowLog.append`
rotates it to a `.1` sibling once it passes the cap — so an unguarded test run does
not merely add rows, it can rename the file a live node is appending to. What the
log holds is the only real-traffic evaluation record the classifier promotion in
PLAN_SCOPE_CLASSIFIER.md §6.5 has, so losing a generation of it is not recoverable
by re-running anything.

The guard is `_no_live_scope_shadow_guard` in tests/conftest.py, autouse and
unexempted. These tests are what keeps it there: it is invisible when it works, and
nothing else in the suite fails if someone deletes it.
"""

from __future__ import annotations

from pathlib import Path

from topos.query import scope_classifier as sc
from topos.query import scope_shadow as ss

LIVE_DIR = Path.home() / ".topos"

#: Distinctive enough that finding it in the operator's log is unambiguous.
SENTINEL = "zzqx-hermetic-guard-probe"


def _reaches_live(path) -> bool:
    """True when `path` lands inside the operator's real ~/.topos."""
    try:
        Path(path).resolve().relative_to(LIVE_DIR.resolve())
    except ValueError:
        return False
    return True


def _verdict(labels=("health:read",)):
    return sc.ScopeVerdict(tuple(labels), 0.8, sc.SOURCE_PROTOTYPE, False, {})


def test_the_default_log_path_is_not_the_operators() -> None:
    assert not _reaches_live(ss.default_log_path()), ss.default_log_path()


def test_a_default_constructed_log_is_not_the_operators() -> None:
    """Every production hook builds its log this way: `(log or ShadowLog())`."""
    assert not _reaches_live(ss.ShadowLog().path), ss.ShadowLog().path


def test_the_arm_switch_is_not_the_operators_flag_file() -> None:
    """Whether shadow is on must not depend on a file in somebody's home dir.

    The flag file is a deliberate operator gesture — the app-shell node inherits no
    shell environment, so touching it is the only reachable way to arm shadow. That
    makes it live state, and a suite that reads it means something different on the
    machine where it is armed than in CI.
    """
    assert not _reaches_live(ss.FLAG_FILE), ss.FLAG_FILE
    assert ss.FLAG_FILE.exists() is False
    assert ss.enabled() is False


def test_an_armed_observation_writes_to_the_guard_path(monkeypatch) -> None:
    """The sharp edge: `force=True` observes whatever `enabled()` says.

    The path is asserted BEFORE anything is written — a guard that has come loose
    must not be discovered by appending to the file this test exists to protect.
    """
    monkeypatch.setenv(ss.ENV_FLAG, "1")
    monkeypatch.setattr(ss, "_breaker_faults", 0)
    log = ss.ShadowLog()
    assert not _reaches_live(log.path), log.path

    record = ss.observe(
        SENTINEL, "health:read", classify_fn=lambda _t: _verdict(), force=True,
    )
    assert record is not None
    # Membership, not equality: the guard directory is session-scoped, so another
    # test may legitimately have written here first.
    assert SENTINEL in [row["text"] for row in log.read()], (
        "observe() resolved a different default log than ShadowLog() did"
    )

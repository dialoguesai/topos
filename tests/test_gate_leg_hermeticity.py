"""The gate's NON-pytest legs must not reach the owner's database either.

``tests/test_owner_database_hermeticity.py`` proves the guard for the pytest
lane. That lane was never the whole gate. ``just gate`` has seven legs and only
two of them are pytest; the rest run their own interpreters, where
``tests/conftest.py`` — the only thing that installed the guard — never executes.

Leg 7 is where that cost something. ``scripts/release_smoke_test.py`` boots the
built wheel's FastAPI app, and with ``TOPOS_DATABASE_PATH`` unset that app
resolved to the developer's own Topos::

    INFO: Serving database /Users/<owner>/.topos/database.db (source=slot, profile=personaldb, schema=62)
    INFO: DB tuning: journal_mode=wal sqlite_vec=True
    DEBUG: Ensured table browser_visits exists

A ``CREATE TABLE IF NOT EXISTS`` against live owner data, run by the ritual a
developer performs before every release, on a machine where a node already holds
that file open in WAL mode. Nobody noticed because the leg printed
``release_smoke_ok`` and exited 0 — and, as the first armed run showed, it
exits 0 even when the connect is REFUSED, because ``core.state`` catches the
failure and the app serves ``/healthcheck`` from a degraded path.

So this file pins three things, each of which was independently absent:

1. every non-pytest gate leg is wrapped in ``scripts/live_db_tripwire.py``;
2. the tripwire really arms a child interpreter, and journals what it refuses
   there — because a refusal a caller swallows is invisible without the journal;
3. ``release_smoke_test.py`` pins the database path on every path through it,
   not only under ``--seeded-db``.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
JUSTFILE = REPO_ROOT / "justfile"
TRIPWIRE = REPO_ROOT / "scripts" / "live_db_tripwire.py"
SMOKE_TEST = REPO_ROOT / "scripts" / "release_smoke_test.py"


def _tripwire() -> ModuleType:
    """Load ``scripts/live_db_tripwire`` by path.

    Not ``sys.path.insert(scripts/)``: that would leave every module in
    ``scripts/`` importable by bare name for the rest of the session, which is a
    shadowing hazard for the sake of one import.
    """
    cached = sys.modules.get("_topos_live_db_tripwire")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("_topos_live_db_tripwire", TRIPWIRE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_topos_live_db_tripwire"] = module
    spec.loader.exec_module(module)
    return module


def _clean_env() -> dict[str, str]:
    """``os.environ`` with every tripwire variable removed.

    So a test that asserts "unarmed" is not quietly armed by the shell it was
    launched from.
    """
    tripwire = _tripwire()
    env = dict(os.environ)
    for name in (
        "PYTHONPATH",
        tripwire.ENV_WATCH_MODULE,
        tripwire.ENV_WATCH_LABEL,
        tripwire.ENV_WATCH_JOURNAL,
    ):
        env.pop(name, None)
    return env


def _gate_body() -> list[str]:
    text = JUSTFILE.read_text(encoding="utf-8")
    match = re.search(r"^gate:[^\n]*\n((?:[ \t]+[^\n]*\n|\n)*)", text, re.MULTILINE)
    assert match is not None, f"no `gate` recipe in {JUSTFILE}"
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]


def test_the_gate_still_has_the_legs_this_file_is_about() -> None:
    """Guards every assertion below against passing on an empty list."""
    body = _gate_body()
    assert len(body) >= 5, body
    assert any("release_smoke_test.py" in line for line in body), body


def test_every_non_pytest_gate_leg_runs_under_the_live_db_tripwire() -> None:
    """The wiring, named so that unwiring it is a red diff rather than silence.

    pytest legs are exempt because ``tests/conftest.py`` installs the same guard
    at session start; wrapping them too would arm it twice, in two separately
    loaded copies of the module, and the inner copy would shadow the outer one's
    records for no gain.
    """
    unguarded = [
        line
        for line in _gate_body()
        if "pytest" not in line
        and not line.startswith("just ")
        and "live_db_tripwire.py" not in line
    ]
    assert not unguarded, (
        f"`just gate` legs run without the owner-database tripwire: {unguarded}. "
        "Each is an interpreter tests/conftest.py never reaches — which is how "
        "the release smoke test came to boot against ~/.topos. Wrap it: "
        "`uv run python scripts/live_db_tripwire.py <script.py> [args]`, or "
        "`--command <exe>` for something that is not a Python script."
    )


def test_the_tripwire_arms_a_child_interpreter_and_journals_what_it_refuses(
    tmp_path: Path,
) -> None:
    """The whole mechanism, end to end, against a throwaway path.

    Deliberately does NOT name ``~/.topos``: proving a hermeticity guard by
    opening the owner's database is the bug the guard exists to prevent. An
    arbitrary file is armed as owner data inside the child instead.

    Both halves are asserted because they fail independently. The refusal is the
    enforcement — nothing is written, and the file must not even be created. The
    journal is the VISIBILITY, and it is the half that was missing: the child
    below swallows the ``RuntimeError`` and exits 0, exactly as the smoke test's
    app boot does, so a parent reading only the exit code learns nothing.
    """
    tripwire = _tripwire()
    journal = tmp_path / "journal.tsv"
    probe = tmp_path / "pretend-owner.db"
    env = tripwire.child_env(
        _clean_env(),
        directory=tmp_path / "stub",
        label="<test child>",
        journal=journal,
    )
    child = textwrap.dedent(
        f"""
        import os, sqlite3, sys

        module = sys.modules["topos_live_db_watch"]
        # Not `module.watching()`: that context manager DISCARDS what it records
        # on exit, by design, so a passing self-test cannot red a session. Here
        # the surviving record is the thing under test.
        module._EXTRA_WATCHED_FILES.add(os.path.realpath({str(probe)!r}))
        try:
            sqlite3.connect({str(probe)!r})
        except RuntimeError:
            pass          # swallowed, exactly like topos.core.state does
        print("child_done")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", child], env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    assert "child_done" in result.stdout
    assert not probe.exists(), (
        "the refusal must land BEFORE sqlite creates the file — a guard that "
        "lets a 0-byte file through has already lost the argument"
    )
    findings = tripwire.journal_findings(journal)
    assert len(findings) == 1, findings
    assert "refused" in findings[0]
    assert str(probe.resolve()) in findings[0]


def test_the_tripwire_self_check_fails_when_the_child_is_not_armed() -> None:
    """``site.execsitecustomize`` swallows exceptions, so silence proves nothing.

    An unarmed interpreter looks identical to a clean one from the outside. The
    positive control is the only thing that tells them apart, and it therefore
    has to actually fail when the arming is absent.
    """
    with pytest.raises(SystemExit) as excinfo:
        _tripwire().verify_armed(sys.executable, _clean_env())
    assert "NOT ARMED" in str(excinfo.value)


def test_the_tripwire_self_check_passes_when_the_child_is_armed(tmp_path: Path) -> None:
    tripwire = _tripwire()
    env = tripwire.child_env(
        _clean_env(), directory=tmp_path / "stub", label="<test child>"
    )
    assert "tripwire_armed" in tripwire.verify_armed(sys.executable, env)


def test_the_tripwire_does_not_put_the_repo_on_a_child_path(tmp_path: Path) -> None:
    """The smoke venv must keep importing the WHEEL, not this working tree.

    Arming a child by adding the repo root to ``PYTHONPATH`` would have been one
    line, and it would have made ``import topos`` in the smoke venv resolve to
    ``./topos`` — deleting the only thing a release smoke test is for.
    """
    env = _tripwire().child_env(
        _clean_env(), directory=tmp_path / "stub", label="<test child>"
    )
    entries = env["PYTHONPATH"].split(os.pathsep)
    added = Path(entries[0])
    assert added.resolve() == (tmp_path / "stub").resolve()
    assert sorted(p.name for p in added.iterdir()) == ["sitecustomize.py"]
    assert str(REPO_ROOT) not in entries, (
        "the tripwire must not hand the repo checkout to a child: the smoke "
        f"venv would then import THIS tree instead of the wheel. {entries}"
    )


def test_the_release_smoke_test_pins_the_database_on_every_path() -> None:
    """``TOPOS_DATABASE_PATH`` is assigned outside any branch.

    Read from the AST rather than by string search, because the defect was
    precisely that the assignment EXISTED — it just lived inside
    ``if seeded_db is not None:``, so the ordinary run, the one the gate does,
    never took it.
    """
    tree = ast.parse(SMOKE_TEST.read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_install_and_verify"
    )

    def _assigns_db_path(body) -> bool:
        for node in body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "TOPOS_DATABASE_PATH"
                ):
                    return True
        return False

    assert _assigns_db_path(func.body), (
        "release_smoke_test.py must set TOPOS_DATABASE_PATH at the top level of "
        "_install_and_verify, not inside a branch. Unset, the built app resolves "
        "to ~/.topos/database.db and runs DDL on the owner's Topos — which is "
        "what `just gate` did before 2026-08-20."
    )


def test_the_release_smoke_test_proves_it_imported_the_wheel() -> None:
    """A smoke test that imports the working tree is not a smoke test.

    ``python -c`` puts the working directory on ``sys.path[0]``. ``just gate``
    runs from the repo root, so the venv's ``import topos`` resolved to
    ``./topos`` and every assertion in the boot check described the source tree
    rather than the artifact about to ship. The fix is a ``cwd=`` on those two
    subprocesses; this pins the assertion that makes the fix checkable instead
    of merely intended.
    """
    source = SMOKE_TEST.read_text(encoding="utf-8")
    assert "wheel_not_under_test" in source, (
        "IMPORT_CHECK_SCRIPT no longer asserts that `topos` came from the "
        "installed wheel's site-packages."
    )

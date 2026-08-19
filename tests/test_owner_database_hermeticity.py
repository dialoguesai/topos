"""The default test lane must not write to the owner's database.

Two halves, because one test cannot do both jobs:

* THE ENFORCEMENT is ``tests/live_db_watch`` plus the conftest hooks that report
  it. Only a session-level hook can see the whole run — an assertion made here
  would happily pass while a test scheduled AFTER it writes to ~/.topos.
* THIS FILE proves the detector is armed and classifies correctly, against a
  throwaway database. A hermeticity guard that had to open the real database to
  test itself would be the bug it is guarding against.

It also pins the selection rule, so nobody has to re-derive it from an incident:
every test that reaches owner data carries an opt-in marker, and the default
lane deselects those markers.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest

from tests import live_db_watch

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_guard_is_actually_installed() -> None:
    """A silent no-op guard is worse than none: it reads as proof."""
    assert live_db_watch.is_installed()
    assert sqlite3.connect is sqlite3.dbapi2.connect


def test_a_read_write_open_of_owner_data_is_recorded(tmp_path: Path) -> None:
    """The positive control. Arm a throwaway file, open it, expect a finding."""
    db = tmp_path / "pretend-owner.db"
    with live_db_watch.watching(db) as capture:
        sqlite3.connect(str(db)).close()
        found = capture.captured()
    assert len(found) == 1, found
    assert found[0].path == str(db.resolve())
    assert found[0].nodeid.endswith("test_a_read_write_open_of_owner_data_is_recorded")
    assert __file__ in found[0].origin


def test_a_read_only_open_of_owner_data_is_not_recorded(tmp_path: Path) -> None:
    """``mode=ro`` is how the live evals should read owner data — never flagged.

    Reading changes nothing on disk. Flagging it would push authors toward
    copying a 488MB database to answer a question they could have asked in
    place.
    """
    db = tmp_path / "pretend-owner.db"
    sqlite3.connect(str(db)).close()  # exists, so a ro open can succeed
    with live_db_watch.watching(db) as capture:
        sqlite3.connect(f"file:{db}?mode=ro", uri=True).close()
        sqlite3.connect(f"file:{db}?immutable=1", uri=True).close()
        assert capture.captured() == []


def test_ordinary_temp_databases_are_not_recorded(tmp_path: Path) -> None:
    """The overwhelming majority of connects in this suite. None may be flagged."""
    db = tmp_path / "scratch.db"
    before = len(live_db_watch.opens())
    sqlite3.connect(str(db)).close()
    sqlite3.connect(":memory:").close()
    assert len(live_db_watch.opens()) == before


@pytest.mark.parametrize(
    "database, uri",
    [
        (":memory:", False),
        ("", False),
        ("file::memory:?cache=shared", True),
        (None, False),
        (b"/tmp/some.db", False),
    ],
)
def test_classifier_survives_every_connect_shape(database, uri) -> None:
    """A guard that raises on an unusual connect breaks the suite it protects."""
    assert live_db_watch.owner_data_write_target(database, uri) is None


def test_the_watched_locations_are_the_real_ones() -> None:
    """Snapshotted from the REAL home, so a test that repoints HOME still counts.

    ``~/.topos`` is watched as a tree, not a single file: the active database
    sits at its top level and archived profiles keep their own underneath
    (topos/storage/db/paths.py).
    """
    home = Path.home()
    active = home / ".topos" / "database.db"
    profile = home / ".topos" / "profiles" / "some-profile" / "database.db"
    assert live_db_watch.owner_data_write_target(str(active)) == str(active)
    assert live_db_watch.owner_data_write_target(str(profile)) == str(profile)
    assert live_db_watch.owner_data_write_target(f"file:{active}?mode=ro", True) is None


def test_default_lane_deselects_every_owner_data_marker() -> None:
    """The lane rule itself, read off pyproject rather than trusted.

    The markers are what keeps these tests out of an ordinary run. When the
    guard fixture merely *waived* protection for them and nothing deselected
    them, `pytest tests/` ran the whole set against the owner's database.
    """
    try:
        import tomllib  # py3.11+
    except ModuleNotFoundError:  # this venv is 3.10
        import tomli as tomllib  # type: ignore[no-redef]

    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        addopts = tomllib.load(fh)["tool"]["pytest"]["ini_options"]["addopts"]
    assert "-m" in addopts
    selection = addopts[addopts.index("-m") + 1]
    for marker in ("live", "e2e", "qq_eval"):
        assert f"not {marker}" in selection, selection


#: The exact shape that walked past ``_no_live_db_guard``: a module resolving
#: the owner's DATABASE off the real home. Evaluated at COLLECTION time, so the
#: env pin the guard applies per test comes too late to change it.
#:
#: Deliberately requires ``database.db`` and not merely ``~/.topos``: the log
#: path assertions in tests/topos/test_app_mode_logging.py name that directory
#: and open nothing.
_OWNER_DB_PATTERN = re.compile(
    r"""(?:Path\.home\(\)|expanduser\(\s*["']~)(?:.|\n){0,200}?database\.db"""
)

_OPT_IN_MARKER_PATTERN = re.compile(r"pytest\.mark\.(live|e2e|qq_eval)\b")

#: The guard's own files talk about these paths for a living.
_SELF = ("tests/live_db_watch.py", "tests/test_owner_database_hermeticity.py")


def _gate_set(path: Path, text: str) -> list[Path]:
    """Which files must carry the marker for ``path`` to be safely reachable.

    A test module gates itself. A HELPER gates nothing on its own — the current
    offender is exactly this shape, ``tests/gap/qq/engine/query_eval_cases.py``
    holding ``LIVE_DB_PATH`` while the marker sits on the test module that uses
    it — so its gate set is the test modules that USE the constant, found by
    name. Importing the helper is not enough to gate on: four modules import it
    for its case tables and never come near the path.
    """
    if path.name.startswith("test_"):
        return [path]
    names = re.findall(
        rf"^\s*([A-Za-z_]\w*)\s*=[^\n]*{_OWNER_DB_PATTERN.pattern}", text, re.MULTILINE
    )
    if not names:
        return []
    users = []
    for candidate in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        if str(candidate.relative_to(REPO_ROOT)) in _SELF:
            continue  # this file names the constant in prose
        body = candidate.read_text(encoding="utf-8", errors="replace")
        if any(re.search(rf"\b{re.escape(n)}\b", body) for n in names):
            users.append(candidate)
    return users


def test_every_module_that_resolves_the_owner_database_is_opt_in() -> None:
    """Nothing may reach the owner's database unless a marker deselects it.

    Static rather than a nested collection run: this has to be cheap enough to
    sit in the default lane (a `--collect-only` subprocess over ~3k tests is
    not), and the defect is a source-level shape, so source is where to look.
    The runtime half — a module that acquires the path some other way — is
    covered by the session guard, which watches connects instead of text.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        rel = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8", errors="replace")
        if rel in _SELF or not _OWNER_DB_PATTERN.search(text):
            continue
        for gate in _gate_set(path, text):
            if not _OPT_IN_MARKER_PATTERN.search(
                gate.read_text(encoding="utf-8", errors="replace")
            ):
                offenders.append(f"{rel} reached from {gate.relative_to(REPO_ROOT)}")
    assert not offenders, (
        "these resolve the owner's ~/.topos database but nothing carries a "
        f"live/e2e/qq_eval marker, so the default lane runs them against it: {offenders}"
    )


#: Helpers that reach ``core.state.get_db_connection()`` somewhere underneath.
#:
#: ``resolve_scope_manifest`` is the one that bit: it reads as a pure registry
#: lookup, but ``manifest_from_scope_entry`` asks ``get_sources_by_scope`` which
#: installed sources back the scope, and that reads ``source_runtime_installs``
#: out of the database (topos/sources/registry.py:730).
_DB_REACHING_CALLS = frozenset(
    {
        "resolve_scope_manifest",
        "get_sources_by_scope",
        "get_db_connection",
        "apply_all_migrations",
        # Matched by NAME only — `x.create()` and `x.connect()` on anything else
        # would also trip. That has no false positives in this suite today, and
        # the failure mode is a loud, one-line fix rather than a silent write to
        # someone's database.
        "create",  # AdapterFactory.create
        "connect",  # sqlite3.connect
    }
)


def _module_scope_calls(tree: ast.Module) -> list[tuple[str, int]]:
    """Calls that run at IMPORT time.

    Module body and class bodies execute on import; a function body does not,
    but its DECORATORS do — a ``@pytest.mark.parametrize`` argument is evaluated
    during collection like any other module-scope expression.
    """
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for decorator in getattr(node, "decorator_list", []):
                visit(decorator)
            return
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _DB_REACHING_CALLS:
                found.append((name, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def test_no_test_module_reaches_the_database_at_import_time() -> None:
    """Collection has no fixtures, so the autouse guard cannot cover it.

    ``tests/features/test_p3_entity_spine.py`` held
    ``MESSAGES_MANIFEST = resolve_scope_manifest("messages:read")`` at module
    scope. That ran during COLLECTION, before ``_no_live_db_guard`` had pinned
    anything, so the unset path resolved to the developer's own
    ~/.topos/database.db and pytest opened it read-write on every plain
    `pytest tests/`. Nothing about the line looks like database access, which is
    why this is a test and not a review habit. Make the call lazy.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        rel = str(path.relative_to(REPO_ROOT))
        if rel in _SELF:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # not ours to fix here
            continue
        for name, lineno in _module_scope_calls(tree):
            offenders.append(f"{rel}:{lineno} calls {name}() at import time")
    assert not offenders, (
        "these run during collection, where no fixture can redirect the database "
        f"path, so they open the owner's ~/.topos: {offenders}"
    )


def test_the_known_owner_database_modules_are_still_marked() -> None:
    """Named, so dropping a marker fails here instead of silently at 3am.

    Every one of these was in the 2026-08-19 run that wrote to ~/.topos.
    """
    known = (
        "tests/gap/qq/engine/test_en_qq_eval_queries.py",
        "tests/query/test_d13_grantee_tier_matrix.py",
        "tests/query/test_selector_live_entity_grant.py",
        "tests/adversarial/test_scope_break_iteration2_live_engine.py",
        "tests/adversarial/test_scope_break_iteration3b_handler_grantee.py",
        "tests/release/iteration4/test_live_engine_pressure.py",
    )
    unmarked = [
        name
        for name in known
        if not _OPT_IN_MARKER_PATTERN.search(
            (REPO_ROOT / name).read_text(encoding="utf-8")
        )
    ]
    assert not unmarked, f"no longer opt-in: {unmarked}"

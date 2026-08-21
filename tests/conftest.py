import os
import sys
import threading
import time
from pathlib import Path

import pytest

# Project root on sys.path for editable installs and `pytest` without install
# (development). Hoisted above every other import in this file because
# `tests.live_db_watch` below must be importable before anything else runs.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Wrap sqlite3.connect FIRST, before this file imports any topos module: the
# guard can only see connects made through the object it replaced, so anything
# that binds the original beforehand is invisible to it. See the module for what
# it watches and why the `_no_live_db_guard` fixture below cannot cover it.
from tests import live_db_watch  # noqa: E402

live_db_watch.install()

# Default env so pydantic Settings() can load when tests import topos.* at collection time.
os.environ.setdefault("TOPOS_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CONTROL_PLANE_URL", "")

# Bind the re-exporting handler modules NOW, before any test can patch
# topos.core.state.get_db_connection.
#
# topos.core.handlers re-exports get_db_connection BY VALUE (`from .common
# import ...`, and common in turn `from ...core.state import ...`), so whichever
# object state holds at FIRST import is what the package keeps for the session.
# A test that patches state and only then triggers that first import bakes its
# own lambda into the package permanently: monkeypatch records the poisoned
# value as the "original" and faithfully restores it at teardown, so undoing the
# patch cements it instead of removing it. Every later test in the session then
# receives that test's closed connection.
#
# It cost tests/core/test_graph_cypher_handler.py two tests, which failed with
# "SQLite objects created in a thread can only be used in that same thread"
# whenever they ran after test_enrichment_entrypoint_parity.py and passed alone.
# Over a dozen test files patch that name, so any of them could be the first
# importer; importing here — before any test runs — makes the order irrelevant.
import topos.core.handlers  # noqa: E402,F401
import topos.core.handlers.enrichment  # noqa: E402,F401
import topos.core.handlers.ingest  # noqa: E402,F401
# No live-model calls from unit tests: cluster recomputes would otherwise try
# the local Ollama labeler (auto mode). Labeler tests inject `complete`.
os.environ.setdefault("TOPOS_CLUSTER_LLM_LABELS", "off")
# Nor live model *downloads*: app startup fires topos.sanitization.prewarm on a
# background thread, which fetches ~2.9GB of Hugging Face weights on a cold
# cache and — in this suite's own words — "starved the app loop for minutes"
# (tests/topos/test_control_plane_client.py::
# test_threaded_client_answers_ping_while_app_loop_is_stalled). CI caches uv,
# never the HF hub (.github/workflows/ci.yml), so every app startup in the
# public lane raced that download; whichever test's startup landed inside the
# window died on asgi-lifespan's 30s budget with a bare TimeoutError. That is
# CI run 31293718827 (test_ingest_source_file_propagates_guard_denial), which
# passed on rerun off the same commit. No test needs the real weights loaded at
# startup: tests/test_prewarm_first_run.py drives the cache-probe helpers
# directly and never calls prewarm_sanitization_models(). setdefault, so a lane
# that genuinely wants the prewarm can still export the variable and opt in.
os.environ.setdefault("SANITIZATION_PREWARM_ON_STARTUP", "false")


PRIVATE_PATH_HINTS = (
    "test_uma_",
    "test_messenger_",
    "test_stage11_",
    "test_source_install_api.py",
    "test_hosted_",
    "test_pooled_",
    "test_phase_",
    "test_engine_sprint",
    "test_data_source_scopes_",
    "test_scope_resolution_",
    "test_manual_enrichment_trigger_flow.py",
    "test_enrichment_orchestrator.py",
    "test_local_mcp.py",
    "test_mcp_stdio_proxy.py",
    "test_imessage_sync.py",
    "test_engine_compute_invoke_contract.py",
    "test_engine_usage_observation.py",
    "test_engine_model_lifecycle_and_queue.py",
    "test_engine_ollama_queue_timing.py",
    "test_sync_client_reliability.py",
    "test_filter_lab.py",
    "test_app_registry.py",
    "evals/privacy",
)


#: Module attributes that must survive every test unchanged.
#:
#: These names are re-exported BY VALUE across modules, so a test that rebinds
#: one can poison it for the rest of the session — and monkeypatch will not save
#: you. If the rebind happens before the importing module's first import, the
#: package binds the patched object, monkeypatch records THAT as the "original",
#: and undoing the patch cements it instead of removing it.
#:
#: The gate cannot be trusted to catch this by ordering alone: the leak that
#: motivated this guard sat inside the gate's own selection and passed on every
#: run, because the gate's order never happened to put the leaking test ahead of
#: a victim. Shuffling would surface such leaks only sometimes, and would name
#: the VICTIM. This names the culprit, deterministically, on every run.
#: Watch the RE-EXPORTS, not the source module. topos.core.state is legitimately
#: re-created by tests that `sys.modules.pop()` core modules to isolate app
#: startup (tests/topos/test_app_routes.py, test_auth.py), which yields a fresh
#: function object with the same name — 10 such rebinds in one gate run, none of
#: them a poisoning. The re-export is where the damage happens anyway: it is the
#: binding every handler actually reads, and the one monkeypatch cements.
_LEAK_WATCHED_ATTRS = (
    ("topos.core.handlers", "get_db_connection"),
    ("topos.core.handlers.common", "get_db_connection"),
)

#: (nodeid, "module.attr: before -> after") for every leak observed this session.
_LEAK_FINDINGS: list[tuple[str, str]] = []
_LEAK_BASELINE: dict = {}


def _leak_snapshot() -> dict:
    snap = {}
    for mod_name, attr in _LEAK_WATCHED_ATTRS:
        mod = sys.modules.get(mod_name)
        if mod is not None:
            snap[(mod_name, attr)] = getattr(mod, attr, None)
    return snap


def pytest_sessionstart(session) -> None:
    """Baseline BEFORE the first test, so no test is exempt.

    Safe to read here only because this file imports the re-exporting handler
    modules at module scope above: sessionstart runs after conftest import, so
    the names being watched are already bound to their real objects.
    """
    del session
    _LEAK_BASELINE.update(_leak_snapshot())


def pytest_runtest_logstart(nodeid: str, location) -> None:
    """Attribute every owner-database connect from here on to THIS test.

    Set at logstart rather than in a fixture so setup-time connects (a
    module-scoped fixture opening an adapter bundle, which is exactly how the
    live evals do it) are attributed to the test that triggered them.
    """
    del location
    live_db_watch.set_current_test(nodeid)


def pytest_runtest_logfinish(nodeid: str) -> None:
    """Compare watched attributes once a test is COMPLETELY done.

    Deliberately a hook and not a fixture. An autouse fixture cannot see this:
    ``_no_live_db_guard`` below requests ``monkeypatch``, which makes monkeypatch
    set up first and therefore finalise LAST — so any fixture-based check runs
    before ``monkeypatch.undo`` and flags every ordinary patch as a leak.
    ``pytest_runtest_logfinish`` fires after teardown has fully completed, which
    is the only point where "still rebound" means "outlived the undo".
    """
    for key, was in _LEAK_BASELINE.items():
        now = _leak_snapshot().get(key, was)
        if now is not was:
            mod_name, attr = key
            _LEAK_FINDINGS.append(
                (nodeid, f"{mod_name}.{attr}: "
                         f"{getattr(was, '__name__', was)!r} -> {getattr(now, '__name__', now)!r}")
            )
            _LEAK_BASELINE[key] = now  # report each leak once, not once per later test

    _disarm_graph_refresh_debounce()

    leaked = _leaked_engine_threads()
    if leaked:
        _THREAD_LEAK_FINDINGS.append((nodeid, f"still alive: {', '.join(leaked)}"))


def _disarm_graph_refresh_debounce() -> None:
    """Cancel any pending graph-refresh debounce timer before the next test.

    ``mark_graph_dirty()`` arms a ``threading.Timer`` (90s by default) that
    later calls ``get_db_connection()`` on its own thread. The timer outlives
    the test that armed it, and by the time it fires ``_no_live_db_guard``'s
    ``monkeypatch`` has already undone the ``TOPOS_DATABASE_PATH`` pin — so the
    rebuild resolves the path afresh and lands on the developer's real
    ``~/.topos/database.db``, attributed to whichever unrelated test happened
    to be running. That is how ``tests/storage/test_connection_tuning.py::
    test_runtime_status_reports_counts`` was reported as opening the owner's
    database on 2026-08-20 from a call stack it does not contain.

    Ordinary ingestion and enrichment tests arm it as a side effect of doing
    their real work, so the fix belongs here rather than in each of them: a
    debounce timer is engine state, and this is the hook that already exists
    for engine state outliving its test.

    Looked up in ``sys.modules`` rather than imported, so a run that never
    touches the feature never pays to load it. Cheap enough for every test: a
    dict lookup, and on a hit one lock plus ``Timer.cancel()``.
    """
    mod = sys.modules.get("topos.features.entities.graph_refresh")
    if mod is None:
        return
    try:
        mod._refresher.shutdown()
    except Exception:  # noqa: BLE001 — teardown hygiene must never fail a test
        pass


#: Engine threads that must not outlive the test that started them.
#:
#: A leaked writer is not a tidiness problem: it keeps writing while the NEXT
#: app instance runs its startup migrations, and a migration that takes the
#: process-wide write gate and then blocks on SQLite pins that gate for the full
#: 30s busy_timeout. Every other writer queues, app startup among them, and the
#: victim fails its own 30s lifespan budget reporting itself as the problem. In
#: CI that read as an unexplained flake in whichever test happened to be
#: starting — never the one that leaked.
#:
#: Prefixes, not exact names, because the two upgrade threads differ
#: (`topos-upgrade-runner` / `topos-upgrade-stamp`).
_THREAD_LEAK_PREFIXES = ("topos-upgrade-", "topos-startup-db")

#: Threads get a moment to finish after teardown before being called a leak — a
#: thread already on its way out is not what this is looking for.
_THREAD_EXIT_GRACE_S = 2.0

_THREAD_LEAK_FINDINGS: list[tuple[str, str]] = []

#: Thread identities already reported. One leak is attributable to the test that
#: created it and to no other; without this, a single leaked thread reappears in
#: every later test's check and buries the one finding that matters under a
#: cascade of innocent ones.
_REPORTED_THREADS: set[int] = set()


def _leaked_engine_threads() -> list[str]:
    deadline = time.monotonic() + _THREAD_EXIT_GRACE_S
    while True:
        leaked = [
            t
            for t in threading.enumerate()
            if t.is_alive()
            and t.name.startswith(_THREAD_LEAK_PREFIXES)
            and t.ident not in _REPORTED_THREADS
        ]
        if not leaked or time.monotonic() >= deadline:
            for t in leaked:
                if t.ident is not None:
                    _REPORTED_THREADS.add(t.ident)
            return sorted(t.name for t in leaked)
        time.sleep(0.05)


def _hint_if_everything_was_deselected(terminalreporter, config) -> None:
    """A bare "no tests ran" after naming a live test is a dead end.

    The default `-m` filter in pyproject deselects the data-touching markers, and
    pytest applies it even when you named the file — deliberately: an exception
    for "but I typed the path" would let a script or an agent reach the owner's
    database by accident, which is exactly how this went unnoticed. So the
    filter stays absolute and the way out gets printed instead of guessed.
    """
    if terminalreporter.stats.get("passed") or terminalreporter.stats.get("failed"):
        return
    if not getattr(config.option, "markexpr", ""):
        return
    if not any(f"not {m}" in config.option.markexpr for m in _LIVE_DB_EXEMPT_MARKERS):
        return
    if not terminalreporter.stats.get("deselected"):
        return
    terminalreporter.write_line("")
    terminalreporter.write_line(
        "everything you selected was deselected by the default lane filter "
        f"(-m {config.option.markexpr!r})."
    )
    terminalreporter.write_line(
        "Those tests read or write real data, so they are opt-in by MARKER, not "
        "by path:"
    )
    terminalreporter.write_line(
        "  just test-owner-db-eval    # against a snapshot of ~/.topos, never the original"
    )
    terminalreporter.write_line(
        "  just test-live-node        # against the node running on :9000"
    )
    terminalreporter.write_line(
        "  pytest <path> -m qq_eval   # your own -m replaces the filter entirely"
    )
    terminalreporter.write_line("See docs/testing/TEST_LANES.md.")


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    del exitstatus
    _hint_if_everything_was_deselected(terminalreporter, config)
    _report_owner_database_writes(terminalreporter)
    if _THREAD_LEAK_FINDINGS:
        terminalreporter.section("engine threads outlived their test", red=True, bold=True)
        for nodeid, detail in _THREAD_LEAK_FINDINGS:
            terminalreporter.write_line(f"{nodeid}\n    {detail}")
        terminalreporter.write_line("")
        terminalreporter.write_line(
            "A thread still alive here keeps writing during LATER tests. When it"
        )
        terminalreporter.write_line(
            "takes the write gate and blocks on SQLite it pins every other writer"
        )
        terminalreporter.write_line(
            "for busy_timeout (30s), which fails an unrelated test's app startup."
        )
        terminalreporter.write_line(
            "Give the thread a stop event, hold it in topos.core.state, and set +"
        )
        terminalreporter.write_line(
            "join it in the app's shutdown_event — see _reap_upgrade_runner."
        )
    if not _LEAK_FINDINGS:
        return
    terminalreporter.section("module state leaked between tests", red=True, bold=True)
    for nodeid, detail in _LEAK_FINDINGS:
        terminalreporter.write_line(f"{nodeid}\n    {detail}")
    terminalreporter.write_line("")
    terminalreporter.write_line(
        "Each of these outlived its own teardown and poisons every later test in"
    )
    terminalreporter.write_line(
        "the session. The usual cause is patching a name BEFORE first-importing a"
    )
    terminalreporter.write_line(
        "module that re-exports it by value: the package binds the patched object,"
    )
    terminalreporter.write_line(
        "monkeypatch records that as the original, and undo cements it. Import the"
    )
    terminalreporter.write_line(
        "re-exporting module at conftest scope so the binding predates any patch."
    )


def _report_owner_database_writes(terminalreporter) -> None:
    """Name every test that aimed a read-write connect at the owner's database.

    Since 2026-08-20 ``live_db_watch`` REFUSES those connects, so in a default
    run these are attempts and not completed writes — the guard raised and
    sqlite never saw the path. They are still listed and they still red the run:
    a test that tried is a test that succeeds the moment somebody sets
    ``TOPOS_TEST_ALLOW_OWNER_DB_WRITES`` while chasing something else.

    Reported here rather than asserted inside a test because no test can see the
    whole session: a violation by a test that runs LATER would pass an assertion
    made earlier. ``tests/test_owner_database_hermeticity.py`` proves the
    detector and the refusal work; this is what fails the run as a whole.
    """
    opted_in = live_db_watch.opted_in_opens()
    if opted_in:
        paths = sorted({o.path for o in opted_in})
        noun = "attempt(s)" if all(o.refused for o in opted_in) else "open(s)"
        terminalreporter.write_line("")
        terminalreporter.write_line(
            f"opt-in lane touched owner data: {len(opted_in)} read-write {noun} "
            f"across {len({o.nodeid for o in opted_in})} test(s) -> {', '.join(paths)}"
        )

    violations = live_db_watch.violations()
    if not violations:
        return
    completed = [v for v in violations if not v.refused]
    terminalreporter.section(
        "tests wrote to the owner's database"
        if completed
        else "tests tried to write to the owner's database",
        red=True,
        bold=True,
    )
    for finding in violations:
        terminalreporter.write_line(str(finding))
    terminalreporter.write_line("")
    if completed:
        terminalreporter.write_line(
            f"{len(completed)} of these REACHED sqlite, because this run set"
        )
        terminalreporter.write_line(
            f"{live_db_watch.ALLOW_ENV}. Rows may now exist in your own database."
        )
    else:
        terminalreporter.write_line(
            "The guard refused each of these, so nothing was written. Each is"
        )
        terminalreporter.write_line(
            "still a code path that WOULD write, to be fixed rather than waived."
        )
    terminalreporter.write_line("")
    terminalreporter.write_line(
        "These aim at a real ~/.topos database READ-WRITE without asking for it."
    )
    terminalreporter.write_line(
        "The `_no_live_db_guard` fixture cannot stop this: it pins"
    )
    terminalreporter.write_line(
        "TOPOS_DATABASE_PATH, and these resolve the path themselves at import"
    )
    terminalreporter.write_line(
        "time (when the env var is still unset) and pass it straight to"
    )
    terminalreporter.write_line(
        "AdapterFactory as db_path=. Either seed a tmp database and point the"
    )
    terminalreporter.write_line(
        "module at it, or mark the test `live` (running node) / `qq_eval` (owner"
    )
    terminalreporter.write_line(
        "database) so it is opt-in — see docs/testing/TEST_LANES.md."
    )


def pytest_sessionfinish(session, exitstatus) -> None:
    """Red the run on a leak, so the gate cannot pass while one is live."""
    del exitstatus
    live_db_watch.set_current_test("<session teardown>")
    if _LEAK_FINDINGS or _THREAD_LEAK_FINDINGS or live_db_watch.violations():
        session.exitstatus = 1


# Lanes that legitimately reach a real database; everything else must never
# touch ~/.topos/database.db (or the other canonical candidates in
# topos.core.state._resolve_database_path_from_settings).
#
# These are OPT-IN, not merely exempt. pyproject's `addopts` deselects all three
# by default, so carrying one of these markers keeps a test out of the ordinary
# gate rather than just waiving its guard — which is the half that was missing
# until 2026-08-19, when a plain `pytest tests/` ran the whole set and wrote 71
# rows into the owner's query_artifacts.
_LIVE_DB_EXEMPT_MARKERS = ("live", "e2e", "qq_eval")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    for item in items:
        path = str(item.fspath)
        if any(hint in path for hint in PRIVATE_PATH_HINTS):
            item.add_marker(pytest.mark.private)
        else:
            item.add_marker(pytest.mark.public)
        if any(item.get_closest_marker(m) for m in _LIVE_DB_EXEMPT_MARKERS):
            live_db_watch.mark_opt_in(item.nodeid)


@pytest.fixture()
def _live_db_guard_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Per-TEST guard file, deliberately not session-scoped.

    One shared guard.db let tests poison each other: a real-data test (the
    iMessage sync runs against this machine's actual chat.db) filled it with
    records, and the enrichment jobs + graph-rebuild subprocesses that data
    spawns kept writing it for the rest of the session — every later test's
    startup then fought those writers through 30s SQLite busy timeouts. A
    fresh file per test contains that fallout: leftover background threads
    keep writing their (now unlinked) old file instead of the next test's.
    Safe only since core.state serializes the owner-connection swap the
    resulting per-test path changes trigger (_owner_conn_lock; the
    unsynchronized version SIGBUSed on 2026-08-08).
    """
    return str(tmp_path_factory.mktemp("no-live-db") / "guard.db")


@pytest.fixture(autouse=True)
def _no_live_db_guard(request, monkeypatch, _live_db_guard_path):
    """Belt-and-braces: force TOPOS_DATABASE_PATH to a tmp file for every test.

    Without this, any code path that reaches topos.core.state.get_db_connection()
    (TestClient startup, install_service, query pipeline fallback, ...) resolves
    the unset path to the developer's live ~/.topos/database.db. Tests still
    override with their own paths via monkeypatch; live/e2e/qq_eval lanes are
    exempt because they intentionally run against a real database.
    """
    if any(request.node.get_closest_marker(m) for m in _LIVE_DB_EXEMPT_MARKERS):
        yield
        return
    monkeypatch.setenv("TOPOS_DATABASE_PATH", _live_db_guard_path)
    try:
        from topos.config.settings import settings as runtime_settings
    except Exception:
        pass
    else:
        # The settings singleton read env at import time; patch it too so code
        # holding a reference sees the guard path without a module reload.
        monkeypatch.setattr(runtime_settings, "topos_database_path", _live_db_guard_path, raising=False)
    yield


@pytest.fixture
def pin_db_path(monkeypatch):
    """Point the SETTINGS path at a test's own database, not just the accessor.

    Patching ``core.state.get_db_connection`` is only half the resolution.
    ``AdapterFactory`` re-resolves by PATH: it asks the accessor for the shared
    connection, compares that connection's file against ``db_path`` from
    settings, and reuses it ONLY when they match — otherwise it opens
    ``db_path`` itself (storage/adapters/factory.py). Since ``_no_live_db_guard``
    above pins ``TOPOS_DATABASE_PATH`` at one session-wide guard.db, a test that
    injects a connection at its own tmp path fails that comparison, and the
    whole adapter bundle silently writes to guard.db instead.

    That mismatch is order-dependent, not deterministic: it only bites once an
    earlier test has migrated guard.db far enough for the writes to succeed
    there, which is why these tests passed alone and failed in a suite. Pinning
    both halves to the same file removes the disagreement.
    """

    def _pin(db_path) -> None:
        monkeypatch.setenv("TOPOS_DATABASE_PATH", str(db_path))
        try:
            from topos.config.settings import settings as runtime_settings
        except Exception:  # noqa: BLE001
            return
        monkeypatch.setattr(
            runtime_settings, "topos_database_path", str(db_path), raising=False
        )

    return _pin


@pytest.fixture(autouse=True)
def _no_live_ollama_probe_guard(request, monkeypatch):
    """Same disease as the live-DB guard, one lane over.

    The config readers now ask the machine which ollama tags are installed
    before resolving a pack role, so an unguarded test opens a socket to
    localhost:11434 and its result depends on what the developer happens to
    have pulled: a pack bound to `phi3:latest` resolves to the pack on a
    laptop without ollama and to the engine default on one with it.

    None is the honest stand-in because it is what the probe already returns
    when nothing answers, and it means "I did not check" rather than "nothing
    is installed" — so guarded tests see exactly the trust-the-pack behaviour
    they were written against. Tests that are about the installed set patch
    this name themselves, which wins over an autouse fixture.
    """
    if any(request.node.get_closest_marker(m) for m in _LIVE_DB_EXEMPT_MARKERS):
        yield
        return
    try:
        from topos.config import model_packs
    except Exception:
        pass
    else:
        monkeypatch.setattr(model_packs, "installed_local_models", lambda: None, raising=False)
    yield


@pytest.fixture(scope="session")
def _scope_shadow_guard_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped, where the live-DB guard above is per-test, and on purpose.

    That guard needs a fresh file per test because the background writers one
    test spawns keep writing it and stall the next through SQLite busy timeouts.
    A JSONL that nothing reads back has no such fallout — every test asserting
    on shadow output passes its own `log=` — so one directory per session is
    enough, and it saves an mktemp on every test in the lane.
    """
    return tmp_path_factory.mktemp("no-live-scope-shadow")


@pytest.fixture(autouse=True)
def _no_live_scope_shadow_guard(monkeypatch, _scope_shadow_guard_dir):
    """The second live file in ~/.topos the suite can reach, after the database.

    `scope_shadow.FLAG_FILE` is an operator gesture: touching
    ~/.topos/scope_shadow.on arms shadow mode for the running node, which is the
    only reachable switch there because the node under the app shell inherits no
    shell environment. On a machine where the operator has done that, `enabled()`
    returns True *inside the test process too* — and then every production hook
    (QueryPipeline.execute, the tools_retrieve handler, the engine-direct
    /tool_index route) appends its observation to ~/.topos/scope_shadow.jsonl,
    the file the live node is writing.

    Appending to it would be bad enough. Since the log gained rotation it is
    worse: `ShadowLog.append` rolls the file to a `.1` sibling once it passes the
    cap, so a test run can rename out from under a node that is concurrently
    appending — silent loss of the evaluation record the classifier promotion in
    PLAN_SCOPE_CLASSIFIER.md §6.5 is measured from.

    The quieter reason is reproducibility: unguarded, whether `enabled()` is True
    depends on whether someone touched a file in their home directory, so the
    suite means something different on an operator's laptop than in CI.

    NO marker exemption, unlike the live-DB guard. That one lets live/e2e/qq_eval
    through because those lanes mean to read a real database; no lane means to
    write the operator's shadow log, and those are precisely the lanes that run
    real queries — they would file synthetic eval traffic into the record as
    though it were the real traffic the log exists to capture. Tests that want
    shadow ON still set ENV_FLAG (or patch FLAG_FILE) themselves and still get
    it; the pinned log path is what makes saying yes safe. A test that means
    to exercise the override just sets ENV_LOG_PATH again — last writer wins.
    """
    try:
        from topos.query import scope_shadow
    except Exception:  # a suite that cannot import it cannot leak through it
        pass
    else:
        # A path that is never created: `enabled()` falls through to the env
        # flag, so opting in still works and opting out is the default.
        monkeypatch.setattr(
            scope_shadow, "FLAG_FILE",
            _scope_shadow_guard_dir / "scope_shadow.on", raising=False,
        )
        # The log moves by its own supported lever, not by replacing the
        # resolver: `default_log_path` reads TOPOS_SCOPE_SHADOW_LOG per call, and
        # a constant lambda here would break the override it exists to provide
        # (tests/query/test_scope_shadow.py::test_log_path_follows_the_env_override).
        # setenv also reaches a ShadowLog built in a subprocess, which setattr
        # never could.
        monkeypatch.setenv(
            scope_shadow.ENV_LOG_PATH,
            str(_scope_shadow_guard_dir / "scope_shadow.jsonl"),
        )
    yield


_CORE_RELOAD_MODULES = (
    "topos.config.settings",
    "topos.core.state",
    "topos.storage.adapters.factory",
)


@pytest.fixture
def module_reload_isolation():
    """Isolation for tests that pop/reload core topos modules.

    Reloading settings/state forks module identities: without cleanup, later
    tests resolve a settings object instantiated under this test's env and a
    db singleton pointed at this test's (deleted) temp database — the classic
    symptom is order-dependent failures in tests/sources and tests/ingestion
    that pass in isolation.

    Teardown *restores the original module objects* rather than purging to
    empty: many tests bind `topos.core.state` at collection time
    (`from topos.core import state as core_state`) and monkeypatch attributes
    on that object, while production code resolves lazily through sys.modules.
    Only restoring the originals makes both lookups agree again; a bare purge
    would leave those collection-time bindings pointing at an orphaned module.

    Yields the purge function so a test can force a fresh import mid-test.
    """

    def _matching(module_names=_CORE_RELOAD_MODULES) -> list:
        return [
            name
            for name in list(sys.modules)
            if any(name == mod or name.startswith(f"{mod}.") for mod in module_names)
        ]

    def _purge(module_names=_CORE_RELOAD_MODULES) -> None:
        for name in _matching(module_names):
            sys.modules.pop(name, None)

    def _reset_db_singleton() -> None:
        state = sys.modules.get("topos.core.state")
        if state is None:
            return
        conn = getattr(state, "db_conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        state.db_conn = None
        state._db_conn_path = None

    snapshot = {name: sys.modules[name] for name in _matching()}
    yield _purge
    # Close whatever fork the test created, then put the originals back.
    _reset_db_singleton()
    for name in _matching():
        if name not in snapshot:
            sys.modules.pop(name, None)
    sys.modules.update(snapshot)
    # Also re-point parent-package attributes: pytest monkeypatch resolves
    # dotted targets by getattr traversal (topos.config → .settings), so a
    # stale parent attr would still expose a forked module.
    for name, module in snapshot.items():
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name) if parent_name else None
        if parent is not None:
            setattr(parent, child, module)

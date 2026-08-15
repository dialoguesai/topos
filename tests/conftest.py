import os
import sys
from pathlib import Path

import pytest

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

# Project root on sys.path for editable installs and `pytest` without install (development).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    del exitstatus, config
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


def pytest_sessionfinish(session, exitstatus) -> None:
    """Red the run on a leak, so the gate cannot pass while one is live."""
    del exitstatus
    if _LEAK_FINDINGS:
        session.exitstatus = 1


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    for item in items:
        path = str(item.fspath)
        if any(hint in path for hint in PRIVATE_PATH_HINTS):
            item.add_marker(pytest.mark.private)
        else:
            item.add_marker(pytest.mark.public)


# Lanes that legitimately reach a real database; everything else must never
# touch ~/.topos/database.db (or the other canonical candidates in
# topos.core.state._resolve_database_path_from_settings).
_LIVE_DB_EXEMPT_MARKERS = ("live", "e2e", "qq_eval")


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

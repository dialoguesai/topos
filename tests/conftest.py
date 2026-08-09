import os
import sys
from pathlib import Path

import pytest

# Default env so pydantic Settings() can load when tests import topos.* at collection time.
os.environ.setdefault("TOPOS_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CONTROL_PLANE_URL", "")
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

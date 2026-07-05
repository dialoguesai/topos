import os
import sys
from pathlib import Path

import pytest

# Default env so pydantic Settings() can load when tests import topos.* at collection time.
os.environ.setdefault("TOPOS_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("CONTROL_PLANE_URL", "")

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

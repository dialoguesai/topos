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
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    for item in items:
        path = str(item.fspath)
        if any(hint in path for hint in PRIVATE_PATH_HINTS):
            item.add_marker(pytest.mark.private)
        else:
            item.add_marker(pytest.mark.public)

from __future__ import annotations

from pathlib import Path


def test_aggregated_read_api_modules_exist() -> None:
    root = Path(__file__).resolve().parents[2] / "topos" / "api"
    assert (root / "sources_overview.py").is_file()
    assert (root / "runtime_bootstrap.py").is_file()
    assert (root / "database_explorer.py").is_file()


def test_handlers_register_aggregated_read_message_types() -> None:
    from topos.core.handlers import HANDLERS

    assert "get_sources_overview" in HANDLERS
    assert "get_runtime_bootstrap" in HANDLERS
    assert "get_database_explorer_summary" in HANDLERS

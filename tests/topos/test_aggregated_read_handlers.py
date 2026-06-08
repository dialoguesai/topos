from __future__ import annotations

from pathlib import Path


def test_aggregated_read_api_modules_exist() -> None:
    root = Path(__file__).resolve().parents[2] / "topos" / "api"
    assert (root / "sources_overview.py").is_file()
    assert (root / "runtime_bootstrap.py").is_file()
    assert (root / "database_explorer.py").is_file()


def test_handlers_register_aggregated_read_message_types() -> None:
    handlers = (Path(__file__).resolve().parents[2] / "topos" / "core" / "handlers.py").read_text()
    assert 'msg_type == "get_sources_overview"' in handlers
    assert 'msg_type == "get_runtime_bootstrap"' in handlers
    assert 'msg_type == "get_database_explorer_summary"' in handlers

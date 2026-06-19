"""
Gap: Model registry catalog — partial jobs → full PRD §6.3 provider defaults
Sprint: EN-P0-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import pytest

from topos.enrichment.models.mvp_defaults import load_mvp_defaults
from topos.enrichment.models.registry import ModelRegistry

pytestmark = pytest.mark.gap


def test_model_registry_catalog_resolves_hf_and_ollama() -> None:
    registry = ModelRegistry()
    load_mvp_defaults(registry)
    entity = registry.get_model_for_task("entity_extraction")
    summary = registry.get_model_for_task("raw_to_summary")
    assert entity is not None and entity["provider"] == "huggingface"
    assert summary is not None and summary["provider"] == "ollama"

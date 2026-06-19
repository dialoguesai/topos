"""Provider routing defaults: HF vs Ollama per task type."""

from topos.enrichment.models.mvp_defaults import load_mvp_defaults
from topos.enrichment.models.registry import ModelRegistry


def test_model_registry_provider_routing() -> None:
    registry = ModelRegistry()
    load_mvp_defaults(registry)

    entity = registry.get_model_for_task("entity_extraction")
    assert entity is not None
    assert entity["provider"] == "huggingface"
    assert entity["huggingface_path"]

    summary = registry.get_model_for_task("raw_to_summary")
    assert summary is not None
    assert summary["provider"] == "ollama"
    assert summary.get("ollama_model")

    rules = registry.get_model_for_task("relationship_scoring")
    assert rules is not None
    assert rules.get("metadata", {}).get("rules_only") is True

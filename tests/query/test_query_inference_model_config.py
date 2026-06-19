"""Query inference uses configurable Ollama model."""

from topos.config.settings import settings


def test_ollama_query_model_setting_default() -> None:
    assert settings.ollama_query_model

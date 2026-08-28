"""Task router: select backend adapter by provider."""

from __future__ import annotations

from .backends import BackendAdapter, get_huggingface_adapter, get_ollama_adapter, get_openai_adapter, get_platform_openai_adapter, get_redpill_adapter, get_stub_adapter
from .tasks import ProcessingTask


def get_adapter_for_task(task: ProcessingTask) -> BackendAdapter:
    """
    Return the backend adapter for this task's model_request.provider.
    huggingface -> HuggingFaceAdapter; ollama -> OllamaAdapter; else StubBackendAdapter.
    """
    provider = (task.model_request.provider or "").strip().lower()
    if provider == "huggingface":
        return get_huggingface_adapter()
    if provider == "ollama":
        return get_ollama_adapter()
    if provider == "openai":
        return get_openai_adapter()
    if provider == "platform":
        return get_platform_openai_adapter()
    if provider == "redpill":
        return get_redpill_adapter()
    return get_stub_adapter()

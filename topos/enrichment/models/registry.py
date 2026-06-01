from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional


class ModelRegistry:
    """In-memory model registry. Supports HuggingFace and Ollama providers."""

    def __init__(self):
        self._models: Dict[str, Dict[str, Any]] = {}

    def register_model(
        self,
        model_id: str,
        model_name: str,
        model_version: str,
        model_type: str,
        task_name: str,
        huggingface_path: str = "",
        is_preferred: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        provider: Literal["ollama", "huggingface"] = "huggingface",
        ollama_model: Optional[str] = None,
    ) -> None:
        self._models[model_id] = {
            "model_id": model_id,
            "model_name": model_name,
            "model_version": model_version,
            "model_type": model_type,
            "task_name": task_name,
            "huggingface_path": huggingface_path or "",
            "is_preferred": is_preferred,
            "metadata": metadata or {},
            "provider": provider,
            "ollama_model": ollama_model,
        }

    def get_model(self, model_id: str) -> Optional[Dict[str, Any]]:
        return self._models.get(model_id)

    def list_models(self, task_name: Optional[str] = None) -> List[Dict[str, Any]]:
        models = list(self._models.values())
        if task_name:
            models = [m for m in models if m.get("task_name") == task_name]
        return models

    def get_preferred_model(self, task_name: str, model_type: str) -> Optional[Dict[str, Any]]:
        for model in self._models.values():
            if model.get("task_name") == task_name and model.get("model_type") == model_type and model.get("is_preferred"):
                return model
        return None

    def get_model_for_task(
        self,
        task_type: str,
        subtype: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Return model spec for a task (e.g. enrichment + url_classification).
        Prefers is_preferred; otherwise first match by task_name or subtype.
        Returns dict with huggingface_path (and later provider, ollama_model).
        """
        candidates = [
            m for m in self._models.values()
            if m.get("task_name") == task_type or (subtype and m.get("task_name") == subtype)
        ]
        if not candidates:
            return None
        preferred = [m for m in candidates if m.get("is_preferred")]
        return (preferred[0] if preferred else candidates[0])

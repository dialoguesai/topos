"""Backend adapter protocol for model inference (PRD §8.2)."""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol


class BackendAdapter(Protocol):
    """Protocol for model backends (Ollama, HuggingFace)."""

    def load_model(self, model_name: str, config: Optional[Dict[str, Any]] = None) -> None:
        """Load or ensure model is loaded. Optional cache."""
        ...

    def run_inference(self, payload: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run inference; payload is task input; config may include subtype, model, etc. Returns output dict."""
        ...

    def unload_model(self, model_name: str) -> None:
        """Unload model to free memory."""
        ...

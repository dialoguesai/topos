"""Stub backend adapter for when no real backend is configured."""

from __future__ import annotations

from typing import Any, Dict, Optional


class StubBackendAdapter:
    """Stub adapter: no real inference, returns fixed dict."""

    def load_model(self, model_name: str, config: Optional[Dict[str, Any]] = None) -> None:
        pass

    def run_inference(self, payload: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"status": "stub", "message": "No backend configured; use Sprint 02+ for real inference"}

    def unload_model(self, model_name: str) -> None:
        pass


def get_stub_adapter() -> StubBackendAdapter:
    return StubBackendAdapter()

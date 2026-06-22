"""Backend adapters for model inference."""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from .base import BackendAdapter
from .huggingface import HuggingFaceAdapter
from .ollama import OllamaAdapter
from .openai import OpenAIAdapter
from .redpill import RedpillAdapter
from .stub import StubBackendAdapter, get_stub_adapter

_huggingface_singleton: HuggingFaceAdapter | None = None
_ollama_singleton: OllamaAdapter | None = None
_openai_singleton: OpenAIAdapter | None = None
_redpill_singleton: RedpillAdapter | None = None
_huggingface_lock = threading.Lock()
_ollama_lock = threading.Lock()
_openai_lock = threading.Lock()
_redpill_lock = threading.Lock()

__all__ = [
    "BackendAdapter",
    "HuggingFaceAdapter",
    "OllamaAdapter",
    "OpenAIAdapter",
    "RedpillAdapter",
    "StubBackendAdapter",
    "get_stub_adapter",
    "get_huggingface_adapter",
    "get_ollama_adapter",
    "get_openai_adapter",
    "get_redpill_adapter",
]


def get_huggingface_adapter() -> HuggingFaceAdapter:
    """Return the shared HuggingFace adapter (loads models on first use, cached afterward)."""
    global _huggingface_singleton
    if _huggingface_singleton is not None:
        return _huggingface_singleton
    with _huggingface_lock:
        if _huggingface_singleton is None:
            _huggingface_singleton = HuggingFaceAdapter()
        return _huggingface_singleton


def get_ollama_adapter() -> OllamaAdapter:
    """Return the shared Ollama adapter (uses ENGINE_OLLAMA_BASE_URL from config)."""
    global _ollama_singleton
    if _ollama_singleton is not None:
        return _ollama_singleton
    with _ollama_lock:
        if _ollama_singleton is None:
            _ollama_singleton = OllamaAdapter()
        return _ollama_singleton


def get_openai_adapter() -> OpenAIAdapter:
    global _openai_singleton
    if _openai_singleton is not None:
        return _openai_singleton
    with _openai_lock:
        if _openai_singleton is None:
            _openai_singleton = OpenAIAdapter()
        return _openai_singleton


def get_redpill_adapter() -> RedpillAdapter:
    global _redpill_singleton
    if _redpill_singleton is not None:
        return _redpill_singleton
    with _redpill_lock:
        if _redpill_singleton is None:
            _redpill_singleton = RedpillAdapter()
        return _redpill_singleton



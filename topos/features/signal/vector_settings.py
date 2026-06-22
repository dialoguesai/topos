"""Environment toggles for vector storage and retrieval."""

from __future__ import annotations

import os


def _flag(name: str, default: str = "on") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "on", "yes")


def vector_format() -> str:
    """Storage encoding for new writes: f32 (default) or json (legacy)."""
    fmt = os.environ.get("TOPOS_VECTOR_FORMAT", "f32").strip().lower()
    return fmt if fmt in ("f32", "json") else "f32"


def embedding_normalize_enabled() -> bool:
    return _flag("TOPOS_EMBEDDING_NORMALIZE", "on")


def vector_chunking_enabled() -> bool:
    return _flag("TOPOS_VECTOR_CHUNKING", "on")


def vector_hybrid_enabled() -> bool:
    return _flag("TOPOS_VECTOR_HYBRID", "on")


def vector_ann_mode() -> str:
    mode = os.environ.get("TOPOS_VECTOR_ANN", "auto").strip().lower()
    if mode in ("auto", "sqlite_vec", "pgvector", "brute_force"):
        return mode
    return "auto"


def cluster_collapse_chunks_enabled() -> bool:
    return _flag("TOPOS_CLUSTER_COLLAPSE_CHUNKS", "on")

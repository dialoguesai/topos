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


def min_similarity_threshold() -> float:
    raw = os.environ.get("TOPOS_VECTOR_MIN_SIMILARITY", "0.30").strip()
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.30


def embed_context_headers_enabled() -> bool:
    """Prepend source/date/conversation context to chunk text before embedding."""
    return _flag("TOPOS_EMBED_CONTEXT_HEADERS", "on")


def rerank_mode() -> str:
    """off | auto | on. auto = rerank when the cross-encoder loads without error."""
    mode = os.environ.get("TOPOS_RERANK", "auto").strip().lower()
    return mode if mode in ("off", "auto", "on") else "auto"


def rerank_candidate_limit() -> int:
    raw = os.environ.get("TOPOS_RERANK_CANDIDATES", "50").strip()
    try:
        return max(5, min(200, int(raw)))
    except ValueError:
        return 50


def fusion_rrf_enabled() -> bool:
    """Single RRF fusion in retrieval summary building (replaces score constants)."""
    return _flag("TOPOS_FUSION_RRF", "on")

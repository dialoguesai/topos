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


def fusion_recency_enabled() -> bool:
    """Exponential recency decay on time-stamped fusion contributors."""
    return _flag("TOPOS_FUSION_RECENCY", "on")


def fusion_recency_half_life_days() -> float:
    raw = os.environ.get("TOPOS_RECENCY_HALF_LIFE_DAYS", "45").strip()
    try:
        return max(1.0, min(3650.0, float(raw)))
    except ValueError:
        return 45.0


def fusion_recency_floor() -> float:
    """Decay never drops below this: old-but-exact matches must stay reachable."""
    raw = os.environ.get("TOPOS_RECENCY_FLOOR", "0.2").strip()
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.2


def vector_evidence_min() -> float:
    """Cosine floor above which a vector hit counts as *evidence* (can justify a
    non-empty result on its own). Hits between the retrieval min-similarity and
    this line still contribute context when other evidence exists — they just
    can't rescue a query about something that isn't there."""
    raw = os.environ.get("TOPOS_VECTOR_EVIDENCE_MIN", "0.40").strip()
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.40


def rare_token_df_max() -> int:
    """A query token whose FTS document frequency is below this is *rare* —
    the query is asking for something specific, and honest retrieval must
    either match it or return nothing (absence honesty)."""
    raw = os.environ.get("TOPOS_RARE_TOKEN_DF_MAX", "30").strip()
    try:
        return max(0, min(10_000, int(raw)))
    except ValueError:
        return 30


def cluster_max_k() -> int:
    """Upper bound on k per clustering facet.

    The historic per-facet cap of 6 collapsed a 20k-record corpus into six
    mega-clusters (55% of members in one). k grows as sqrt(n) up to this cap.
    """
    raw = os.environ.get("TOPOS_CLUSTER_MAX_K", "64").strip()
    try:
        return max(2, min(512, int(raw)))
    except ValueError:
        return 64


def cluster_split_size() -> int:
    """Members above which a cluster is re-clustered within itself.

    Measured live 2026-08-15: 23 clusters of 120+ members held 51% of all
    members, and the labeler cannot name a 446-member bag with one subject —
    the base-name collisions ("Cameron and Danielle" x13) concentrate in
    exactly these clusters. 0 disables splitting.
    """
    raw = os.environ.get("TOPOS_CLUSTER_SPLIT_SIZE", "120").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 120


def cluster_label_raw_text_enabled() -> bool:
    """Labeling reads canonical (pre-redaction) text; stored rows stay redacted.

    The privacy filter strips exactly the nouns a labeler needs ([NAME],
    [ACCOUNT] in text_preview), so labels and distinguishing terms were being
    minted from de-specified text. Owner decision 2026-08-15: the label prompt
    and term extraction read the canonical row IN MEMORY; every persisted
    surface (text_preview on embeddings and members) keeps redaction exactly
    where and how it is.
    """
    return _flag("TOPOS_CLUSTER_LABEL_RAW_TEXT", "on")


def cluster_assign_threshold() -> float:
    """Min cosine for incremental assignment to an existing cluster.

    Below it, embeddings go to the candidate pool for the next consolidation.
    0.60 let mega-clusters absorb everything; 0.75 is deliberately strict.
    """
    raw = os.environ.get("TOPOS_CLUSTER_ASSIGN_THRESHOLD", "0.75").strip()
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.75


def cluster_llm_labels_mode() -> str:
    """off | auto | on. auto = LLM labels when the local model answers; any
    failure falls back to the deterministic term labels."""
    mode = os.environ.get("TOPOS_CLUSTER_LLM_LABELS", "auto").strip().lower()
    return mode if mode in ("off", "auto", "on") else "auto"


def cluster_max_records() -> int:
    """Embedding rows loaded into a topic-cluster recompute.

    Historic default was 5000, which silently sampled ~20% of a re-enriched
    corpus; with the numpy k-means path the full corpus is affordable.
    """
    raw = os.environ.get("TOPOS_CLUSTER_MAX_RECORDS", "25000").strip()
    try:
        return max(100, min(200_000, int(raw)))
    except ValueError:
        return 25_000

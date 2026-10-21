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


def derived_object_index_enabled() -> bool:
    """Whether the derived-object lane is searched and written.

    Off is a real position, not just a rollback: it makes the node answer from
    raw records only. Both the writer and the reader consult this, so turning
    it off stops the index growing AND stops it being served — an index that
    keeps being written while nothing reads it is the state this whole lane
    was built to end.
    """
    return _flag("TOPOS_DERIVED_OBJECT_INDEX", "on")


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


def fusion_vector_head() -> int:
    """How many of the vector lane's own best evidence hits the fused list keeps at its head.

    Weighted RRF ranks the vector lane (1.0, recency-decayed) behind every undecayed lane
    weighted 1.5-2.0, so its first hit could not reach the 25-item list, let alone the
    first 12 that home chat's synthesis reads. Measured on an owner snapshot 2026-10-21
    (counts only, 300 known-item turns outside iMessage): the vector lane's FIRST hit was
    the answering record in 121 of them and its second in 22; the fused list held the
    answer in 14. 0 disables the head.
    """
    raw = os.environ.get("TOPOS_FUSION_VECTOR_HEAD", "2").strip()
    try:
        return max(0, min(10, int(raw)))
    except ValueError:
        return 2


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


def evidence_shaping_enabled() -> bool:
    """Source-blind shaping of the evidence that reaches the answer. Default off.

    One switch for four rules, each read only from relevance, text, conversation
    identity and time, never from which connector a row came from:

    1. the vector lane groups its hits by conversation (or by a short session when a
       row has no conversation), so one long session is one witness;
    2. near-duplicate hits collapse to the better-ranked copy;
    3. a hit far below the best cosine does not pad the lane;
    4. derived items cannot take more than `derived_share_max` of any prefix of the
       fused list while source records remain to fill it.

    Measured on the owner's snapshot (21 Oct 2026, 112 questions): the vector lane
    held the answering record in its top five for 143 of the 164 known-item turns
    that lost it, and vector items filled 0.2% of the fused top 20.
    """
    return _flag("TOPOS_EVIDENCE_SHAPING", "off")


def evidence_pool_limit() -> int:
    """Vector candidates fetched before rules 1–3 cut them back to the lane's 20."""
    raw = os.environ.get("TOPOS_EVIDENCE_POOL", "60").strip()
    try:
        return max(20, min(100, int(raw)))
    except ValueError:
        return 60


def evidence_group_passages() -> int:
    """Passages one conversation may contribute; the second comes after every group's first.
    0 turns grouping off (hits keep their order and are cut to the lane's size)."""
    raw = os.environ.get("TOPOS_EVIDENCE_GROUP_PASSAGES", "2").strip()
    try:
        return max(0, min(10, int(raw)))
    except ValueError:
        return 2


def evidence_group_bonus() -> float:
    """Lift per distinct supporting hit in a group (at most three count)."""
    raw = os.environ.get("TOPOS_EVIDENCE_GROUP_BONUS", "0.05").strip()
    try:
        return max(0.0, min(0.5, float(raw)))
    except ValueError:
        return 0.05


def evidence_session_gap_minutes() -> float:
    """Hits with no conversation id chain into one session while each is within this
    many minutes of the previous one from the same stream. 0 keeps every such hit apart."""
    raw = os.environ.get("TOPOS_EVIDENCE_SESSION_GAP_MINUTES", "30").strip()
    try:
        return max(0.0, min(24 * 60.0, float(raw)))
    except ValueError:
        return 30.0


def evidence_duplicate_jaccard() -> float:
    """Word-shingle Jaccard at or above which two hits are one. 0 disables."""
    raw = os.environ.get("TOPOS_EVIDENCE_DUPLICATE_JACCARD", "0.8").strip()
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.8


def evidence_floor_margin() -> float:
    """Cosine distance below the best hit beyond which a hit is a far neighbour. 0 disables.

    Calibrated 21 Oct 2026 for all-MiniLM-L6-v2 on the owner's snapshot, split half: on
    the calibration half 0.15 was the smallest of 0.05/0.08/0.10/0.12/0.15/0.20 to keep
    95% of the answering records found in the 60-hit pool (97.3%); on the held-out half
    it kept 97.9% and cut 38% of pool hits. Re-calibrate after an embedding-model change.
    """
    raw = os.environ.get("TOPOS_EVIDENCE_FLOOR_MARGIN", "0.15").strip()
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.15


def derived_share_max() -> float:
    """Largest share of any fused-list prefix derived items may hold while source
    records remain. 1.0 is the plain fusion."""
    raw = os.environ.get("TOPOS_DERIVED_SHARE_MAX", "0.5").strip()
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.5

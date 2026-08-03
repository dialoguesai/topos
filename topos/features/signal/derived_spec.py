"""Hashed derived-layer recipe (PLAN_GRAPH_QUERY_AND_LATENT_EDGES §5 M4).

txtai indexes are reproducible from one config dict. Ours are reproducible only
from a migration chain plus many modules of orchestration — until this module
exists. ``derived_spec()`` collects the parameters that actually govern the
derived layer from where they already live, and ``derived_spec_version()`` is a
stable hash of that collection.

A node can ask "was my derived layer built under the current recipe?" by
comparing the hash stored in ``engine_config`` against the live hash. That is
what makes Lane-D reprocessing in PLAN_NODE_RELEASE_MIGRATIONS.md *decidable*
rather than a judgement call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ENGINE_CONFIG_KEY = "derived.spec_version"


def derived_spec() -> Dict[str, Any]:
    """The governing parameters of the derived layer, read from their homes.

    Keys are sorted at hash time; do not depend on insertion order here. Values
    are JSON-serialisable primitives only — never live objects or callables.
    """
    from ...engine.backends.huggingface import active_embedding_model
    from ..entities.affinity import (
        AFFINITY_CEILING_RATIO,
        AFFINITY_DEFAULT_PERCENTILE,
        AFFINITY_FLOOR_ABS,
        AFFINITY_SAMPLE_ANCHORS,
        AFFINITY_SPEC_VERSION,
        AFFINITY_TOPN,
    )
    from ..entities.context_vectors import (
        CENTROID_DEGENERACY_EPSILON,
        CONTEXT_VECTOR_ENTITY_TYPE,
        MIN_CONTEXT_MENTIONS,
        MIN_CONTEXT_SOURCES,
    )
    from . import vector_settings

    return {
        "affinity": {
            "ceiling_ratio": AFFINITY_CEILING_RATIO,
            "default_percentile": AFFINITY_DEFAULT_PERCENTILE,
            "floor_abs": AFFINITY_FLOOR_ABS,
            "sample_anchors": AFFINITY_SAMPLE_ANCHORS,
            "spec_version": AFFINITY_SPEC_VERSION,
            "topn": AFFINITY_TOPN,
        },
        "context_vectors": {
            "degeneracy_epsilon": CENTROID_DEGENERACY_EPSILON,
            "entity_type": CONTEXT_VECTOR_ENTITY_TYPE,
            "min_mentions": MIN_CONTEXT_MENTIONS,
            "min_sources": MIN_CONTEXT_SOURCES,
        },
        "embedding": {
            "model": active_embedding_model(),
            "normalize": vector_settings.embedding_normalize_enabled(),
            "vector_format": vector_settings.vector_format(),
        },
        "retrieval": {
            "ann_mode": vector_settings.vector_ann_mode(),
            "chunking": vector_settings.vector_chunking_enabled(),
            "context_headers": vector_settings.embed_context_headers_enabled(),
            "hybrid": vector_settings.vector_hybrid_enabled(),
            "min_similarity": vector_settings.min_similarity_threshold(),
        },
    }


def derived_spec_version(spec: Optional[Dict[str, Any]] = None) -> str:
    """Stable sha256 over the derived-layer recipe.

    Canonical JSON (sorted keys, no whitespace) so the hash is insensitive to
    dict insertion order and to cosmetic formatting.
    """
    payload = spec if spec is not None else derived_spec()
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def persist_derived_spec_version(
    conn: sqlite3.Connection, *, version: Optional[str] = None
) -> str:
    """Write the current (or supplied) hash into ``engine_config`` and return it."""
    from ...core.state import set_engine_config_value

    resolved = version or derived_spec_version()
    set_engine_config_value(conn, ENGINE_CONFIG_KEY, resolved)
    return resolved


def stored_derived_spec_version(conn: sqlite3.Connection) -> Optional[str]:
    from ...core.state import get_engine_config_value

    value = get_engine_config_value(conn, ENGINE_CONFIG_KEY)
    return str(value) if value else None


def derived_spec_changed(conn: sqlite3.Connection) -> bool:
    """True when the live recipe differs from the one last persisted on this node."""
    stored = stored_derived_spec_version(conn)
    if stored is None:
        return True
    return stored != derived_spec_version()

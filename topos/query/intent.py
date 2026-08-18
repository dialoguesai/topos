"""Intent normalization and hashing (PRD §8.3)."""

from __future__ import annotations

import hashlib
import re


def normalize_query(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def compute_intent_hash(
    *, scope_id: str, access_mode: str, query_text: str, retrieval_text: str = ""
) -> str:
    """Hash of everything that steers what a turn RETRIEVES.

    `retrieval_text` is part of the identity, not an annotation: it steers the rare-gate
    needles and (since P2) the semantic query, so two calls sharing scope + mode + query
    but differing in `retrieval_text` retrieve DIFFERENT things. Before it was hashed,
    they collided here, `_load_cached_artifact` served the first call's `public_result`
    verbatim as the second's, and the response was stamped `turn_outcome: memory_hit` —
    a healthy-looking, per-section fabrication. Found by the P3 design review
    (2026-08-18) as a latent hazard one field away from live.
    """
    normalized = normalize_query(query_text)
    payload = f"{scope_id}|{access_mode}|{normalized}"
    needle = normalize_query(retrieval_text)
    if needle and needle != normalized:
        payload += f"|needles:{needle}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

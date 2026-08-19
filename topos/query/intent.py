"""Intent normalization and hashing (PRD §8.3)."""

from __future__ import annotations

import hashlib
import re
from typing import Optional, Sequence

#: Field separator inside a multi-valued hash segment. A control character, because
#: `normalize_query` only lowercases and collapses whitespace — an owner sentence can
#: contain `|`, `,` or `;`, and two different requests must never be able to produce
#: the same payload by punctuation alone.
_UNIT = "\x1f"


def normalize_query(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def compute_intent_hash(
    *,
    scope_id: str,
    access_mode: str,
    query_text: str,
    retrieval_text: str = "",
    needle_parts: Optional[Sequence[str]] = None,
    time_windows: Optional[Sequence[Sequence[str]]] = None,
) -> str:
    """Hash of everything that steers what a turn RETRIEVES.

    `retrieval_text` is part of the identity, not an annotation: it steers the rare-gate
    needles and (since P2) the semantic query, so two calls sharing scope + mode + query
    but differing in `retrieval_text` retrieve DIFFERENT things. Before it was hashed,
    they collided here, `_load_cached_artifact` served the first call's `public_result`
    verbatim as the second's, and the response was stamped `turn_outcome: memory_hit` —
    a healthy-looking, per-section fabrication. Found by the P3 design review
    (2026-08-18) as a latent hazard one field away from live.

    `needle_parts` and `time_windows` extend that identity for exactly the same reason,
    and the hazard is the same one: P3 gives a request per-part needles and a differenced
    ask two windows, both of which change what comes back while leaving scope + mode +
    query + retrieval_text identical. Two report sections that differ only in their parts,
    or one week's comparison and the next's, would otherwise collide — and a collision
    here is not a stale answer, it is one section's findings served as another's under a
    `memory_hit` stamp.

    Order is significant for both: the parts are the request's parts in request order,
    and the windows are baseline-then-current. Absent (every caller today) leaves the
    payload byte-identical to the pre-P3 formula, so no cached artifact is orphaned.
    """
    normalized = normalize_query(query_text)
    payload = f"{scope_id}|{access_mode}|{normalized}"
    needle = normalize_query(retrieval_text)
    if needle and needle != normalized:
        payload += f"|needles:{needle}"
    parts = [p for p in (normalize_query(p) for p in (needle_parts or [])) if p]
    # A lone part that merely repeats the needles (or the query, when there are none)
    # is the single-needle request spelled a second way — hashing it separately would
    # split today's cache for no difference in what is retrieved.
    if parts and parts != [needle or normalized]:
        payload += "|parts:" + _UNIT.join(parts)
    windows = [
        f"{str(bounds[0]).strip()}~{str(bounds[1]).strip()}"
        for bounds in (time_windows or [])
        if bounds is not None and len(bounds) >= 2
    ]
    if windows:
        payload += "|windows:" + _UNIT.join(windows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

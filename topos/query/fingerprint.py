"""Retrieval fingerprint for cache invalidation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional


def compute_retrieval_fingerprint(
    *,
    scope_id: str,
    access_mode: str,
    filter_manifest: Optional[Dict[str, Any]] = None,
    data_health_version: str = "mvp",
    source_ids: Optional[List[str]] = None,
    disclosure_tier: str = "owner_raw",
    grant_id: str = "owner",
    field_transforms: Optional[Any] = None,
    packet_resolution: str = "scores_only",
) -> str:
    """Cache-invalidation + isolation key for a retrieval.

    Includes the *disclosure dimensions* (tier, grant identity, field transforms) — not just
    the retrieval config — so a cache entry can never be shared across grants or tiers even
    when a future cache is widened beyond the per-session scope (§B.4, prerequisite for §E).
    Within a session these values are constant, so memory hits are unaffected.
    """
    fm = json.dumps(filter_manifest or {}, sort_keys=True, default=str)
    ft = json.dumps(field_transforms or [], sort_keys=True, default=str)
    ids = ",".join(sorted({str(s).strip() for s in (source_ids or []) if str(s).strip()}))
    payload = (
        f"{scope_id}|{access_mode}|{data_health_version}|{ids}|{fm}"
        f"|tier={disclosure_tier}|grant={grant_id}|ft={ft}|pr={packet_resolution}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

"""Retrieval fingerprint for cache invalidation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from ..__version__ import __version__ as _ENGINE_VERSION


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

    Includes the ENGINE VERSION because a cached answer is only valid for the code
    that produced it. Without it no engine upgrade could invalidate the cache: live
    2026-08-26 the node was rebuilt with a fix for "Who's in my family?" and the chat
    kept replaying the pre-fix payload as `turn_outcome: memory_hit` — junk entity
    labels, 21 minutes after the fix went live, with a 24h session TTL still to run.
    Bumping the version now flushes every stale entry on release, exactly as it
    should. (Within one version, dev rebuilds still reuse the cache — a dev iterating
    on retrieval should expire the session or bump the version.)
    """
    fm = json.dumps(filter_manifest or {}, sort_keys=True, default=str)
    ft = json.dumps(field_transforms or [], sort_keys=True, default=str)
    ids = ",".join(sorted({str(s).strip() for s in (source_ids or []) if str(s).strip()}))
    payload = (
        f"{scope_id}|{access_mode}|{data_health_version}|{ids}|{fm}"
        f"|tier={disclosure_tier}|grant={grant_id}|ft={ft}|pr={packet_resolution}"
        f"|engine={_ENGINE_VERSION}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

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
) -> str:
    fm = json.dumps(filter_manifest or {}, sort_keys=True, default=str)
    ids = ",".join(sorted({str(s).strip() for s in (source_ids or []) if str(s).strip()}))
    payload = f"{scope_id}|{access_mode}|{data_health_version}|{ids}|{fm}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

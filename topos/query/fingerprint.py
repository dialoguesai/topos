"""Retrieval fingerprint for cache invalidation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional


def compute_retrieval_fingerprint(
    *,
    scope_id: str,
    access_mode: str,
    filter_manifest: Optional[Dict[str, Any]] = None,
    data_health_version: str = "mvp",
) -> str:
    fm = json.dumps(filter_manifest or {}, sort_keys=True, default=str)
    payload = f"{scope_id}|{access_mode}|{data_health_version}|{fm}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

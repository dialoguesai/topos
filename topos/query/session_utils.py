"""Query session artifact validation."""

from __future__ import annotations

from typing import Any, Dict, List

from .types import FORBIDDEN_ARTIFACT_KEYS


def validate_public_result(payload: Dict[str, Any]) -> None:
    forbidden = _find_forbidden_keys(payload)
    if forbidden:
        raise ValueError(f"public_result contains forbidden keys: {forbidden}")


def _find_forbidden_keys(obj: Any, prefix: str = "") -> List[str]:
    found: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_ARTIFACT_KEYS:
                found.append(k if not prefix else f"{prefix}.{k}")
            found.extend(_find_forbidden_keys(v, k))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found.extend(_find_forbidden_keys(item, f"{prefix}[{i}]"))
    return found


def build_cache_key(*, scope_id: str, access_mode: str, intent_hash: str,
                    packet_resolution: str = "scores_only") -> str:
    # packet_resolution is a DISCLOSURE dimension (same rule as the retrieval
    # fingerprint): a downgrade must miss the cache, never be served a stored
    # high-resolution answer. scores_only keeps the legacy 3-part key so
    # pre-feature artifacts stay reachable for unchanged-policy nodes.
    if packet_resolution and packet_resolution != "scores_only":
        return f"{scope_id}:{access_mode}:{intent_hash}:pr={packet_resolution}"
    return f"{scope_id}:{access_mode}:{intent_hash}"

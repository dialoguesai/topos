"""Read-time disclosure tier: owner raw vs pre-redacted canonical text."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from .field_registry import (
    PII_DISCLOSURE_FIELDS,
    disclosure_column,
    disclosure_hash_column,
)

logger = logging.getLogger("topos.disclosure.tier")

DisclosureTier = Literal["owner_raw", "default_disclosure"]

_INGEST_PII_TRANSFORMS = frozenset({"pii_redaction", "name_removal", "contact_removal"})
_INGEST_PLATFORM_TRANSFORMS = _INGEST_PII_TRANSFORMS | frozenset({"nsfw_sanitization"})
_DISCLOSURE_CEILING_RANK = {"default": 0, "partial": 1, "elevated": 2, "raw": 3}


def resolve_disclosure_tier(
    *,
    requester_id: str = "owner",
    owner_id: str = "owner",
    is_grantee_request: bool = False,
    explicit_tier: Optional[DisclosureTier] = None,
    disclosure_ceiling: Optional[str] = None,
) -> DisclosureTier:
    if explicit_tier in ("owner_raw", "default_disclosure"):
        return explicit_tier
    req = str(requester_id or "").strip() or "owner"
    own = str(owner_id or "").strip() or "owner"
    is_grantee = is_grantee_request or (req != own and req != "owner" and req != "mcp")
    if not is_grantee:
        return "owner_raw"
    ceiling = str(disclosure_ceiling or "default").strip().lower()
    if ceiling in ("partial", "elevated", "raw"):
        # Elevation not implemented — fail closed to default disclosure.
        logger.debug("disclosure_ceiling=%s not supported; using default_disclosure", ceiling)
    return "default_disclosure"


def apply_disclosure_tier_to_rows(
    rows: List[Dict[str, Any]],
    *,
    table: str,
    tier: DisclosureTier,
) -> List[Dict[str, Any]]:
    from .content_policy import apply_grantee_content_policy

    return apply_grantee_content_policy(rows, table=table, tier=tier)


def _swap_disclosure_columns(
    rows: List[Dict[str, Any]],
    *,
    table: str,
) -> List[Dict[str, Any]]:
    fields = PII_DISCLOSURE_FIELDS.get(table, ())
    if not fields:
        return rows
    out: List[Dict[str, Any]] = []
    for row in rows:
        copy = dict(row)
        for field in fields:
            disc_col = disclosure_column(field)
            disclosed = copy.get(disc_col)
            if isinstance(disclosed, str) and disclosed.strip():
                copy[field] = disclosed
            elif copy.get(field):
                logger.debug(
                    "disclosure pending for %s.%s record=%s",
                    table,
                    field,
                    copy.get("record_id") or copy.get("message_id"),
                )
        out.append(copy)
    return out


def strip_ingest_pii_transforms(field_transforms: Optional[List[Any]]) -> Optional[List[Any]]:
    """Remove PII transforms already applied at ingest for default_disclosure reads."""
    if not field_transforms:
        return field_transforms
    out: List[Any] = []
    for tf in field_transforms:
        if isinstance(tf, dict):
            tids = list(tf.get("transform_ids") or [])
            if not tids and tf.get("transform_id"):
                tids = [str(tf["transform_id"])]
            remaining = [t for t in tids if t not in _INGEST_PLATFORM_TRANSFORMS]
            if not remaining:
                continue
            updated = dict(tf)
            if "transform_ids" in updated:
                updated["transform_ids"] = remaining
            elif "transform_id" in updated:
                updated["transform_id"] = remaining[0]
            out.append(updated)
        else:
            tid = getattr(tf, "transform_id", None)
            if tid in _INGEST_PLATFORM_TRANSFORMS:
                continue
            out.append(tf)
    return out or None

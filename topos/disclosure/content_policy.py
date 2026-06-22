"""Grantee content policy: disclosure tier + NSFW exclusion (Platform Privacy Layer)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from .field_registry import PII_DISCLOSURE_FIELDS, disclosure_column, disclosure_hash_column
from .tier import DisclosureTier, _swap_disclosure_columns

logger = logging.getLogger("topos.disclosure.content_policy")

NSFW_TAG_COLUMNS = ("content_nsfw", "content_nsfw_score", "content_nsfw_model")


def is_record_nsfw(row: Dict[str, Any]) -> bool:
    """True when ingest-time NSFW tag marks the record as not safe for grantee reads."""
    flag = row.get("content_nsfw")
    if flag in (1, True, "1"):
        return True
    if isinstance(flag, str) and flag.strip().lower() in ("1", "true", "yes", "nsfw"):
        return True
    return False


def exclude_nsfw_rows_for_grantee(
    rows: List[Dict[str, Any]],
    *,
    tier: DisclosureTier,
) -> List[Dict[str, Any]]:
    if tier == "owner_raw" or not rows:
        return rows
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for row in rows:
        if is_record_nsfw(row):
            dropped += 1
            continue
        kept.append(row)
    if dropped:
        logger.debug("grantee NSFW exclusion dropped %d row(s)", dropped)
    return kept


def apply_grantee_content_policy(
    rows: List[Dict[str, Any]],
    *,
    table: str,
    tier: DisclosureTier,
) -> List[Dict[str, Any]]:
    """Owner: raw rows including NSFW. Grantee: disclosure text + NSFW rows removed."""
    if tier == "owner_raw" or not rows:
        return rows
    disclosed = _swap_disclosure_columns(rows, table=table)
    filtered = exclude_nsfw_rows_for_grantee(disclosed, tier=tier)
    return [_strip_internal_privacy_columns(row, table) for row in filtered]


def _strip_internal_privacy_columns(row: Dict[str, Any], table: str) -> Dict[str, Any]:
    copy = dict(row)
    for field in PII_DISCLOSURE_FIELDS.get(table, ()):
        copy.pop(disclosure_column(field), None)
        copy.pop(disclosure_hash_column(field), None)
    copy.pop("content_disclosure_model", None)
    for col in NSFW_TAG_COLUMNS:
        copy.pop(col, None)
    return copy

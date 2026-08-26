"""Read-time disclosure tier: owner raw vs pre-redacted canonical text."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from .field_registry import (
    DISCLOSURE_APPLIED_MARKER,
    DISCLOSURE_PENDING_PLACEHOLDER,
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
    principal: "Optional[object]" = None,
) -> DisclosureTier:
    # Determine grantee status FIRST. A grantee request must never be elevated to
    # owner_raw by an explicit tier in the payload — that field is requester-influenced
    # upstream, so honoring it before the grantee check is a fail-open (B.2).
    #
    # P1 (principal fabric): when the channel verified a client class, the class
    # decides and the payload-id heuristic below — INCLUDING its "mcp" whitelist,
    # which any keyholder could claim (the fail-open this module's own warning
    # describes) — is bypassed entirely. An OWNER_APP surface takes the owner
    # path; a THIRD_PARTY client is clamped like a grantee, explicit tier
    # ignored. The heuristic survives only for CP_RELAY (the CP already
    # classified the caller and stamps owner ids for native clients only) and
    # for legacy callers with no principal, where removing "mcp" would demote
    # the owner's own single-key surfaces before they learn the owner key.
    from ..principal import OWNER_APP, THIRD_PARTY

    req = str(requester_id or "").strip() or "owner"
    own = str(owner_id or "").strip() or "owner"
    cls = getattr(principal, "cls", None)
    if cls == OWNER_APP and not is_grantee_request:
        is_grantee = False
    elif cls == THIRD_PARTY:
        is_grantee = True
    else:
        is_grantee = is_grantee_request or (req != own and req != "owner" and req != "mcp")

    if is_grantee:
        if explicit_tier == "owner_raw":
            logger.warning(
                "explicit_tier=owner_raw ignored for grantee request (requester=%s owner=%s); "
                "clamping to default_disclosure",
                req,
                own,
            )
        ceiling = str(disclosure_ceiling or "default").strip().lower()
        if ceiling in ("partial", "elevated", "raw"):
            # Elevation not implemented — fail closed to default disclosure.
            logger.debug("disclosure_ceiling=%s not supported; using default_disclosure", ceiling)
        return "default_disclosure"

    # Owner (or internal mcp) path: an explicit tier may be honored.
    if explicit_tier in ("owner_raw", "default_disclosure"):
        return explicit_tier
    return "owner_raw"


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
        # Idempotence: once a row has been resolved by the grantee content policy, a second
        # application must be a no-op. Without this, the already-redacted content (which no
        # longer has a disclosure column) is mistaken for pending raw and overwritten with
        # the placeholder — over-redacting legitimate grantee content. The read path applies
        # disclosure more than once (store.list + retrieval, SQL pre-swap + Python), so the
        # transform must be safe under repetition.
        if copy.get(DISCLOSURE_APPLIED_MARKER):
            out.append(copy)
            continue
        for field in fields:
            disc_col = disclosure_column(field)
            disclosed = copy.get(disc_col)
            value = copy.get(field)
            if isinstance(disclosed, str) and disclosed.strip():
                copy[field] = disclosed
            elif isinstance(value, str) and value == DISCLOSURE_PENDING_PLACEHOLDER:
                # Already placeholdered upstream (e.g. SQL coalesce) — fixed point, no-op.
                continue
            elif value:
                # Ingest disclosure has not completed for this record and no disclosure text
                # is available: fail CLOSED — never surface raw to a grantee.
                logger.warning(
                    "disclosure pending; withholding raw %s.%s from grantee record=%s",
                    table,
                    field,
                    copy.get("record_id") or copy.get("message_id"),
                )
                copy[field] = DISCLOSURE_PENDING_PLACEHOLDER
        copy[DISCLOSURE_APPLIED_MARKER] = True
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

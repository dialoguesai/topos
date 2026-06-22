"""Post-retrieval disclosure filter pipeline (PRD §8)."""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

from shared.filtering import FieldTransform, filter_manifest_from_storage

from ..disclosure.tier import strip_ingest_pii_transforms
from ..uma_filters import apply_filter_manifest, extract_field_transforms
from .types import FilteredContext, RetrievalBundle

_EMAIL_RE = re.compile(r"[\w.-]+@[\w.-]+\.\w+")
_PHONE_RE = re.compile(r"\+?\d[\d\s()-]{7,}\d")
_NSFW_TOKENS = ("nsfw", "xxx")


def _transform_field(tf: Any, field: str, default: Any = None) -> Any:
    if isinstance(tf, dict):
        return tf.get(field, default)
    return getattr(tf, field, default)


def _redact_pii(text: str) -> str:
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    return text


def _sanitize_nsfw(text: str) -> str:
    lower = text.lower()
    for tok in _NSFW_TOKENS:
        if tok in lower:
            return "[SANITIZED]"
    return text


def _apply_field_transforms(rows: List[Dict[str, Any]], transforms: Optional[List[FieldTransform]]) -> List[Dict[str, Any]]:
    if not transforms:
        return rows
    out: List[Dict[str, Any]] = []
    for row in rows:
        copy = dict(row)
        for tf in transforms:
            field = _transform_field(tf, "field")
            table_id = _transform_field(tf, "table_id")
            tids = _transform_field(tf, "transform_ids") or []
            if table_id and copy.get("_table") and copy.get("_table") != table_id:
                continue
            if field not in copy or not isinstance(copy[field], str):
                continue
            val = copy[field]
            if "pii_redaction" in tids:
                val = _redact_pii(val)
            if "nsfw_sanitization" in tids:
                val = _sanitize_nsfw(val)
            copy[field] = val
        out.append(copy)
    return out


class DisclosureFilterPipeline:
    def apply(
        self,
        bundle: RetrievalBundle,
        *,
        filter_manifest: Optional[Dict[str, Any]] = None,
        field_transforms: Optional[List[Any]] = None,
        access_mode: str = "raw",
        disclosure_tier: str = "owner_raw",
    ) -> FilteredContext:
        applied: List[str] = []
        packet = copy.deepcopy(bundle.context_packet or {})

        if access_mode == "summary":
            packet.pop("rows", None)
            for row in packet.get("summaries") or []:
                if isinstance(row, dict):
                    row.pop("content", None)
            applied.append("summary_mode_strip_raw")

        if access_mode == "inference":
            for key in ("rows", "summaries", "content", "messages"):
                packet.pop(key, None)
            applied.append("inference_mode_strip_evidence")

        rows = packet.get("rows")
        if isinstance(rows, list) and filter_manifest:
            fm = filter_manifest_from_storage(filter_manifest)
            if fm:
                rows = apply_filter_manifest(rows, fm)
                packet["rows"] = rows
                applied.append("filter_manifest")

        if isinstance(rows, list):
            transforms = field_transforms
            if transforms is None and isinstance(filter_manifest, dict):
                transforms = extract_field_transforms(
                    {"field_transforms": filter_manifest.get("field_transforms")}
                )
            if disclosure_tier == "default_disclosure":
                from ..disclosure.content_policy import exclude_nsfw_rows_for_grantee

                rows = exclude_nsfw_rows_for_grantee(rows, tier=disclosure_tier)  # type: ignore[arg-type]
                applied.append("nsfw_exclusion")
                transforms = strip_ingest_pii_transforms(transforms)
                if transforms is not None:
                    applied.append("ingest_disclosure_pii")
            packet["rows"] = _apply_field_transforms(rows, transforms)
            if transforms:
                applied.append("field_transforms")

        return FilteredContext(context_packet=packet, filters_applied=applied)

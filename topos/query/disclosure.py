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
# The loose phone pattern also catches ISO dates (YYYY-MM-DD[ T]HH:MM). Skip candidates that
# begin with a date shape so a bare date is not redacted as a phone number — while still
# redacting real phone numbers (which never start with \d{4}-\d{2}-\d{2}). Guarding here rather
# than tightening the phone pattern avoids UNDER-redacting phones (a leak is worse than an
# over-redacted date).
_ISO_DATE_PREFIX_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NSFW_TOKENS = ("nsfw", "xxx")


def _transform_field(tf: Any, field: str, default: Any = None) -> Any:
    if isinstance(tf, dict):
        return tf.get(field, default)
    return getattr(tf, field, default)


def _redact_phone(match: "re.Match[str]") -> str:
    candidate = match.group(0)
    if _ISO_DATE_PREFIX_RE.match(candidate):
        return candidate  # a date (or datetime), not a phone number
    return "[REDACTED_PHONE]"


def _redact_pii(text: str) -> str:
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _PHONE_RE.sub(_redact_phone, text)
    return text


def _sanitize_nsfw(text: str) -> str:
    lower = text.lower()
    for tok in _NSFW_TOKENS:
        if tok in lower:
            return "[SANITIZED]"
    return text


# Text-bearing keys on summary/score/fact items that must be PII-scrubbed for grantees.
# Non-`rows` artifacts (summaries, scores, and future facts/dossiers) surface through these
# keys; without scrubbing them a grantee could receive raw PII in a summary_text/topic that
# never passed through the row-level filters (B.3 — the dense-upgrade blocker).
_GRANTEE_TEXT_KEYS = (
    "topic",
    "summary_text",
    "label",
    "text",
    "text_preview",
    "content_preview",
    "entity_text",
    "tag",
    "title",
    "description",
)


def _scrub_grantee_text_items(items: List[Any]) -> List[Any]:
    """Grantee content policy for non-row artifacts: drop NSFW-flagged items and PII-redact
    every text-bearing field. Unconditional for grantees — a backstop independent of any
    per-field transforms, mirroring the row-level ingest disclosure guarantee."""
    from ..disclosure.content_policy import is_record_nsfw

    out: List[Any] = []
    for item in items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        if is_record_nsfw(item):
            continue
        scrubbed = dict(item)
        for key in _GRANTEE_TEXT_KEYS:
            val = scrubbed.get(key)
            if isinstance(val, str) and val:
                scrubbed[key] = _sanitize_nsfw(_redact_pii(val))
        out.append(scrubbed)
    return out


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

        # B.3 — grantee content filter for non-row artifacts. Runs regardless of whether rows
        # exist (in summary/inference mode rows are stripped, so this is the ONLY content
        # filter on the path summaries/scores/facts travel to a grantee).
        if disclosure_tier == "default_disclosure":
            for key in ("summaries", "scores", "semantic_hits", "facts"):
                seq = packet.get(key)
                if isinstance(seq, list) and seq:
                    packet[key] = _scrub_grantee_text_items(seq)
                    applied.append(f"grantee_scrub_{key}")

        return FilteredContext(context_packet=packet, filters_applied=applied)

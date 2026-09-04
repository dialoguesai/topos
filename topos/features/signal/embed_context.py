"""Contextual embed-text construction.

Personal-data chunks often lose meaning in isolation ("she said yes"). Prepending
a compact header (record kind, date, title/source) before embedding materially
improves retrieval; the stored search_text/text_preview stay raw so FTS and
display are unaffected.
"""

from __future__ import annotations

import re

from typing import Any, Dict, Optional

from .vector_settings import embed_context_headers_enabled

_TABLE_LABELS = {
    "ai_chat_messages": "ai chat",
    "ai_chat_message": "ai chat",
    "activity_events": "web activity",
    "activity_event": "web activity",
    "journal_entries": "journal",
    "journal_entry": "journal",
    "conversation_messages": "message",
    "conversation_message": "message",
    "profile_records": "profile",
    "profile_record": "profile",
    "calendar_events": "calendar",
    "calendar_event": "calendar",
    "contacts": "contact",
    "financial_transactions": "finance",
    "location_events": "place",
}


def _event_date(msg: Dict[str, Any]) -> str:
    for field in ("event_at", "ts", "entry_at", "occurred_at", "starts_at", "created_at"):
        value = msg.get(field)
        if value:
            return str(value)[:10]
    return ""


def context_header(msg: Dict[str, Any], *, record_type: Optional[str] = None) -> str:
    """Compact one-line context header, or empty string when disabled."""
    if not embed_context_headers_enabled():
        return ""
    kind = str(
        record_type
        or msg.get("record_type")
        or msg.get("_table")
        or msg.get("canonical_table")
        or ""
    )
    label = _TABLE_LABELS.get(kind, kind.replace("_", " ")).strip()
    parts = [p for p in (label, _event_date(msg)) if p]
    title = str(msg.get("title") or "").strip()
    org = str(msg.get("organization") or "").strip()
    place = str(msg.get("place_name") or "").strip()
    people = str(msg.get("people") or msg.get("attendees") or "").strip()
    for extra in (title, org, place, people):
        if extra and extra not in parts:
            parts.append(extra)
    if not parts:
        return ""
    return " | ".join(parts[:4])


# Canonical record kind -> signal dimension. Before 2026-07-06 every
# embedding defaulted to 'memory', which made all per-dimension machinery
# (faceted clustering, dimension-filtered search, brief scoping) a silent
# no-op. Keys cover record_type values, canonical table names, and the
# profile record kinds observed live.
_DIMENSION_BY_RECORD_KIND = {
    "conversation_message": "relationships",
    "conversation_messages": "relationships",
    "contact": "relationships",
    "contacts": "relationships",
    "journal_entry": "wellbeing",
    "journal_entries": "wellbeing",
    "activity_event": "interests",
    "activity_events": "interests",
    "browser_visit": "interests",
    "browser_visits": "interests",
    "calendar_event": "time",
    "calendar_events": "time",
    "financial_transaction": "resources",
    "financial_transactions": "resources",
    "location_event": "places",
    "location_events": "places",
    "profile_record": "work",
    "profile_records": "work",
    # profile_records.record_type values
    "experience": "work",
    "education": "work",
    "skill": "work",
    "certification": "work",
    "bio": "work",
    # AI chat is genuinely cross-cutting; it stays in the default dimension.
    "ai_chat_message": "memory",
    "ai_chat_messages": "memory",
    "transcript_segment": "memory",
    "transcript_segments": "memory",
    "transcripts": "memory",
}


# Record kind is usually enough, but not always: the same kind can arrive
# from a source that means something different by it. Keyed (kind, source_id),
# checked before the kind alone.
#
# The GitHub connector writes one journal row per authored COMMIT, so the
# whole commit stream was landing in wellbeing — 123 rows on one live node,
# and with them 19 of 163 topic clusters named "Wellbeing Tracker (…)" over
# terms like "merge branch", "gitignore", "build". The labeler was doing its
# best: asked to "name the state, rhythm or condition" for a cluster of
# commits, the only honest answer left is the dimension's own noun. A commit
# is work; what a person writes in a journal is wellbeing. Origin decides.
_DIMENSION_BY_RECORD_KIND_AND_SOURCE = {
    ("journal_entry", "github_activity"): "work",
    ("journal_entries", "github_activity"): "work",
}


def dimension_for_record(msg: Dict[str, Any], *, record_type: Optional[str] = None) -> str:
    """Signal dimension for a canonical record (default 'memory')."""
    explicit = str(msg.get("signal_dimension") or "").strip()
    if explicit:
        return explicit
    source = str(msg.get("source_id") or "").strip().lower()
    for kind in (
        record_type,
        msg.get("record_type"),
        msg.get("_table"),
        msg.get("canonical_table"),
    ):
        key = str(kind or "").strip().lower()
        if not key:
            continue
        if source and (key, source) in _DIMENSION_BY_RECORD_KIND_AND_SOURCE:
            return _DIMENSION_BY_RECORD_KIND_AND_SOURCE[(key, source)]
        if key in _DIMENSION_BY_RECORD_KIND:
            return _DIMENSION_BY_RECORD_KIND[key]
    return "memory"


# Serialization artifacts that leak into message content when a source parser
# fails to decode rich-text payloads (e.g. iMessage attributedBody ->
# NSKeyedArchiver "streamtyped" blobs). Embedding/NER over these produces junk
# clusters and junk entities; the real fix is the parser, this is the derived-
# layer defense.
_BINARY_ARTIFACT_MARKERS = (
    "streamtyped",
    "NSKeyedArchiver",
    "NSMutableAttributedString",
    "NSAttributedString",
    "NSDictionary",
    "__kIM",
    # bplist class-table fragments survive even when the archiver header is
    # truncated away ("Z$classnameX$classesWNSValue…") — 761 such embeddings
    # were live on 2026-07-06 and formed part of a junk cluster.
    "$classname",
    "$classes",
)


# A document that is nothing but redaction placeholders carries no information
# to retrieve on. It happens when a record's only embeddable field is a
# disclosed one — a location event whose whole document is its `place_name` —
# so redaction collapses the document to the token itself. Measured on the
# owner's node 2026-08-27: **134 such embeddings, 126 of them the literal
# `[ADDRESS]` sharing 23 distinct vectors between them**. They occupy ANN slots,
# match everything and nothing, and the junk reaper could not see them because
# it only knew about binary-serialization markers.
_PLACEHOLDER_ONLY = re.compile(r"^(?:\s*\[[A-Z_]{2,20}\]\s*[-–—,;:/]*\s*)+$")


def is_placeholder_only(text: str) -> bool:
    """True when a document consists solely of redaction placeholders."""
    value = str(text or "").strip()
    return bool(value) and bool(_PLACEHOLDER_ONLY.match(value))


def is_derivable_content(text: str) -> bool:
    """False for serialization garbage that must not enter derived layers."""
    value = str(text or "")
    if not value.strip():
        return False
    if is_placeholder_only(value):
        return False
    head = value[:200]
    return not any(marker in head for marker in _BINARY_ARTIFACT_MARKERS)


def embeddable_content(msg: Dict[str, Any]) -> str:
    """Primary text for embedding; falls back through descriptive fields.

    The fallback list must cover identifier-bearing record types that have no
    `content` column — contacts (display_name), contact_identifiers
    (identifier), location events (place_name/city) — or those records never
    enter the vector/FTS index at all (found by the query-quality eval: phone
    numbers and place names were structurally unindexable)."""
    content = str(msg.get("content") or "").strip()
    if content and is_derivable_content(content):
        return content
    if content:
        return ""  # junk content: never fall through to title/url of a message
    parts = [
        str(msg.get(field) or "").strip()
        for field in (
            "title",
            "organization",
            "description",
            "url",
            "place_name",
            "city",
            "display_name",
            "identifier",
        )
    ]
    return " — ".join(p for p in parts if p)


def build_embed_text(msg: Dict[str, Any], chunk: str, *, record_type: Optional[str] = None) -> str:
    """Header + chunk (the text actually sent to the embedding model)."""
    header = context_header(msg, record_type=record_type)
    if not header:
        return chunk
    return f"{header}\n{chunk}"

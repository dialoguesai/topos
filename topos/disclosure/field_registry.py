"""Canonical tables/fields that receive ingest-time PII disclosure columns."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# NOTE: the `documents` lane is intentionally absent from every map in this
# module. It is an owner-only leaf (store-and-view only) that is never disclosed
# or shared, so it does not participate in the disclosure/redaction pipeline.
CANONICAL_ID_COLUMN: Dict[str, str] = {
    "ai_chat_messages": "message_id",
    "conversation_messages": "message_id",
    "journal_entries": "entry_id",
    "location_events": "event_id",
}

# canonical_table -> raw fields redacted at ingest.
#
# A field here does NOT have to be a column on the table. The privacy layer
# redacts `msg[field]` in the in-flight record — which is what protects every
# downstream consumer (embeddings, FTS, extraction) — while the persisted
# disclosure write filters itself to columns that actually exist.
PII_DISCLOSURE_FIELDS: Dict[str, Tuple[str, ...]] = {
    "ai_chat_messages": ("content", "content_rendered"),
    "conversation_messages": ("content",),
    "journal_entries": ("content",),
    # `content` is listed alongside `place_name` deliberately, and dropping it
    # would be a privacy regression rather than a tidy-up. The signal record a
    # location fan-out child carries into enrichment sets BOTH: `place_name` is
    # the column, and `content` is the copy that gets embedded, FTS-indexed and
    # fed to extraction. Today the child is misfiled as a journal entry, so
    # `("content",)` is what redacts it — 138 embeddings on the owner's node read
    # `[ADDRESS]` only because of that accident. Correcting the table stamp
    # without this line turns them back into raw home and gym addresses.
    "location_events": ("place_name", "content"),
}

_GROUP_TO_TABLE = {
    "ai_messages": "ai_chat_messages",
    "conversations": "conversation_messages",
    "journal": "journal_entries",
}


# Grantee-facing placeholder when a record's ingest disclosure has not completed.
# Read paths must emit this instead of raw content (fail closed). Kept in sync with
# the SQL fallback literal in storage/adapters/sqlite/stores.py disclosure specs.
DISCLOSURE_PENDING_PLACEHOLDER = "[disclosure pending]"

# Idempotence marker: set on a row once the grantee content policy has resolved it, so a
# second application is a no-op instead of re-treating already-redacted content as "pending"
# and overwriting it with the placeholder. Survives strip so re-application can see it.
DISCLOSURE_APPLIED_MARKER = "_disclosure_applied"


def disclosure_column(field: str) -> str:
    return f"{field}_disclosure"


def disclosure_hash_column(field: str) -> str:
    return f"{field}_disclosure_hash"


def canonical_table_for_group(group: Optional[str]) -> Optional[str]:
    if not group:
        return None
    return _GROUP_TO_TABLE.get(str(group).strip())


def canonical_table_for_message(msg: Dict[str, Any], *, source_group: Optional[str] = None) -> Optional[str]:
    explicit = msg.get("_table") or msg.get("canonical_table")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if source_group:
        return canonical_table_for_group(source_group)
    return None


def fields_for_table(table: str) -> Tuple[str, ...]:
    return PII_DISCLOSURE_FIELDS.get(table, ())


def stamp_canonical_table(records, *, source_group: Optional[str]) -> None:
    """Fill in ``_table`` for a batch, honouring each record's own declaration.

    The pipeline used to do ``rec.setdefault("_table", <group default>)``, which
    only consults ``_table`` — so a record that correctly declared
    ``canonical_table`` and left ``_table`` unset was overwritten with the
    BATCH'S table. Fan-out children are exactly that shape: the location child
    stamps ``canonical_table='location_events'`` and gets ``_table`` filled in
    with ``journal_entries``, because that is the group its parent belongs to.

    Five readers resolve the table as ``_table or canonical_table``, so the group
    default won. On the owner's node 2026-08-27 that single omission put all 362
    place rows in the wrong table for: the ingest-time PII disclosure write
    (addressed to journal_entries by a location id, matching zero rows while
    reporting success), the grant bound on entity mentions (a journal-only grant
    admitted location evidence), the embedding dimension (360 place records filed
    as `wellbeing`, leaving the shipped `places` dimension empty), the
    belief-role gate that should have declined to extract goals from a bare place
    name, and the journal category histogram (28% over-reported).

    ``canonical_table_for_message`` already encodes the correct precedence —
    ``_table``, then the record's own ``canonical_table``, then the group. This
    just applies it where the pipeline was guessing.
    """
    for rec in records:
        if not isinstance(rec, dict):
            continue
        table = canonical_table_for_message(rec, source_group=source_group)
        if table:
            rec["_table"] = table

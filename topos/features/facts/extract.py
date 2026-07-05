"""Rules-based atomic fact extraction from canonical rows.

Deliberately conservative: only patterns with unambiguous provenance become
facts. An LLM extraction pass can layer on later (brief_fallback pattern);
the store's belief revision makes that safe to add.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional

from .store import FactStore


def _owner_entity_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT entity_id FROM entities WHERE is_self=1 LIMIT 1"
    ).fetchone()
    if row:
        return str(row[0])
    from ..entities.resolver import EntityResolver

    resolver = EntityResolver(conn)
    entity_id = resolver._create_entity("Owner", "person", is_self=True)
    conn.commit()
    return entity_id


def _source_ref(table: str, record_id: Any, source_id: Any = None) -> Dict[str, str]:
    ref = {"table": table, "record_id": str(record_id or "")}
    # source_id makes scrub attribution exact (no timeline lookup needed).
    if source_id:
        ref["source_id"] = str(source_id)
    return ref


_CURRENT_HINTS = re.compile(r"\b(present|current|currently|now|ongoing)\b", re.I)
_YEAR_RANGE = re.compile(r"(\d{4})\s*[–—-]\s*(\d{4}|present)", re.I)
_SINCE_YEAR = re.compile(r"\bsince\s+(\d{4})\b", re.I)
_OPEN_RANGE = re.compile(r"(\d{4})\s*[–—-]\s*(?=$|[^\d\w]|present)", re.I)
_TRAINING = re.compile(r"\b(half marathon|marathon|triathlon|10k|5k)\b", re.I)

# Resume entries frequently carry no dates at all; currency is signaled by the
# leading verb's tense ("Lead ingestion…" vs "Built privacy-first…").
_PAST_VERBS = frozenset(
    "built led created designed developed managed launched shipped drove ran "
    "wrote delivered implemented founded architected established directed "
    "oversaw maintained supported served coordinated".split()
)
_PRESENT_VERBS = frozenset(
    "lead leads leading build builds building advise advises advising manage "
    "manages managing design designs designing develop develops developing "
    "run runs running direct directs directing coordinate coordinates "
    "coordinating oversee oversees overseeing maintain maintains maintaining".split()
)


def _experience_currency(description: str) -> tuple:
    """Returns (is_current, period_start, period_end, confidence)."""
    match = _YEAR_RANGE.search(description)
    if match:
        end = match.group(2).lower()
        return (end == "present", match.group(1), None if end == "present" else end, 0.9)
    since = _SINCE_YEAR.search(description)
    if since:
        return (True, since.group(1), None, 0.9)
    open_range = _OPEN_RANGE.search(description)
    if open_range:
        return (True, open_range.group(1), None, 0.85)
    if _CURRENT_HINTS.search(description):
        return (True, None, None, 0.85)
    words = re.findall(r"[a-z]+", description.lower())
    leading = words[0] if words else ""
    if leading in _PRESENT_VERBS:
        return (True, None, None, 0.75)
    if leading in _PAST_VERBS or leading.endswith("ed"):
        return (False, None, None, 0.75)
    # No signal at all: resumes list current roles undated more often than
    # past ones — assume current at low confidence (belief revision corrects).
    return (True, None, None, 0.6)


def extract_profile_facts(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    record_type = str(row.get("record_type") or "").lower()
    out: List[Dict[str, Any]] = []
    if record_type == "experience":
        org = str(row.get("organization") or "").strip()
        title = str(row.get("title") or "").strip()
        description = str(row.get("description") or "")
        if not org:
            return out
        is_current, period_start, period_end, confidence = _experience_currency(description)
        valid_from = f"{period_start}-01-01T00:00:00+00:00" if period_start else None
        # Advisory/board roles are held *concurrently* with employment; they
        # get a multi-valued predicate so they never supersede the day job.
        is_advisory = bool(re.search(r"\b(advisor|adviser|advisory|board member)\b", title, re.I))
        # NB: valid_from/valid_to on the fact row track *belief* validity
        # (superseded-by), not the state's own period. "worked_at Lumon
        # 2021-2024" is a currently-true claim about a past period, so the
        # period lives in the payload and the row stays active.
        fact: Dict[str, Any] = {
            "predicate": "advises" if is_advisory else ("works_at" if is_current else "worked_at"),
            "object_value": org,
            "confidence": confidence,
            "dimension": "profile",
            "valid_from": valid_from,
        }
        if period_start:
            fact["period_start"] = period_start
        if period_end:
            fact["period_end"] = period_end
        out.append(fact)
        # role_is is the *primary* current role (single-valued); advisory
        # titles ride on the multi-valued advises fact instead of competing.
        if is_current and title and not is_advisory:
            out.append(
                {
                    "predicate": "role_is",
                    "object_value": title,
                    "confidence": confidence,
                    "dimension": "profile",
                    "valid_from": valid_from,
                }
            )
    elif record_type == "certification":
        title = str(row.get("title") or "").strip()
        if title:
            out.append(
                {
                    "predicate": "certified_in",
                    "object_value": title,
                    "confidence": 0.9,
                    "dimension": "profile",
                }
            )
    return out


def extract_journal_facts(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    content = str(row.get("content") or "")
    out: List[Dict[str, Any]] = []
    match = _TRAINING.search(content)
    if match and str(row.get("category") or "").lower() in ("exercise", "health", ""):
        out.append(
            {
                "predicate": "training_for",
                "object_value": match.group(1).lower(),
                "confidence": 0.7,
                "dimension": "wellbeing",
                # Journal-derived habits are more intimate than resume facts.
                "disclosure": "owner_only",
            }
        )
    return out


_EXTRACTORS = {
    "profile_records": extract_profile_facts,
    "journal_entries": extract_journal_facts,
}


def extract_facts_from_batch(
    conn: sqlite3.Connection,
    rows: List[Dict[str, Any]],
) -> int:
    """Extract and assert facts about the owner from a canonical batch."""
    from ..lifecycle.exclusions import excluded_record_ids

    store = FactStore(conn)
    owner = _owner_entity_id(conn)
    excluded_records = excluded_record_ids(conn)
    written = 0
    for row in rows:
        if str(row.get("record_id") or row.get("message_id") or row.get("id") or "") in excluded_records:
            continue
        table = str(row.get("_table") or row.get("canonical_table") or "")
        if not table:
            if row.get("record_type") is not None and row.get("organization") is not None:
                table = "profile_records"
            elif row.get("entry_at") is not None or row.get("mood_tag") is not None:
                table = "journal_entries"
        extractor = _EXTRACTORS.get(table)
        if extractor is None:
            continue
        record_id = row.get("record_id") or row.get("message_id") or row.get("id")
        for spec in extractor(row):
            asserted = store.assert_fact(
                subject_entity_id=owner,
                predicate=spec["predicate"],
                object_value=spec["object_value"],
                dimension=spec.get("dimension", "profile"),
                confidence=float(spec.get("confidence") or 0.7),
                source_refs=[_source_ref(table, record_id, row.get("source_id"))],
                valid_from=spec.get("valid_from"),
                disclosure=spec.get("disclosure", "scoped"),
                period_start=spec.get("period_start"),
                period_end=spec.get("period_end"),
            )
            if asserted is not None:  # None = owner-excluded, never re-asserted
                written += 1
    return written

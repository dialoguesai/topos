"""Rules-based atomic fact extraction from canonical rows.

Deliberately conservative: only patterns with unambiguous provenance become
facts. An LLM extraction pass can layer on later (brief_fallback pattern);
the store's belief revision makes that safe to add.
"""

from __future__ import annotations

import inspect
import re
import sqlite3
from typing import Any, Dict, List, Optional

from ..provenance.roles import owner_authored
from .store import FactStore

# Contract 4 (PLAN_PROVENANCE_SPLIT P4.4): assert_fact grows asserted_by in the
# same build wave; feature-detect so this module works on either side of that
# landing. Rule extraction is owner-gated, so everything here asserts as owner.
_ASSERT_FACT_SUPPORTS_ASSERTED_BY = (
    "asserted_by" in inspect.signature(FactStore.assert_fact).parameters
)
_OWNER_ASSERT_KWARGS: Dict[str, Any] = (
    {"asserted_by": "owner"} if _ASSERT_FACT_SUPPORTS_ASSERTED_BY else {}
)


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
    elif record_type == "education":
        org = str(row.get("organization") or "").strip()
        if org:
            out.append(
                {
                    "predicate": "studied_at",
                    "object_value": org,
                    "confidence": 0.9,
                    "dimension": "profile",
                }
            )
    elif record_type == "skill":
        title = str(row.get("title") or "").strip()
        if title:
            out.append(
                {
                    "predicate": "skilled_in",
                    "object_value": title,
                    "confidence": 0.85,
                    "dimension": "profile",
                }
            )
    return out


# Session categories that describe a recurring physical/mental practice.
# Belief revision turns repeated low-confidence assertions into one fact with
# accumulated source_refs, so per-session asserts at modest confidence are
# exactly the right shape.
_PRACTICE_CATEGORIES = frozenset(
    {
        "run",
        "running",
        "walk",
        "walking",
        "bicycling",
        "cycling",
        "biking",
        "weight-lifting",
        "weightlifting",
        "lifting",
        "yoga",
        "meditation",
        "meditate",
        "climbing",
        "swim",
        "swimming",
        "hike",
        "hiking",
        "rowing",
        "pilates",
        "stretching",
    }
)


def _entity_matched_project(conn: Optional[sqlite3.Connection], category: str) -> Optional[str]:
    """Category resolved against the entity spine (org/topic) → project name.

    'Topos' or 'Dialogues' as a journal session category is a work-project
    signal, but only when the name is already a known entity — that keeps the
    rule unambiguous instead of guessing which categories are projects.
    """
    if conn is None or not category:
        return None
    try:
        row = conn.execute(
            """
            SELECT canonical_name FROM entities
            WHERE normalized_name = ? AND entity_type IN ('org', 'topic')
            LIMIT 1
            """,
            (category.strip().lower(),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row else None


def extract_journal_facts(
    row: Dict[str, Any],
    conn: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    content = str(row.get("content") or "")
    category = str(row.get("category") or "").strip()
    out: List[Dict[str, Any]] = []
    match = _TRAINING.search(content)
    if match and category.lower() in ("exercise", "health", ""):
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
    if category.lower() in _PRACTICE_CATEGORIES:
        out.append(
            {
                "predicate": "practices",
                "object_value": category.lower(),
                "confidence": 0.55,
                "dimension": "wellbeing",
                "disclosure": "owner_only",
            }
        )
    else:
        project = _entity_matched_project(conn, category)
        if project:
            out.append(
                {
                    "predicate": "works_on",
                    "object_value": project,
                    "confidence": 0.6,
                    "dimension": "work",
                }
            )
    return out


# First-person declarations in owner-authored messages. Deliberately few and
# strict (leading "I", capitalized object head where a proper noun is
# expected): precision over recall — the store's conflict queue catches the
# rest. Confidence sits below resume-derived facts so a chat remark never
# silently supersedes a strong incumbent (CONFLICT_CONFIDENCE_MARGIN).
_MSG_FACT_PATTERNS = (
    (
        "lives_in",
        re.compile(r"\bI (?:currently |now )?live in ([A-Z][A-Za-z .'\-]{2,30})"),
        0.6,
        "owner_only",
        "places",
        False,
    ),
    (
        "lives_in",
        re.compile(r"\bI (?:just |recently )?moved to ([A-Z][A-Za-z .'\-]{2,30})"),
        0.6,
        "owner_only",
        "places",
        True,  # a move dates the claim: valid_from = message event_at
    ),
    (
        "works_at",
        re.compile(r"\bI (?:now )?work (?:at|for) ([A-Z][\w .&'\-]{1,40})"),
        0.6,
        "scoped",
        "work",
        False,
    ),
    (
        "works_at",
        re.compile(r"\bI(?:'m| am) (?:now )?working (?:at|for) ([A-Z][\w .&'\-]{1,40})"),
        0.6,
        "scoped",
        "work",
        False,
    ),
    (
        "training_for",
        re.compile(r"\bI(?:'m| am) training for (?:a |an |the )?([A-Za-z0-9][A-Za-z0-9 \-]{2,30})"),
        0.55,
        "owner_only",
        "wellbeing",
        False,
    ),
)

# Object captures run to a length cap; cut at clause boundaries first.
_OBJECT_STOPS = re.compile(
    r"\s+(?:and|but|so|because|though|although|while|which|that|since|until|"
    r"right now|these days|at the moment)\b.*$",
    re.I,
)


_OBJECT_TRAILING_TIME_WORDS = re.compile(
    r"(?:\s+(?:now|today|tomorrow|yesterday|currently|lately|soon|again))+$", re.I
)


def _clean_object(raw: str) -> Optional[str]:
    value = re.split(r"[.,!?;:\n\"()]", str(raw or ""), maxsplit=1)[0]
    value = _OBJECT_STOPS.sub("", value)
    value = _OBJECT_TRAILING_TIME_WORDS.sub("", value).strip(" '’-")
    if not (2 <= len(value) <= 40):
        return None
    if "http" in value.lower() or "@" in value:
        return None
    return value


def _is_owner_authored(row: Dict[str, Any], table: str = "") -> bool:
    """Did the owner type this row? Thin shim kept for existing callers/tests;
    the table-aware semantics (ai_chat sender_type vs conversation
    is_from_self/sender_id, ambiguous-'human' fails closed) now live in
    features.provenance.roles.record_role — THE single source of truth for
    role (PLAN_PROVENANCE_SPLIT P1.2).
    """
    return owner_authored(row, table=table)


def extract_message_facts(
    row: Dict[str, Any],
    conn: Optional[sqlite3.Connection] = None,
    *,
    table: str = "",
) -> List[Dict[str, Any]]:
    if not _is_owner_authored(row, table):
        return []
    content = str(row.get("content") or "")
    from ..signal.embed_context import is_derivable_content

    if not content.strip() or not is_derivable_content(content):
        return []
    out: List[Dict[str, Any]] = []
    for predicate, pattern, confidence, disclosure, dimension, dated in _MSG_FACT_PATTERNS:
        for match in pattern.finditer(content):
            value = _clean_object(match.group(1))
            if not value:
                continue
            fact: Dict[str, Any] = {
                "predicate": predicate,
                "object_value": value.lower() if predicate == "training_for" else value,
                "confidence": confidence,
                "dimension": dimension,
                "disclosure": disclosure,
            }
            if dated and row.get("event_at"):
                fact["valid_from"] = str(row.get("event_at"))
            out.append(fact)
    return out


_LOCATION_MIN_DISTINCT_DAYS = 3


def derive_location_facts(conn: sqlite3.Connection) -> int:
    """Aggregate rule: the city with the most distinct visit days → lives_in.

    Location events are visits, not residence; only sustained presence
    (>= _LOCATION_MIN_DISTINCT_DAYS distinct days, strictly more than any
    other city) is unambiguous enough to assert — at low confidence, so a
    stronger source (message/profile) supersedes cleanly.
    """
    try:
        rows = conn.execute(
            """
            SELECT city, COUNT(DISTINCT DATE(event_at)) AS days
            FROM location_events
            WHERE city IS NOT NULL AND TRIM(city) != ''
            GROUP BY city ORDER BY days DESC LIMIT 2
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    if not rows:
        return 0
    top_city, top_days = str(rows[0][0]), int(rows[0][1])
    runner_up_days = int(rows[1][1]) if len(rows) > 1 else 0
    if top_days < _LOCATION_MIN_DISTINCT_DAYS or top_days == runner_up_days:
        return 0
    store = FactStore(conn)
    asserted = store.assert_fact(
        subject_entity_id=_owner_entity_id(conn),
        predicate="lives_in",
        object_value=top_city,
        dimension="places",
        confidence=0.55,
        source_refs=[_source_ref("location_events", f"aggregate:{top_city}")],
        disclosure="owner_only",
        **_OWNER_ASSERT_KWARGS,
    )
    return 1 if asserted else 0


# Message extractors are bound per-table so the owner gate knows which
# sender-identity convention applies (see _is_owner_authored).
_EXTRACTORS = {
    "profile_records": lambda row, conn=None: extract_profile_facts(row),
    "journal_entries": extract_journal_facts,
    "conversation_messages": lambda row, conn=None: extract_message_facts(
        row, conn, table="conversation_messages"
    ),
    "ai_chat_messages": lambda row, conn=None: extract_message_facts(
        row, conn, table="ai_chat_messages"
    ),
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
            elif str(row.get("sender_type") or "") in ("user", "assistant"):
                table = "ai_chat_messages"
            elif row.get("is_from_self") is not None or row.get("sender_id") is not None:
                table = "conversation_messages"
        extractor = _EXTRACTORS.get(table)
        if extractor is None:
            continue
        record_id = row.get("record_id") or row.get("message_id") or row.get("id")
        for spec in extractor(row, conn):
            # All rule extractors are owner-gated (message extractors via the
            # authored role; journal/profile by construction) — assert as owner.
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
                **_OWNER_ASSERT_KWARGS,
            )
            if asserted is not None:  # None = owner-excluded, never re-asserted
                written += 1

    # B4 (PLAN_PROVENANCE_SPLIT P4.3): the rules pass above is the FLOOR. When
    # the role-gated LLM pass is enabled, run it ADDITIVELY over the same batch
    # — assert_fact de-dupes on object_key, so rules+LLM never double-count. The
    # LLM pass is fully self-contained (its own role gate, its own try/except)
    # and NEVER raises into ingestion; a belt-and-braces guard here keeps the
    # rules floor's return value intact even if the module import itself fails.
    try:
        from .llm_extract import extract_owner_facts_llm, facts_llm_enabled

        if facts_llm_enabled():
            written += extract_owner_facts_llm(conn, rows)
    except Exception as exc:  # noqa: BLE001 — ingestion must never crash on the LLM pass
        import logging

        logging.getLogger("topos.features.facts.extract").warning(
            "LLM fact pass skipped (%s); rules-only facts retained", exc
        )
    return written

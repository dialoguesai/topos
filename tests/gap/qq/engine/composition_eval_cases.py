"""Composition eval: graded cases that test whether queries can extract specific
information from each layer of the database, scored against ground truth.

Two lanes share this framework:
  - LIVE lane (C-series, this module): oracles computed by SQL from the live DB
    at run time — the expected answer is whatever the data actually says, so
    correctness is gradeable even as the node grows.
  - SEEDED lane (S-series, composition_seed_corpus.py): deterministic canary
    needles planted per storage layer in a scratch DB — reproducible across
    machines and releases.

Grading is 0-1 with partial credit per case, not pass/fail:
  correctness  — fraction of oracle needle-groups present in the response
  specificity  — precision@5: fraction of top items on-topic
  attribution  — fraction of expected retrieval sources that appeared
  groundedness — negative controls only: absence of confident noise
  composite    — weighted mean (negative cases: groundedness alone)

Poor numbers are the point: this is the accountability baseline, not a gate.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from query_eval_cases import (
    _DEFAULT_LATENCY_MS,
    _blob,
    _public_result,
    _summary_items,
)

COMPOSITION_WEIGHTS = {"correctness": 0.45, "specificity": 0.25, "attribution": 0.30}


@dataclass
class Oracle:
    """Ground truth for one case. Each needle group is a list of alternatives;
    the group counts as matched if ANY alternative appears in the response."""

    needle_groups: List[List[str]]
    note: str = ""
    ok: bool = True  # False → the DB lacks the data to grade this case (vacuous)


QueryText = Union[str, Callable[[sqlite3.Connection], str]]


@dataclass(frozen=True)
class CompositionCase:
    id: str
    lane: str  # "live" | "seeded"
    query: QueryText
    scope_id: str
    access_mode: str
    oracle: Callable[[sqlite3.Connection], Oracle]
    expected_sources: Tuple[str, ...] = ()
    topic_terms: Tuple[str, ...] = ()
    negative: bool = False
    temporal_days: Optional[int] = None
    layer: str = ""  # storage layer this case probes (for the coverage matrix)
    description: str = ""
    max_latency_ms: int = 0

    def __post_init__(self) -> None:
        if self.max_latency_ms <= 0:
            object.__setattr__(
                self, "max_latency_ms", _DEFAULT_LATENCY_MS.get(self.access_mode, 10000)
            )

    def query_text(self, conn: sqlite3.Connection) -> str:
        return self.query(conn) if callable(self.query) else self.query


# --- Scoring ------------------------------------------------------------------------

_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})")


def _response_items(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = _summary_items(response)
    if items:
        return items
    pr = _public_result(response)
    rows = pr.get("rows") or pr.get("raw_rows") or pr.get("messages") or []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _observed_sources(items: List[Dict[str, Any]]) -> List[str]:
    return sorted({str(i.get("retrieval_source") or "") for i in items if i.get("retrieval_source")})


def _source_matches(expected: str, observed: List[str]) -> bool:
    return any(o == expected or o.startswith(expected) for o in observed)


def _item_dates(item: Dict[str, Any]) -> List[datetime]:
    dates = []
    for m in _DATE_RE.finditer(json.dumps(item, default=str)):
        try:
            dates.append(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc))
        except ValueError:
            continue
    return dates


def score_composition(
    case: CompositionCase, response: Dict[str, Any], oracle: Oracle
) -> Dict[str, Any]:
    denied = response.get("turn_outcome") == "denied" or bool(response.get("deny_reason"))
    items = _response_items(response)
    blob_all = _blob(_public_result(response))
    observed = _observed_sources(items)
    reasons: List[str] = []

    scores: Dict[str, Optional[float]] = {
        "correctness": None, "specificity": None, "attribution": None, "groundedness": None,
    }

    if case.negative:
        # A query about something absent must not return confident noise.
        if denied or not items:
            scores["groundedness"] = 1.0
            reasons.append("no fabricated results (empty/denied)")
        else:
            scores["groundedness"] = round(max(0.0, 1.0 - len(items) / 10.0), 3)
            reasons.append(f"{len(items)} items returned for a topic that does not exist")
        composite = scores["groundedness"]
        return _pack(case, scores, composite, reasons, items, observed, oracle, denied)

    if denied:
        reasons.append(f"denied: {response.get('deny_reason')}")
        return _pack(case, dict.fromkeys(scores, 0.0), 0.0, reasons, items, observed, oracle, denied)

    # correctness: oracle needle-group recall (temporal cases grade window fit instead)
    if case.temporal_days is not None:
        window_start = datetime.now(timezone.utc) - timedelta(days=case.temporal_days * 2)
        dated = [i for i in items if _item_dates(i)]
        if not dated:
            scores["correctness"] = 0.0
            reasons.append("no dated items — cannot verify the time window was honored")
        else:
            in_window = sum(1 for i in dated if max(_item_dates(i)) >= window_start)
            scores["correctness"] = round(in_window / len(dated), 3)
            reasons.append(f"{in_window}/{len(dated)} dated items within ~{case.temporal_days}d window")
    elif oracle.needle_groups:
        matched = sum(
            1 for group in oracle.needle_groups if any(alt.lower() in blob_all for alt in group)
        )
        scores["correctness"] = round(matched / len(oracle.needle_groups), 3)
        missed = [g[0] for g in oracle.needle_groups if not any(a.lower() in blob_all for a in g)]
        reasons.append(
            f"needles {matched}/{len(oracle.needle_groups)}"
            + (f" (missed: {', '.join(missed[:3])})" if missed else "")
        )
    else:
        scores["correctness"] = 0.0
        reasons.append("oracle produced no needles")

    # specificity: precision@5 against topic terms
    if case.topic_terms:
        top = items[:5]
        if top:
            on_topic = sum(1 for i in top if any(t in _blob(i) for t in case.topic_terms))
            scores["specificity"] = round(on_topic / len(top), 3)
            reasons.append(f"top-{len(top)} on-topic {on_topic}/{len(top)}")
        else:
            scores["specificity"] = 0.0
            reasons.append("no items")

    # attribution: did the expected layers contribute?
    if case.expected_sources:
        hit = sum(1 for e in case.expected_sources if _source_matches(e, observed))
        scores["attribution"] = round(hit / len(case.expected_sources), 3)
        missing = [e for e in case.expected_sources if not _source_matches(e, observed)]
        if missing:
            reasons.append(f"missing sources: {', '.join(missing)}")

    weighted = [(COMPOSITION_WEIGHTS[k], v) for k, v in scores.items()
                if v is not None and k in COMPOSITION_WEIGHTS]
    total_w = sum(w for w, _ in weighted) or 1.0
    composite = round(sum(w * v for w, v in weighted) / total_w, 3)
    return _pack(case, scores, composite, reasons, items, observed, oracle, denied)


def _pack(case, scores, composite, reasons, items, observed, oracle, denied) -> Dict[str, Any]:
    return {
        "case_id": case.id,
        "lane": case.lane,
        "layer": case.layer,
        "scope_id": case.scope_id,
        "access_mode": case.access_mode,
        "composite": composite,
        "scores": scores,
        "vacuous": not oracle.ok,
        "denied": denied,
        "n_items": len(items),
        "sources": observed,
        "oracle_note": oracle.note,
        "reason": "; ".join(reasons)[:300],
    }


# --- Live-lane oracles (SQL against the node's own database) -------------------------

def _one(conn: sqlite3.Connection, sql: str, *params) -> Optional[tuple]:
    try:
        return conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None


def _all(conn: sqlite3.Connection, sql: str, *params) -> List[tuple]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _contact_names_for_identifier(conn: sqlite3.Connection, identifier: str) -> List[str]:
    rows = _all(
        conn,
        """SELECT DISTINCT c.display_name FROM contact_identifiers ci
           JOIN contacts c ON c.contact_id = ci.contact_id
           WHERE ci.identifier = ? AND c.display_name IS NOT NULL""",
        identifier,
    )
    return [r[0] for r in rows if r and r[0]]


def oracle_c1_top_contact_volume(conn: sqlite3.Connection) -> Oracle:
    row = _one(
        conn,
        """SELECT payload_json FROM signal_facts
           WHERE fact_id LIKE 'stat:messages.volume.by_contact:%'
           ORDER BY json_extract(payload_json, '$.stat_summary.n') DESC LIMIT 1""",
    )
    if not row:
        return Oracle([], "no messages.volume stat insights in DB", ok=False)
    payload = json.loads(row[0])
    ident = str(payload.get("group_key") or "")
    n = payload.get("stat_summary", {}).get("n")
    who = [ident, *(_contact_names_for_identifier(conn, ident))]
    groups = [ [a for a in who if a] ]
    if n:
        groups.append([str(n)])
    return Oracle(groups, f"top contact by volume: {who[-1] if who else '?'} ({n} msgs)")


def oracle_c2_recent_messages(conn: sqlite3.Connection) -> Oracle:
    rows = _all(
        conn,
        """SELECT content FROM conversation_messages
           WHERE content IS NOT NULL AND length(content) > 40
           ORDER BY event_at DESC LIMIT 3""",
    )
    groups = []
    for (content,) in rows:
        words = re.sub(r"\s+", " ", str(content)).strip()
        snippet = " ".join(words.split(" ")[:5])
        if len(snippet) > 15:
            groups.append([snippet])
    if not groups:
        return Oracle([], "no recent conversation_messages with content", ok=False)
    return Oracle(groups, f"{len(groups)} newest message snippets")


def oracle_c3_calendar(conn: sqlite3.Connection) -> Oracle:
    rows = _all(
        conn,
        """SELECT title, COUNT(*) FROM calendar_events WHERE title IS NOT NULL
           GROUP BY title ORDER BY COUNT(*) DESC LIMIT 2""",
    )
    if not rows:
        return Oracle([], "no calendar_events", ok=False)
    groups = [[str(t)] for t, _ in rows]
    return Oracle(groups, f"top event titles: {[t for t, _ in rows]}")


def oracle_c4_journal(conn: sqlite3.Connection) -> Oracle:
    rows = _all(
        conn,
        """SELECT category, COUNT(*) FROM journal_entries WHERE category IS NOT NULL
           GROUP BY category ORDER BY COUNT(*) DESC LIMIT 2""",
    )
    if not rows:
        return Oracle([], "no journal_entries", ok=False)
    return Oracle([[str(c)] for c, _ in rows], f"top journal categories: {[c for c, _ in rows]}")


def oracle_c5_places(conn: sqlite3.Connection) -> Oracle:
    rows = _all(
        conn,
        """SELECT place_name, COUNT(*) FROM location_events WHERE place_name IS NOT NULL
           GROUP BY place_name ORDER BY COUNT(*) DESC LIMIT 2""",
    )
    if not rows:
        return Oracle([], "no location_events", ok=False)
    return Oracle([[str(p)] for p, _ in rows], f"top places: {[p for p, _ in rows]}")


def oracle_c6_browsing(conn: sqlite3.Connection) -> Oracle:
    rows = _all(
        conn,
        """SELECT title, COUNT(*) FROM activity_events
           WHERE title IS NOT NULL AND length(title) > 8
           GROUP BY title ORDER BY COUNT(*) DESC LIMIT 3""",
    )
    if not rows:
        return Oracle([], "no activity_events", ok=False)
    return Oracle([[str(t)[:40]] for t, _ in rows], f"top activity titles: {[str(t)[:30] for t, _ in rows]}")


def _top_contact_with_identifier(conn: sqlite3.Connection) -> Optional[Tuple[str, str]]:
    rows = _all(
        conn,
        """SELECT c.display_name, ci.identifier FROM contacts c
           JOIN contact_identifiers ci ON ci.contact_id = c.contact_id
           WHERE c.is_self = 0 AND c.display_name IS NOT NULL AND length(c.display_name) > 2
           ORDER BY c.contact_id LIMIT 1""",
    )
    return (str(rows[0][0]), str(rows[0][1])) if rows else None


def query_c7_contact(conn: sqlite3.Connection) -> str:
    pair = _top_contact_with_identifier(conn)
    name = pair[0] if pair else "my first contact"
    return f"Find the contact record for {name}"


def oracle_c7_contact(conn: sqlite3.Connection) -> Oracle:
    pair = _top_contact_with_identifier(conn)
    if not pair:
        return Oracle([], "no contacts with identifiers", ok=False)
    name, ident = pair
    # Needle is the identifier, not the name (the name is in the query itself).
    return Oracle([[ident]], f"contact {name} → identifier must surface")


def oracle_c8_cluster_specificity(conn: sqlite3.Connection) -> Oracle:
    return Oracle([["sketch", "illustr", "pencil", "draw"]], "niche art topic (specificity-focused)")


def oracle_c9_temporal(conn: sqlite3.Connection) -> Oracle:
    return Oracle([], "graded on dated items within the window")


def oracle_c10_negative(conn: sqlite3.Connection) -> Oracle:
    return Oracle([], "fabricated topic — nothing in the DB should match")


def oracle_c11_active_hours(conn: sqlite3.Connection) -> Oracle:
    rows = _all(
        conn,
        """SELECT payload_json FROM signal_facts
           WHERE fact_id LIKE 'stat:activity.hour_of_week%' OR fact_id LIKE 'stat:ai_chat.hour_of_week%'
           LIMIT 2""",
    )
    if not rows:
        return Oracle([], "no hour_of_week stat insights", ok=False)
    groups = []
    for (payload,) in rows:
        tag = str(json.loads(payload).get("tag") or "")
        if len(tag) > 10:
            groups.append([tag[:50]])
    if not groups:
        return Oracle([], "hour_of_week stats have no tags", ok=False)
    return Oracle(groups, f"{len(groups)} hour-of-week stat tags")


def oracle_c12_fusion(conn: sqlite3.Connection) -> Oracle:
    return Oracle([["luc"]], "entity + volume fusion for Luc")


LIVE_COMPOSITION_CASES: List[CompositionCase] = [
    CompositionCase(
        "C1", "live", "How many messages have I exchanged with my most frequent contact?",
        "messages:read", "summary", oracle_c1_top_contact_volume,
        expected_sources=("stat_insight",), topic_terms=("message", "contact", "talk"),
        layer="stats:messages.volume",
        description="Stats layer answers a volume aggregate with the actual top contact"),
    CompositionCase(
        "C2", "live", "Show me my most recent messages", "messages:read", "raw",
        oracle_c2_recent_messages, layer="canonical:conversation_messages",
        description="Raw mode surfaces the actual newest rows of the 22k-row message store"),
    CompositionCase(
        "C3", "live", "What is on my calendar, what meetings do I have?", "schedule:read", "summary",
        oracle_c3_calendar, expected_sources=("canonical:calendar_events",),
        topic_terms=("meeting", "calendar", "standup", "session", "event"),
        layer="canonical:calendar_events",
        description="Schedule scope surfaces actual event titles"),
    CompositionCase(
        "C4", "live", "How often do I journal, and what about?", "health:read", "summary",
        oracle_c4_journal, expected_sources=("stat_insight", "canonical:journal_entries"),
        topic_terms=("journal", "topos", "chill", "weight", "wellbeing"),
        layer="canonical:journal_entries+stats",
        description="Journal categories + journaling stats reach the response"),
    CompositionCase(
        "C5", "live", "Where do I spend my time? Which places do I go?", "places:read", "summary",
        oracle_c5_places, expected_sources=("canonical:location_events",),
        topic_terms=("place", "location", "apt", "fitness", "trail"),
        layer="canonical:location_events",
        description="Places scope surfaces actual top locations"),
    CompositionCase(
        "C6", "live", "What have I been browsing and reading online?", "activity:read", "summary",
        oracle_c6_browsing, expected_sources=("canonical:activity_events",),
        topic_terms=("brows", "read", "web", "site", "watch"),
        layer="canonical:activity_events",
        description="Activity scope surfaces actual browsing titles"),
    CompositionCase(
        "C7", "live", query_c7_contact, "contacts:resolve", "raw", oracle_c7_contact,
        layer="canonical:contacts",
        description="Contact resolution returns the identifier, not just the queried name"),
    CompositionCase(
        "C8", "live", "pencil sketch illustration techniques", "ai_conversations:read", "summary",
        oracle_c8_cluster_specificity,
        topic_terms=("sketch", "illustr", "pencil", "draw", "art"),
        layer="clusters+vector",
        description="Niche query must rank niche items — giant generic clusters fail precision@5"),
    CompositionCase(
        "C9", "live", "What was I doing last week?", "messages:read", "summary",
        oracle_c9_temporal, temporal_days=7, layer="planner:time_window",
        description="Relative time window actually constrains results"),
    CompositionCase(
        "C10", "live", "What do I know about the Zorblatt-9 submarine project?",
        "messages:read", "summary", oracle_c10_negative, negative=True,
        layer="negative_control",
        description="Fabricated topic must not return confident noise"),
    CompositionCase(
        "C11", "live", "What hours of the day am I typically most active?", "activity:read", "summary",
        oracle_c11_active_hours, expected_sources=("stat_insight",),
        topic_terms=("hour", "active", "morning", "evening", "week"),
        layer="stats:hour_of_week",
        description="Hour-of-week stats answer a rhythm question"),
    CompositionCase(
        "C12", "live", "Tell me about Luc and how often we talk", "relationship_context:read", "summary",
        oracle_c12_fusion, expected_sources=("entity_dossier", "stat_insight"),
        topic_terms=("luc",), layer="fusion:entities+stats",
        description="Cross-layer fusion: entity spine AND stats must both contribute"),
]


# --- C13–C30: qq-catalog-4 expansion (Phase 1) ----------------------------------------
# More cases per layer, prioritizing the weak layers the qq-catalog-3 runs exposed
# (conversation_messages, contacts, calendar, location, activity). Known-item cases embed
# query keywords from one part of the target row and needle another part, so lexical echo
# of the query can't fake a hit.

_STOPWORDS = frozenset(
    "about after all also and are because been before but cant could dont for from have "
    "her his into just like more most not our she that the their them then there they "
    "this was were what when where which will with would your youre".split()
)


def _distinctive_words(text: str, k: int) -> List[str]:
    words = []
    for w in re.findall(r"[a-zA-Z]{5,}", text.lower()):
        if w not in _STOPWORDS and w not in words:
            words.append(w)
        if len(words) == k:
            break
    return words


def _known_item(conn: sqlite3.Connection, sql: str) -> Optional[Tuple[str, str]]:
    """(query_keywords, needle_snippet) from the longest row the SQL returns: keywords come
    from the first half of the content, the needle from the second half."""
    row = _one(conn, sql)
    if not row or not row[0]:
        return None
    content = re.sub(r"\s+", " ", str(row[0])).strip()
    half = len(content) // 2
    keywords = _distinctive_words(content[:half], 3)
    tail_words = content[half:].split(" ")
    needle = " ".join(tail_words[1:5])  # skip the possibly-split first word
    if len(keywords) < 2 or len(needle) < 12:
        return None
    return " ".join(keywords), needle


_KNOWN_ITEM_MESSAGE_SQL = """
    SELECT content FROM conversation_messages
    WHERE content IS NOT NULL AND length(content) > 200
    ORDER BY event_at DESC LIMIT 1"""

# ai_chat contents are short (prompt titles); target the longest available >= 80 chars.
_KNOWN_ITEM_AICHAT_SQL = """
    SELECT content FROM ai_chat_messages
    WHERE content IS NOT NULL AND length(content) >= 80
    ORDER BY length(content) DESC LIMIT 1"""


def query_c13_message_needle(conn: sqlite3.Connection) -> str:
    pair = _known_item(conn, _KNOWN_ITEM_MESSAGE_SQL)
    return f"Find my messages about {pair[0]}" if pair else "Find my recent long messages"


def oracle_c13_message_needle(conn: sqlite3.Connection) -> Oracle:
    pair = _known_item(conn, _KNOWN_ITEM_MESSAGE_SQL)
    if not pair:
        return Oracle([], "no long recent conversation message to target", ok=False)
    return Oracle([[pair[1]]], f"known-item message; needle: {pair[1][:40]!r}")


def _contact_at_offset(conn: sqlite3.Connection, offset: int) -> Optional[Tuple[str, str]]:
    rows = _all(
        conn,
        """SELECT c.display_name, ci.identifier FROM contacts c
           JOIN contact_identifiers ci ON ci.contact_id = c.contact_id
           WHERE c.is_self = 0 AND c.display_name IS NOT NULL AND length(c.display_name) > 2
           ORDER BY c.contact_id LIMIT 1 OFFSET ?""",
        offset,
    )
    return (str(rows[0][0]), str(rows[0][1])) if rows else None


def query_c14_contact2(conn: sqlite3.Connection) -> str:
    pair = _contact_at_offset(conn, 25)
    return f"Find the contact record for {pair[0]}" if pair else "Find my second contact"


def oracle_c14_contact2(conn: sqlite3.Connection) -> Oracle:
    pair = _contact_at_offset(conn, 25)
    if not pair:
        return Oracle([], "no 26th contact with identifier", ok=False)
    return Oracle([[pair[1]]], f"contact {pair[0]} → identifier must surface")


def query_c15_contact_summary(conn: sqlite3.Connection) -> str:
    pair = _contact_at_offset(conn, 50)
    first = pair[0].split()[0] if pair else "Alex"
    return f"What do I know about my contact {first}?"


def oracle_c15_contact_summary(conn: sqlite3.Connection) -> Oracle:
    pair = _contact_at_offset(conn, 50)
    if not pair:
        return Oracle([], "no 51st contact", ok=False)
    # Query embeds the first name; the needle is the FULL display name (must resolve).
    return Oracle([[pair[0]]], f"summary must surface full name {pair[0]!r}")


def query_c16_calendar_item(conn: sqlite3.Connection) -> str:
    row = _one(conn, "SELECT title FROM calendar_events WHERE title IS NOT NULL ORDER BY starts_at DESC LIMIT 1")
    word = _distinctive_words(str(row[0]), 1) if row else []
    return f"What is on my calendar involving {word[0]}?" if word else "What is my latest calendar event?"


def oracle_c16_calendar_item(conn: sqlite3.Connection) -> Oracle:
    row = _one(conn, "SELECT title FROM calendar_events WHERE title IS NOT NULL ORDER BY starts_at DESC LIMIT 1")
    if not row:
        return Oracle([], "no calendar_events", ok=False)
    return Oracle([[str(row[0])]], f"latest event title: {row[0]!r}")


def oracle_c17_calendar_stats(conn: sqlite3.Connection) -> Oracle:
    row = _one(
        conn,
        """SELECT payload_json FROM signal_facts
           WHERE fact_id LIKE 'stat:calendar.commitment.duration%' LIMIT 1""",
    )
    if not row:
        return Oracle([], "no calendar commitment stats", ok=False)
    tag = str(json.loads(row[0]).get("tag") or "")
    if len(tag) < 10:
        return Oracle([], "calendar stat has no tag", ok=False)
    return Oracle([[tag[:50]]], f"commitment-duration stat tag: {tag[:40]!r}")


def oracle_c18_recent_place(conn: sqlite3.Connection) -> Oracle:
    rows = _all(
        conn,
        """SELECT DISTINCT place_name FROM location_events
           WHERE place_name IS NOT NULL ORDER BY event_at DESC LIMIT 2""",
    )
    if not rows:
        return Oracle([], "no location_events", ok=False)
    # Either of the two most recent places counts (boundary rows can tie on timestamps).
    return Oracle([[str(p) for p, in rows]], f"most recent places: {[p for p, in rows]}")


def oracle_c19_cities(conn: sqlite3.Connection) -> Oracle:
    rows = _all(
        conn,
        """SELECT city, COUNT(*) FROM location_events WHERE city IS NOT NULL
           GROUP BY city ORDER BY COUNT(*) DESC LIMIT 2""",
    )
    if not rows:
        return Oracle([], "no cities in location_events", ok=False)
    return Oracle([[str(c)] for c, _ in rows], f"top cities: {[c for c, _ in rows]}")


def oracle_c20_domains(conn: sqlite3.Connection) -> Oracle:
    rows = _all(
        conn,
        """SELECT url, COUNT(*) FROM activity_events WHERE url IS NOT NULL
           GROUP BY url ORDER BY COUNT(*) DESC LIMIT 30""",
    )
    domains: Dict[str, int] = {}
    for url, n in rows:
        m = re.match(r"https?://(?:www\.)?([^/]+)", str(url))
        if m:
            domains[m.group(1)] = domains.get(m.group(1), 0) + int(n)
    top = sorted(domains.items(), key=lambda kv: -kv[1])[:2]
    if not top:
        return Oracle([], "no parseable activity urls", ok=False)
    return Oracle([[d] for d, _ in top], f"top domains: {[d for d, _ in top]}")


def query_c21_repo(conn: sqlite3.Connection) -> str:
    return "What GitHub repositories or code projects have I been looking at?"


def oracle_c21_repo(conn: sqlite3.Connection) -> Oracle:
    rows = _all(
        conn,
        """SELECT title FROM activity_events
           WHERE (url LIKE '%github%' OR title LIKE '%github%') AND title IS NOT NULL
           GROUP BY title ORDER BY COUNT(*) DESC LIMIT 2""",
    )
    if not rows:
        return Oracle([], "no github activity", ok=False)
    return Oracle([[str(t)[:40]] for t, in rows], f"github titles: {[str(t)[:30] for t, in rows]}")


def oracle_c22_moods(conn: sqlite3.Connection) -> Oracle:
    rows = _all(
        conn,
        """SELECT mood_tag, COUNT(*) FROM journal_entries WHERE mood_tag IS NOT NULL
           GROUP BY mood_tag ORDER BY COUNT(*) DESC LIMIT 3""",
    )
    if not rows:
        return Oracle([], "no journal mood tags", ok=False)
    # Any of the top-3 moods counts — top-1 alone is too sensitive to ties.
    return Oracle([[str(m) for m, _ in rows]], f"top moods: {[m for m, _ in rows]}")


def oracle_c23_journal_duration(conn: sqlite3.Connection) -> Oracle:
    row = _one(
        conn,
        """SELECT payload_json FROM signal_facts
           WHERE fact_id LIKE 'stat:journal.duration.by_category%' LIMIT 1""",
    )
    if not row:
        return Oracle([], "no journal duration stats", ok=False)
    tag = str(json.loads(row[0]).get("tag") or "")
    if len(tag) < 10:
        return Oracle([], "journal duration stat has no tag", ok=False)
    return Oracle([[tag[:50]]], f"journal-duration stat tag: {tag[:40]!r}")


def oracle_c24_spend_categories(conn: sqlite3.Connection) -> Oracle:
    rows = _all(
        conn,
        """SELECT category, COUNT(*) FROM financial_transactions WHERE category IS NOT NULL
           GROUP BY category ORDER BY COUNT(*) DESC LIMIT 2""",
    )
    if not rows:
        return Oracle([], "no financial_transactions categories", ok=False)
    return Oracle([[str(c)] for c, _ in rows], f"top spend categories: {[c for c, _ in rows]}")


def oracle_c25_spend_stats(conn: sqlite3.Connection) -> Oracle:
    row = _one(
        conn,
        """SELECT payload_json FROM signal_facts
           WHERE fact_id LIKE 'stat:financial.spend.by_category%' LIMIT 1""",
    )
    if not row:
        return Oracle([], "no financial spend stats", ok=False)
    tag = str(json.loads(row[0]).get("tag") or "")
    if len(tag) < 10:
        return Oracle([], "spend stat has no tag", ok=False)
    return Oracle([[tag[:50]]], f"spend stat tag: {tag[:40]!r}")


def query_c26_aichat_needle(conn: sqlite3.Connection) -> str:
    pair = _known_item(conn, _KNOWN_ITEM_AICHAT_SQL)
    return f"Find my AI conversations about {pair[0]}" if pair else "Find my recent AI conversations"


def oracle_c26_aichat_needle(conn: sqlite3.Connection) -> Oracle:
    pair = _known_item(conn, _KNOWN_ITEM_AICHAT_SQL)
    if not pair:
        return Oracle([], "no long recent ai_chat message to target", ok=False)
    return Oracle([[pair[1]]], f"known-item ai chat; needle: {pair[1][:40]!r}")


def _entity_at_offset(conn: sqlite3.Connection, offset: int) -> Optional[str]:
    rows = _all(
        conn,
        """SELECT e.canonical_name FROM entities e
           JOIN entity_mentions em ON em.entity_id = e.entity_id
           WHERE length(e.canonical_name) > 3
           GROUP BY e.entity_id ORDER BY COUNT(em.mention_id) DESC LIMIT 1 OFFSET ?""",
        offset,
    )
    return str(rows[0][0]) if rows else None


def query_c27_entity2(conn: sqlite3.Connection) -> str:
    name = _entity_at_offset(conn, 3)
    return f"Tell me what I know about {name}" if name else "Tell me about my top entity"


def oracle_c27_entity2(conn: sqlite3.Connection) -> Oracle:
    name = _entity_at_offset(conn, 3)
    if not name:
        return Oracle([], "no 4th most-mentioned entity", ok=False)
    return Oracle([[name]], f"entity spine must surface {name!r}")


def oracle_c28_cadence(conn: sqlite3.Connection) -> Oracle:
    row = _one(
        conn,
        """SELECT payload_json FROM signal_facts
           WHERE fact_id LIKE 'stat:messages.cadence.by_contact:%' LIMIT 1""",
    )
    if not row:
        return Oracle([], "no cadence stats", ok=False)
    payload = json.loads(row[0])
    ident = str(payload.get("group_key") or payload.get("fact_id", "").rsplit(":", 1)[-1])
    who = [ident, *(_contact_names_for_identifier(conn, ident))]
    who = [w for w in who if w]
    if not who:
        return Oracle([], "cadence stat has no contact", ok=False)
    return Oracle([who], f"messaging cadence for {who[-1]!r}")


def oracle_c29_goals(conn: sqlite3.Connection) -> Oracle:
    rows = _all(
        conn,
        """SELECT payload_json FROM signal_objects WHERE object_type = 'user_goals'
           ORDER BY LENGTH(payload_json) DESC LIMIT 2""",
    )
    groups = []
    for (payload,) in rows:
        goal = str(json.loads(payload).get("goal_text") or "")
        words = " ".join(goal.split(" ")[:5])
        if len(words) > 12:
            groups.append([words])
    if not groups:
        return Oracle([], "no user_goals with goal_text", ok=False)
    return Oracle(groups, f"{len(groups)} goal snippets")


def oracle_c30_temporal_month(conn: sqlite3.Connection) -> Oracle:
    return Oracle([], "graded on dated items within the window")


LIVE_COMPOSITION_CASES.extend([
    CompositionCase(
        "C13", "live", query_c13_message_needle, "messages:read", "summary",
        oracle_c13_message_needle, layer="canonical:conversation_messages",
        description="Known-item message search: keywords from one half, needle from the other"),
    CompositionCase(
        "C14", "live", query_c14_contact2, "contacts:resolve", "raw", oracle_c14_contact2,
        layer="canonical:contacts",
        description="Second contact resolution (26th contact, not the first)"),
    CompositionCase(
        "C15", "live", query_c15_contact_summary, "contacts:resolve", "summary",
        oracle_c15_contact_summary, layer="canonical:contacts",
        description="First-name contact lookup must resolve the full display name"),
    CompositionCase(
        "C16", "live", query_c16_calendar_item, "schedule:read", "summary",
        oracle_c16_calendar_item, expected_sources=("canonical:calendar_events",),
        topic_terms=("meeting", "calendar", "event", "session"),
        layer="canonical:calendar_events",
        description="Known-item calendar lookup by one title word"),
    CompositionCase(
        "C17", "live", "How much of my time is committed to meetings?", "schedule:read", "summary",
        oracle_c17_calendar_stats, expected_sources=("stat_insight",),
        topic_terms=("commit", "meeting", "duration", "calendar", "hour"),
        layer="stats:calendar.commitment",
        description="Calendar commitment stats answer a time-budget question"),
    CompositionCase(
        "C18", "live", "Where was I most recently? What was the last place I visited?",
        "places:read", "summary", oracle_c18_recent_place,
        expected_sources=("canonical:location_events",),
        topic_terms=("place", "visit", "location", "recent"),
        layer="canonical:location_events",
        description="Recency-ordered place lookup"),
    CompositionCase(
        "C19", "live", "Which cities have I spent time in?", "places:read", "summary",
        oracle_c19_cities, expected_sources=("canonical:location_events",),
        topic_terms=("city", "san", "austin", "travel", "location"),
        layer="canonical:location_events",
        description="City-level aggregation over location events"),
    CompositionCase(
        "C20", "live", "What websites do I visit the most?", "activity:read", "summary",
        oracle_c20_domains, expected_sources=("canonical:activity_events",),
        topic_terms=("visit", "site", "web", "brows"),
        layer="canonical:activity_events",
        description="Domain-level aggregation over browsing activity"),
    CompositionCase(
        "C21", "live", query_c21_repo, "activity:read", "summary", oracle_c21_repo,
        expected_sources=("canonical:activity_events",),
        topic_terms=("github", "repo", "code"),
        layer="canonical:activity_events",
        description="Topic-scoped activity lookup (github)"),
    CompositionCase(
        "C22", "live", "What moods do I record most often in my journal?", "health:read", "summary",
        oracle_c22_moods, expected_sources=("canonical:journal_entries",),
        topic_terms=("mood", "journal", "feel"),
        layer="canonical:journal_entries",
        description="Mood-tag aggregation over journal entries"),
    CompositionCase(
        "C23", "live", "How long do I usually spend journaling?", "health:read", "summary",
        oracle_c23_journal_duration, expected_sources=("stat_insight",),
        topic_terms=("journal", "duration", "minute", "time"),
        layer="stats:journal.duration",
        description="Journal duration stats answer a habit question"),
    CompositionCase(
        "C24", "live", "What categories do I spend money on?", "resources:read", "summary",
        oracle_c24_spend_categories, expected_sources=("canonical:financial_transactions",),
        topic_terms=("spend", "transaction", "money", "salary", "transfer", "invest"),
        layer="canonical:financial_transactions",
        description="Category aggregation over financial transactions"),
    CompositionCase(
        "C25", "live", "How much do I spend by category?", "resources:read", "summary",
        oracle_c25_spend_stats, expected_sources=("stat_insight",),
        topic_terms=("spend", "categor", "total"),
        layer="stats:financial.spend",
        description="Financial spend stats reach the response"),
    CompositionCase(
        "C26", "live", query_c26_aichat_needle, "ai_conversations:read", "summary",
        oracle_c26_aichat_needle, layer="canonical:ai_chat_messages",
        description="Known-item AI-chat search: keywords one half, needle the other"),
    CompositionCase(
        "C27", "live", query_c27_entity2, "relationship_context:read", "summary",
        oracle_c27_entity2, expected_sources=("entity_dossier", "entity_mention"),
        layer="entities:spine",
        description="Entity-spine lookup for the 4th most-mentioned entity"),
    CompositionCase(
        "C28", "live", "How often do I message people — what is my messaging cadence?",
        "messages:read", "summary", oracle_c28_cadence,
        expected_sources=("stat_insight",),
        topic_terms=("cadence", "message", "often", "contact"),
        layer="stats:messages.cadence",
        description="Cadence stats surface the actual per-contact rhythm"),
    CompositionCase(
        "C29", "live", "What are my current goals and what am I working toward?",
        "work_context:read", "summary", oracle_c29_goals,
        expected_sources=("user_goal",),
        topic_terms=("goal", "work", "project"),
        layer="signals:user_goals",
        description="User goals surface with their actual goal text"),
    CompositionCase(
        "C30", "live", "What was I doing this past month?", "messages:read", "summary",
        oracle_c30_temporal_month, temporal_days=30, layer="planner:time_window",
        description="30-day relative window actually constrains results"),
])

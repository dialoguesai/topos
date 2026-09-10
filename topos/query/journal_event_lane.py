"""Lived-event journal lane — diaries as evidence when the owner asks who they were with.

Live 2026-09-08: "activities with [named people]" and "when did I see a movie"
spent the owner fan-out's four slots on ai_conversations / work_context /
relationship_context / activity. ``health:read`` — the one scope whose tables
include ``journal_entries`` — is sixth in a seven-entry candidate list cut at
``MAX_SCOPE_ROUTES = 4``, so it is dropped for exactly these asks. Fun diary
rows (theater, tailgate, ``people`` column filled) sat on the temporal graph
while browsing stats filled the cap.

What this lane adds under ``health:read``: an ask-gated scan of the recent
journal head, matched on the people/place/category the question names. The
recent lane is blind recency bounded by SOURCE and a 14-day window
(``_RECENT_WINDOW_DAYS``), so a diary row older than a fortnight, or one that
loses the top-10 to chat rows, is unreachable no matter how exactly it answers
the question.

Privacy posture — two gates, the same pair ``graph_lane`` carries:

* **Owner-only.** Below ``owner_raw`` the lane returns nothing and writes no
  ledger receipt — a receipt for a withheld lane is an existence signal.
  Sound because ``resolve_disclosure_tier`` never elevates a grantee there.
* **Inside the scope ceiling.** ``journal_entries`` must be in the manifest's
  ``canonical_tables``, which resolves to ``health:read`` and nothing else.
  The lane therefore cannot widen what any scope may disclose. There is NO
  named-scope exception: ``graph_lane`` has one because relationship structure
  is what ``relationship_context:read``'s own card advertises, and no scope's
  card advertises the diary. This is the gate an earlier draft of this module
  lacked — it took no manifest at all, so it read ``journal_entries`` under
  every scope and would have delivered raw diary prose under grants whose
  ceiling is ``summary``.

Fusion weight is the canonical lane's own (1.0), never above: this is ordinary
evidence that arrived by the ask instead of by a content key — the same
argument the thread, graph and commitment lanes make. It carries no diversity
floor; ``graph``'s two-slot floor was earned with a live-snapshot measurement
(8 items, 0 survivors) and this lane has no such measurement yet.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from . import narrowing as _N

logger = logging.getLogger(__name__)

JOURNAL_EVENT_LANE_MAX_ITEMS = 8

# Keep this aligned with topos-react-app lived_social_event routing.
_LIVED_SOCIAL_RE = re.compile(
    r"(?:"
    r"\bwho (?:was|were) i with\b|"
    r"\bwhat did i (?:see|do with)\b|"
    r"\bwhat we did with each other\b|"
    r"\bactivit(?:y|ies) (?:i(?:'ve| have) had )?with\b|"
    r"\b(?:see|saw|watch(?:ed)?|go see|went to see) (?:a |the )?movie\b|"
    r"\b(?:have i done |done )?anything fun\b"
    r")",
    re.I,
)

#: Venue words a "movie" ask implies. They widen the ROW match, but must not
#: count as a people hit — otherwise every cinema row scores as if the question
#: had named a person.
_VENUE_SYNONYMS = ("movie", "theater", "theatre", "cinema")

_MATCH_STOP = frozenset(
    {
        "activities",
        "activity",
        "lately",
        "recently",
        "recent",
        "check",
        "had",
        "ive",
        "something",
        "anything",
        "fun",
        "together",
        "each",
        "other",
        "what",
        "who",
        "when",
        "with",
        "have",
        "done",
        "did",
        "see",
        "saw",
        "watch",
        "watched",
        "go",
        "went",
        "the",
        "and",
        "for",
        "this",
        "that",
        "from",
        "about",
        "been",
        "being",
        # The gate's own phrasings put these in every matching query, so any
        # that survive here become needles that match on prose. `\bwho (?:was|
        # were) i with\b` guarantees "was"; a bare `any(n in blob)` then
        # matched every diary row containing it, so "who was I with?" answered
        # with therapy, biopsy and layoff rows and dropped the row naming the
        # people. Function words are never evidence that a row is the answer.
        "was",
        "were",
        "are",
        "is",
        "am",
        "there",
        "here",
        "last",
        "next",
        "weekend",
        "week",
        "month",
        "year",
        "day",
        "days",
        "night",
        "time",
        "times",
        "any",
        "some",
        "all",
        "much",
        "many",
        "more",
        "most",
        "out",
        "over",
        "back",
        "into",
        "onto",
        "than",
        "then",
        "them",
        "they",
        "their",
        "you",
        "your",
        "our",
        "his",
        "her",
        "hers",
        "its",
        "not",
        "but",
        "off",
        "just",
        "only",
        "also",
        "very",
        "get",
        "got",
        "getting",
        "going",
        "went",
        "come",
        "came",
        "make",
        "made",
        "know",
        "think",
        "want",
        "need",
        "like",
        "how",
        "why",
        "where",
        "which",
        "whom",
    }
)


def lived_social_event_ask(query_text: str) -> bool:
    return bool(_LIVED_SOCIAL_RE.search(str(query_text or "")))


def _match_needles(query_text: str) -> List[str]:
    q = str(query_text or "").lower()
    needles: List[str] = []
    for raw in re.findall(r"[a-z][a-z0-9']{2,}", q):
        token = raw.replace("'", "")
        if token in _MATCH_STOP or len(token) < 3:
            continue
        if token not in needles:
            needles.append(token)
    if re.search(r"\bmovie\b", q):
        for extra in _VENUE_SYNONYMS:
            if extra not in needles:
                needles.append(extra)
    return needles


def _row_summary(row: Dict[str, Any]) -> str:
    parts = [
        str(row.get(field) or "")
        for field in ("content", "mood_tag", "category", "people", "place_name")
        if row.get(field)
    ]
    entry_at = str(row.get("entry_at") or "")
    if entry_at:
        parts.append(entry_at[:10])
    return " — ".join(parts)


def _people_hit(people: str, needles: List[str]) -> bool:
    blob = people.lower()
    return any(n in blob for n in needles if n not in _VENUE_SYNONYMS)


def journal_event_items(
    conn: Any,
    *,
    query_text: str,
    scope_id: str,
    manifest: Any,
    disclosure_tier: str,
    ledger: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Journal rows for a lived-social owner ask, as fusion-ready summary items.

    Returns [] — with no ledger receipt — unless the tier is owner_raw, the
    scope already authorizes ``journal_entries``, and the ask matches the
    lived-social gate.
    """
    if str(disclosure_tier or "") != "owner_raw":
        return []
    # Scope ceiling. `journal_entries` resolves into canonical_tables for
    # health:read alone, so the lane reaches no table its grant does not
    # already name and cannot widen any scope's disclosure. Deliberately no
    # named-scope exception — see the module docstring.
    if "journal_entries" not in list(getattr(manifest, "canonical_tables", None) or []):
        return []
    if conn is None or not lived_social_event_ask(query_text):
        return []

    needles = _match_needles(query_text)
    if not needles:
        return []

    try:
        rows = conn.execute(
            "SELECT entry_id, entry_at, mood_tag, category, content, people, "
            "place_name, source_id FROM journal_entries "
            "ORDER BY entry_at DESC LIMIT 80"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — missing table must not kill the turn
        logger.debug("journal event lane: journal_entries unread: %s", exc)
        return []

    collected: List[Dict[str, Any]] = []
    seen: set = set()
    for row in rows:
        entry_id = str(row[0] or "")
        if not entry_id or entry_id in seen:
            continue
        content = str(row[4] or "")
        people = str(row[5] or "")
        place = str(row[6] or "")
        category = str(row[3] or "")
        blob = f"{content} {people} {place} {category}".lower()
        # A whole word, not a substring: "ada" must not match "adamant". The
        # trailing boundary is the load-bearing half -- a leading `\b` alone
        # still matches a longer word that merely STARTS with the needle. The
        # optional plural keeps "movie" reaching "movies" without reopening it.
        if not any(
            re.search(rf"\b{re.escape(n)}(?:s|es|'s)?\b", blob) for n in needles
        ):
            continue
        seen.add(entry_id)
        people_match = _people_hit(people, needles)
        item: Dict[str, Any] = {
            "topic": (content or place or people or "journal")[:120],
            "summary_text": _row_summary(
                {
                    "content": content,
                    "mood_tag": row[2],
                    "category": category,
                    "people": people,
                    "place_name": place,
                    "entry_at": row[1],
                }
            ),
            "record_id": entry_id,
            "source_id": str(row[7] or ""),
            "dimension": "health",
            "event_at": row[1],
            "relevance_score": 0.97 if people_match else 0.92,
            "retrieval_source": "journal_event_lane",
        }
        collected.append(item)

    collected.sort(key=lambda i: i.get("relevance_score") or 0.0, reverse=True)
    dropped = max(0, len(collected) - JOURNAL_EVENT_LANE_MAX_ITEMS)
    items = collected[:JOURNAL_EVENT_LANE_MAX_ITEMS]
    if items and ledger is not None:
        try:
            ledger.record(
                _N.STAGE_RETRIEVAL,
                "contributed",
                "journal_event_lane",
                dropped=dropped,
                detail={
                    "contributed": len(items),
                    "needles": len(needles),
                },
            )
        except Exception:  # noqa: BLE001 — the ledger never breaks a turn
            pass
    return items

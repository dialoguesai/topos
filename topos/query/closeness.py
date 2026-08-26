"""Close-circle ranking computed from actual interaction.

WHY THIS EXISTS
---------------
"Who's in my close circle?" was answered from `rel.relationship` facts — six rows
a pack happened to extract from journal sentences. The people the owner actually
talks to were absent: live 2026-08-26 the answer omitted the three highest-volume
correspondents in the corpus while listing "friend in software sales".

The `RelationshipEdge` store looked like the right source and is not: its
`warmth_band` and `cadence_band` are hardcoded literals in
`features/signal/extraction/rule_extractors.py` (all 216 rows read "medium" /
"recent"), so it can enumerate contacts but cannot rank them.

The signal was never missing, only unjoined. `conversation_messages.sender_id`
holds the RAW handle — "+15127400415", an email, a transcript speaker label —
while names live in `contacts`, reachable only through `contact_identifiers`.
Nothing in the query path performed that hop, so all 4,866 inbound messages
resolved to zero named people. Normalising both sides and joining recovers the
whole picture (top of the live corpus: 319, 206, 164, 109 messages).

WHAT COUNTS AS CLOSE
--------------------
Volume and recency, banded rather than scored, because a raw count invites the
model to read precision that monthly sync gaps do not support. Bands are relative
to THIS owner's corpus: "high" means high for them, so a light texter is not
flattened into a single band.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

#: Phrasings that ask who the owner is CLOSE to, as opposed to who they are
#: related to. Kept separate from `facts_direct._ALIASES` so that "family" keeps
#: its role-based answer (parent / sibling) instead of an interaction ranking.
_CLOSENESS_RE = re.compile(
    r"\b(close circle|inner circle|closest|close friends?|best friends?|"
    r"my people|who do i (talk|speak|message|text) to most)\b"
)
_OWNER_RE = re.compile(r"\b(i|my|me|am i|do i|i'm)\b")

#: Non-person sender ids: transcript speaker labels and system markers, which
#: would otherwise become "people" the owner is close to.
_NON_PERSON = re.compile(r"^(speaker\s*\d+|sys|rec|system|unknown|me|self)$", re.I)


def matches_closeness(query_text: str) -> bool:
    q = (query_text or "").lower()
    return bool(_OWNER_RE.search(q) and _CLOSENESS_RE.search(q))


def _normalise(raw: Any) -> str:
    """One key shape for both sides of the join.

    Phone numbers arrive as "+1 (512) 740-0415" in the address book and
    "+15127400415" on the message, so digits-only, last ten, drops the country
    code and every separator. Everything else (email, handle) folds to lowercase.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    digits = "".join(ch for ch in s if ch.isdigit())
    # A handle can contain digits; only treat it as a phone when it is ALL
    # digits and separators, which an email or @handle never is.
    if digits and not any(ch.isalpha() for ch in s):
        return digits[-10:] if len(digits) >= 10 else digits
    return s.lower()


def _parse_ts(raw: Any) -> Optional[datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _warmth_band(rank: int, total: int) -> str:
    """Relative to this corpus — see module docstring."""
    if total <= 0:
        return "low"
    # (rank + 1) so the LAST person reaches 1.0. With `rank / total` the bottom
    # of a small circle could never fall below 0.75, and a four-person corpus had
    # no "low" band at all.
    pct = (rank + 1) / total
    if pct <= 0.25:
        return "high"
    if pct <= 0.75:
        return "medium"
    return "low"


def _cadence_band(last: Optional[datetime], now: datetime) -> str:
    if last is None:
        return "dormant"
    age = now - last
    if age <= timedelta(days=14):
        return "recent"
    if age <= timedelta(days=60):
        return "occasional"
    return "dormant"


def compute_close_circle(
    conn: sqlite3.Connection,
    *,
    limit: int = 15,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Named correspondents ranked by inbound volume, with warmth and cadence.

    `now` defaults to the newest message in the corpus rather than the wall
    clock: cadence should describe the data, so a node that has not synced for a
    week does not silently demote everyone to "dormant".
    """
    ident_to_contact: Dict[str, str] = {}
    for contact_id, identifier in conn.execute(
        "SELECT contact_id, identifier FROM contact_identifiers "
        "WHERE identifier IS NOT NULL AND identifier != ''"
    ).fetchall():
        key = _normalise(identifier)
        if key:
            ident_to_contact.setdefault(key, str(contact_id))

    names: Dict[str, str] = {}
    for contact_id, display_name in conn.execute(
        "SELECT contact_id, display_name FROM contacts "
        "WHERE display_name IS NOT NULL AND display_name != ''"
    ).fetchall():
        names[str(contact_id)] = str(display_name)

    agg: Dict[str, Dict[str, Any]] = {}
    newest: Optional[datetime] = None
    for sender_id, count, last_at in conn.execute(
        "SELECT sender_id, COUNT(*), MAX(event_at) FROM conversation_messages "
        "WHERE is_from_self=0 AND sender_id IS NOT NULL AND sender_id != '' "
        "GROUP BY sender_id"
    ).fetchall():
        if _NON_PERSON.match(str(sender_id).strip()):
            continue
        contact_id = ident_to_contact.get(_normalise(sender_id))
        if not contact_id:
            continue           # a handle with no address-book entry stays anonymous
        name = names.get(contact_id)
        if not name:
            continue           # a contact with no name cannot be reported as a person
        ts = _parse_ts(last_at)
        if ts and (newest is None or ts > newest):
            newest = ts
        row = agg.setdefault(name, {"person": name, "messages": 0, "last_at": None})
        row["messages"] += int(count or 0)
        if ts and (row["last_at"] is None or ts > row["last_at"]):
            row["last_at"] = ts

    if not agg:
        return []

    ranked = sorted(agg.values(), key=lambda r: (-r["messages"], r["person"]))
    anchor = now or newest or datetime.now(timezone.utc)
    total = len(ranked)
    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(ranked[:limit]):
        out.append(
            {
                "person": row["person"],
                "messages": row["messages"],
                "last_contact": row["last_at"].date().isoformat() if row["last_at"] else None,
                "warmth_band": _warmth_band(idx, total),
                "cadence_band": _cadence_band(row["last_at"], anchor),
            }
        )
    return out


def compose_close_circle_answer(people: List[Dict[str, Any]]) -> str:
    lines = []
    for p in people:
        bits = [f"{p['warmth_band']} warmth", f"{p['cadence_band']} contact"]
        if p.get("last_contact"):
            bits.append(f"last {p['last_contact']}")
        bits.append(f"{p['messages']} messages")
        lines.append(f"- {p['person']}: " + " · ".join(bits))
    return "\n".join(lines)


def try_close_circle(
    conn: sqlite3.Connection,
    query_text: str,
    *,
    packet_resolution: str,
    limit: int = 15,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """The lane. Returns an answer payload, or None to fall through.

    Gated on packet_resolution exactly as `facts_direct` is: a non-owner floors
    to scores_only upstream, so contact names can never leave by this route.
    """
    if not matches_closeness(query_text):
        return None
    if packet_resolution not in ("facts", "facts_all"):
        return None
    people = compute_close_circle(conn, limit=limit, now=now)
    if not people:
        return None
    return {
        "answer_type": "facts",
        "answer": compose_close_circle_answer(people),
        "items": [p["person"] for p in people],
        "close_circle": people,
        "close_circle_direct": True,
    }

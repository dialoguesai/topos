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

WHAT THIS MEASURES, AND WHAT IT DOES NOT
----------------------------------------
Inbound message VOLUME and RECENCY. Nothing else. The fields are named for that
and not for what it might imply: an earlier cut called them `warmth_band` and
`cadence_band`, which asserted a reading of the relationship that counting cannot
support — the same defect as the hardcoded "medium" this lane was written to
replace, one step subtler.

Specifically NOT consulted, though all of it is in the store: the owner's 2,802
OUTBOUND messages (so there is no reciprocity — someone who texts a lot and gets
nothing back still ranks), 4,887 emotion labels, 2,594 topics, 1,792 sentiment
rows, and whether a thread is 1:1 (153) or a group (14), so a busy group inflates
everyone in it equally.

Banded rather than scored, because a raw count invites the model to read precision
that monthly sync gaps do not support. Bands are relative to THIS owner's corpus:
"high" means high for them, so a light texter is not flattened into one band.

A real closeness judgement belongs in a derivation pack writing reviewable
`rel.closeness_tier` facts, which is the predicate facts_direct already asks for
and nothing yet populates. This lane is the interim answer, not that judgement.
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
    r"\b(close circle|inner circle|closest|close friends?|best friends?|friends?|"
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


def _correspondence_band(rank: int, total: int) -> str:
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


def _recency_band(last: Optional[datetime], now: datetime) -> str:
    if last is None:
        return "dormant"
    age = now - last
    if age <= timedelta(days=14):
        return "recent"
    if age <= timedelta(days=60):
        return "occasional"
    return "dormant"


def _blackholed_terms(conn: sqlite3.Connection) -> set:
    """Names the owner asked to be forgotten.

    The lane reads `contacts` and `conversation_messages` straight, so nothing
    upstream strips a blackholed person for it — a person the owner erased would
    otherwise be handed back as a close contact. Matched on the WHOLE normalised
    name: token overlap produced false positives against place blackholes (the
    place "Old Saybrook - Jeff's Place" matches the person "Zulu Alpha" on
    "jeff", and erasing a place must not erase a person).
    """
    try:
        from ..features.lifecycle.blackhole import blackholed_name_terms

        return {str(t).strip().lower() for t in (blackholed_name_terms(conn) or set()) if str(t).strip()}
    except Exception:  # noqa: BLE001 — a missing store must not fail open OR break the turn
        try:
            rows = conn.execute(
                "SELECT normalized_name FROM entity_blackholes").fetchall()
            return {str(r[0]).strip().lower() for r in rows if r and str(r[0]).strip()}
        except sqlite3.Error:
            return set()


def _normalised_person(name: str) -> str:
    try:
        from ..features.lifecycle.blackhole import normalize_entity_name

        return str(normalize_entity_name(name) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return str(name or "").strip().lower()


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

    blocked = _blackholed_terms(conn)
    names: Dict[str, str] = {}
    # `is_self` excludes the owner: their own handle appears as a sender on
    # self-threads and messages synced from a second device, which put them in
    # their own close circle (live 2026-08-26, rank 27).
    try:
        contact_rows = conn.execute(
            "SELECT contact_id, display_name FROM contacts "
            "WHERE display_name IS NOT NULL AND display_name != '' "
            "AND COALESCE(is_self, 0) = 0"
        ).fetchall()
    except sqlite3.Error:                     # fixtures without the column
        contact_rows = conn.execute(
            "SELECT contact_id, display_name FROM contacts "
            "WHERE display_name IS NOT NULL AND display_name != ''"
        ).fetchall()
    for contact_id, display_name in contact_rows:
        if _normalised_person(str(display_name)) in blocked:
            continue
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

    # RECIPROCITY FIRST, where the person graph can supply it.
    #
    # Ranking by inbound volume alone put Whiskey Lima at the top on 1,388 messages
    # while the graph screen led with Mike November — the same question answered two
    # ways, and chat had the weaker one. The graph's closeness is reciprocity, then
    # recency, then volume, and derived relationship facts can raise it where behaviour
    # understates the tie; a broadcaster with thousands of messages is not close, and
    # 98 of 151 ties on this corpus are one-way.
    #
    # Volume stays the fallback, not the answer: a node whose person graph has not been
    # built yet still gets a ranked list rather than nothing.
    graph_closeness = _person_graph_closeness(conn)
    for row in agg.values():
        row["closeness"] = graph_closeness.get(_normalised_person(row["person"]))
    measured = any(r.get("closeness") is not None for r in agg.values())
    ranked = sorted(
        agg.values(),
        key=lambda r: (-(r.get("closeness") if r.get("closeness") is not None else -1),
                       -r["messages"], r["person"])
        if measured else (-r["messages"], r["person"]),
    )
    anchor = now or newest or datetime.now(timezone.utc)
    total = len(ranked)
    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(ranked[:limit]):
        out.append(
            {
                "person": row["person"],
                "messages": row["messages"],
                "last_contact": row["last_at"].date().isoformat() if row["last_at"] else None,
                "correspondence_band": _correspondence_band(idx, total),
                "recency_band": _recency_band(row["last_at"], anchor),
                # None means the graph has no measured tie for them — NOT distance zero.
                "closeness": row.get("closeness"),
                "ranked_by": "reciprocity" if measured else "inbound volume",
            }
        )
    return out


def _person_graph_closeness(conn: sqlite3.Connection) -> Dict[str, float]:
    """Closeness per person, from the same computation the graph screen draws.

    Keyed on the NORMALISED name because this lane resolves people through the address
    book while the graph resolves them through messenger keys and entity ids; the name is
    the only key both sides reliably hold. Returns {} on any failure — a node mid-upgrade,
    or one with no messaging substrate, falls back to volume rather than to nothing.
    """
    try:
        from ..analytics.dataset_resolution import resolve_messaging_dataset
        from ..analytics.person_graph import attach_closeness, build_person_nodes

        dataset_id, _ = resolve_messaging_dataset(conn, "")
        nodes = build_person_nodes(conn, dataset_id)
        attach_closeness(conn, dataset_id, nodes)
    except Exception:  # noqa: BLE001 — this lane must never break a turn
        return {}
    out: Dict[str, float] = {}
    for node in nodes:
        if node.get("is_owner"):
            continue
        value = node.get("closeness")
        key = _normalised_person(str(node.get("label") or ""))
        if key and isinstance(value, (int, float)):
            # A person can surface twice (messenger peer and extracted entity); keep the
            # stronger reading rather than whichever the scan happened to reach last.
            if key not in out or value > out[key]:
                out[key] = float(value)
    return out


def compose_close_circle_answer(people: List[Dict[str, Any]]) -> str:
    lines = []
    for p in people:
        bits = []
        if p.get("closeness") is not None:
            bits.append(f"closeness {p['closeness']:.2f}")
        bits += [f"{p['correspondence_band']} correspondence",
                 f"{p['messages']} messages inbound"]
        if p.get("last_contact"):
            bits.append(f"last {p['last_contact']} ({p['recency_band']})")
        else:
            bits.append(f"last contact {p['recency_band']}")
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
    # Computed once at full width: the warmth bands are already relative to the
    # whole set inside compute_close_circle, so slicing after keeps them correct
    # and gives the honest total without a second pass over the corpus.
    everyone = compute_close_circle(conn, limit=10_000, now=now)
    if not everyone:
        return None
    people = everyone[:limit]
    payload = {
        "answer_type": "facts",
        "answer": compose_close_circle_answer(people),
        "items": [p["person"] for p in people],
        "close_circle_direct": True,
    }
    # `close_circle` (the structured block) is deliberately NOT returned: it
    # restated `answer` and `items` for 1,866 of a 3,435-byte payload and nothing
    # consumes it. compute_close_circle() still returns it for a caller that wants
    # structure.
    #
    # Say what was cut. The cap hid 18 of 33 people on the live corpus, and an
    # undisclosed cap is what makes a ranked list read as a complete one — the
    # same failure this lane exists to correct.
    total = len(everyone)
    if total > len(people):
        payload["close_circle_truncated"] = {
            "shown": len(people),
            "total": total,
            # State the omission, do not offer a remedy: no caller can pass a
            # limit today, so "ask for more" was a promise with nothing behind it.
            "note": "ranked by inbound message volume; the rest are not shown",
        }
    return payload

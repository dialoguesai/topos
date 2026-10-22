"""Source-blind shaping of retrieved evidence (``TOPOS_EVIDENCE_SHAPING``, default off).

Every rule here reads relevance, text, conversation identity and time. None reads
which connector a row came from, and none sets a per-source quota: relevance is per
question, and a source may rightly fill the context when it holds the best evidence.
What the rules remove is volume that is not evidence — the same conversation counted
thirty times, the same words stored twice, neighbours far below the best match, and
derived summaries crowding out the records they summarise.

Rules 1–3 shape the vector lane (`shape_vector_hits`). Rule 4 is a selection helper
the fusion step calls (`bounded_derived_selection`). The vector-lane rules never add a
key to a hit: conversation ids can embed phone numbers and e-mail addresses, so they
are looked up here and never leave this module.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

#: Rank offset in a group's score, 1 / (K + best_rank). Keeps the lift from supporting
#: hits proportional: near the top it moves a group about one place, deeper about three.
_GROUP_RANK_OFFSET = 10
#: Supporting hits beyond this many add nothing; a thirty-row session is not thirty times
#: as relevant as a one-row one.
_GROUP_SUPPORT_CAP = 3
#: Tokens compared per hit. Index bodies run to a few thousand characters; the opening
#: tokens decide whether two of them are the same text.
_SHINGLE_TOKENS = 400
_SHINGLE_SIZE = 3
_TOKEN_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# Rule 3 — relevance floor
# ---------------------------------------------------------------------------


def apply_relevance_floor(
    hits: List[Dict[str, Any]], *, margin: float, protect: int = 0
) -> List[Dict[str, Any]]:
    """Drop hits whose cosine is more than `margin` below the best cosine in the list.

    The reference is the best cosine, not the first hit: hybrid and rerank order can put
    a lower cosine first. Keyword-only hits carry no cosine and are kept — the hybrid
    merge keeps them for the same reason (a cosine threshold must not silently drop a
    keyword match). The first `protect` hits are never dropped: that is the ranker's own
    head, ordered on keyword and rerank evidence the cosine does not see. On the owner's
    snapshot the ranker's first hit was the answering record with a cosine more than
    0.15 below the pool's best, and a floor over the whole list cut it. `margin` <= 0
    disables the rule.
    """
    if margin <= 0 or not hits:
        return list(hits)
    sims = [float(h["similarity"]) for h in hits if h.get("similarity") is not None]
    if not sims:
        return list(hits)
    floor = max(sims) - margin
    return [
        h for idx, h in enumerate(hits)
        if idx < protect or h.get("similarity") is None or float(h["similarity"]) >= floor - 1e-9
    ]


# ---------------------------------------------------------------------------
# Rule 2 — near-duplicate removal
# ---------------------------------------------------------------------------


def _shingles(text: str) -> FrozenSet[Tuple[str, ...]]:
    tokens = _TOKEN_RE.findall(str(text or "").lower())[:_SHINGLE_TOKENS]
    if len(tokens) < _SHINGLE_SIZE:
        return frozenset({tuple(tokens)}) if tokens else frozenset()
    return frozenset(tuple(tokens[i:i + _SHINGLE_SIZE]) for i in range(len(tokens) - _SHINGLE_SIZE + 1))


def _hit_text(hit: Dict[str, Any]) -> str:
    return str(hit.get("search_text") or hit.get("text_preview") or "")


def drop_near_duplicates(hits: List[Dict[str, Any]], *, threshold: float) -> List[Dict[str, Any]]:
    """Keep the better-ranked of any two hits whose word-shingle Jaccard is >= `threshold`.

    Whatever the source: the same conversation imported twice, a prompt pasted into two
    sessions, a page title stored per visit. The vector service already folds rows with
    one content hash; this catches the copies that differ by a timestamp or a word.
    A hit with no text cannot be judged and is kept. `threshold` <= 0 disables the rule.
    """
    if threshold <= 0 or len(hits) < 2:
        return list(hits)
    kept: List[Dict[str, Any]] = []
    kept_shingles: List[FrozenSet[Tuple[str, ...]]] = []
    for hit in hits:
        sh = _shingles(_hit_text(hit))
        if sh and any(
            other and len(sh & other) / len(sh | other) >= threshold - 1e-12
            for other in kept_shingles
        ):
            continue
        kept.append(hit)
        kept_shingles.append(sh)
    return kept


# ---------------------------------------------------------------------------
# Rule 1 — conversation grouping
# ---------------------------------------------------------------------------


def lookup_conversation_ids(conn: Optional[Any], record_ids: Iterable[str]) -> Dict[str, str]:
    """record_id -> conversation_id, read from the index rows themselves.

    Read here rather than carried on the hit, so the id never reaches a packet. A record
    with no conversation id is absent from the result. Any read failure means no
    grouping by conversation, never a failed query.
    """
    ids = sorted({str(r) for r in record_ids if r})
    if conn is None or not ids:
        return {}
    found: Dict[str, str] = {}
    try:
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            for record_id, conversation_id in conn.execute(
                "SELECT record_id, conversation_id FROM signal_embeddings"
                f" WHERE record_id IN ({placeholders})"
                " AND conversation_id IS NOT NULL AND conversation_id <> ''",
                tuple(chunk),
            ):
                found.setdefault(str(record_id), str(conversation_id))
    except Exception:  # noqa: BLE001 — grouping is an improvement, never a dependency
        return {}
    return found


def _instant(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def group_keys(
    hits: Sequence[Dict[str, Any]],
    conversation_ids: Dict[str, str],
    *,
    gap_minutes: float,
) -> List[str]:
    """One group key per hit, in hit order.

    A hit with a conversation id groups with its conversation (namespaced by its stream,
    so two streams that reuse an id stay apart). A hit without one chains into a session
    with the other hits of its stream whose times are each within `gap_minutes` of the
    previous one. An undated hit without a conversation is its own group.
    """
    keys: List[Optional[str]] = [None] * len(hits)
    sessions: Dict[str, List[Tuple[datetime, int]]] = {}
    for idx, hit in enumerate(hits):
        stream = str(hit.get("source_id") or "")
        record_id = str(hit.get("record_id") or "")
        conversation = conversation_ids.get(record_id)
        if conversation:
            keys[idx] = f"c:{stream}:{conversation}"
            continue
        ts = _instant(hit.get("event_at"))
        if ts is None or gap_minutes <= 0:
            keys[idx] = f"r:{idx}:{record_id}"
            continue
        sessions.setdefault(stream, []).append((ts, idx))
    gap_seconds = gap_minutes * 60.0
    for stream, timed in sessions.items():
        timed.sort()
        session = 0
        previous: Optional[datetime] = None
        for ts, idx in timed:
            if previous is not None and (ts - previous).total_seconds() > gap_seconds:
                session += 1
            keys[idx] = f"s:{stream}:{session}"
            previous = ts
    return [str(k) for k in keys]


def group_by_conversation(
    hits: List[Dict[str, Any]],
    keys: Sequence[str],
    *,
    limit: int,
    passages: int,
    bonus: float,
) -> List[Dict[str, Any]]:
    """Order groups by their best hit plus a small lift for distinct supporting hits.

    A group scores 1 / (K + best_rank) * (1 + bonus * min(supporters, 3)), where
    supporters are the group's other hits (already de-duplicated). The first passage of
    every group comes before any group's second, so one long session is one witness and
    the slots it would have taken go to the next conversations. At most `passages` hits
    per group, at most `limit` in all. Hits are returned as they came in.
    """
    groups: Dict[str, List[int]] = {}
    for idx, key in enumerate(keys):
        groups.setdefault(key, []).append(idx)

    def score(members: List[int]) -> float:
        support = min(len(members) - 1, _GROUP_SUPPORT_CAP)
        return (1.0 / (_GROUP_RANK_OFFSET + members[0])) * (1.0 + bonus * support)

    ordered = sorted(groups.values(), key=lambda members: (-score(members), members[0]))
    out: List[Dict[str, Any]] = []
    for depth in range(max(1, passages)):
        for members in ordered:
            if depth < len(members):
                out.append(hits[members[depth]])
                if len(out) >= limit:
                    return out
    return out


def cap_per_conversation(
    hits: List[Dict[str, Any]],
    keys: Sequence[str],
    *,
    limit: int,
    passages: int,
) -> List[Dict[str, Any]]:
    """The ranker's order, with each conversation capped at `passages` hits.

    A long session keeps its best passages where the ranker put them; the rest of it
    gives its slots to the next hits of other conversations, from deeper in the pool.
    No hit moves ahead of a better-ranked one, so a lane head pinned downstream (the
    fusion's vector head) still carries the ranker's best hits.
    """
    taken: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []
    for hit, key in zip(hits, keys):
        if taken.get(key, 0) >= passages:
            continue
        taken[key] = taken.get(key, 0) + 1
        out.append(hit)
        if len(out) >= limit:
            break
    return out


def shape_vector_hits(
    hits: List[Dict[str, Any]],
    *,
    conn: Optional[Any],
    limit: int,
) -> List[Dict[str, Any]]:
    """Rules 3, 2 and 1, in that order, when the switch is on; the list itself when off.

    Floor first so far neighbours never count as support; de-duplicate before grouping
    so a copy is not a second supporting hit. The floor gates only what the wider pool
    adds beyond the lane's own `limit`: grouping pulls deeper hits up when a
    conversation collapses, and those are where far neighbours would pad the lane.
    """
    from ..features.signal.vector_settings import (
        evidence_duplicate_jaccard,
        evidence_floor_margin,
        evidence_group_bonus,
        evidence_group_order,
        evidence_group_passages,
        evidence_session_gap_minutes,
        evidence_shaping_enabled,
    )

    if not evidence_shaping_enabled():
        return hits
    kept = apply_relevance_floor(hits, margin=evidence_floor_margin(), protect=limit)
    kept = drop_near_duplicates(kept, threshold=evidence_duplicate_jaccard())
    passages = evidence_group_passages()
    if passages <= 0:
        return kept[:limit]
    conversations = lookup_conversation_ids(conn, (h.get("record_id") for h in kept))
    keys = group_keys(kept, conversations, gap_minutes=evidence_session_gap_minutes())
    if evidence_group_order() == "rank":
        return cap_per_conversation(kept, keys, limit=limit, passages=passages)
    return group_by_conversation(
        kept,
        keys,
        limit=limit,
        passages=passages,
        bonus=evidence_group_bonus(),
    )


# ---------------------------------------------------------------------------
# Rule 4 — bounded derived share
# ---------------------------------------------------------------------------


def bounded_derived_selection(
    ranked: Sequence[str],
    *,
    is_derived: Callable[[str], bool],
    is_record: Callable[[str], bool],
    share: float,
    cap: int,
) -> List[str]:
    """Select up to `cap` keys in rank order, holding derived keys to `share` of every prefix.

    When the next key is derived and taking it would put more than ceil(share * n)
    derived keys in the first n, the best remaining record key goes there instead. When
    no record key remains, derived keys fill the rest: this bounds a share, it never
    forces one. Keys that are neither (context-only filler) are taken in rank order and
    never promoted. Records keep their order among themselves, and so do derived keys.
    """
    remaining = list(ranked)
    out: List[str] = []
    n_derived = 0
    while remaining and len(out) < cap:
        head = remaining[0]
        if is_derived(head) and n_derived + 1 > math.ceil(share * (len(out) + 1) - 1e-9):
            promoted = next((key for key in remaining if is_record(key)), None)
            if promoted is not None:
                remaining.remove(promoted)
                out.append(promoted)
                continue
        remaining.pop(0)
        out.append(head)
        n_derived += int(is_derived(head))
    return out

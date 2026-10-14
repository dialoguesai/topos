"""The closed lane allowlist for p2c-v1: ranking inside the permitted set, and nowhere else.

Every statistic here is computed over the members of one grant's index, which
holds only P(g). No owner lane (vector search, FTS, the canonical lister, the
query-embedding cache) is imported or reachable, so no hidden row can enter a
score, a document frequency, an ordering or a timing term. The node-wide FTS5
index is deliberately not used: its bm25() takes IDF and average length from the
whole table, so the order of two permitted records would move with hidden text.

Lanes: `lexical` (Okapi BM25, statistics over P), `vector` (exact cosine over P's
stored chunk vectors, a member scoring its best chunk, a fixed floor). Fusion is
reciprocal-rank fusion. Ties break on event time then the opaque id, never an
internal id. Rerank is off in v1; `rerank` is a hook that sees member texts only.
"""
from __future__ import annotations

import math
from typing import Callable, Sequence

from .search_index import LoadedIndex, Member, tokenize

LANES = ("lexical", "vector")
BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60
VECTOR_FLOOR = 0.25
LANE_DEPTH = 200


TIME_BUCKET_US = {"none": None, "day": 86_400 * 1_000_000, "second": 1}


def _event(member: Member, precision: str = "second") -> int:
    """The tie-break time, never finer than the grant releases: a same-day order under `day`, or any
    order under `none`, would otherwise disclose the time of day the view withholds."""
    bucket = TIME_BUCKET_US[precision]
    if bucket is None or member.event_at_us is None:
        return -1
    return member.event_at_us // bucket


def lexical(index: LoadedIndex, query: str) -> list[tuple[str, float]]:
    terms = sorted(set(tokenize(query)))
    members = index.members
    if not terms or not members:
        return []
    count = len(members)
    average = sum(member.doc_len for member in members) / count or 1.0
    frequency = {term: sum(1 for member in members if term in member.terms) for term in terms}
    scored = []
    for member in members:
        score = 0.0
        for term in terms:
            tf = member.terms.get(term, 0)
            if not tf:
                continue
            idf = math.log(1.0 + (count - frequency[term] + 0.5) / (frequency[term] + 0.5))
            score += idf * tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * member.doc_len / average))
        if score > 0:
            scored.append((member.opaque_id, score))
    return scored


def vector(index: LoadedIndex, query_vector: Sequence[float] | None) -> list[tuple[str, float]]:
    if not query_vector or not index.vectors:
        return []
    norm = math.sqrt(sum(value * value for value in query_vector)) or 1.0
    unit = [value / norm for value in query_vector]
    scored = []
    for member in index.members:
        best = None
        for chunk in index.vectors.get(member.opaque_id, ()):
            if len(chunk) != len(unit):
                continue
            chunk_norm = math.sqrt(sum(value * value for value in chunk)) or 1.0
            similarity = sum(a * b for a, b in zip(unit, chunk)) / chunk_norm
            best = similarity if best is None else max(best, similarity)
        if best is not None and best >= VECTOR_FLOOR:
            scored.append((member.opaque_id, best))
    return scored


def _ranked(index: LoadedIndex, scored: list[tuple[str, float]], precision: str = "second",
            depth: int = LANE_DEPTH) -> list[str]:
    by_id = {member.opaque_id: member for member in index.members}
    scored.sort(key=lambda item: (-item[1], -_event(by_id[item[0]], precision), item[0]))
    return [opaque for opaque, _ in scored[:depth]]


def _fused(index: LoadedIndex, lists: list[list[str]], precision: str) -> list[str]:
    """Reciprocal-rank fusion of the lanes' lists; ties on event time (never finer than released), then the id."""
    fused: dict[str, float] = {}
    for ranked in lists:
        for position, opaque in enumerate(ranked):
            fused[opaque] = fused.get(opaque, 0.0) + 1.0 / (RRF_K + position + 1)
    by_id = {member.opaque_id: member for member in index.members}
    return sorted(fused, key=lambda opaque: (-fused[opaque], -_event(by_id[opaque], precision), opaque))


def within(index: LoadedIndex, lower_us: int, upper_us: int) -> LoadedIndex:
    """The members this request may ever release by time; every statistic is computed over these only.

    A permitted record outside the effective window is not releasable to this grant's
    search, so it must not move the order of one that is (the twin-corpus oracle).
    """
    members = tuple(member for member in index.members
                    if member.event_at_us is not None and lower_us <= member.event_at_us <= upper_us)
    keep = {member.opaque_id for member in members}
    return LoadedIndex(index.basis, index.model, index.dims, members,
                       {opaque: vectors for opaque, vectors in index.vectors.items() if opaque in keep})


def rank(index: LoadedIndex, query: str, query_vector: Sequence[float] | None, *, limit: int, lower_us: int,
         upper_us: int, precision: str = "none",
         rerank: Callable[[str, list[str]], list[str]] | None = None, spare: int = 0,
         fill: bool = False) -> list[str]:
    """Opaque ids of releasable-by-time members in fused rank order, at most `limit` of them.

    `spare` (FT168-D6): up to that many further candidates after those, for a walk that passes some by without
    counting them (`search_release.candidate_limits`). The first `limit` are exactly what the call without `spare`
    returns. The spare ones are what follows them in that same order, and then the members each lane ranks below
    its depth, fused the same way: a lane keeps its best LANE_DEPTH, so without this the list ended there however
    many were asked for.

    `fill` (lane 172, answers mode inside a question's time window): when the lanes score fewer members than asked
    for, the rest of the window's members follow, newest first (the same time key as the ties, never finer than
    released, then the id). "What did <owner> do yesterday?" carries almost no words, so the lanes could score
    nothing in a window that holds the day's items: the window itself is the question's subject there. The scored
    members keep their order and come first; only members already inside `lower_us`..`upper_us` are added."""
    index = within(index, lower_us, upper_us)
    scored = [lexical(index, query), vector(index, query_vector)]
    order = _fused(index, [_ranked(index, lane, precision) for lane in scored], precision)
    if rerank is not None:
        known = set(order)
        order = [opaque for opaque in rerank(query, list(order)) if opaque in known]
    if spare <= 0:
        return _filled(index, order[:limit], limit, precision) if fill else order[:limit]
    seen = set(order)
    deeper = _fused(index, [_ranked(index, lane, precision, depth=LANE_DEPTH + spare) for lane in scored], precision)
    ranked = (order + [opaque for opaque in deeper if opaque not in seen])[:limit + spare]
    return _filled(index, ranked, limit + spare, precision) if fill else ranked


def _filled(index: LoadedIndex, ranked: list[str], size: int, precision: str) -> list[str]:
    """`ranked`, then the window's other members newest first, up to `size` in all (`rank`'s `fill`)."""
    if len(ranked) >= size:
        return ranked
    seen = set(ranked)
    rest = sorted((member for member in index.members if member.opaque_id not in seen),
                  key=lambda member: (-_event(member, precision), member.opaque_id))
    return ranked + [member.opaque_id for member in rest[:size - len(ranked)]]

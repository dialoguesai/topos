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

The floor is `VECTOR_FLOOR` on every path but one: answers mode passes its own
(`vector_floor`, lane GD-R, `search_release.answer_vector_floor`). A floor only
decides which members of P the vector lane scores; it never reaches outside P.

Two readings of the query's words, each off unless the node's switch is on
(`search_release.lexical_options`). `plural_fold`: a query word also matches its
English plural or singular in a member's term bag (`_variants`). The forms are
made from the query word alone and looked up in the bags of the members inside
the request's window, so the index is read as stored (no rebuild) and nothing
outside P, or outside the window, can lend a form. `drop_request_words`: the
words a request is phrased with (`REQUEST_WORDS`) are left out of the query when
it has any other word. Both read the query and P's stored bags, nothing else.
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


# The words a request is phrased with ("Can you tell me about the kiln?", "Please summarize the kiln."). Query
# wording, never index terms: the stored term bags are read as they are. A few are also things ("show", "can").
REQUEST_WORDS = frozenset({"can", "could", "would", "should", "how", "about", "tell", "please", "give", "show",
                           "summarize", "summarise", "describe"})

# Words that end in "s" and are no plural, whose remainder is another common word: "news" is not "new".
_NOT_PLURAL = frozenset({"news", "hers"})
_HISS = ("s", "x", "z", "ch", "sh")


def _plurals(word: str) -> tuple[str, ...]:
    """The regular English plurals of `word`: "hobby" to "hobbies", "box" to "boxes", "potato" to "potatoes" and
    "potatos", "project" to "projects". Nothing for a word under three letters."""
    if len(word) < 3:
        return ()
    if word[-1] == "y" and word[-2] not in "aeiou":
        return (word[:-1] + "ies",)
    if word.endswith(_HISS):
        return (word + "es",)
    return (word + "s", word + "es") if word[-1] == "o" else (word + "s",)


def _variants(term: str) -> tuple[str, ...]:
    """`term`, then the other words that are its regular plural or that it is the regular plural of.

    Plural forms only: no stem, no synonym, no irregular plural ("leaf" and "leaves", "person" and "people" stay
    apart). It cannot tell a name from a word: a name and the same name with an "s" meet. Each singular is one whose
    own plural (`_plurals`) is `term`, so the relation is the same in both directions: "caches" finds "cache", "cache"
    finds "caches". Only ASCII letters, three or more of them; anything else (a number, "café", any other script)
    matches itself alone, as before. A word that is the plural of nothing in use ("status" of "statu") gains a form
    no bag holds, which costs one failed lookup.

    It reads spelling, not meaning, so it also joins words that are no plural of each other. Any word of four or
    more letters ending in "s" is read as a possible plural: "lens" also matches "len", "series" looks up "serie"
    and "sery". Any word of three or more letters gains its "-s" or "-es" form: "los" also matches "loses", "add"
    matches "adds". All of it stays among the members the lane was given."""
    if len(term) < 3 or not term.isascii() or not term.isalpha() or term in _NOT_PLURAL:
        return (term,)
    singulars = []
    if len(term) >= 4 and term[-1] == "s":
        singulars = [term[:-1]]
        if term.endswith("es"):
            singulars.append(term[:-2])
        if term.endswith("ies"):
            singulars.append(term[:-3] + "y")
    forms = [term]
    for form in [single for single in singulars if term in _plurals(single)] + list(_plurals(term)):
        if form not in forms and form not in _NOT_PLURAL:
            forms.append(form)
    return tuple(forms)


def lexical(index: LoadedIndex, query: str, *, plural_fold: bool = False,
            drop_request_words: bool = False) -> list[tuple[str, float]]:
    """Okapi BM25 over the members of `index`, every statistic taken from them alone.

    `drop_request_words`: the query's request words (`REQUEST_WORDS`) are left out, so a member that only shares
    "can", "tell" and "about" with the question is not scored and does not outrank the one that names its subject.
    A query made of nothing else keeps them all. A subject that is itself one of those words ("What show did they
    watch?") is left out with them; the vector lane and the query's other words still read it.

    `plural_fold`: each query word stands for itself and its plural forms (`_variants`). A member's count of the
    word is the sum over those forms and its document frequency counts a member holding any of them, so a word asked
    in the singular and written in the plural is one word, scored once. A query that holds both forms of a word
    ("project" and "projects") counts it once too, and a stored form stands for one query word only: the first, in
    sorted order, that reaches it. A query word an earlier one already reached ("lens" after "len") is that same
    word, and its own further forms ("lenses") join it, so no form of a query word goes unread: "len lens" scores
    what "lens" alone scores. Two query words that are different spellings of one plural ("potatos" and
    "potatoes") both reach "potato", and an item that says "potato" is scored for it once, as it is for either
    alone. Off, the default: exact words, as before."""
    words = set(tokenize(query))
    terms = sorted(words - REQUEST_WORDS or words) if drop_request_words else sorted(words)
    members = index.members
    if not terms or not members:
        return []
    count = len(members)
    average = sum(member.doc_len for member in members) / count or 1.0
    if plural_fold:
        groups, group_of = [], {}
        for term in terms:              # sorted, so a singular comes before its own plural and stands for both
            group = group_of.get(term)
            if group is None:
                group = []
                groups.append(group)
            for form in _variants(term):
                if form not in group_of:
                    group_of[form] = group
                    group.append(form)
        counts = [[sum(member.terms.get(form, 0) for form in group) for group in groups] for member in members]
    else:
        counts = [[member.terms.get(term, 0) for term in terms] for member in members]
    frequency = [sum(1 for row in counts if row[position]) for position in range(len(counts[0]))]
    scored = []
    for member, row in zip(members, counts):
        score = 0.0
        for position, tf in enumerate(row):
            if not tf:
                continue
            idf = math.log(1.0 + (count - frequency[position] + 0.5) / (frequency[position] + 0.5))
            score += idf * tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * member.doc_len / average))
        if score > 0:
            scored.append((member.opaque_id, score))
    return scored


def vector(index: LoadedIndex, query_vector: Sequence[float] | None, *,
           vector_floor: float = VECTOR_FLOOR) -> list[tuple[str, float]]:
    """Members whose best chunk scores at least `vector_floor` in cosine. Lane GD-R: answers mode may pass a lower
    floor (`search_release.answer_vector_floor`); every other caller keeps `VECTOR_FLOOR`."""
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
        if best is not None and best >= vector_floor:
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
         fill: bool = False, vector_floor: float = VECTOR_FLOOR, plural_fold: bool = False,
         drop_request_words: bool = False) -> list[str]:
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
    members keep their order and come first; only members already inside `lower_us`..`upper_us` are added.

    `vector_floor` (lane GD-R, answers mode only): the vector lane's cosine floor, `VECTOR_FLOOR` unless the caller
    passes another. It moves which already-permitted members the vector lane scores, never which members are here.

    `plural_fold`, `drop_request_words` (`search_release.lexical_options`): the lexical lane also matches a query
    word's plural forms, and leaves the query's request words out (`lexical`). Both read the query and the term bags
    of the members inside the window, after `within`, and nothing else. The vector lane is not touched."""
    index = within(index, lower_us, upper_us)
    scored = [lexical(index, query, plural_fold=plural_fold, drop_request_words=drop_request_words),
              vector(index, query_vector, vector_floor=vector_floor)]
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

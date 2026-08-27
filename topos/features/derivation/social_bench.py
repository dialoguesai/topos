"""G5 — role shapes from the owner's own recurring work, and THE BENCH.

The bench's brief: *"whom would I put on my bench for the roles I keep needing filled —
demonstrated work, never a job title, ordered by warmth."* Three parts, each honest about
its floor:

  * ROLE SHAPES — recurring themes in the owner's dated work record (journal entries and
    goals joined to their source record's EVENT time — never `created_at`, which is
    extraction time; using it makes every role look born on the day the extractor ran).
  * CANDIDATES — people carrying `net.demonstrated_skill` facts that overlap a role's
    terms. Today that is zero people, because the outward pack ships disabled; the slate
    says so rather than padding.
  * ORDERING — the stored calibrated `warmth_band` (G3), because a bench you cannot reach
    is a list, not a bench.

`roles_without_candidates` is a FIELD, not a footnote: on a node with no capability facts
it is the whole answer, and hiding it would turn an honest empty bench into a broken-looking
one.

Blocking (the R/S/**B** of the plan) is stated as unavailable rather than faked: the node
has no signal for "this work blocks other work", and the plan's own review says this clause
may not be answerable honestly in v1.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

#: A role must recur across at least this many distinct ISO weeks. One busy afternoon is a
#: task; a shape that returns week after week is a role.
MIN_RECURRENCE_WEEKS = 3

#: And carry at least this many evidence records, so a weekly one-liner doesn't rank beside
#: a body of work.
MIN_EVIDENCE = 5

_STOP = frozenset("""a an and are as at be been but by for from get got had has have i im in
is it its just me my n need not of on or our out so than that the their them they this to
up was we were will with you your want wants like more some when what who how all also can
do does did make made new one two would should could really via then over about time today
week day days going go trying try still into after through there because where while been
being other others another each own very much many things thing stuff way ways first next
last people item items use used using run runs running complete completed start started
work working accomplish accomplished accomplishment feel felt feeling think thinking know
knowing see seeing look looking well good better best today tomorrow yesterday""".split())

_WORD = re.compile(r"[a-z][a-z0-9_-]{2,}")


def _terms(text: str) -> List[str]:
    return [w for w in _WORD.findall(str(text or "").lower()) if w not in _STOP]


def build_role_corpus(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """(event date, text, walkable ref) for the owner's own work record.

    Journal entries carry `entry_at` directly. Goals are DATED THROUGH THEIR SOURCE RECORD
    — a goal that cannot be joined to a dated record is dropped, not defaulted, because
    `created_at` is the extraction-time trap the plan's acceptance criteria name.
    """
    out: List[Dict[str, Any]] = []
    try:
        for eid, at, content in conn.execute(
                "SELECT entry_id, entry_at, content FROM journal_entries"
                " WHERE entry_at IS NOT NULL AND content IS NOT NULL"):
            out.append({"date": str(at)[:10], "text": str(content),
                        "ref": {"table": "journal_entries", "record_id": str(eid)}})
    except sqlite3.Error:
        pass
    try:
        for gid, text, at in conn.execute(
                """SELECT g.goal_id, g.goal_text, j.entry_at FROM user_goals g
                   JOIN journal_entries j
                     ON j.entry_id = g.record_id OR j.source_record_id = g.record_id
                   WHERE g.goal_text IS NOT NULL"""):
            out.append({"date": str(at)[:10], "text": str(text),
                        "ref": {"table": "user_goals", "record_id": str(gid)}})
        for gid, text, at in conn.execute(
                """SELECT g.goal_id, g.goal_text, m.event_at FROM user_goals g
                   JOIN conversation_messages m ON m.message_id = g.record_id
                   WHERE g.goal_text IS NOT NULL AND m.event_at IS NOT NULL"""):
            out.append({"date": str(at)[:10], "text": str(text),
                        "ref": {"table": "user_goals", "record_id": str(gid)}})
    except sqlite3.Error:
        pass
    # dedup: the grow connector ingests the same journal through two sources, so identical
    # texts arrive twice and would double every count
    seen: set = set()
    unique = []
    for r in out:
        k = (r["date"], r["text"])
        if r["date"] and len(r["date"]) == 10 and k not in seen:
            seen.add(k)
            unique.append(r)
    return unique


def build_role_shapes_from_clusters(conn: sqlite3.Connection, *,
                                    top_n: int = 8) -> List[Dict[str, Any]]:
    """Role shapes from the engine's OWN topic clusters — the plan's L5-2, as written.

    The term-counting fallback below produced "into / accomplished / things": journal
    furniture, because raw co-occurrence cannot tell how the owner writes from what the
    owner does. The cluster machinery already can — embeddings plus labelling — and the
    `work` dimension's clusters name real shapes ("merge / branch", "relay / engine",
    "layout / settings / client"). Recurrence is measured over the DATED corpus members,
    so a shape earns its place by returning week after week, not by cluster size.
    """
    try:
        rows = conn.execute("""
            SELECT m.cluster_id, t.label, t.label_terms_json,
                   COUNT(DISTINCT strftime('%Y-W%W', j.entry_at)),
                   COUNT(DISTINCT m.record_id)
            FROM topic_cluster_members m
            JOIN topic_clusters t ON t.cluster_id = m.cluster_id AND t.dimension = 'work'
            JOIN journal_entries j
              ON j.entry_id = m.record_id OR j.source_record_id = m.record_id
            WHERE j.entry_at IS NOT NULL
            GROUP BY m.cluster_id ORDER BY 4 DESC, 5 DESC""").fetchall()
    except sqlite3.Error:
        return []
    shapes = []
    for cid, label, terms_json, weeks, evidence in rows:
        if weeks < MIN_RECURRENCE_WEEKS or evidence < MIN_EVIDENCE:
            continue
        try:
            terms = [str(t) for t in json.loads(terms_json or "[]")]
        except (ValueError, TypeError):
            terms = _terms(str(label or ""))
        refs = [{"table": "topic_cluster_members", "record_id": str(r[0])}
                for r in conn.execute(
                    "SELECT record_id FROM topic_cluster_members WHERE cluster_id=?"
                    " LIMIT 12", (cid,))]
        shapes.append({
            "role_shape_id": f"role:{cid}",
            "label": str(label or cid),
            "label_terms": terms or _terms(str(label or "")),
            "recurrence_weeks": int(weeks),
            "evidence_count": int(evidence),
            "score": float(weeks),
            "evidence": refs,
            "self_performed_share": 1.0,
            "blocking_score": None,
        })
    return shapes[:top_n]


def build_role_shapes(conn: sqlite3.Connection, *, top_n: int = 8) -> List[Dict[str, Any]]:
    """Recurring work themes with recurrence and evidence, floors applied.

    Deliberately term-cooccurrence, not embeddings: the labels are crude, but every shape
    is reproducible arithmetic over walkable evidence, which is what the review demanded
    after the role-competence audit found a gold set that graded its own extractor. An
    embedding upgrade (the plan's L5-2 reuse) slots in behind the same interface.
    """
    corpus = build_role_corpus(conn)
    if not corpus:
        return []
    from datetime import date as _date

    def week(d: str) -> str:
        try:
            return _date(int(d[:4]), int(d[5:7]), int(d[8:10])).strftime("%G-W%V")
        except ValueError:
            return ""

    # Document-frequency band, computed from THIS corpus. The first run without it named
    # the owner's roles "into / accomplished / things" — journal template vocabulary that
    # appears in most entries. A term present in more than a fifth of the record is furniture
    # of the writing habit, not a shape of the work; a term in fewer than three records is an
    # incident. Both are measured out rather than stop-listed by hand, so the filter follows
    # the corpus instead of my guesses about it.
    df: Counter = Counter()
    for row in corpus:
        for t in set(_terms(row["text"])):
            df[t] += 1
    n_docs = max(len(corpus), 1)
    def _role_term(t: str) -> bool:
        return 3 <= df[t] <= n_docs * 0.20

    by_term: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"weeks": set(), "refs": [], "co": Counter()})
    for row in corpus:
        ts = [t for t in _terms(row["text"]) if _role_term(t)]
        wk = week(row["date"])
        if not wk:
            continue
        for t in set(ts):
            e = by_term[t]
            e["weeks"].add(wk)
            if len(e["refs"]) < 40:
                e["refs"].append(row["ref"])
            for o in set(ts):
                if o != t:
                    e["co"][o] += 1

    import math

    shapes = []
    for term, e in by_term.items():
        if len(e["weeks"]) < MIN_RECURRENCE_WEEKS or len(e["refs"]) < MIN_EVIDENCE:
            continue
        co = [w for w, _n in e["co"].most_common(4)]
        # Recurrence alone ranked journal furniture first — a filler word recurs weekly by
        # definition. Weeks x idf prefers terms that recur AND discriminate: a term in 4%
        # of records that returns 12 weeks running is a role; one in 15% of records
        # returning 17 weeks is how the owner writes.
        idf = math.log(n_docs / max(df[term], 1))
        shapes.append({
            "role_shape_id": f"role:{term}",
            "label": " / ".join([term] + co[:2]),
            "label_terms": [term] + co,
            "recurrence_weeks": len(e["weeks"]),
            "evidence_count": len(e["refs"]),
            "score": round(len(e["weeks"]) * idf, 2),
            "evidence": e["refs"][:12],
            "self_performed_share": 1.0,   # journal + goals are owner-authored by source
            "blocking_score": None,        # NO SIGNAL EXISTS — stated, never faked
        })
    shapes.sort(key=lambda s: -s["score"])
    # collapse shapes whose term sets nest inside a stronger shape's
    kept: List[Dict[str, Any]] = []
    seen_terms: List[set] = []
    for sh in shapes:
        ts = set(sh["label_terms"])
        if any(len(ts & prev) >= 3 for prev in seen_terms):
            continue
        kept.append(sh)
        seen_terms.append(ts)
        if len(kept) >= top_n:
            break
    return kept


def find_candidates(conn: sqlite3.Connection, role: Dict[str, Any]) -> List[Dict[str, Any]]:
    """People with a DEMONSTRATED skill overlapping the role's terms — never a title."""
    out = []
    try:
        rows = conn.execute(
            """SELECT object_key, payload_json FROM signal_objects
               WHERE ontology_id='net.capability' AND valid_to IS NULL
                 AND json_extract(payload_json,'$.predicate')='net.demonstrated_skill'"""
        ).fetchall()
    except sqlite3.Error:
        return out
    terms = set(role.get("label_terms") or [])
    for key, pj in rows:
        try:
            p = json.loads(pj or "{}")
        except (ValueError, TypeError):
            continue
        val = p.get("value_struct") or {}
        skill_terms = set(_terms(str(val.get("skill") or "")))
        if not (skill_terms & terms):
            continue
        subj = (key or "").split(":")[1] if (key or "").count(":") else ""
        nm = conn.execute("SELECT canonical_name FROM entities WHERE entity_id=?",
                          (subj,)).fetchone()
        band = conn.execute(
            "SELECT warmth_band FROM messenger_dyad_stats WHERE a_person_id=?"
            " OR b_person_id=? LIMIT 1", (subj, subj)).fetchone()
        out.append({"person_id": subj, "name": nm[0] if nm else subj,
                    "skill": val.get("skill"), "basis": val.get("basis"),
                    "warmth_band": band[0] if band else None})
    order = {"warm": 0, "steady": 1, "cooling": 2, "dormant": 3, None: 4, "never_direct": 5}
    out.sort(key=lambda c: order.get(c["warmth_band"], 4))
    return out


def build_bench_slate(conn: sqlite3.Connection) -> Dict[str, Any]:
    """THE BENCH: roles from the owner's own record, candidates by demonstrated skill,
    ordered by warmth — with what is missing stated as data."""
    roles = build_role_shapes_from_clusters(conn)
    basis = "topic_clusters(dimension=work) x dated journal recurrence"
    if not roles:
        # a node whose cluster machinery has not run still gets an answer, marked cruder
        roles = build_role_shapes(conn)
        basis = "term-recurrence fallback (no work-dimension clusters on this node)"
    slate = []
    without: List[str] = []
    for role in roles:
        cands = find_candidates(conn, role)
        slate.append({**{k: role[k] for k in
                         ("role_shape_id", "label", "recurrence_weeks", "evidence_count",
                          "score", "self_performed_share", "blocking_score")},
                      "candidates": cands})
        if not cands:
            without.append(role["label"])
    return {
        "roles": slate,
        "roles_without_candidates": without,
        "coverage": {
            "role_basis": basis,
            "role_substrate": "journal_entries + event-dated user_goals",
            "candidate_substrate": "net.demonstrated_skill facts",
            "blocking_signal": "unavailable — no proxy shipped, stated rather than faked",
        },
    }

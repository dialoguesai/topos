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
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date as _date
from typing import Any, Dict, List, Optional, Set

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


def _iso_week(at: str) -> str:
    """ISO week of a date string, or "" when it cannot be read as one."""
    try:
        return "-W%s" % _date(int(at[:4]), int(at[5:7]), int(at[8:10])).strftime("%V")
    except (ValueError, IndexError):
        return ""


def _terms(text: str) -> List[str]:
    return [w for w in _WORD.findall(str(text or "").lower()) if w not in _STOP]


#: Below this many dated goals the work record is too thin to name a role from, and the
#: whole corpus is used instead — said out loud in `role_basis` rather than assumed.
MIN_WORK_RECORDS = 40


def build_role_corpus(conn: sqlite3.Connection, *,
                      substrate: str = "all") -> List[Dict[str, Any]]:
    """(event date, text, walkable ref) for the owner's own work record.

    Journal entries carry `entry_at` directly. Goals are DATED THROUGH THEIR SOURCE RECORD
    — a goal that cannot be joined to a dated record is dropped, not defaulted, because
    `created_at` is the extraction-time trap the plan's acceptance criteria name.

    `substrate="work"` returns the goals alone. The two record types are both owner-authored
    but they are not one corpus: goals state work, journal entries narrate a life, and their
    vocabularies barely overlap. Pooled, the document frequencies that decide what counts as
    distinctive are computed across two different languages, and a word can look rare-and-
    recurring merely by belonging to the smaller one. Measured on the live node that was not
    a subtlety -- 84% of the records were goals, and every single role the bench named came
    from the other 16%: `little`, `something`, `lot`, `too` and `him` appear in 100% journal
    and zero goals. The bench was describing a diary.
    """
    out: List[Dict[str, Any]] = []
    if substrate != "work":
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
    # Dated through EVERY table that can date a work-cluster member, not just the journal.
    #
    # This read used to join `journal_entries` alone and returned nothing, so the bench fell
    # through to the term-counting fallback and reported "no work-dimension clusters on this
    # node" -- while ten of them sat there naming real work. Measured: all 123 members of the
    # work clusters carry `record_type='journal_entry'` and `source_id='github_activity'`,
    # and NONE is in `journal_entries`. They are commits, keyed `github:owner/repo:sha`, and
    # `activity_events` holds the same commit as `push:owner/repo:sha`. One prefix apart.
    #
    # The lesson is the message, not the join: a fallback that names a missing input should
    # be checked against whether the input is actually missing. It was not.
    #
    # Each path runs on its own, because a node that lacks one of these tables must still be
    # dated by the others -- written as a single UNION, one missing table took the whole
    # query down and silently returned "no roles".
    DATING_PATHS = (
        """SELECT m.cluster_id, m.record_id, j.entry_at FROM topic_cluster_members m
             JOIN journal_entries j
               ON j.entry_id = m.record_id OR j.source_record_id = m.record_id
            WHERE j.entry_at IS NOT NULL""",
        """SELECT m.cluster_id, m.record_id, a.occurred_at FROM topic_cluster_members m
             JOIN activity_events a ON a.source_record_id = m.record_id
            WHERE a.occurred_at IS NOT NULL""",
        # The commit case: `github:owner/repo:sha` here, `push:owner/repo:sha` there.
        """SELECT m.cluster_id, m.record_id, a.occurred_at FROM topic_cluster_members m
             JOIN activity_events a
               ON a.source_record_id = 'push:' || substr(m.record_id, 8)
            WHERE a.occurred_at IS NOT NULL AND m.record_id LIKE 'github:%'""",
    )
    dated: Dict[Any, Dict[str, Any]] = {}
    for sql in DATING_PATHS:
        try:
            rows_for_path = conn.execute(sql).fetchall()
        except sqlite3.Error:
            continue  # this node does not have that table; the others still count
        for cid, record_id, at in rows_for_path:
            if not at:
                continue
            entry = dated.setdefault(cid, {"weeks": set(), "records": set()})
            entry["weeks"].add(str(at)[:4] + _iso_week(str(at)))
            entry["records"].add(str(record_id))
    if not dated:
        return []
    try:
        meta = {cid: (label, terms) for cid, label, terms in conn.execute(
            "SELECT cluster_id, label, label_terms_json FROM topic_clusters"
            " WHERE dimension = 'work'")}
    except sqlite3.Error:
        return []
    rows = [(cid, meta[cid][0], meta[cid][1], len(v["weeks"]), len(v["records"]))
            for cid, v in dated.items() if cid in meta]
    rows.sort(key=lambda r: (-r[3], -r[4], str(r[0])))
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


def build_role_shapes(conn: sqlite3.Connection, *, top_n: int = 8,
                      substrate: str = "work") -> List[Dict[str, Any]]:
    """Recurring work themes with recurrence and evidence, floors applied.

    Deliberately term-cooccurrence, not embeddings: the labels are crude, but every shape
    is reproducible arithmetic over walkable evidence, which is what the review demanded
    after the role-competence audit found a gold set that graded its own extractor. An
    embedding upgrade (the plan's L5-2 reuse) slots in behind the same interface.
    """
    corpus = build_role_corpus(conn, substrate=substrate)
    if len(corpus) < MIN_WORK_RECORDS and substrate == "work":
        corpus = build_role_corpus(conn, substrate="all")
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

    # `records` counts, `refs` keeps a walkable sample. They were the same list, so
    # `evidence_count` reported the SAMPLE CAP: every role above the cap came back as
    # exactly 40 records, which is both wrong and flat -- `topos` has 168.
    by_term: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"weeks": set(), "refs": [], "records": 0, "co": Counter()})
    for row in corpus:
        ts = [t for t in _terms(row["text"]) if _role_term(t)]
        wk = week(row["date"])
        if not wk:
            continue
        for t in set(ts):
            e = by_term[t]
            e["weeks"].add(wk)
            e["records"] += 1
            if len(e["refs"]) < 40:
                e["refs"].append(row["ref"])
            for o in set(ts):
                if o != t:
                    e["co"][o] += 1

    import math

    shapes = []
    for term, e in by_term.items():
        if len(e["weeks"]) < MIN_RECURRENCE_WEEKS or e["records"] < MIN_EVIDENCE:
            continue
        co = [w for w, _n in e["co"].most_common(4)]
        # RECURRENCE x MASS. A role is a shape that both returns and has a body of work
        # behind it, and the ranking has to say so, because recurrence alone cannot: this
        # corpus spans 18 distinct weeks, so the week count saturates -- it ranges 6 to 16
        # across every surviving term, a factor of 2.7 -- while the old `weeks x idf`
        # multiplied it by a quantity that ranges 2.2 to 4.6 and REWARDS RARITY. idf
        # therefore decided the ranking, and among terms that recur nearly every week the
        # rarest won: `place` (23 records, 14 weeks) scored 63.8 against `topos` (248
        # records, 16 weeks) at 34.8. The intent behind idf was right -- filter the
        # furniture of a writing habit -- but the df BAND above already does that, and
        # measured out to 20% of the corpus it keeps `topos` at 11% while dropping what
        # appears in most records. Doing the job twice inverted the answer.
        shapes.append({
            "role_shape_id": f"role:{term}",
            "label": " / ".join([term] + co[:2]),
            "label_terms": [term] + co,
            "recurrence_weeks": len(e["weeks"]),
            "evidence_count": e["records"],
            "evidence_sampled": len(e["refs"]),
            "score": round(len(e["weeks"]) * math.log1p(df[term]), 2),
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


def build_bench_slate(conn: sqlite3.Connection,
                      dataset_id: Optional[str] = None) -> Dict[str, Any]:
    """THE BENCH: roles from the owner's own record, candidates by demonstrated skill,
    ordered by warmth — with what is missing stated as data."""
    roles = build_role_shapes_from_clusters(conn)
    basis = "topic_clusters(dimension=work) x recurrence over their dated source records"
    substrate = "topic_clusters(dimension=work): commits and journal entries"
    if not roles:
        # a node whose cluster machinery has not run still gets an answer, marked cruder
        work_records = len(build_role_corpus(conn, substrate="work"))
        used_work = work_records >= MIN_WORK_RECORDS
        roles = build_role_shapes(conn)
        basis = ("term-recurrence x mass over the work record"
                 if used_work else
                 "term-recurrence x mass over the whole record — the work record is too "
                 "thin here to name a role from on its own")
        substrate = ("event-dated user_goals (%d records)" % work_records if used_work
                     else "journal_entries + event-dated user_goals")
    # Who is near this work at all. Kept out of the per-role slate on purpose: the roles do
    # not separate in embedding space, so a per-role assignment would be an invention.
    try:
        from ...analytics.dataset_resolution import resolve_messaging_dataset
        resolved, _ = resolve_messaging_dataset(conn, dataset_id or "")
        engagement = work_engagement(conn, resolved)
    except Exception as exc:  # noqa: BLE001
        engagement = {"people": [], "coverage": {"reason": f"not computed: {exc}"}}

    slate = []
    without: List[str] = []
    for role in roles:
        cands = find_candidates(conn, role)
        slate.append({**{k: role[k] for k in
                         ("role_shape_id", "label", "recurrence_weeks", "evidence_count",
                          "score", "self_performed_share", "blocking_score")
                         if k in role},
                      "candidates": cands})
        if not cands:
            without.append(role["label"])
    return {
        "roles": slate,
        "roles_without_candidates": without,
        # The candidate half of the request, answered with what the record actually supports.
        # `roles_without_candidates` says nobody is EVIDENCED for a role; this says who is
        # nearest the work. They are different claims and the report keeps them apart.
        "people_close_to_this_work": engagement.get("people", []),
        "engagement_coverage": engagement.get("coverage", {}),
        "coverage": {
            "role_basis": basis,
            "role_substrate": substrate,
            "candidate_substrate": "net.demonstrated_skill facts",
            "blocking_signal": "unavailable — no proxy shipped, stated rather than faked",
            # 1.0 on every role because both substrates are owner-authored BY CONSTRUCTION.
            # It is true, not measured, and it discriminates nothing — said here so the
            # field is never read as evidence that a role is self-performed.
            "self_performed_signal": ("constant 1.0 by construction: the substrate is the "
                                      "owner's own writing, so this separates no role from "
                                      "another"),
        },
    }


# --------------------------------------------------------------------------- G5-2

#: A person needs this many embedded messages before a top-k mean means anything. Below it
#: one stray message about a deployment decides the ranking.
MIN_ENGAGEMENT_MESSAGES = 15

#: How many of a person's closest messages are averaged. The single best message is noise --
#: anyone can mention a database once -- and the mean over everything is dominated by small
#: talk, which is most of what any real conversation is.
ENGAGEMENT_TOP_K = 5


def work_engagement(conn: sqlite3.Connection, dataset_id: str,
                    limit: int = 8) -> Dict[str, Any]:
    """Who talks with the owner about the owner's work — NOT who can do a given role.

    The bench's candidate half asks for people whose demonstrated work maps to a role. That
    cannot be answered on this node: there are zero `net.demonstrated_skill` facts and the
    person-to-work-cluster join returns zero pairs, because roles are built from commits and
    people are known through conversations, and the clustering puts those in disjoint
    dimensions (`work` vs `relationships`) with no overlap at all.

    What CAN be answered, from records the owner already owns: whose conversations sit near
    the owner's work. Both sides are already embedded -- 123 of 123 work-cluster commits and
    4,003 messages, same 384-dimension model -- so this is a read, not a pipeline.

    It is deliberately ONE list rather than one per role, and that is a measured limit, not
    a simplification. The ten role centroids sit at 0.730 mean cosine to each OTHER against
    0.773 within themselves: a separation of 0.043. They are the same person's commit
    messages about the same codebase, and at that granularity they are one region, not ten.
    Ranking a person against an individual role produced lifts of two hundredths and the
    same three people at the top of every role -- message volume wearing a job title. So the
    region is scored whole, and the report says what it is.

    Ordered by WARMTH, as the request asks: a warm second-best is worth more than a cold
    ideal, and a person you cannot reach is not on a bench.
    """
    from ...analytics.person_graph import attach_closeness, build_person_nodes
    from ...features.signal.vector_codec import decode_vector

    def _unit(values: List[float]) -> Optional[List[float]]:
        total = math.sqrt(sum(v * v for v in values))
        return [v / total for v in values] if total else None

    def _vectors(sql: str, args: tuple = ()) -> Dict[str, List[float]]:
        out: Dict[str, List[float]] = {}
        try:
            rows = conn.execute(sql, args).fetchall()
        except sqlite3.Error:
            return out
        for record_id, blob, fmt in rows:
            try:
                unit = _unit(decode_vector(blob, str(fmt or "f32")))
            except Exception:  # noqa: BLE001 — a single bad vector is not an outage
                continue
            if unit:
                out[str(record_id)] = unit
        return out

    commits = _vectors(
        "SELECT record_id, vector_blob, vector_format FROM signal_embeddings"
        " WHERE source_id = 'github_activity' AND vector_blob IS NOT NULL")
    try:
        members = [str(r[0]) for r in conn.execute(
            "SELECT m.record_id FROM topic_cluster_members m JOIN topic_clusters t"
            " ON t.cluster_id = m.cluster_id AND t.dimension = 'work'")]
    except sqlite3.Error:
        members = []
    # The same commit is keyed `github:owner/repo:sha` by the clusters and, in places,
    # `github:push:owner/repo:sha` by the embeddings. Both are tried; neither is canonical.
    region = [commits[k] for k in
              (c for m in members for c in (m, "github:push:" + m[7:]) if c in commits)]
    if not region:
        return {"people": [], "coverage": {
            "reason": "no embedded work records on this node yet"}}
    centroid = _unit([sum(col) / len(region) for col in zip(*region)])
    if not centroid:
        return {"people": [], "coverage": {"reason": "work region has no direction"}}

    message_vectors = _vectors(
        "SELECT se.record_id, se.vector_blob, se.vector_format FROM signal_embeddings se"
        " WHERE se.vector_blob IS NOT NULL AND se.record_type = 'conversation_message'")
    if not message_vectors:
        return {"people": [], "coverage": {"reason": "no embedded conversations"}}

    nodes = build_person_nodes(conn, dataset_id)
    attach_closeness(conn, dataset_id, nodes)
    by_key: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        if node.get("is_owner"):
            continue
        for key in node.get("messenger_keys", []):
            by_key.setdefault(str(key), node)

    from ...analytics.messenger_directed import SELF_KEY, load_messages

    try:
        rows = load_messages(conn, dataset_id)
    except sqlite3.Error:
        rows = []
    # Everyone in a conversation owns its messages, the owner excluded — they are in every
    # conversation, so counting them would make every subject universal.
    conv_people: Dict[str, Set[str]] = {}
    conv_messages: Dict[str, List[str]] = {}
    for conv, message_id, sender, _at, from_self, _src, _reply in rows:
        conv_messages.setdefault(str(conv), []).append(str(message_id))
        if from_self or not sender or str(sender) == SELF_KEY:
            continue
        node = by_key.get(str(sender))
        if node:
            conv_people.setdefault(str(conv), set()).add(str(node["node_id"]))

    scores: Dict[str, List[float]] = {}
    for conv, node_ids in conv_people.items():
        for message_id in conv_messages.get(conv, ()):  # noqa: B007
            vector = message_vectors.get(message_id)
            if not vector:
                continue
            similarity = sum(a * b for a, b in zip(vector, centroid))
            for node_id in node_ids:
                scores.setdefault(node_id, []).append(similarity)

    label = {str(n["node_id"]): n for n in nodes}
    people: List[Dict[str, Any]] = []
    for node_id, sims in scores.items():
        if len(sims) < MIN_ENGAGEMENT_MESSAGES:
            continue
        top = sorted(sims, reverse=True)[:ENGAGEMENT_TOP_K]
        node = label.get(node_id) or {}
        people.append({
            "node_id": node_id,
            "name": node.get("label"),
            "needs_name": bool(node.get("needs_name")),
            "engagement": round(sum(top) / len(top), 4),
            "messages_considered": len(sims),
            "closeness": node.get("closeness"),
            "tie_state": node.get("tie_state"),
            "basis": "their conversations with you sit near the work your commits describe",
        })
    if not people:
        # Two very different silences, and the first version reported both as the second --
        # a node with no people at all was told nobody talks enough, which sends the reader
        # looking for a threshold problem that is not there.
        if not by_key:
            reason = "no messaging people are known on this node yet"
        elif not scores:
            reason = "no conversation with a known person has an embedded message"
        else:
            reason = (f"the closest correspondent has "
                      f"{max(len(v) for v in scores.values())} embedded messages; "
                      f"{MIN_ENGAGEMENT_MESSAGES} are needed to rank one")
        return {"people": [], "coverage": {"reason": reason}}

    ranked = sorted(people, key=lambda p: -float(p["engagement"] or 0))
    median = ranked[len(ranked) // 2]["engagement"]
    # WARMTH decides the order, as the request asks. Engagement decides who is on the list.
    shortlist = sorted(ranked[:limit],
                       key=lambda p: (-(p["closeness"] or 0), -float(p["engagement"] or 0)))
    return {
        "people": shortlist,
        "coverage": {
            "scored": len(ranked),
            "median_engagement": median,
            "ordered_by": "warmth, then engagement — a warm second-best beats a cold ideal",
            "means": ("people whose conversations with you sit near your own work, NOT "
                      "people evidenced to be able to do it"),
            "why_not_per_role": (
                "the ten roles are one region in this space, not ten: 0.730 mean cosine "
                "between them against 0.773 within, a separation of 0.043. Scoring a "
                "person against a single role returned lifts of two hundredths and the "
                "same three people at the top of every role, which is message volume "
                "wearing a job title"),
        },
    }

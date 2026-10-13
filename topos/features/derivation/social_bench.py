"""The bench: node-selected people for possible help with source-backed goals.

Purpose, capability, discussion context and readiness are separate claims. Recurring
work themes remain available as diagnostics; they no longer establish help needs.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date as _date
from datetime import datetime, timezone
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
    from .bench_evidence import goal_sources
    for row in goal_sources(conn):
        if row['owner_authored']:
            out.append({'date': row['event_at'][:10], 'text': row['text'],
                        'ref': {'table': 'user_goals', 'record_id': row['goal_id']}})
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


def find_candidates(conn: sqlite3.Connection, role: Dict[str, Any], *, dataset_id: Optional[str] = None) -> List[Dict[str, Any]]:
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
        if not isinstance(p, dict):
            continue
        val = p.get("value_struct") or {}
        if not isinstance(val, dict) or val.get("basis") not in {
                "shipped_artifact", "reviewed_work", "taught_others", "was_consulted"}:
            continue
        skill_terms = set(_terms(str(val.get("skill") or "")))
        if not (skill_terms & terms):
            continue
        subj = p.get("subject_entity_id") or ((key or "").split(":")[1] if (key or "").count(":") else "")
        nm = conn.execute("SELECT canonical_name FROM entities WHERE entity_id=?",
                          (subj,)).fetchone()
        if not nm:
            continue
        try:
            band = conn.execute(
                "SELECT warmth_band FROM messenger_dyad_stats WHERE (a_person_id=? OR b_person_id=?)"
                + (" AND dataset_id=?" if dataset_id else "") + " ORDER BY warmth_band LIMIT 1",
                (subj, subj, dataset_id) if dataset_id else (subj, subj)).fetchone()
        except sqlite3.Error:
            band = None
        out.append({"person_id": subj, "name": nm[0] if nm else subj,
                    "skill": val.get("skill"), "basis": val.get("basis"),
                    "warmth_band": band[0] if band else None,
                    "capability_status": "demonstrated", "readiness_status": "unknown",
                    "fit_status": "inferred_skill_match",
                    "evidence": [{"table": "signal_objects", "object_key": key}]})
    order = {"warm": 0, "steady": 1, "cooling": 2, "dormant": 3, None: 4, "never_direct": 5}
    out.sort(key=lambda c: order.get(c["warmth_band"], 4))
    return out


def build_bench_slate(conn: sqlite3.Connection,
                      dataset_id: Optional[str] = None, *, now=None) -> Dict[str, Any]:
    """THE BENCH: roles from the owner's own record, candidates by demonstrated skill,
    ordered by warmth — with what is missing stated as data."""
    from .bench_evidence import help_purposes, discussion_candidates
    from .bench_readiness import assess_bench_candidate, assess_readiness, POLICY_VERSION
    assessment_time = now or datetime.now(timezone.utc)
    if assessment_time.tzinfo is None:
        assessment_time = assessment_time.replace(tzinfo=timezone.utc)
    roles = help_purposes(conn, now=assessment_time)
    basis = "recent owner-backed goal statements; possible help, not confirmed unmet needs"
    substrate = "canonical event-dated goals from journal, AI and human conversations"
    resolved = dataset_id or ""
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
        cands = find_candidates(conn, role, dataset_id=resolved)
        if not cands:
            cands = discussion_candidates(conn, resolved, role, now=assessment_time)
        for candidate in cands:
            # Current adapters retrieve skills and discussion overlap only. Do not
            # silently interpret that as an exhaustive search for offers or readiness.
            readiness = assess_readiness(person_id=candidate["person_id"],
                purpose_id=role["role_shape_id"], dataset_id=resolved,
                as_of=assessment_time, coverage="not_integrated")
            candidate["readiness"] = readiness
            candidate["readiness_status"] = readiness["status"]
            candidate["bench_qualification"] = assess_bench_candidate(role, candidate, readiness)
        slate.append({**{k: role[k] for k in
                         ("role_shape_id", "label", "recurrence_weeks", "evidence_count",
                          "score", "self_performed_share", "blocking_score", "purpose_basis",
                          "need_status", "goal_status", "event_at", "evidence")
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
            "readiness_policy": POLICY_VERSION,
            "readiness_adapter": "not_integrated",
            "ready_candidate_count": sum(c["bench_qualification"]["status"] == "ready"
                                         for role in slate for c in role["candidates"]),
            "role_basis": basis,
            "role_substrate": substrate,
            "candidate_substrate": "demonstrated-skill facts, otherwise explicitly unverified discussion partners",
            "purpose_window_days": 60,
            "readiness_signal": "unknown unless separately evidenced; warmth is not willingness",
            "purpose_status": "possible help for recorded goals; completion and priority unverified",
            "blocking_signal": "unavailable — no proxy shipped, stated rather than faked",
            "self_performed_signal": "unknown; owner authorship does not prove performed work",
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
    """Work-wide conversation similarity, not purpose fit or demonstrated capability.

    Use embeddings from one compatible space. GitHub provenance identifies work even
    when topic-cluster dimensions change. Coverage describes the current read rather
    than copying measurements from a historical corpus.
    """
    from ...analytics.person_graph import attach_closeness, build_person_nodes
    from ...features.signal.vector_codec import decode_vector

    def _unit(values: List[float]) -> Optional[List[float]]:
        total = math.sqrt(sum(v * v for v in values))
        return [v / total for v in values] if total else None

    # Never compare embeddings from different models or dimensions, even when their
    # numeric lengths happen to agree. Legacy fixtures without metadata retain one space.
    space = None
    try:
        space = conn.execute("SELECT model,provider,dims FROM signal_embeddings WHERE source_id='github_activity' AND vector_blob IS NOT NULL GROUP BY model,provider,dims ORDER BY COUNT(*) DESC,model,provider,dims LIMIT 1").fetchone()
    except sqlite3.Error:
        pass

    def _vectors(sql: str, args: tuple = ()) -> Dict[str, List[float]]:
        if space is not None:
            sql += " AND model IS ? AND provider IS ? AND dims IS ?"
            args += tuple(space)
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
    # Source type establishes the work region; cluster taxonomy may change independently.
    region = list(commits.values())
    if not region:
        return {"people": [], "coverage": {
            "reason": "no embedded work records on this node yet"}}
    dimensions = Counter(map(len, region)).most_common(1)[0][0]
    region = [v for v in region if len(v) == dimensions]
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
            if not vector or len(vector) != len(centroid):
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
            "why_not_per_role": "Work-wide conversation similarity does not establish purpose-specific capability.",
        },
    }

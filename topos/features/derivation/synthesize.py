"""R3 synthesizers (shadow scope): no-LLM inferred facts from ACCUMULATED evidence.

This is the "facts born from many records" path: an inferred fact's source_refs is a
SET — the contributing records (sampled, capped) plus any contributing FACTS (as
signal_objects refs), so `how did this become a fact` is answerable by walking refs.
"""
from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .packs import Pack
from .writer import DerivationWriter

MIN_DAYS_CHRONOTYPE = 21
MIN_WEEKS_BLOCK = 3


def _authored_events(conn: sqlite3.Connection) -> List[Tuple[str, str, str]]:
    """(iso_ts, table, record_id) for owner-authored activity."""
    out = []
    for r in conn.execute("SELECT entry_at, entry_id FROM journal_entries WHERE entry_at IS NOT NULL"):
        out.append((str(r[0]), "journal_entries", str(r[1])))
    for r in conn.execute(
            "SELECT event_at, message_id FROM conversation_messages WHERE actor_role='authored' AND event_at IS NOT NULL"):
        out.append((str(r[0]), "conversation_messages", str(r[1])))
    return out


def _parse(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None


def synthesize_rhythm(conn: sqlite3.Connection, w: DerivationWriter, pack: Pack, owner: str) -> List[Dict[str, Any]]:
    events = [(dt, t, rid) for ts, t, rid in _authored_events(conn) if (dt := _parse(ts))]
    days = {dt.date() for dt, _, _ in events}
    results = []
    if len(days) >= MIN_DAYS_CHRONOTYPE:
        hours = Counter(dt.hour for dt, _, _ in events)
        n = sum(hours.values())
        early = sum(hours[h] for h in range(5, 9)) / n
        late = (sum(hours[h] for h in range(22, 24)) + sum(hours[h] for h in range(0, 3))) / n
        if early > 0.12 and early > 2 * late:
            val = "early_bird"
        elif late > 0.12 and late > 2 * early:
            val = "night_owl"
        elif early > 0.10 and late > 0.10:
            val = "flexible"
        else:
            val = "irregular"
        sample = [{"table": t, "record_id": rid} for _, t, rid in events[:20]]
        r = w.assert_pack_fact(pack=pack, predicate="behavior.chronotype", subject_entity_id=owner,
                               value=val, actor_role="synthesis", source_refs=sample,
                               confidence=min(0.9, 0.5 + n / 2000))
        results.append({"predicate": "behavior.chronotype", "value": val,
                        "evidence": {"events": n, "days": len(days), "early_share": round(early, 3),
                                     "late_share": round(late, 3)}, "outcome": r["outcome"]})
    # routine blocks: (dow, 2h-bucket) recurring across >= MIN_WEEKS_BLOCK distinct weeks
    buckets: Dict[Tuple[int, int], set] = defaultdict(set)
    refs: Dict[Tuple[int, int], List] = defaultdict(list)
    for dt, t, rid in events:
        b = (dt.weekday(), dt.hour // 2 * 2)
        buckets[b].add(dt.isocalendar()[:2])
        refs[b].append({"table": t, "record_id": rid})
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    top = sorted(((b, wk) for b, wk in buckets.items() if len(wk) >= MIN_WEEKS_BLOCK),
                 key=lambda x: -len(x[1]))[:3]
    for (dow, hr), weeks in top:
        label = f"{dows[dow]} {hr:02d}:00-{hr+2:02d}:00"
        val = {"label": label, "time_of_week": label, "activity_kind": "authored_activity"}
        r = w.assert_pack_fact(pack=pack, predicate="behavior.routine_block", subject_entity_id=owner,
                               value=val, actor_role="synthesis", source_refs=refs[(dow, hr)][:20],
                               confidence=min(0.85, 0.4 + len(weeks) / 20))
        results.append({"predicate": "behavior.routine_block", "value": label,
                        "evidence": {"distinct_weeks": len(weeks)}, "outcome": r["outcome"]})
    return results


def synthesize_closeness(conn: sqlite3.Connection, w: DerivationWriter, pack: Pack, owner: str,
                         *, window_days: Optional[int] = 90, now=None) -> List[Dict[str, Any]]:
    """`rel.closeness_tier` — the pack's declared graph_labeling lens.

    Inputs are the two the lens declares: the `communicates_with` edges give WHO,
    and `comms_stats` gives frequency, initiation balance, recency and channels.
    An earlier cut used the edges alone, which made the tier a decayed-volume rank
    with a relationship word on it — and the live edge ranking put "self" first
    (weight 2772) and a bare phone number second, so it would have written
    `{person: "self", tier: "inner_circle"}` and named a handle as a person.

    Guards, in order: the owner is never their own circle; a name with no letters
    is an identifier, not a person; a blackholed person stays erased.
    """
    from .comms_stats import comms_stats, looks_like_a_person_name

    stats = comms_stats(conn, window_days=window_days, now=now)
    blocked = _blackholed(conn)

    rows = conn.execute("""
        SELECT e.edge_id, e.weight,
               CASE WHEN e.src_entity_id=? THEN e.dst_entity_id ELSE e.src_entity_id END AS other
        FROM entity_edges e
        WHERE e.edge_type='communicates_with' AND e.valid_to IS NULL
          AND (e.src_entity_id=? OR e.dst_entity_id=?) ORDER BY e.weight DESC""",
        (owner, owner, owner)).fetchall()

    scored = []
    for edge_id, weight, other in rows:
        if other == owner:
            continue
        ent = conn.execute(
            "SELECT canonical_name, entity_type, COALESCE(is_self,0) FROM entities WHERE entity_id=?",
            (other,)).fetchone()
        if not ent or ent[1] != "person" or int(ent[2] or 0) == 1:
            continue
        name = str(ent[0] or "")
        if not looks_like_a_person_name(name) or _norm_name(name) in blocked:
            continue
        st = stats.get(other)
        if not st:
            continue                      # no interaction inside the evidence window
        total = st["inbound"] + st["outbound"]
        if total < 2:
            continue                      # a single message is not a relationship
        # Balanced exchange counts for more than raw volume: 1.0 at an even split,
        # 0.0 when one side does all the talking.
        reciprocity = 1.0 - min(1.0, abs(0.5 - st["initiation_balance"]) * 2)
        volume = math.log1p(total)
        intimacy = st["one_to_one_share"]
        # Intimacy is weighted lightly (0.7-1.0, not 0.5-1.0) on purpose: it comes
        # from `conversation_participants`, which on this node carries 206 rows under
        # retired ids against 70 canonical, so "this thread is a group" is a weaker
        # claim than the counts are. At the old weight one person with 201 balanced
        # group messages fell below another with 25 private ones.
        score = volume * (0.5 + 0.5 * reciprocity) * (0.7 + 0.3 * intimacy)
        scored.append((edge_id, other, name, st, total, score))

    scored.sort(key=lambda r: (-r[5], r[2]))
    n = len(scored)
    results: List[Dict[str, Any]] = []
    for i, (edge_id, other, name, st, total, score) in enumerate(scored):
        pct = (i + 1) / n if n else 1.0
        tier = ("inner_circle" if pct <= 0.10 else
                "close" if pct <= 0.35 else
                "regular" if pct <= 0.75 else "peripheral")
        val = {"person": name, "tier": tier}
        # Confidence tracks how much evidence there is, and never saturates the way
        # `min(0.85, 0.5 + weight/200)` did — live weights reach 2772, so every row
        # pinned to 0.85 and the number carried nothing.
        confidence = round(min(0.9, 0.35 + math.log1p(total) / 12.0), 3)
        r = w.assert_pack_fact(
            pack=pack, predicate="rel.closeness_tier", subject_entity_id=owner,
            value=val, actor_role="synthesis",
            source_refs=[{"table": "entity_edges", "record_id": edge_id,
                          "note": (f"{st['inbound']} in / {st['outbound']} out over "
                                   f"{window_days}d, balance {st['initiation_balance']}, "
                                   f"1:1 share {st['one_to_one_share']}, "
                                   f"last {st['last_contact']}")}],
            confidence=confidence, object_entity_id=other)
        results.append({"predicate": "rel.closeness_tier", "person": name, "tier": tier,
                        "inbound": st["inbound"], "outbound": st["outbound"],
                        "score": round(score, 2), "outcome": r["outcome"]})
    return results


def _norm_name(name: str) -> str:
    try:
        from ..lifecycle.blackhole import normalize_entity_name
        return str(normalize_entity_name(name) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return str(name or "").strip().lower()


def _blackholed(conn: sqlite3.Connection) -> set:
    try:
        from ..lifecycle.blackhole import blackholed_name_terms
        return {str(t).strip().lower() for t in (blackholed_name_terms(conn) or set())}
    except Exception:  # noqa: BLE001
        try:
            return {str(r[0]).strip().lower()
                    for r in conn.execute("SELECT normalized_name FROM entity_blackholes")}
        except sqlite3.Error:
            return set()


def synthesize_trajectory(conn: sqlite3.Connection, w: DerivationWriter, pack: Pack, owner: str) -> List[Dict[str, Any]]:
    """Trajectory synthesis (work.career declares it, min_evidence 3): venture
    history and professional visibility DERIVED from accumulated career events,
    projects and research output — never from one record. Evidence sets ride in
    source_refs; valid_from anchors to the NEWEST completing evidence (the
    owner's accumulation rule, 2026-08-26)."""
    import json as _json

    rows = conn.execute(
        "SELECT object_id, object_key, payload_json, valid_from FROM signal_objects"
        " WHERE object_type='fact' AND valid_to IS NULL AND ontology_id='work.career'"
        f" AND object_key LIKE 'fact:{owner}:%'").fetchall()
    events, projects, research, shapes = [], [], [], []
    refs = []
    newest = None
    for oid, key, pj, vf in rows:
        try:
            p = _json.loads(pj or "{}")
        except (ValueError, TypeError):
            continue
        pred = str(p.get("predicate") or "")
        val = p.get("value_struct") or {}
        refs_row = p.get("source_refs") or []
        if pred == "work.career_event":
            events.append(val); refs += refs_row
        elif pred == "work.project":
            projects.append(val); refs += refs_row
        elif pred == "work.research_output":
            research.append(val); refs += refs_row
        elif pred == "work.employment_shape":
            shapes.append(str(p.get("object_value") or ""))
        d = str(vf or "")[:10]
        if d and (newest is None or d > newest):
            newest = d
    results: List[Dict[str, Any]] = []
    evidence_n = len(events) + len(projects)
    if evidence_n < 3:
        return results

    # venture_history: founder shape + started/closed_venture events + project volume
    ev_names = {str(e.get("event")) for e in events}
    if "founder" in shapes or "started_venture" in ev_names:
        if "closed_venture" in ev_names:
            vh = "exited" if "exited" in ev_names else "founded_once"
        elif "founder" in shapes and len(projects) >= 5:
            vh = "founded_once"
        else:
            vh = "side_hustle"
        r = w.assert_pack_fact(pack=pack, predicate="work.venture_history",
                               subject_entity_id=owner, value=vh, actor_role="synthesis",
                               source_refs=refs[:12], confidence=0.6,
                               event_date=newest)
        results.append({"predicate": "work.venture_history", "value": vh,
                        "outcome": r.get("outcome"), "evidence": evidence_n})

    # professional_visibility: research output / public shipping breadth
    shipped = sum(1 for p in projects if str(p.get("status")) == "shipped")
    if research or shipped >= 3:
        vis = "industry_recognized" if len(research) >= 3 else "locally_known"
        r = w.assert_pack_fact(pack=pack, predicate="work.professional_visibility",
                               subject_entity_id=owner, value=vis, actor_role="synthesis",
                               source_refs=refs[:12], confidence=0.55,
                               event_date=newest)
        results.append({"predicate": "work.professional_visibility", "value": vis,
                        "outcome": r.get("outcome"), "evidence": evidence_n})
    return results


#: predicate -> (implementation, accepts a window argument)
#:
#: Keyed on PREDICATE rather than on `kind`, because kind does not identify an
#: implementation: 19 declarations share kind "pattern" and no two of them compute
#: the same thing. Packs declare 55 lenses and three are implemented, so the
#: dispatcher must skip the rest as a normal outcome rather than an error.
_LENS_IMPLS = {
    "behavior.chronotype": (synthesize_rhythm, False),
    "behavior.routine_block": (synthesize_rhythm, False),
    "rel.closeness_tier": (synthesize_closeness, True),
    "work.venture_history": (synthesize_trajectory, False),
    "work.professional_visibility": (synthesize_trajectory, False),
}


def run_pack_lenses(conn: sqlite3.Connection, w: DerivationWriter, pack: Pack, owner: str,
                    *, now=None) -> Dict[str, Any]:
    """Run every implemented producer lens a pack declares.

    Packs have carried `synthesis[]` since the catalog was written and `Pack.lenses`
    documents itself as "what the runtime will dispatch on" — but nothing dispatched.
    `synthesize_closeness` had zero callers, so `rel.closeness_tier` sat empty while
    facts_direct asked for it on every closeness question and the query layer answered
    from a volume rank instead.

    Reconcilers are not run here: they open a review and assert nothing, which is a
    different contract and a different surface.
    """
    ran: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    seen_impls = set()
    for lens in (pack.lenses or []):
        if not lens.is_producer:
            skipped.append({"kind": lens.kind, "reason": "reconciler"})
            continue
        impl = None
        for predicate in lens.predicates:
            if predicate in _LENS_IMPLS:
                impl = (predicate, *_LENS_IMPLS[predicate])
                break
        if impl is None:
            skipped.append({"kind": lens.kind, "predicates": lens.predicates,
                            "reason": "declared, not implemented"})
            continue
        predicate, func, takes_window = impl
        if func in seen_impls:
            continue                       # one computation can fill several predicates
        seen_impls.add(func)
        kwargs = {"now": now} if takes_window else {}
        if takes_window:
            # The lens's own evidence floor, honoured rather than hardcoded: this is
            # what "min_evidence: 90d" was declared for.
            kwargs["window_days"] = lens.min_evidence.days
        try:
            out = func(conn, w, pack, owner, **kwargs)
        except Exception as exc:  # noqa: BLE001 — one lens must not sink the pack
            skipped.append({"kind": lens.kind, "predicates": lens.predicates,
                            "reason": f"error: {type(exc).__name__}: {exc}"})
            continue
        ran.append({"kind": lens.kind, "predicate": predicate, "facts": len(out or []),
                    "window_days": kwargs.get("window_days"), "results": out or []})
    return {"pack": pack.pack, "ran": ran, "skipped": skipped,
            "facts_written": sum(r["facts"] for r in ran)}

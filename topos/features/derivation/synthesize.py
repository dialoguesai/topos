"""R3 synthesizers (shadow scope): no-LLM inferred facts from ACCUMULATED evidence.

This is the "facts born from many records" path: an inferred fact's source_refs is a
SET — the contributing records (sampled, capped) plus any contributing FACTS (as
signal_objects refs), so `how did this become a fact` is answerable by walking refs.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
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


def reanchor_closeness_facts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Re-date `rel.closeness_tier` facts that anchored to the synthesis clock.

    Before the lens passed `event_date`, every tier it wrote took the run time
    as its anchor -- on the node this was found on, all 26 landed inside ONE
    SECOND, and the graph then showed every ranked person as active that moment.

    Re-running the lens does NOT fix them. The VALUE is unchanged, so
    `assert_pack_fact` returns noop/corroborated, and neither path touches
    `valid_from`. Only the anchor was ever wrong, so this corrects it in place
    from the same evidence the lens now reads -- the dyad's last message.

    Facts whose person cannot be matched back to a dyad are left alone rather
    than guessed at: a wrong date is worse than the honest one already stored.
    Edges pick the new anchors up on the next graph rebuild, which is why the
    manifest runs this BEFORE `entity_graph`.
    """
    report = {"examined": 0, "reanchored": 0, "unmatched": 0}
    try:
        cols = {str(r[1]) for r in conn.execute(
            "PRAGMA table_info(messenger_dyad_stats)").fetchall()}
    except sqlite3.Error:
        return report                  # no rail on this node
    if "last_ts" not in cols:
        return report                  # nothing better than what is stored

    from .person_bridge import handle_to_entity, normalise_handle

    by_handle = handle_to_entity(conn)
    newest: Dict[str, str] = {}
    for a_key, b_key, last_ts in conn.execute(
        "SELECT a_key, b_key, last_ts FROM messenger_dyad_stats"
        " WHERE involves_self=1 AND peer_class='human' AND last_ts IS NOT NULL"
    ).fetchall():
        stamp = str(last_ts or "").strip()
        if not stamp:
            continue
        for key in (a_key, b_key):
            entity_id = by_handle.get(normalise_handle(str(key or "")))
            if entity_id and stamp > newest.get(entity_id, ""):
                newest[entity_id] = stamp

    rows = conn.execute(
        "SELECT object_id, payload_json, valid_from FROM signal_objects"
        " WHERE object_type='fact' AND valid_to IS NULL"
        " AND json_extract(payload_json,'$.predicate')='rel.closeness_tier'"
    ).fetchall()
    updates = []
    for object_id, payload_json, valid_from in rows:
        report["examined"] += 1
        try:
            subject = json.loads(payload_json or "{}").get("object_entity_id") or ""
        except (TypeError, ValueError):
            subject = ""
        stamp = newest.get(str(subject))
        if not stamp:
            report["unmatched"] += 1
            continue
        if str(valid_from or "")[:19] == stamp[:19]:
            continue                   # already anchored to the evidence
        updates.append((stamp, object_id))
    for stamp, object_id in updates:
        conn.execute(
            "UPDATE signal_objects SET valid_from=? WHERE object_id=?", (stamp, object_id))
        report["reanchored"] += 1
    if updates:
        conn.commit()
    return report


def _date_from_gap(gap_days, now=None) -> Optional[str]:
    """Days-since-last-contact -> an ISO date, for rails without `last_ts`."""
    try:
        days = float(gap_days)
    except (TypeError, ValueError):
        return None
    if days < 0 or days > 36500:
        return None
    base = now or datetime.now(timezone.utc)
    if isinstance(base, str):
        try:
            base = datetime.fromisoformat(base.replace("Z", "+00:00"))
        except ValueError:
            return None
    return (base - timedelta(days=days)).date().isoformat()


def synthesize_closeness(conn: sqlite3.Connection, w: DerivationWriter, pack: Pack, owner: str,
                         *, window_days: Optional[int] = 90, now=None) -> List[Dict[str, Any]]:
    """`rel.closeness_tier` — the pack's declared graph_labeling lens.

    Reads `messenger_dyad_stats`, the L1 directed-analytics rail, rather than
    recomputing interaction from messages. The 2026-08-25 owner decision was that
    the rail is the ANALYTICAL view and closeness_tier the durable FACT view, and
    that they "share evidence but are not merged" — sharing means exactly one of
    them derives it. The rail also knows things a second pass would not: session
    initiation, reply latency, reciprocal streaks, drift, and which peers are
    automated (29 of 180 dyads on this node).

    `window_days` is kept for the lens's declared floor but the rail's stats are
    lifetime rollups; recency enters through `recent_gap_days` instead, which is a
    better answer to "is this live?" than a hard cut that erases a decade-old
    friendship because nobody texted this quarter.
    """
    from .person_bridge import handle_to_entity, looks_like_a_person_name

    by_handle = handle_to_entity(conn)
    blocked = _blackholed(conn)
    # `last_ts` is probed rather than selected outright. The except below turns any
    # SQL error into "the rail has not run yet" and returns NO facts, so naming a
    # column an older rail lacks would not degrade the lens -- it would silently
    # delete it, on exactly the nodes least likely to notice.
    try:
        cols = {str(r[1]) for r in conn.execute(
            "PRAGMA table_info(messenger_dyad_stats)").fetchall()}
    except sqlite3.Error:
        cols = set()
    has_last_ts = "last_ts" in cols
    try:
        rows = conn.execute(
            "SELECT a_key, b_key, total_msgs, balance, reciprocal_periods,"
            "       longest_reciprocal_streak_months, recent_gap_days, tie_state,"
            f"      {'last_ts' if has_last_ts else 'NULL'}"
            "  FROM messenger_dyad_stats"
            " WHERE involves_self=1 AND peer_class='human'").fetchall()
    except sqlite3.Error:
        return []                      # the rail has not run yet

    scored = []
    per_entity: Dict[str, Dict[str, Any]] = {}
    for a_key, b_key, total, balance, recip_periods, streak, gap, tie_state, last_ts in rows:
        # One side of an owner dyad is the owner; the other is the partner.
        partner_key = b_key if str(a_key).strip().lower() in _SELF_KEYS else a_key
        if str(partner_key).strip().lower() in _SELF_KEYS:
            continue
        if str(tie_state or "") == "broadcast_only":
            continue                   # a channel talking at the owner is not a tie
        entity_id = by_handle.get(_norm_handle(partner_key))
        if not entity_id or entity_id == owner:
            continue
        ent = conn.execute(
            "SELECT canonical_name, entity_type, COALESCE(is_self,0) FROM entities WHERE entity_id=?",
            (entity_id,)).fetchone()
        if not ent or ent[1] != "person" or int(ent[2] or 0) == 1:
            continue
        name = str(ent[0] or "")
        if not looks_like_a_person_name(name) or _norm_name(name) in blocked:
            continue
        total = int(total or 0)
        if total < 2:
            continue                   # a single message is not a relationship
        # One PERSON can hold several handles, and the rail keys dyads by handle:
        # Hotel India has two numbers and arrived as two dyads (105 msgs and 31),
        # which both split her traffic and listed her twice in the same answer.
        acc = per_entity.setdefault(entity_id, {
            "name": name, "total": 0, "bal_weighted": 0.0,
            "recip_periods": 0, "streak": 0, "gap": None, "tie": None,
            "last_ts": None})
        acc["total"] += total
        acc["bal_weighted"] += float(balance or 0.0) * total
        acc["recip_periods"] = max(acc["recip_periods"], int(recip_periods or 0))
        acc["streak"] = max(acc["streak"], int(streak or 0))
        g_here = float(gap) if gap is not None else None
        if g_here is not None and (acc["gap"] is None or g_here < acc["gap"]):
            acc["gap"] = g_here        # most recent contact across her channels
        lt = str(last_ts or "").strip()
        if not lt and gap is not None:
            # An older rail has no `last_ts`. The gap is days-since-last-contact,
            # so the date is recoverable -- approximate, but anchoring to a real
            # position in the past beats anchoring every person to the run clock.
            lt = _date_from_gap(gap, now)
        if lt and (acc["last_ts"] is None or lt > acc["last_ts"]):
            acc["last_ts"] = lt      # newest contact across her channels
        if _TIE_RANK.get(str(tie_state or ""), 9) < _TIE_RANK.get(str(acc["tie"] or ""), 9):
            acc["tie"] = tie_state     # the liveliest channel describes the tie
        continue

    for entity_id, acc in per_entity.items():
        name, total = acc["name"], acc["total"]
        balance = acc["bal_weighted"] / total if total else 0.0
        recip_periods, streak = acc["recip_periods"], acc["streak"]
        gap, tie_state = acc["gap"], acc["tie"]
        last_contact = acc["last_ts"]

        # `balance` is -1..1 and 0 is an even exchange, so |balance| IS the
        # one-sidedness the old cut had to derive from raw counts.
        reciprocity = 1.0 - min(1.0, abs(float(balance or 0.0)))
        persistence = min(1.0, float(streak or 0) / 6.0)
        g = float(gap) if gap is not None else 9999.0
        recency = 1.0 if g <= 14 else (0.7 if g <= 60 else (0.4 if g <= 180 else 0.2))
        tie_mult = {"active": 1.0, "cooling": 0.8, "one_sided": 0.5,
                    "dormant": 0.4}.get(str(tie_state or ""), 0.7)
        score = (math.log1p(total) * (0.4 + 0.6 * reciprocity)
                 * (0.6 + 0.4 * persistence) * recency * tie_mult)
        scored.append((entity_id, name, total, balance, recip_periods, streak,
                       gap, tie_state, score, last_contact))

    scored.sort(key=lambda r: (-r[8], r[1]))
    n = len(scored)
    results: List[Dict[str, Any]] = []
    for i, (entity_id, name, total, balance, recip_periods, streak, gap, tie_state,
            score, last_contact) in enumerate(scored):
        pct = (i + 1) / n if n else 1.0
        tier = ("inner_circle" if pct <= 0.10 else
                "close" if pct <= 0.35 else
                "regular" if pct <= 0.75 else "peripheral")
        confidence = round(min(0.9, 0.35 + math.log1p(total) / 12.0), 3)
        r = w.assert_pack_fact(
            pack=pack, predicate="rel.closeness_tier", subject_entity_id=owner,
            value={"person": name, "tier": tier}, actor_role="synthesis",
            source_refs=[{"table": "messenger_dyad_stats", "record_id": f"{entity_id}",
                          "note": (f"{total} msgs, balance {balance}, "
                                   f"{recip_periods} reciprocal periods, "
                                   f"{streak}mo reciprocal streak, "
                                   f"last contact {gap}d ago, tie {tie_state}")}],
            confidence=confidence, object_entity_id=entity_id,
            # A closeness tier is a STANDING RANK, not an event, so it has no
            # occurrence of its own -- and without an evidence date every tier
            # anchored to the synthesis clock instead. That put all 26 of them on
            # one instant, and the graph then showed every ranked person as active
            # that second: people last spoken to in May and June rendered inside
            # the most recent few days. The dyad's last message is the newest
            # evidence that completed the rank, which is what the writer asks
            # accumulation facts to anchor to.
            event_date=last_contact)
        results.append({"predicate": "rel.closeness_tier", "person": name, "tier": tier,
                        "msgs": total, "balance": balance, "tie_state": tie_state,
                        "score": round(score, 2), "outcome": r["outcome"]})
    return results


#: Both spellings the rail uses for the owner side of a dyad.
_SELF_KEYS = {"self", "me", "owner"}

#: Liveliest first — when one person reaches the owner on several channels, the
#: most active of them describes the tie.
_TIE_RANK = {"active": 0, "cooling": 1, "one_sided": 2, "dormant": 3}


def _norm_handle(raw: Any) -> str:
    from .person_bridge import normalise_handle
    return normalise_handle(raw)


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
    for lens in _declared_lenses(pack):
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
            kwargs["window_days"] = (lens.min_evidence_days
                                     if hasattr(lens, "min_evidence_days")
                                     else lens.min_evidence.days)
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

#: Producer kinds, mirrored rather than imported: `Pack.lenses` and the Lens/PRODUCER_KINDS
#: machinery are not on main yet, and a dispatcher that only works against one session's
#: uncommitted working tree is not a dispatcher. `synthesis[]` — the raw list — has been
#: on every pack since the catalog was written, so read that and use the parsed objects
#: only when they are actually there.
_PRODUCER_KINDS = ("pattern", "disposition", "trajectory", "rhythm", "stylometry",
                   "trend", "graph_labeling")
_DURATION_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


class _RawLens:
    """The subset of a lens this dispatcher needs, read from the raw declaration."""

    def __init__(self, entry: Dict[str, Any]) -> None:
        self.kind = str(entry.get("kind") or "")
        pred = entry.get("predicate")
        preds = entry.get("predicates")
        self.predicates = ([str(p) for p in preds] if isinstance(preds, list)
                           else ([str(pred)] if pred else []))
        self.min_evidence_days = _parse_days(entry.get("min_evidence"))

    @property
    def is_producer(self) -> bool:
        return self.kind in _PRODUCER_KINDS


def _parse_days(value: Any) -> Optional[int]:
    """`min_evidence` ships in three spellings; only durations carry days."""
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return None
    m = re.match(r"^(\d+)\s*([dwmy])$", str(value).strip(), re.I)
    return int(m.group(1)) * _DURATION_DAYS[m.group(2).lower()] if m else None


def _declared_lenses(pack: Pack) -> List[Any]:
    parsed = getattr(pack, "lenses", None)
    if parsed:
        return list(parsed)
    return [_RawLens(e) for e in (getattr(pack, "synthesis", None) or [])
            if isinstance(e, dict)]

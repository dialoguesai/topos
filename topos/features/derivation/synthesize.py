"""R3 synthesizers (shadow scope): no-LLM inferred facts from ACCUMULATED evidence.

This is the "facts born from many records" path: an inferred fact's source_refs is a
SET — the contributing records (sampled, capped) plus any contributing FACTS (as
signal_objects refs), so `how did this become a fact` is answerable by walking refs.
"""
from __future__ import annotations

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


def synthesize_closeness(conn: sqlite3.Connection, w: DerivationWriter, pack: Pack, owner: str) -> List[Dict[str, Any]]:
    rows = conn.execute("""
        SELECT e.edge_id, e.weight,
               CASE WHEN e.src_entity_id=? THEN e.dst_entity_id ELSE e.src_entity_id END AS other
        FROM entity_edges e
        WHERE e.edge_type='communicates_with' AND e.valid_to IS NULL
          AND (e.src_entity_id=? OR e.dst_entity_id=?) ORDER BY e.weight DESC""",
        (owner, owner, owner)).fetchall()
    results = []
    ranked = []
    for edge_id, weight, other in rows:
        ent = conn.execute("SELECT canonical_name, entity_type FROM entities WHERE entity_id=?", (other,)).fetchone()
        if ent and ent[1] == "person" and float(weight or 0) >= 1.0:
            ranked.append((edge_id, float(weight), other, ent[0]))
    for i, (edge_id, weight, other, name) in enumerate(ranked[:20]):
        tier = "inner_circle" if i < 3 else ("close" if i < 8 else "regular")
        val = {"person": name, "tier": tier}
        # evidence set: the comms edge (itself an accumulation) — walkable provenance
        r = w.assert_pack_fact(pack=pack, predicate="rel.closeness_tier", subject_entity_id=owner,
                               value=val, actor_role="synthesis",
                               source_refs=[{"table": "entity_edges", "record_id": edge_id,
                                             "note": f"decayed comms weight {weight:.1f}"}],
                               confidence=min(0.85, 0.5 + weight / 200), object_entity_id=other)
        results.append({"predicate": "rel.closeness_tier", "person": name, "tier": tier,
                        "weight": round(weight, 1), "outcome": r["outcome"]})
    return results

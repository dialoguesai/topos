"""Attention dashboard data (shared by the HTTP route and the WS handler —
the CP signal proxy forwards message_type signal_attention_dashboard here)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict


def attention_dashboard_data(conn: sqlite3.Connection, *, days: int = 14,
                             include_titles: bool = True) -> Dict[str, Any]:
    from .badges import current_badge, earned_badges, rank, worn_badge
    from .readiness import newsletter_readiness

    cutoff = conn.execute(
        "SELECT date(MAX(day), ?) FROM triage_verdicts", (f"-{days} day",)).fetchone()[0]
    verdicts = []
    for row in conn.execute(
            "SELECT day, record_id, canonical_table, verdict, comp_novelty, comp_resid, "
            "knn_surprisal, item_kl, attachment, gen_score, align_mass, visit_count, "
            "engagement_kind, junk, user_label, grounds_json FROM triage_verdicts "
            "WHERE day >= ? ORDER BY day", (cutoff or "1970-01-01",)):
        verdicts.append({
            "day": row[0], "record_id": row[1] if include_titles else None,
            "table": row[2], "verdict": row[3], "comp_novelty": row[4],
            "comp_resid": row[5], "knn_surprisal": row[6], "item_kl": row[7],
            "attachment": row[8], "gen_score": row[9], "align_mass": row[10],
            "visit_count": row[11], "engagement_kind": row[12], "junk": row[13],
            "user_label": row[14],
            "grounds": json.loads(row[15] or "[]"),
        })
    summaries = []
    for (pj,) in conn.execute(
            "SELECT payload_json FROM signal_objects WHERE object_type='attention_summary' "
            "AND valid_to IS NULL ORDER BY object_key"):
        p = json.loads(pj)
        if not include_titles:
            for section in ("surface", "signal", "seeds"):
                for item in p.get(section, []):
                    item.pop("title", None)
        summaries.append(p)
    if include_titles and verdicts:
        ids = [v["record_id"] for v in verdicts]
        titles: Dict[str, str] = {}
        def chunks(seq, n=400):
            for i in range(0, len(seq), n):
                yield seq[i:i + n]
        for tbl, key, col in (
                ("activity_events", "event_id", "title"),
                ("journal_entries", "entry_id", "category || ' @ ' || COALESCE(place_name,'?')"),
                ("ai_chat_messages", "message_id", "substr(COALESCE(content,''),1,80)"),
                ("conversation_messages", "message_id", "substr(COALESCE(content,''),1,80)")):
            try:
                for ch in chunks(ids):
                    q = f"SELECT {key}, {col} FROM {tbl} WHERE {key} IN ({','.join('?' * len(ch))})"
                    for rid, t in conn.execute(q, ch):
                        titles.setdefault(rid, t)
            except sqlite3.OperationalError:
                continue
        for v in verdicts:
            v["title"] = (titles.get(v["record_id"]) or v["table"] or "")[:90]

    intents = [
        json.loads(pj) for (pj,) in conn.execute(
            "SELECT payload_json FROM signal_objects WHERE object_type='declared_intent' "
            "AND valid_to IS NULL")
    ]
    return {
        "days": days,
        "verdicts": verdicts,
        "summaries": summaries,
        "active_intents": [i for i in intents if i.get("status", "active") == "active"],
        "badges": earned_badges(conn),
        "current_badge": current_badge(conn),
        "rank": rank(conn),
        "worn": worn_badge(conn),
        "readiness": newsletter_readiness(conn),
        "disclosure": "owner_only",
    }

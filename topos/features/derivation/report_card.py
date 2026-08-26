"""WA.E derivation report card as CODE (protects: attribution of person-facts
to the correct subject; junk gates; gold retention — PLAN_DERIVATION_WAVE2 §WA.E).

Three scores per pack over a window, computed from the training ledger + owner
verdicts. Any one alone is gameable; together they describe the pipeline:

  acceptance   — verifier-accepted / judged (the pipeline's own precision proxy)
  owner_upheld — 1 - (owner-rejected stored facts / stored facts owner reviewed)
                 (the human-gold correction rate; verified_by_owner counts as upheld)
  reroute_rate — rerouted / judged (attribution catches — visibility, not a gate)

The full junk/attribution/retention batteries remain offline instruments run
against graded corpora; this card is the LIVE, always-on shadow of them.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict


def compute_report_card(conn: sqlite3.Connection, days: int = 30) -> Dict[str, Any]:
    out: Dict[str, Any] = {"window_days": days, "packs": {}}
    try:
        rows = conn.execute(
            "SELECT pack_id, vstatus, COUNT(*) FROM derivation_training_ledger"
            " WHERE ts >= datetime('now', ?) GROUP BY pack_id, vstatus",
            (f"-{int(days)} day",)).fetchall()
    except sqlite3.OperationalError:
        return out
    per: Dict[str, Dict[str, int]] = {}
    for pack_id, vstatus, n in rows:
        per.setdefault(pack_id, {})[str(vstatus)] = int(n)
    for pack_id, counts in per.items():
        judged = sum(counts.values())
        accepted = counts.get("accepted", 0) + counts.get("rerouted", 0)
        card = {
            "judged": judged,
            "acceptance": round(accepted / judged, 3) if judged else None,
            "reroute_rate": round(counts.get("rerouted", 0) / judged, 3) if judged else None,
            "rejected": counts.get("rejected", 0) + counts.get("rejected_majority", 0),
            "grounding_rejects": counts.get("grounding_reject", 0),
        }
        out["packs"][pack_id] = card
    # owner verdicts over stored pack facts (gold)
    try:
        for pack_id, upheld, rejected in conn.execute(
            """SELECT ontology_id,
                      SUM(CASE WHEN json_extract(payload_json,'$.verified_by_owner')=1 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN json_extract(payload_json,'$.excluded_by_owner')=1
                               OR (valid_to IS NOT NULL AND updated_by LIKE '%reject%') THEN 1 ELSE 0 END)
               FROM signal_objects WHERE object_type='fact' AND ontology_id IS NOT NULL
               GROUP BY ontology_id"""):
            card = out["packs"].setdefault(pack_id, {})
            reviewed = int(upheld or 0) + int(rejected or 0)
            card["owner_reviewed"] = reviewed
            card["owner_upheld"] = round(int(upheld or 0) / reviewed, 3) if reviewed else None
    except sqlite3.OperationalError:
        pass
    return out

"""The commitment ledger, owner's half — promises the owner made, per person.

Item 5 of `PLAN_SOCIAL_GRAPH_PERSON_QUALITIES.md`. Obligations & Reciprocity is 0 of 8 on
the network catalog because no lane extracts promises; `obligations.commitments` is that
lane as a pack, and it has never run. Its own routing prefilter passes 2,816 of the owner's
7,011 messages ("I can show you around" counts), which on a local model is a working day
of inference for a ledger. So this module chooses the records itself — the owner's OWN
messages and journal lines that are promise-shaped — and feeds them to the pack runner
with the pack's prefilter off. Measured live: 118 messages.

Owner's half on purpose. `commit.made` is an owner-subject fact ("I owe Dana an intro" is
about my obligation, with Dana as the counterparty), so no outward pack and no consent
row are involved; what the other person promised the owner is their half and waits for
the outward lane. Reliability, when the record can say it, is kept over kept+overdue —
never inferred from silence, exactly as the pack's abstention rules say.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Dict, List

logger = logging.getLogger("topos.features.derivation.commitments")

PACK_ID = "obligations.commitments"
#: Per run, so a first pass is a bounded spend the owner can see the yield of.
MAX_PER_RUN = 40

_VERBS = (r"(send|get|bring|intro|introduce|review|pay|call|share|forward|look|check|do|"
          r"have|make|set|write|drop|grab|book|follow|ping|text|email|put|add|fix|ship|"
          r"finish|deliver|cover|owe|schedule|ask|talk|show|give|help|read)")
#: First-person future commitment with an object, or an explicit promise marker. Tight
#: on purpose: the pack's own exemplars are the recall net; this is the precision one.
PROMISE = re.compile(
    r"\b(i(?:'|’)ll|i will|i(?:'|’)ve got you|i owe you|i promise|remind me to|"
    r"don(?:'|’)t let me forget|i(?:'|’)ll have it)\b.{0,40}\b" + _VERBS + r"\b"
    r"|\bi owe you\b|\bi promise\b|\bremind me to\b",
    re.IGNORECASE,
)


def is_promise_shaped(text: Any) -> bool:
    return bool(PROMISE.search(str(text or "")))


def promise_shaped_records(conn: sqlite3.Connection, *, limit: int = 2000) -> List[Dict[str, Any]]:
    """The owner's own messages and journal lines that read like a promise, newest first.

    Role is set to `authored` here by construction — these rows are `is_from_self` — because
    `conversation_messages.actor_role` is blank on most owner messages and the history walk
    reads blank as `observed`, which no owner-subject pack accepts.
    """
    out: List[Dict[str, Any]] = []
    try:
        rows = conn.execute(
            "SELECT message_id, content, event_at FROM conversation_messages"
            " WHERE is_from_self=1 AND content IS NOT NULL AND LENGTH(content)>15"
            " ORDER BY event_at DESC LIMIT ?", (int(limit) * 10,)).fetchall()
    except sqlite3.Error:
        rows = []
    for mid, text, at in rows:
        if is_promise_shaped(text):
            out.append({"table": "conversation_messages", "record_id": str(mid),
                        "text": str(text)[:6000], "date": str(at or "")[:10],
                        "role": "authored", "source_id": ""})
    try:
        jrows = conn.execute(
            "SELECT entry_id, content, entry_at FROM journal_entries"
            " WHERE content IS NOT NULL AND LENGTH(content)>15 ORDER BY entry_at DESC LIMIT ?",
            (int(limit),)).fetchall()
    except sqlite3.Error:
        jrows = []
    for eid, text, at in jrows:
        if is_promise_shaped(text):
            out.append({"table": "journal_entries", "record_id": str(eid),
                        "text": str(text)[:6000], "date": str(at or "")[:10],
                        "role": "authored", "source_id": ""})
    out.sort(key=lambda r: r["date"], reverse=True)
    return out[:limit]


def refresh_commitments(conn: sqlite3.Connection, *, limit: int = MAX_PER_RUN) -> Dict[str, Any]:
    """Run the pack over the promise-shaped owner records only. Enables nothing itself:
    the pack is owner-subject and the owner switches it on in the lens catalog."""
    from .surfaces import run_pack_backfill

    records = promise_shaped_records(conn)
    stats = run_pack_backfill(conn, PACK_ID, limit, records=records, use_prefilter=False)
    stats["promise_shaped"] = len(records)
    return stats


# --------------------------------------------------------------------------- read-back

def attach_commitments(conn: Any, nodes: List[Dict[str, Any]]) -> Dict[str, int]:
    """The ledger per person: what the owner promised them, what they promised the owner,
    and what the record says became of it — onto the node as `commitments`."""
    by_entity = {str(n["entity_id"]): n for n in nodes if n.get("entity_id") and not n.get("is_owner")}
    if not by_entity:
        return {"attached": 0}
    try:
        rows = conn.execute(
            "SELECT payload_json, valid_from FROM signal_objects"
            " WHERE object_type='fact' AND valid_to IS NULL"
            " AND (payload_json LIKE '%commit.made%' OR payload_json LIKE '%commit.resolved%')"
        ).fetchall()
    except sqlite3.Error:
        return {"attached": 0}
    attached = 0
    for payload, valid_from in rows:
        try:
            fact = json.loads(payload or "{}")
        except (TypeError, ValueError):
            continue
        predicate = str(fact.get("predicate") or "")
        if predicate not in ("commit.made", "commit.resolved"):
            continue
        struct = fact.get("value_struct") or fact.get("value") or {}
        node = by_entity.get(str(struct.get("counterparty_entity_id") or fact.get("object_entity_id") or ""))
        if node is None:
            continue
        ledger = node.setdefault("commitments", {
            "owed_by_you": [], "owed_to_you": [], "resolved": [],
            "kept": 0, "open": 0, "overdue": 0, "released": 0, "reliability": None,
            "basis": ("promises in your own words — messages you sent and journal lines you "
                      "wrote; status only as the record states it, never inferred from silence"),
        })
        entry = {"description": str(struct.get("description") or ""),
                 "due": struct.get("due") or None,
                 "status": str(struct.get("status") or "unknown"),
                 "at": str(fact.get("occurrence") or valid_from or "")[:10] or None,
                 "quote": (fact.get("quote") or "")[:200] or None}
        was_empty = not (ledger["owed_by_you"] or ledger["owed_to_you"] or ledger["resolved"])
        if predicate == "commit.resolved":
            entry["outcome"] = str(struct.get("outcome") or "")
            ledger["resolved"].append(entry)
        elif str(struct.get("direction") or "") == "owed_to_owner":
            ledger["owed_to_you"].append(entry)
        else:
            ledger["owed_by_you"].append(entry)
        if predicate == "commit.made" and entry["status"] in ("kept", "open", "overdue", "released"):
            ledger[entry["status"]] += 1
        if was_empty:
            attached += 1
    for node in by_entity.values():
        ledger = node.get("commitments")
        if not ledger:
            continue
        judged = ledger["kept"] + ledger["overdue"]
        ledger["reliability"] = round(ledger["kept"] / judged, 2) if judged else None
        for key in ("owed_by_you", "owed_to_you", "resolved"):
            ledger[key].sort(key=lambda e: e.get("at") or "", reverse=True)
            ledger[key] = ledger[key][:8]
    return {"attached": attached}

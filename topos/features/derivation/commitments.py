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
            "SELECT message_id, content, event_at, conversation_id FROM conversation_messages"
            " WHERE is_from_self=1 AND content IS NOT NULL AND LENGTH(content)>15"
            " ORDER BY event_at DESC LIMIT ?", (int(limit) * 10,)).fetchall()
    except sqlite3.Error:
        rows = []
    hits = [(str(mid), str(text), str(at or "")[:10], str(conv or ""))
            for mid, text, at, conv in rows if is_promise_shaped(text)]
    # THE RECIPIENT. In a direct message "I'll read it" is a promise to the person being
    # texted, and their name is nowhere in the text — measured on the first live pass: 40
    # promise-shaped records, 40 model calls, zero assertions, because the pack (rightly)
    # refuses a commitment with no counterparty. So each DM row carries its partner, from
    # the record and the identity bridge, never from the text; a group thread carries none
    # and the pack must find a named counterparty in the words or abstain.
    recipients = _dm_recipients(conn, sorted({c for _, _, _, c in hits if c}))
    for mid, text, at, conv in hits:
        rec = {"table": "conversation_messages", "record_id": mid,
               "text": text[:6000], "date": at, "role": "authored", "source_id": ""}
        who = recipients.get(conv)
        if who:
            rec["recipient"], rec["recipient_entity_id"], rec["recipient_key"] = who
        out.append(rec)
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


def _dm_recipients(conn: sqlite3.Connection, conversation_ids: List[str]) -> Dict[str, tuple]:
    """conversation_id -> (display name, entity id) of the ONE other party in a DM.

    A conversation with two or more other senders is a group; it gets no recipient, so a
    promise in it needs a named counterparty in the text — the same rule as before.
    """
    if not conversation_ids:
        return {}
    peers: Dict[str, set] = {}
    for chunk_start in range(0, len(conversation_ids), 400):
        chunk = conversation_ids[chunk_start:chunk_start + 400]
        placeholders = ",".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                f"SELECT DISTINCT conversation_id, sender_id FROM conversation_messages"
                f" WHERE conversation_id IN ({placeholders}) AND COALESCE(is_from_self,0)=0"
                f" AND sender_id IS NOT NULL AND sender_id<>'' AND sender_id<>'self'", chunk).fetchall()
        except sqlite3.Error:
            return {}
        for conv, sender in rows:
            peers.setdefault(str(conv), set()).add(str(sender))
    singles = {conv: next(iter(ss)) for conv, ss in peers.items() if len(ss) == 1}
    if not singles:
        return {}
    try:
        from ...analytics.messenger_directed import resolve_peer_identities
        idents = resolve_peer_identities(conn, sorted(set(singles.values())))
    except Exception:  # noqa: BLE001 — identity is decoration here; the key is the label
        idents = {}
    # Measured 2026-09-06: of 33 single-party DM conversations with a promise in them, 6
    # resolve to a person entity; 13 more are ambiguous (two contact rows for one number)
    # and 12 have no clean entity. The person graph already draws those people as
    # `msg:<key>` nodes, so the counterparty label is the conversation's own key when no
    # entity is known — the ledger then attaches by messenger key, as the graph does.
    out: Dict[str, tuple] = {}
    for conv, key in singles.items():
        _cid, eid, display = idents.get(key, (None, None, None))
        label = str(display or "") if display and any(ch.isalpha() for ch in str(display)) else ""
        out[conv] = (label or "the person you are texting", str(eid or ""), str(key))
    return out


def refresh_commitments(conn: sqlite3.Connection, *, limit: int = MAX_PER_RUN) -> Dict[str, Any]:
    """Run the pack over the promise-shaped owner records only. Enables nothing itself:
    the pack is owner-subject and the owner switches it on in the lens catalog."""
    from .surfaces import run_pack_backfill

    records = promise_shaped_records(conn)
    stats = run_pack_backfill(conn, PACK_ID, limit, records=records, use_prefilter=False)
    stats["promise_shaped"] = len(records)
    return stats


# --------------------------------------------------------------------------- read-back

#: A status signal is the most perishable fact on a person. Past this, the card stops
#: asserting it — "said 12 Aug, expired 10 Nov" — rather than reading a two-year-old job as
#: current (plan §3 row 3, the "#6 failure mode").
STATUS_TTL_DAYS = 90


def attach_status_signals(conn: Any, nodes: List[Dict[str, Any]], *, today: Any = None) -> Dict[str, int]:
    """`net.status_signal` facts (net.character) onto the speaker's node, with expiry at read.

    Expired signals are kept on the node as `expired` rather than dropped: "they were hiring
    in June" is still worth a line, it is just not "they are hiring".
    """
    from datetime import date, datetime, timedelta

    by_entity = {str(n["entity_id"]): n for n in nodes if n.get("entity_id") and not n.get("is_owner")}
    if not by_entity:
        return {"attached": 0}
    try:
        rows = conn.execute(
            "SELECT payload_json, valid_from FROM signal_objects"
            " WHERE object_type='fact' AND valid_to IS NULL"
            " AND payload_json LIKE '%net.status_signal%'").fetchall()
    except sqlite3.Error:
        return {"attached": 0}
    now = today or date.today()
    if isinstance(now, datetime):
        now = now.date()
    attached = 0
    for payload, valid_from in rows:
        try:
            fact = json.loads(payload or "{}")
        except (TypeError, ValueError):
            continue
        if str(fact.get("predicate") or "") != "net.status_signal":
            continue
        struct = fact.get("value_struct") or fact.get("value") or {}
        node = by_entity.get(str(fact.get("subject_entity_id") or struct.get("person_entity_id") or ""))
        if node is None:
            continue
        said = str(fact.get("occurrence") or valid_from or "")[:10]
        try:
            said_on = date.fromisoformat(said)
        except (TypeError, ValueError):
            said_on = None
        expires = (said_on + timedelta(days=STATUS_TTL_DAYS)) if said_on else None
        entry = {"kind": str(struct.get("kind") or ""), "detail": str(struct.get("detail") or ""),
                 "said_on": said or None, "expires_on": expires.isoformat() if expires else None,
                 "expired": bool(expires and expires < now),
                 "quote": (fact.get("quote") or "")[:200] or None,
                 "basis": "their own words, in a message to you"}
        signals = node.setdefault("status_signals", {"current": [], "expired": [],
                                                    "ttl_days": STATUS_TTL_DAYS})
        (signals["expired"] if entry["expired"] else signals["current"]).append(entry)
        if len(signals["current"]) + len(signals["expired"]) == 1:
            attached += 1
    for node in by_entity.values():
        sig = node.get("status_signals")
        if sig:
            for key in ("current", "expired"):
                sig[key].sort(key=lambda e: e.get("said_on") or "", reverse=True)
                sig[key] = sig[key][:6]
    return {"attached": attached}

def attach_commitments(conn: Any, nodes: List[Dict[str, Any]]) -> Dict[str, int]:
    """The ledger per person: what the owner promised them, what they promised the owner,
    and what the record says became of it — onto the node as `commitments`."""
    from .person_bridge import normalise_handle as _norm

    by_entity = {str(n["entity_id"]): n for n in nodes if n.get("entity_id") and not n.get("is_owner")}
    by_key = {_norm(k): n for n in nodes if not n.get("is_owner")
              for k in (n.get("messenger_keys") or []) if k}
    if not by_entity and not by_key:
        return {"attached": 0}
    try:
        rows = conn.execute(
            "SELECT payload_json, valid_from FROM signal_objects"
            " WHERE object_type='fact' AND valid_to IS NULL"
            " AND (payload_json LIKE '%commit.made%' OR payload_json LIKE '%commit.resolved%'"
            "      OR payload_json LIKE '%net.promise%')"
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
        if predicate not in ("commit.made", "commit.resolved", "net.promise"):
            continue
        struct = fact.get("value_struct") or fact.get("value") or {}
        # a net.promise is the SPEAKER's fact: its subject is the person, not a counterparty
        if predicate == "net.promise":
            node = by_entity.get(str(fact.get("subject_entity_id") or struct.get("person_entity_id") or ""))
        else:
            node = by_entity.get(str(struct.get("counterparty_entity_id") or fact.get("object_entity_id") or ""))
            if node is None:
                # an unnamed partner: the counterparty is the conversation's key, and the
                # graph keys that person by the same handle
                cp = str(struct.get("counterparty") or "")
                if cp.lower().startswith("key:"):
                    node = by_key.get(_norm(cp[4:]))
        if node is None:
            continue
        ledger = node.setdefault("commitments", {
            "owed_by_you": [], "owed_to_you": [], "resolved": [],
            "kept": 0, "open": 0, "overdue": 0, "released": 0, "reliability": None,
            "basis": ("promises in your own words — messages you sent and journal lines you "
                      "wrote — and, marked 'them', promises they made you in theirs; status only "
                      "as the record states it, never inferred from silence"),
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
        elif predicate == "net.promise":
            entry["stated_by"] = "them"
            ledger["owed_to_you"].append(entry)
        elif str(struct.get("direction") or "") == "owed_to_owner":
            ledger["owed_to_you"].append(entry)
        else:
            ledger["owed_by_you"].append(entry)
        if predicate in ("commit.made", "net.promise") and entry["status"] in ("kept", "open", "overdue", "released"):
            ledger[entry["status"]] += 1
        if was_empty:
            attached += 1
    for node in list(by_entity.values()) + list(by_key.values()):
        ledger = node.get("commitments")
        if not ledger or ledger.get("_finalised"):
            continue
        ledger["_finalised"] = True
        judged = ledger["kept"] + ledger["overdue"]
        ledger["reliability"] = round(ledger["kept"] / judged, 2) if judged else None
        for key in ("owed_by_you", "owed_to_you", "resolved"):
            ledger[key].sort(key=lambda e: e.get("at") or "", reverse=True)
            ledger[key] = ledger[key][:8]
        ledger.pop("_finalised", None)
    return {"attached": attached}

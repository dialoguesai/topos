"""Per-partner interaction statistics — the `comms_stats` input a lens declares.

`relationships.social` declares its closeness lens as

    {kind: graph_labeling, predicate: rel.closeness_tier,
     inputs: [communicates_with_edges, comms_stats], min_evidence: 90d}

and the predicate's own note asks for "frequency, initiation balance, recency,
channels". Only the edges half existed, so a tier computed from them was a volume
rank wearing a relationship word — the defect this module exists to remove.

WHAT IT MEASURES
----------------
Per partner, inside the lens's evidence window:

  inbound / outbound     both directions, so a correspondent who sends a lot and
                         receives nothing back is visible as exactly that
  initiation_balance     outbound / (inbound + outbound), 0.0-1.0. ~0.5 is mutual;
                         near 0 is someone talking AT the owner; near 1 is the
                         owner talking at them
  last_contact           newest message either way
  one_to_one_share       share of the partner's messages in 1:1 threads rather
                         than groups — a busy group chat otherwise promotes
                         everyone in it equally
  channels               distinct source systems the partner reaches the owner on

The join is the same one the query lane needs: `conversation_messages.sender_id`
holds a RAW handle, names live in `contacts`, and only `contact_identifiers`
bridges them. Entities reach contacts through `entities.contact_id` (57/57 of the
owner's comms partners are linked on the live node).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


def normalise_handle(raw: Any) -> str:
    """One key shape for both sides of the identifier join.

    Phones arrive as "+1 (512) 740-0415" in the address book and "+15127400415"
    on the message; digits-only/last-ten collapses both. Anything containing a
    letter (email, @handle) folds to lowercase instead.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits and not any(ch.isalpha() for ch in s):
        return digits[-10:] if len(digits) >= 10 else digits
    return s.lower()


def looks_like_a_person_name(name: str) -> bool:
    """A phone number is an identifier, not a person.

    Unnamed contacts carry their handle as `entities.canonical_name`, and the
    owner's own entity is literally named "self". A fact reading
    `{person: "+17184834576", tier: "inner_circle"}` names nobody, and one reading
    `{person: "self"}` makes the owner their own inner circle — both were in the
    top three of the live edge ranking.
    """
    s = str(name or "").strip()
    if len(s) < 2:
        return False
    low = s.lower()
    if low.startswith(("self", "me:", "owner", "unknown", "user:", "system", "sys:", "rec:")):
        return False
    if ":" in s:
        # `scheme:value` is an identifier shape, never a name. "unknown:0" has
        # letters, passed the alpha test, and was written as a person.
        return False
    if "@" in s:
        # An email has letters and passes the alpha test, so it reached the writer
        # and was rejected there as "identifier in value" — correct, but only after
        # it had taken a tier slot and shifted everyone else's percentile.
        return False
    return any(ch.isalpha() for ch in s)


def _parse_ts(raw: Any) -> Optional[datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _entity_to_handles(conn: sqlite3.Connection) -> Dict[str, set]:
    """entity_id -> {normalised handle}, via entities.contact_id."""
    out: Dict[str, set] = {}
    try:
        rows = conn.execute(
            "SELECT e.entity_id, ci.identifier FROM entities e"
            " JOIN contact_identifiers ci ON ci.contact_id = e.contact_id"
            " WHERE e.contact_id IS NOT NULL AND e.contact_id != ''"
            "   AND ci.identifier IS NOT NULL AND ci.identifier != ''"
        ).fetchall()
    except sqlite3.Error:
        return out
    for entity_id, identifier in rows:
        key = normalise_handle(identifier)
        if key:
            out.setdefault(str(entity_id), set()).add(key)
    return out


def _group_threads(conn: sqlite3.Connection) -> set:
    """Conversations with more than two participants."""
    try:
        rows = conn.execute(
            "SELECT conversation_id FROM conversation_participants"
            " GROUP BY conversation_id HAVING COUNT(*) > 2"
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {str(r[0]) for r in rows}


def comms_stats(
    conn: sqlite3.Connection,
    *,
    window_days: Optional[int] = 90,
    now: Optional[datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """Interaction stats per partner entity_id.

    `now` defaults to the newest message in the corpus, not the wall clock: the
    window should describe the DATA, so a node that has not synced for a fortnight
    does not report everyone as having gone quiet.
    """
    handles = _entity_to_handles(conn)
    if not handles:
        return {}
    handle_to_entity: Dict[str, str] = {}
    for entity_id, keys in handles.items():
        for k in keys:
            handle_to_entity.setdefault(k, entity_id)

    groups = _group_threads(conn)
    try:
        rows = conn.execute(
            "SELECT sender_id, is_from_self, event_at, conversation_id, source_id"
            " FROM conversation_messages WHERE sender_id IS NOT NULL AND sender_id != ''"
        ).fetchall()
    except sqlite3.Error:
        return {}

    stamped = []
    newest: Optional[datetime] = None
    for sender_id, is_self, event_at, conv_id, source_id in rows:
        ts = _parse_ts(event_at)
        if ts and (newest is None or ts > newest):
            newest = ts
        stamped.append((sender_id, int(is_self or 0), ts, conv_id, source_id))

    anchor = now or newest or datetime.now(timezone.utc)
    floor = anchor - timedelta(days=int(window_days)) if window_days else None

    out: Dict[str, Dict[str, Any]] = {}
    for sender_id, is_self, ts, conv_id, source_id in stamped:
        if floor is not None and (ts is None or ts < floor):
            continue
        # An outbound message is attributed to the OTHER party in the thread: the
        # owner is the sender, so sender_id cannot identify who they said it to.
        if is_self:
            partners = _conversation_partners(conn, conv_id, handle_to_entity)
            for entity_id in partners:
                row = out.setdefault(entity_id, _blank())
                row["outbound"] += 1
                _note(row, ts, conv_id, source_id, groups)
            continue
        entity_id = handle_to_entity.get(normalise_handle(sender_id))
        if not entity_id:
            continue
        row = out.setdefault(entity_id, _blank())
        row["inbound"] += 1
        _note(row, ts, conv_id, source_id, groups)

    for row in out.values():
        total = row["inbound"] + row["outbound"]
        row["initiation_balance"] = round(row["outbound"] / total, 3) if total else 0.0
        seen = row.pop("_threads_seen")
        row["one_to_one_share"] = round(row.pop("_one_to_one") / seen, 3) if seen else 0.0
        row["channels"] = sorted(row["channels"])
        row["last_contact"] = row["last_contact"].isoformat() if row["last_contact"] else None
    return out


def _blank() -> Dict[str, Any]:
    return {"inbound": 0, "outbound": 0, "last_contact": None, "channels": set(),
            "_threads_seen": 0, "_one_to_one": 0}


def _note(row: Dict[str, Any], ts, conv_id, source_id, groups: set) -> None:
    if ts and (row["last_contact"] is None or ts > row["last_contact"]):
        row["last_contact"] = ts
    if source_id:
        row["channels"].add(str(source_id))
    row["_threads_seen"] += 1
    if str(conv_id) not in groups:
        row["_one_to_one"] += 1


_PARTNER_CACHE: Dict[int, Dict[str, list]] = {}


def _conversation_partners(conn: sqlite3.Connection, conv_id, handle_to_entity: Dict[str, str]) -> list:
    """Non-owner entity ids in a conversation, resolved through participants."""
    cache = _PARTNER_CACHE.setdefault(id(conn), {})
    key = str(conv_id)
    if key in cache:
        return cache[key]
    try:
        rows = conn.execute(
            "SELECT ci.identifier FROM conversation_participants cp"
            " JOIN contact_identifiers ci ON ci.contact_id = cp.contact_id"
            " WHERE cp.conversation_id = ?", (conv_id,)).fetchall()
    except sqlite3.Error:
        rows = []
    seen, partners = set(), []
    for (identifier,) in rows:
        entity_id = handle_to_entity.get(normalise_handle(identifier))
        if entity_id and entity_id not in seen:
            seen.add(entity_id)
            partners.append(entity_id)
    cache[key] = partners
    return partners

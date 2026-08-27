"""The person-centric social graph: one node per person, evidence-gated.

Distinct from the temporal KG, which answers "what is true". This answers "what is true about
my relationships" — a different unit (always a person), different edges (relational), and a
different failure mode. A thin KG is merely incomplete; a thin social graph is WRONG, because
a missing person reads as "you don't know them" and a person with no warmth reads as "you are
not close". Absence is a claim here, which is why evidence is a property of every node.

Built on two decisions the owner made 2026-08-27 (PLAN_SOCIAL_GRAPH_PERSON_CENTRIC §9):

* **Evidence only.** The address book is a NAMING source, never a node source. Importing it
  would add ~1,106 people with no evidence of any relationship — a graph that looks rich and
  means nothing.
* **One node per person.** Somebody who is both a messenger peer and an extracted entity is
  ONE node holding both identities, not two nodes that happen to be the same human.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Set

#: Message traffic that is not a relationship. 29 of this node's 180 peers are SMS shortcodes
#: (2FA codes, delivery notices); they are not people and must never occupy a social graph.
PEER_CLASS_HUMAN = "human"


def resolve_owner_identity(conn: Any) -> Dict[str, Any]:
    """Every entity id that is the owner, collapsed into one logical identity.

    Extraction emitted the owner THREE times on the live node — `Owner` (1,239 edges),
    `self` (95 edges) and a second `self` (0) — splitting 1,334 edges across three ids. Any
    "me" node built from one of them is wrong on arrival.

    Resolved at READ time on purpose. A destructive merge of `entities` rewrites edges and
    mentions in place, and merge is not reliably reversible on this codebase; the graph does
    not need the rows rewritten, only read as one. If a real merge lands later this function
    keeps working — it will simply return a single id.

    Returns the canonical id (the one carrying the most edges), every alias id, and a label.
    """
    try:
        rows = conn.execute(
            "SELECT entity_id, canonical_name FROM entities WHERE is_self = 1").fetchall()
    except sqlite3.Error:
        return {"canonical_id": None, "ids": set(), "label": "You"}
    ids = {str(r[0]) for r in rows if r and r[0]}
    if not ids:
        return {"canonical_id": None, "ids": set(), "label": "You"}

    best, best_edges, best_name = None, -1, None
    for eid, name in rows:
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM entity_edges WHERE src_entity_id=? OR dst_entity_id=?",
                (eid, eid)).fetchone()[0]
        except sqlite3.Error:
            n = 0
        if n > best_edges:
            best, best_edges, best_name = str(eid), n, name
    # "Owner"/"self" are extraction artifacts, not what a person calls themselves.
    label = clean_label(best_name)
    if label.lower() in ("", "owner", "self", "me"):
        label = "You"
    return {"canonical_id": best, "ids": ids, "label": label, "edge_count": best_edges}


def clean_label(value: Any) -> str:
    """A display label with no control characters and no runaway whitespace.

    Extraction emits fragments of records as names — `"Topos\n\nAccomplished"` is a real
    canonical_name on this node. A literal newline inside a label produces JSON that strict
    parsers reject outright, and renders as a broken multi-line node label even when it does
    not. Cleaned once here rather than in every consumer.
    """
    text = str(value or "")
    text = "".join(" " if ch in "\r\n\t" else ch for ch in text if ch.isprintable() or ch in " \r\n\t")
    text = " ".join(text.split())
    return text[:120]


def _digits_key(value: Any) -> Optional[str]:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    return text[-10:] if len(text) >= 10 else None


def build_person_nodes(conn: Any, dataset_id: str, *,
                       include_automated: bool = False) -> List[Dict[str, Any]]:
    """One node per person, from evidence, with identities bundled.

    Three ways onto the graph, and the node says which:

    * `messaged`  — the owner exchanged DMs with them
    * `mentioned` — they are named in a record the owner authored or received

    `evidence` is not decoration: it gates what may be computed. A node with
    `messaged=False` has no cadence, so warmth, drift, reciprocity and a bench slot are not
    thin — they are UNAVAILABLE, and the screen has to say so rather than render a zero.
    """
    from .messenger_directed import (MESSENGER_DYAD_STATS_TABLE, SELF_KEY,
                                     resolve_peer_identities)

    owner = resolve_owner_identity(conn)
    owner_ids: Set[str] = set(owner["ids"])

    peers: List[str] = []
    try:
        sql = (f"SELECT DISTINCT CASE WHEN a_key=? THEN b_key ELSE a_key END"
               f"  FROM {MESSENGER_DYAD_STATS_TABLE}"
               f" WHERE dataset_id=? AND involves_self=1")
        args: List[Any] = [SELF_KEY, dataset_id]
        if not include_automated:
            sql += " AND peer_class = ?"
            args.append(PEER_CLASS_HUMAN)
        peers = [str(r[0]) for r in conn.execute(sql, args).fetchall() if r and r[0]]
    except sqlite3.Error:
        peers = []

    idents = resolve_peer_identities(conn, peers) if peers else {}

    nodes: Dict[str, Dict[str, Any]] = {}

    def node_for(key: str) -> Dict[str, Any]:
        return nodes.setdefault(key, {
            "node_id": key, "entity_id": None, "messenger_keys": [], "contact_id": None,
            "label": None, "evidence": {"messaged": False, "mentioned": False},
            "is_owner": False, "message_count": 0, "mention_count": 0,
        })

    # --- messaged -------------------------------------------------------------------
    by_entity: Dict[str, str] = {}
    for peer in peers:
        contact_id, entity_id, display = idents.get(peer, (None, None, None))
        if entity_id and str(entity_id) in owner_ids:
            continue  # the owner's own handle is not a peer
        # A person with an entity id keys on it, so a second identity for the same human
        # lands on the SAME node instead of creating a duplicate (owner decision D-3).
        key = f"ent:{entity_id}" if entity_id else f"msg:{peer}"
        if entity_id:
            by_entity[str(entity_id)] = key
        n = node_for(key)
        n["messenger_keys"].append(peer)
        n["evidence"]["messaged"] = True
        if entity_id:
            n["entity_id"] = str(entity_id)
        if contact_id and not n["contact_id"]:
            n["contact_id"] = str(contact_id)
        if display and any(ch.isalpha() for ch in str(display)) and not n["label"]:
            n["label"] = clean_label(display)

    # --- mentioned ------------------------------------------------------------------
    try:
        rows = conn.execute(
            "SELECT e.entity_id, MAX(e.canonical_name), COUNT(*)"
            "  FROM entity_mentions m JOIN entities e ON e.entity_id = m.entity_id"
            " WHERE e.entity_type = 'person' GROUP BY e.entity_id").fetchall()
    except sqlite3.Error:
        rows = []
    for entity_id, name, count in rows:
        eid = str(entity_id)
        if eid in owner_ids:
            continue
        key = by_entity.get(eid, f"ent:{eid}")
        n = node_for(key)
        n["entity_id"] = eid
        n["evidence"]["mentioned"] = True
        n["mention_count"] = int(count or 0)
        if name and any(ch.isalpha() for ch in str(name)) and not n["label"]:
            n["label"] = clean_label(name)

    # --- message volume, used for ranking and for the naming queue -------------------
    try:
        vq = (f"SELECT CASE WHEN a_key=? THEN b_key ELSE a_key END, total_msgs"
              f"  FROM {MESSENGER_DYAD_STATS_TABLE}"
              f" WHERE dataset_id=? AND involves_self=1")
        volume = {str(k): int(v or 0) for k, v in conn.execute(vq, (SELF_KEY, dataset_id))}
    except sqlite3.Error:
        volume = {}
    for n in nodes.values():
        n["message_count"] = sum(volume.get(k, 0) for k in n["messenger_keys"])
        # A node with no alphabetic label is a phone number on screen. Say so explicitly
        # rather than letting the caller infer it from the shape of the string.
        n["needs_name"] = not bool(n["label"])
        if not n["label"]:
            n["label"] = n["messenger_keys"][0] if n["messenger_keys"] else (n["entity_id"] or "unknown")

    out = sorted(nodes.values(),
                 key=lambda r: (-r["message_count"], -r["mention_count"], r["node_id"]))

    # --- the owner, centred (owner decision D-4) -------------------------------------
    if owner["canonical_id"] or peers:
        out.insert(0, {
            "node_id": "owner",
            "entity_id": owner["canonical_id"],
            "entity_id_aliases": sorted(owner_ids),
            "messenger_keys": [SELF_KEY],
            "contact_id": None,
            "label": owner["label"],
            "evidence": {"messaged": True, "mentioned": True},
            "is_owner": True,
            "message_count": sum(volume.values()),
            "mention_count": 0,
            "needs_name": False,
        })
    return out


def naming_queue(conn: Any, dataset_id: str, *, limit: int = 25) -> Dict[str, Any]:
    """Unnamed HUMAN peers, busiest first — where naming effort actually pays.

    Automatic recovery is exhausted on this corpus: only 32 of 136 peer phone numbers appear
    in the address book at all, and 30 of those are already named, so a digit-match against
    every named contact recovers ZERO further names. These people simply are not in the
    address book, and only the owner can say who they are.

    So the useful thing is not another join, it is ORDER: naming the ten busiest unknowns
    covers most of the traffic, while an alphabetical list spends the same attention on
    strangers.
    """
    nodes = build_person_nodes(conn, dataset_id)
    people = [n for n in nodes if not n["is_owner"]]
    unnamed = [n for n in people if n["needs_name"] and n["evidence"]["messaged"]]
    unnamed.sort(key=lambda r: -r["message_count"])
    covered = sum(n["message_count"] for n in unnamed[:limit])
    total_unnamed = sum(n["message_count"] for n in unnamed)
    return {
        "dataset_id": dataset_id,
        "unnamed_count": len(unnamed),
        "named_count": sum(1 for n in people if not n["needs_name"]),
        "messages_behind_unnamed": total_unnamed,
        "messages_covered_by_this_page": covered,
        "queue": [{
            "node_id": n["node_id"],
            "peer_key": n["messenger_keys"][0] if n["messenger_keys"] else None,
            "messenger_keys": n["messenger_keys"],
            "contact_id": n["contact_id"],
            "entity_id": n["entity_id"],
            "message_count": n["message_count"],
        } for n in unnamed[:limit]],
        "coverage": {
            "basis": "human peers only; automated shortcodes are excluded, not merely ranked low",
            "automatic_recovery": ("exhausted — only 32 of 136 peer numbers appear in the "
                                   "address book, and 30 of those are already named"),
        },
    }

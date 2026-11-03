"""The people the owner can rate, busiest first, in milliseconds.

The calibration screen's ratings card wants a handful of people: named, messaged, not the
owner, not dismissed, with fewer than four Big Five domains rated by the owner. It read the
WHOLE person graph for that (`read_person_graph`) and kept five nodes of 2,893. Measured on
a copy of the owner's database, 28 Oct 2026: 10 s warm, of which structure 3.4 s, the
heard-about rail 4.1 s, context affinity 1.4 s, the node builder itself 0.3-0.6 s. And it
ran on the app's event loop (`handle_relationship_reads` is not an off-loop handler): the
22.6 s stall logged at 17:04:29Z that day shows the loop inside that read, with every other
owner frame queued behind it.

A candidate needs none of that. Who the owner messages comes from the dyad stats; who they
are from the contact and entity spine; what the owner changed from the overlay; what the
owner has rated from the disposition facts. So this reads those alone — the same rows the
graph's node builder reads, minus the mention sweep over every entity — and nothing else.

The filter is the app's `worthRating`, kept here as `worth_rating` so the card and the node
cannot disagree about who is a candidate; the node shape is the graph's `PersonNode`, the
fields the card reads, so the card can fall back to the graph on an older node and render
the same rows.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Set

from .person_graph import (PEER_CLASS_HUMAN, _digits10, clean_label, owner_identifiers,
                           person_disposition_facts, resolve_owner_identity)

#: The domains the owner rates for another person. Negative Emotionality is withheld for
#: third parties (owner decision 2026-09-05; `surfaces.THIRD_PARTY_EXCLUDED_DOMAINS`), so a
#: person is fully rated at four, never five. The same four the app's `DOMAINS` lists.
RATED_DOMAINS = ("extraversion", "agreeableness", "conscientiousness", "open_mindedness")

CANDIDATE_BASIS = ("named people you message, not you, not dismissed, with fewer than "
                   f"{len(RATED_DOMAINS)} domains rated by you; busiest first")


def rated_domains(node: Dict[str, Any]) -> int:
    """How many of the rateable domains carry a level — the app's `ratedOf`."""
    domains = ((node.get("disposition") or {}).get("domains") or {})
    return sum(1 for d in RATED_DOMAINS if (domains.get(d) or {}).get("level"))


def worth_rating(node: Dict[str, Any]) -> bool:
    """The app's `worthRating`, to the letter: someone the owner can rate and has not
    finished rating. Named, with an entity to rate, messaged, not the owner, not dismissed."""
    return (bool(node.get("entity_id")) and not node.get("is_owner") and not node.get("needs_name")
            and not node.get("dismissed") and int(node.get("message_count") or 0) > 0
            and rated_domains(node) < len(RATED_DOMAINS))


def _messaged_people(conn: Any, dataset_id: str, *, owner_ids: Set[str]) -> List[Dict[str, Any]]:
    """One node per messaged human peer, keyed and labelled as the graph's node builder keys
    and labels them, from the dyad stats and the identity spine alone."""
    from .messenger_directed import MESSENGER_DYAD_STATS_TABLE, SELF_KEY, resolve_peer_identities

    try:
        rows = conn.execute(
            f"SELECT CASE WHEN a_key=? THEN b_key ELSE a_key END, total_msgs"
            f"  FROM {MESSENGER_DYAD_STATS_TABLE}"
            f" WHERE dataset_id=? AND involves_self=1 AND peer_class=?",
            (SELF_KEY, dataset_id, PEER_CLASS_HUMAN)).fetchall()
    except sqlite3.Error:
        rows = []
    volume: Dict[str, int] = {}
    for key, msgs in rows:
        if key:
            volume[str(key)] = volume.get(str(key), 0) + int(msgs or 0)
    if not volume:
        return []
    idents = resolve_peer_identities(conn, list(volume))
    owner_phones = owner_identifiers(conn)["phones"]

    nodes: Dict[str, Dict[str, Any]] = {}
    for peer, msgs in volume.items():
        contact_id, entity_id, display = idents.get(peer, (None, None, None))
        if entity_id and str(entity_id) in owner_ids:
            continue  # the owner's own handle is not a peer
        if owner_phones and _digits10(peer) in owner_phones:
            continue  # messaging yourself is not a relationship
        # The graph's keys: a person with an entity id keys on it, so two handles for one
        # human are one node (owner decision D-3), and the card's rows carry the ids the
        # graph would.
        key = f"ent:{entity_id}" if entity_id else f"msg:{peer}"
        n = nodes.setdefault(key, {
            "node_id": key, "entity_id": None, "messenger_keys": [], "contact_id": None,
            "label": None, "evidence": {"messaged": True, "mentioned": False},
            "is_owner": False, "message_count": 0, "mention_count": 0,
        })
        n["messenger_keys"].append(peer)
        n["message_count"] += msgs
        if entity_id:
            n["entity_id"] = str(entity_id)
        if contact_id and not n["contact_id"]:
            n["contact_id"] = str(contact_id)
        if display and any(ch.isalpha() for ch in str(display)) and not n["label"]:
            n["label"] = clean_label(display)

    # What the spine says about the entities these people resolve to, as the graph's
    # mention pass says it: the canonical name names a person the contact row left unnamed
    # (when the entity has been seen in a record), and the aliases take part in the fold.
    by_entity = {n["entity_id"]: n for n in nodes.values() if n["entity_id"]}
    if by_entity:
        for chunk in _chunks(list(by_entity), 400):
            marks = ",".join("?" * len(chunk))
            try:
                spine = conn.execute(
                    f"SELECT e.entity_id, e.canonical_name, e.aliases_json FROM entities e"
                    f" WHERE e.entity_id IN ({marks})"
                    f"   AND EXISTS (SELECT 1 FROM entity_mentions m WHERE m.entity_id = e.entity_id)",
                    chunk).fetchall()
            except sqlite3.Error:
                spine = []
            for entity_id, name, aliases_json in spine:
                n = by_entity.get(str(entity_id))
                if n is None:
                    continue
                n["evidence"]["mentioned"] = True
                if name and any(ch.isalpha() for ch in str(name)) and not n["label"]:
                    n["label"] = clean_label(name)
                aliases = _aliases(aliases_json)
                if aliases:
                    n["aliases"] = aliases

    for n in nodes.values():
        n["needs_name"] = not bool(n["label"])
        if not n["label"]:
            n["label"] = n["messenger_keys"][0] if n["messenger_keys"] else (n["entity_id"] or "unknown")
    return list(nodes.values())


def _mentioned_people(conn: Any, *, exclude: Set[str]) -> List[Dict[str, Any]]:
    """Mention-only person nodes, as the graph's node builder shapes them, for the fold.

    The graph folds a messaged contact with no entity into a mentioned person of the same
    name (`auto_link_duplicates`), and that is where 61 of its 157 rating candidates got
    the entity a rating is written against (copy of the owner's database, 28 Oct 2026).
    The door runs the same fold, so it needs the same mention-side nodes: one per person
    entity seen in a record and not already held by a messaged node.
    """
    try:
        rows = conn.execute(
            "SELECT e.entity_id, e.canonical_name, e.aliases_json FROM entities e"
            " WHERE e.entity_type = 'person'"
            "   AND EXISTS (SELECT 1 FROM entity_mentions m WHERE m.entity_id = e.entity_id)").fetchall()
        counts = dict(conn.execute(
            "SELECT entity_id, COUNT(*) FROM entity_mentions GROUP BY entity_id").fetchall())
    except sqlite3.Error:
        return []
    out: List[Dict[str, Any]] = []
    for entity_id, name, aliases_json in rows:
        eid = str(entity_id)
        if eid in exclude:
            continue
        label = clean_label(name) if name and any(ch.isalpha() for ch in str(name)) else None
        node = {
            "node_id": f"ent:{eid}", "entity_id": eid, "messenger_keys": [], "contact_id": None,
            "label": label or eid, "evidence": {"messaged": False, "mentioned": True},
            "is_owner": False, "message_count": 0, "mention_count": int(counts.get(entity_id) or 0),
            "needs_name": not label,
        }
        aliases = _aliases(aliases_json)
        if aliases:
            node["aliases"] = aliases
        out.append(node)
    # The graph's node order, which decides which of two same-name mentioned people a
    # messaged node takes its entity from: most mentioned first, then by id.
    out.sort(key=lambda n: (-n["mention_count"], n["node_id"]))
    return out


def _aliases(aliases_json: Any) -> List[str]:
    try:
        parsed = json.loads(aliases_json or "[]")
    except (TypeError, ValueError):
        return []
    return [str(a) for a in parsed if str(a or "").strip()] if isinstance(parsed, list) else []


def _attach_mentions(conn: Any, nodes: List[Dict[str, Any]]) -> None:
    """Mention counts for the few nodes that are served, so the row says what the graph's
    would. Indexed on `entity_mentions(entity_id)`: a lookup per served node, never a sweep."""
    ids = [n["entity_id"] for n in nodes if n.get("entity_id")]
    if not ids:
        return
    counts: Dict[str, int] = {}
    for chunk in _chunks(ids, 400):
        marks = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                f"SELECT entity_id, COUNT(*) FROM entity_mentions WHERE entity_id IN ({marks})"
                f" GROUP BY entity_id", chunk).fetchall()
        except sqlite3.Error:
            rows = []
        for entity_id, count in rows:
            counts[str(entity_id)] = int(count or 0)
    for n in nodes:
        n["mention_count"] = counts.get(str(n.get("entity_id") or ""), 0)
        n["evidence"]["mentioned"] = n["mention_count"] > 0


def rating_candidates(conn: Any, dataset_id: str, *, limit: int = 25) -> Dict[str, Any]:
    """The top `limit` candidates by message count, and how many candidates there are.

    `dataset_id` is already resolved (see `relationship_reads.read_rating_candidates`).
    Reads: dyad stats, contacts and identifiers, entities (by id), the overlay, the
    disposition facts, and mention counts for the served rows. Never the mention sweep,
    the edges, the structure, the luck rail, the readings, the commitments.
    """
    from .person_graph import auto_link_duplicates
    from .person_overlay import apply_overlay, load as load_overlay

    owner_ids = set(resolve_owner_identity(conn)["ids"])
    messaged = _messaged_people(conn, dataset_id, owner_ids=owner_ids)
    held = {n["entity_id"] for n in messaged if n["entity_id"]}
    nodes = messaged + _mentioned_people(conn, exclude=held | owner_ids)
    # The owner's corrections OVER the derived rows, as the graph applies them: a rename
    # names a person, a dismissal keeps them off the card, a merge folds two handles.
    overlay_rows = load_overlay(conn, dataset_id)
    nodes = apply_overlay(nodes, overlay_rows)
    # The graph's own fold, on the same inputs: a messaged contact with no entity takes
    # the entity of the one mentioned person of that name, unless the owner split them.
    nodes = auto_link_duplicates(
        nodes, split_ids=[r["value"] or r["subject_id"] for r in overlay_rows
                          if r["action"] == "split"])
    nodes = [n for n in nodes if n["evidence"]["messaged"]]
    disposition = person_disposition_facts(conn, nodes)
    candidates = [n for n in nodes if worth_rating(n)]
    candidates.sort(key=lambda n: (-int(n.get("message_count") or 0), str(n["node_id"])))
    served = candidates[:max(0, int(limit))]
    _attach_mentions(conn, served)
    return {
        "dataset_id": dataset_id,
        "limit": int(limit),
        "candidates": served,
        "unrated_count": len(candidates),
        "candidate_basis": CANDIDATE_BASIS,
        "rated_domains": list(RATED_DOMAINS),
        "excluded_domains": ["negative_emotionality"],
        "disposition": disposition,
        "overlay_actions": len(overlay_rows),
        "coverage": {
            "messaged_people": len(nodes),
            "named": sum(1 for n in nodes if not n.get("needs_name")),
            "basis": ("people you message, from the dyad stats and the contact and entity "
                      "spine; a person named only in your records, never messaged, has "
                      "nothing to rate here"),
        },
    }


def _chunks(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


__all__ = ["RATED_DOMAINS", "rated_domains", "rating_candidates", "worth_rating"]

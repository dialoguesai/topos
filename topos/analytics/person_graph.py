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

import re
import sqlite3
from typing import Any, Dict, List, Optional, Set

#: Message traffic that is not a relationship. 29 of this node's 180 peers are SMS shortcodes
#: (2FA codes, delivery notices); they are not people and must never occupy a social graph.
PEER_CLASS_HUMAN = "human"


def _normalized_name(value: Any) -> str:
    return " ".join(re.sub(r"[^a-z ]", " ", str(value or "").lower()).split())


def _digits10(value: Any) -> Optional[str]:
    text = re.sub(r"\D", "", str(value or ""))
    return text[-10:] if len(text) >= 10 else None


def owner_identifiers(conn: Any) -> Dict[str, Any]:
    """What this node canonically knows about who its owner is.

    Deliberately NOT a name-similarity search. `Bravo Yankee` and `Charlie Yankee` are real
    other people on this corpus who share the owner's surname; a fuzzy rule would swallow
    them into the owner node and delete two humans from the graph.
    """
    names, phones = set(), set()
    try:
        for (dn,) in conn.execute("SELECT display_name FROM user_identity"):
            n = _normalized_name(dn)
            if n:
                names.add(n)
    except sqlite3.Error:
        pass
    try:
        for (phone,) in conn.execute("SELECT my_phone_number FROM signal_identity"):
            d = _digits10(phone)
            if d:
                phones.add(d)
    except sqlite3.Error:
        pass
    return {"names": names, "phones": phones}


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
        return {"canonical_id": None, "ids": set(), "label": "You", "merge_candidates": []}
    ids = {str(r[0]) for r in rows if r and r[0]}

    # `is_self` alone missed SIX owner entities on the live node — `Jonny Johnson` (29
    # mentions), `Jonny` (20), `Delta Yankee` (13), and more — so the owner was drawn on
    # their own social graph as up to six separate contacts. These three rules use what the
    # node actually KNOWS rather than what a name looks like.
    known = owner_identifiers(conn)
    candidates: List[Dict[str, str]] = []
    try:
        people = conn.execute(
            "SELECT entity_id, canonical_name, contact_id FROM entities"
            " WHERE entity_type='person'").fetchall()
    except sqlite3.Error:
        people = []
    for eid, name, contact_id in people:
        eid = str(eid)
        if eid in ids:
            continue
        why = None
        if contact_id:
            try:
                if conn.execute("SELECT 1 FROM contacts WHERE contact_id=? AND is_self=1",
                                (contact_id,)).fetchone():
                    why = "your own contact card"
            except sqlite3.Error:
                pass
        if not why and _normalized_name(name) and _normalized_name(name) in known["names"]:
            why = "the name you gave this node"
        if not why and contact_id and known["phones"]:
            try:
                for (ident,) in conn.execute(
                        "SELECT identifier FROM contact_identifiers WHERE contact_id=?",
                        (contact_id,)):
                    if _digits10(ident) in known["phones"]:
                        why = "your own phone number"
                        break
            except sqlite3.Error:
                pass
        if why:
            ids.add(eid)
            rows.append((eid, name))
        else:
            # A shared surname is NOT evidence. Offered for the owner to confirm, never taken.
            tokens = set(_normalized_name(name).split())
            for owner_name in known["names"]:
                owner_tokens = set(owner_name.split())
                if len(owner_tokens) > 1 and len(owner_tokens & tokens) >= len(owner_tokens) - 1 \
                        and owner_tokens & tokens:
                    candidates.append({
                        "entity_id": eid, "label": str(name or ""),
                        "reason": f"shares {', '.join(sorted(owner_tokens & tokens))} "
                                  f"with your name — confirm before merging",
                    })
                    break

    if not ids:
        return {"canonical_id": None, "ids": set(), "label": "You",
                "merge_candidates": candidates}

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
    return {"canonical_id": best, "ids": ids, "label": label, "edge_count": best_edges,
            "merge_candidates": candidates}


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
    # The owner's own handles. A self-thread ("Notes to Self", or texting your own number)
    # arrives as an ordinary peer that resolves to NO entity, so the entity check above never
    # fires and the owner is drawn as one of their own contacts.
    owner_phones = owner_identifiers(conn)["phones"]

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
        if owner_phones and _digits10(peer) in owner_phones:
            continue  # messaging yourself is not a relationship
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
    # Grouped by SOURCE and authorship as well as entity, because the band depends on the
    # posture of each source the person appears under and on whether the owner wrote the row.
    try:
        detail = conn.execute(
            "SELECT e.entity_id, m.source_id, COALESCE(m.authored_by_owner,0), COUNT(*)"
            "  FROM entity_mentions m JOIN entities e ON e.entity_id = m.entity_id"
            " WHERE e.entity_type = 'person'"
            " GROUP BY e.entity_id, m.source_id, COALESCE(m.authored_by_owner,0)").fetchall()
    except sqlite3.Error:
        detail = []
    postures = source_postures(conn, dataset_id, {d[1] for d in detail})
    posture_error = postures.pop("__error__", None)
    facts: Dict[str, Dict[str, Any]] = {}
    for entity_id, source_id, authored, n in detail:
        f = facts.setdefault(str(entity_id), {
            "sources": set(), "owner_authored": 0, "non_ambient": 0, "mentions": 0})
        f["sources"].add(str(source_id))
        f["mentions"] += int(n or 0)
        if int(authored or 0) == 1:
            f["owner_authored"] += int(n or 0)
        if postures.get(str(source_id), "mixed") != POSTURE_AMBIENT:
            f["non_ambient"] += int(n or 0)

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
        f = facts.get(str(n.get("entity_id") or ""), {})
        n["band"], n["band_reason"] = classify_band(
            messaged=n["evidence"]["messaged"],
            owner_authored=int(f.get("owner_authored", 0)),
            distinct_sources=len(f.get("sources", ())),
            non_ambient_mentions=int(f.get("non_ambient", 0)),
            mention_count=int(n["mention_count"]),
        )
        n["sources"] = sorted(f.get("sources", ()))
        n["needs_name"] = not bool(n["label"])
        if not n["label"]:
            n["label"] = n["messenger_keys"][0] if n["messenger_keys"] else (n["entity_id"] or "unknown")

    out = sorted(nodes.values(),
                 key=lambda r: (-r["message_count"], -r["mention_count"], r["node_id"]))
    # Surfaced so a caller can tell "these are the bands" from "posture never resolved".
    build_person_nodes.last_postures = dict(postures)          # type: ignore[attr-defined]
    build_person_nodes.last_posture_error = posture_error      # type: ignore[attr-defined]

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
            "band": BAND_CORE,
            "band_reason": "this is you",
            "sources": [],
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


# --------------------------------------------------------------------------- bands

#: How strongly the record supports treating someone as part of the owner's life. Bands, not
#: a score: a band can state its reason in a sentence the owner can argue with, and a score
#: cannot.
BAND_CORE = "core"
BAND_NAMED = "named"
BAND_DISCUSSED = "discussed"
BAND_AMBIENT = "ambient"
BAND_ORDER = (BAND_CORE, BAND_NAMED, BAND_DISCUSSED, BAND_AMBIENT)

#: Sources whose rows are exposure rather than expression. Read from the SOURCE CONTRACT
#: (`sources.registry.effective_posture`), never from a list of connector names — the first
#: draft of this model ranked grow_journal > imessage > browser_visits, which is three
#: source_ids that happen to be on one node and worthless to anybody who connects Slack.
POSTURE_AMBIENT = "ambient"


def source_postures(conn: Any, dataset_id: str, source_ids) -> Dict[str, str]:
    """posture per source, honouring the owner's per-connector override.

    An unknown source resolves to `mixed` — the registry's own default, and the safe
    direction: a new connector's people are neither promoted to Core nor buried as Ambient
    until its rows say more.
    """
    out: Dict[str, str] = {}
    try:
        from ..sources.registry import effective_posture
    except Exception as exc:  # noqa: BLE001
        # Falling back silently would hand every source the `mixed` default, which quietly
        # promotes every ambient sighting into "discussed" — a plausible band distribution
        # built on a failure nobody saw. Record it instead.
        out["__error__"] = f"registry import failed: {type(exc).__name__}"
        return out
    for source_id in {str(s) for s in source_ids if s}:
        try:
            out[source_id] = str(effective_posture(source_id, dataset_id, conn) or "mixed")
        except Exception as exc:  # noqa: BLE001
            out[source_id] = "mixed"
            out.setdefault("__error__", f"{source_id}: {type(exc).__name__}")
    return out


def classify_band(*, messaged: bool, owner_authored: int, distinct_sources: int,
                  non_ambient_mentions: int, mention_count: int):
    """(band, reason). Pure, and deliberately free of any connector name.

    The reason is not decoration. "Seen once, on a page you visited" is falsifiable and the
    owner can correct it; a rank cannot be argued with.
    """
    if messaged:
        return BAND_CORE, "you exchange messages with them"
    if owner_authored > 0:
        return BAND_NAMED, "you wrote their name down yourself"
    if distinct_sources >= 2:
        return BAND_NAMED, f"they turn up in {distinct_sources} different places"
    if non_ambient_mentions > 0 and mention_count >= 2:
        return BAND_DISCUSSED, f"mentioned {mention_count} times in things you took part in"
    if non_ambient_mentions > 0:
        return BAND_AMBIENT, "mentioned once, in something you took part in"
    return BAND_AMBIENT, ("seen once in passing" if mention_count <= 1
                          else f"seen {mention_count} times in passing, never discussed")


# --------------------------------------------------------------------------- edges

#: How an edge came to be known. These are NOT interchangeable and the screen must not
#: render them alike: one is the owner's lived experience, one is their own account of it,
#: and one is somebody else's account of two other people.
ATTRIBUTION_OBSERVED = "observed"              # the owner and X exchanged messages
ATTRIBUTION_OWNER_ASSERTED = "owner_asserted"  # the owner's own record names them
ATTRIBUTION_RECEIVED = "in_your_records"       # somebody named them TO the owner
ATTRIBUTION_THIRD_PARTY = "third_party_asserted"  # somebody else's record names two others

#: The privacy boundary runs between owner-to-person and person-to-person, NOT between
#: authored and received. Someone mentioning Dana in a message to the owner makes the owner
#: aware of Dana — that awareness is the owner's own record and is first-party. What is NOT
#: first-party is asserting that Dana knows Priya on the strength of somebody else's message.


def build_person_edges(conn: Any, dataset_id: str, nodes: List[Dict[str, Any]], *,
                       include_third_party: bool = False) -> List[Dict[str, Any]]:
    """Edges between person nodes, each carrying who asserted it.

    Third-party-asserted edges are computed but **off by default** (owner decision D-1). A
    graph of who-knows-whom between two people who are not the owner is a dossier about
    non-consenting third parties; it stays node-local, never becomes a stored fact about
    them, and renders only when the owner asks for it.
    """
    from .messenger_directed import MESSENGER_DYAD_STATS_TABLE, SELF_KEY

    by_key: Dict[str, str] = {}
    for n in nodes:
        for mk in n.get("messenger_keys", []):
            by_key[str(mk)] = n["node_id"]
        if n.get("entity_id"):
            by_key[f"ent:{n['entity_id']}"] = n["node_id"]
    owner_node = next((n["node_id"] for n in nodes if n.get("is_owner")), None)
    edges: Dict[tuple, Dict[str, Any]] = {}

    def add(a: Optional[str], b: Optional[str], kind: str, attribution: str, weight: float):
        if not a or not b or a == b:
            return
        key = (a, b, attribution) if a < b else (b, a, attribution)
        e = edges.setdefault(key, {
            "source": key[0], "target": key[1], "kind": kind,
            "attribution": attribution, "weight": 0.0, "evidence_count": 0,
        })
        e["weight"] += float(weight)
        e["evidence_count"] += 1

    # A — observed: the owner exchanged messages with them.
    try:
        rows = conn.execute(
            f"SELECT CASE WHEN a_key=? THEN b_key ELSE a_key END, total_msgs"
            f"  FROM {MESSENGER_DYAD_STATS_TABLE}"
            f" WHERE dataset_id=? AND involves_self=1 AND peer_class=?",
            (SELF_KEY, dataset_id, PEER_CLASS_HUMAN)).fetchall()
    except sqlite3.Error:
        rows = []
    for peer, msgs in rows:
        add(owner_node, by_key.get(str(peer)), "messaged", ATTRIBUTION_OBSERVED, int(msgs or 0))

    # B — the owner's records name this person. Split by who wrote the record, because
    # "I mentioned Dana" and "somebody mentioned Dana to me" are different facts. Both are
    # first-party: both are the owner's own corpus, and both are why they know the name at
    # all. Withholding the received half would leave 189 people floating unconnected in a
    # graph that does in fact know how the owner came to hear of them.
    try:
        rows = conn.execute(
            "SELECT m.entity_id, COALESCE(m.authored_by_owner,0), COUNT(*)"
            "  FROM entity_mentions m JOIN entities e ON e.entity_id = m.entity_id"
            " WHERE e.entity_type='person' GROUP BY m.entity_id, COALESCE(m.authored_by_owner,0)"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for eid, by_owner, n in rows:
        add(owner_node, by_key.get(f"ent:{eid}"), "mentioned",
            ATTRIBUTION_OWNER_ASSERTED if int(by_owner or 0) == 1 else ATTRIBUTION_RECEIVED,
            int(n or 0))

    # B2 / C — two people named in the SAME record. Attribution follows who wrote it, which
    # is the whole point: the owner writing "met Dana with Priya" is their own memory, while
    # somebody else's message saying it is a claim about two other people.
    try:
        rows = conn.execute(
            "SELECT m1.entity_id, m2.entity_id, MAX(COALESCE(m1.authored_by_owner,0))"
            "  FROM entity_mentions m1"
            "  JOIN entity_mentions m2 ON m1.record_id = m2.record_id"
            "                         AND m1.entity_id < m2.entity_id"
            "  JOIN entities e1 ON e1.entity_id=m1.entity_id AND e1.entity_type='person'"
            "  JOIN entities e2 ON e2.entity_id=m2.entity_id AND e2.entity_type='person'"
            " GROUP BY m1.entity_id, m2.entity_id").fetchall()
    except sqlite3.Error:
        rows = []
    for a_eid, b_eid, by_owner in rows:
        attribution = (ATTRIBUTION_OWNER_ASSERTED if int(by_owner or 0) == 1
                       else ATTRIBUTION_THIRD_PARTY)
        if attribution == ATTRIBUTION_THIRD_PARTY and not include_third_party:
            continue
        add(by_key.get(f"ent:{a_eid}"), by_key.get(f"ent:{b_eid}"),
            "co_mentioned", attribution, 1)

    out = list(edges.values())
    out.sort(key=lambda e: (-e["weight"], e["source"], e["target"]))
    return out

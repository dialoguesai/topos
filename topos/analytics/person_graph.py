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

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

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

    Deliberately NOT a name-similarity search. `Bravo Yankee` and `Wendel Yankee` are real
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

    # `is_self` alone missed SIX owner entities on the live node — `Sierra Yankee` (29
    # mentions), `Robin` (20), `Delta Yankee` (13), and more — so the owner was drawn on
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
            "SELECT e.entity_id, MAX(e.canonical_name), COUNT(*), MAX(e.aliases_json)"
            "  FROM entity_mentions m JOIN entities e ON e.entity_id = m.entity_id"
            " WHERE e.entity_type = 'person' GROUP BY e.entity_id").fetchall()
    except sqlite3.Error:
        rows = []
    for entity_id, name, count, aliases_json in rows:
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
        # The spine's own nickname record, carried so the duplicate fold can use it. The
        # entity for "Rowan Alvestad" already lists "Rowan" here; without it the fold groups
        # on the display name alone and a person known by both never meets themselves.
        try:
            parsed = json.loads(aliases_json or "[]")
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, list) and parsed:
            n["aliases"] = [str(a) for a in parsed if str(a or "").strip()]

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
ATTRIBUTION_CO_PRESENT = "co_present"          # they were in a room WITH the owner

#: Above this many people a thread is a mailing list, not a room the owner shared with
#: anyone, and pairing everyone in it would invent n-squared relationships that never
#: existed. Same reasoning as MAX_BROADCAST_ROSTER in messenger_directed.
MAX_CO_PRESENT_ROSTER = 32
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
    by_contact: Dict[str, str] = {}
    for n in nodes:
        for mk in n.get("messenger_keys", []):
            by_key[str(mk)] = n["node_id"]
        if n.get("entity_id"):
            by_key[f"ent:{n['entity_id']}"] = n["node_id"]
        if n.get("contact_id"):
            by_contact[str(n["contact_id"])] = n["node_id"]
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

    # A2 — co-presence: two people in a group conversation the OWNER was in.
    #
    # First-party, and it renders by default. The owner was in the room; that two of their
    # contacts were also in it is something they witnessed, not something a third party
    # asserted about two strangers. Losing this was a real regression when the graph moved
    # from the messaging view to the person view — without "these two know each other" a
    # social graph is a star, not a network.
    #
    # Derived through `load_messages`/`classify_conversations`, the same normalisation that
    # produced the peer keys everywhere else. Two alternatives were measured and rejected:
    # raw `sender_id` grouping (counts owner variants and duplicate handles as separate
    # people, which inflated this to 182 before normalisation), and the precomputed
    # `messenger_social_edges` table (keyed on contact ids that only 114 of 437 nodes carry,
    # and contaminated with `test-dataset:` contacts — 9 usable edges against 16 here).
    from .messenger_directed import EDGE_KIND_DM, classify_conversations, load_messages

    try:
        rows = load_messages(conn, dataset_id)
    except sqlite3.Error:
        rows = []
    if rows:
        kinds = classify_conversations(rows)
        members: Dict[str, Set[str]] = {}
        for conv, _mid, sender, _at, from_self, _src, _reply in rows:
            if from_self or not sender or str(sender) == SELF_KEY:
                continue
            members.setdefault(str(conv), set()).add(str(sender))
        for conv, people in members.items():
            if kinds.get(conv) == EDGE_KIND_DM:
                continue  # a DM has one peer; co-presence needs a room
            resolved = sorted({by_key[p] for p in people if p in by_key})
            # A broadcast blast to a huge roster is a mailing list, not a room the owner
            # shared with anyone: pairing everyone in it would invent n^2 relationships.
            if len(resolved) < 2 or len(resolved) > MAX_CO_PRESENT_ROSTER:
                continue
            for i in range(len(resolved)):
                for j in range(i + 1, len(resolved)):
                    add(resolved[i], resolved[j], "co_present", ATTRIBUTION_CO_PRESENT, 1)

    out = list(edges.values())
    out.sort(key=lambda e: (-e["weight"], e["source"], e["target"]))
    return out


# --------------------------------------------------------------------------- C-4 / C-6

def merge_suggestions(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Nodes that look like the same human, offered with evidence — never merged silently.

    Two people genuinely can share a name (`Bravo Yankee` and `Wendel Yankee` are both real
    here), so a silent merge would be a falsehood the owner cannot see. Measured on the live
    node: 8 name collisions between people the owner messages and people they only mention.
    """
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for n in nodes:
        if n.get("is_owner") or n.get("needs_name"):
            continue
        key = _normalized_name(n.get("label"))
        if len(key) < 3:
            continue  # a one-token nickname is not evidence of anything
        by_name.setdefault(key, []).append(n)

    out: List[Dict[str, Any]] = []
    for key, group in by_name.items():
        if len(group) < 2:
            continue
        group = sorted(group, key=lambda n: (-int(n.get("message_count", 0)),
                                             -int(n.get("mention_count", 0))))
        keep, rest = group[0], group[1:]
        for other in rest:
            def _how(n: Dict[str, Any]) -> str:
                ev = n.get("evidence", {})
                if ev.get("messaged"):
                    return f"you message them ({n.get('message_count', 0)} messages)"
                return f"mentioned {n.get('mention_count', 0)} times"
            out.append({
                "keep": keep["node_id"], "merge": other["node_id"],
                "label": keep.get("label"),
                "reason": f"both named {keep.get('label')!r} — "
                          f"{_how(keep)}, and {_how(other)}",
            })
    out.sort(key=lambda r: str(r["label"]))
    return out


# --------------------------------------------------------------------------- appearances (C-6)

#: Two ways a person shows up on a card — not the same thing.
#:
#: * mentioned — their name was extracted from a record body (`entity_mentions`).
#:   Any connector that runs NER lands here with no further code.
#: * participated — they were a party to the record even if unnamed in the body
#:   (canonical `conversation_participants` / `conversation_messages`). Any
#:   connector that writes those tables (today's messengers; Slack tomorrow)
#:   lands here the same way. `source_id` is a column on the row, never a branch.
#:
#: Calendar attendees live in JSON metadata, not a person-keyed participant
#: table, so they are not read here.
APPEARANCE_MENTIONED = "mentioned"
APPEARANCE_PARTICIPATED = "participated"
APPEARANCE_SHOW = 6
APPEARANCE_FETCH = 24
APPEARANCE_EXCERPT = 220

_IDENTIFIER_PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{6,}\d)")
_IDENTIFIER_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


def _appearance_table_exists(conn: Any, name: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone())
    except sqlite3.Error:
        return False


def _appearance_columns(conn: Any, name: str) -> Set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({name})").fetchall()}
    except sqlite3.Error:
        return set()


def _appearance_chunks(items: Sequence[Any], size: int = 400) -> List[List[Any]]:
    seq = [x for x in items if x is not None and str(x) != ""]
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def _collapse_ws(text: Any) -> str:
    return " ".join(str(text or "").split())


def _scrub_identifier_shapes(text: str) -> str:
    """Phones and emails out of snippets so they never land in the snapshot JSON."""
    cleaned = _IDENTIFIER_PHONE_RE.sub("•••", text)
    return _IDENTIFIER_EMAIL_RE.sub("•••", cleaned)


def _excerpt_around(full: str, token: str, width: int = APPEARANCE_EXCERPT) -> str:
    """A short window around a matched name — never a full thread."""
    text = _collapse_ws(full)
    if not text:
        return ""
    needle = _collapse_ws(token)
    if needle and len(needle) >= 2:
        idx = text.lower().find(needle.lower())
        if idx >= 0:
            start = max(0, idx - 50)
            end = min(len(text), idx + len(needle) + 150)
            snippet = text[start:end]
            if start > 0:
                snippet = "…" + snippet.lstrip()
            if end < len(text):
                snippet = snippet.rstrip() + "…"
            return snippet[:width]
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "…"


def appearance_source_label(source_id: str) -> str:
    """Human word for a source_id. Registry title if cheap, else the raw id."""
    sid = str(source_id or "").strip()
    if not sid:
        return "unknown"
    try:
        from ..sources.registry import REGISTRY

        src = REGISTRY.get(sid)
        name = getattr(src, "display_name", None) if src is not None else None
        if name:
            return str(name)
    except Exception:  # noqa: BLE001 — display is best-effort
        pass
    return re.sub(r"[_-]+", " ", sid).strip() or "unknown"


def _load_appearance_record_texts(conn: Any, record_ids: Sequence[str]) -> Dict[str, str]:
    """Original line for a mention record_id, from canonical tables. First hit wins.

    Tables are discovered from the schema (id column + a body/title column), not
    listed by connector. A new source that writes a canonical row with those
    columns is picked up with no edit here.
    """
    out: Dict[str, str] = {}
    ids = [str(rid) for rid in record_ids if rid]
    if not ids:
        return out

    try:
        tables = [
            str(r[0]) for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
    except sqlite3.Error:
        return out

    ranked: List[Tuple[int, str, str, List[str]]] = []
    id_cols = ("message_id", "entry_id", "event_id", "record_id")
    text_cols = ("content", "title", "place_name", "project", "goal", "accomplished")
    for table in tables:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table):
            continue
        cols = _appearance_columns(conn, table)
        id_col = next((c for c in id_cols if c in cols), None)
        fields = [c for c in text_cols if c in cols]
        if not id_col or not fields:
            continue
        rank = 0 if "content" in fields else 1
        ranked.append((rank, table, id_col, fields))
    ranked.sort()

    for _rank, table, id_col, fields in ranked:
        missing = [rid for rid in ids if rid not in out]
        select_text = ", ".join(fields)
        for chunk in _appearance_chunks(missing):
            placeholders = ",".join("?" for _ in chunk)
            try:
                rows = conn.execute(
                    f"SELECT {id_col}, {select_text} FROM {table} "
                    f"WHERE {id_col} IN ({placeholders})",
                    chunk,
                ).fetchall()
            except sqlite3.Error:
                break
            for rid, *parts in rows:
                key = str(rid)
                if key in out:
                    continue
                text = _collapse_ws(" ".join(str(p) for p in parts if p))
                if text:
                    out[key] = text
    return out


def _appearance_is_rich(text: str, surface: str) -> bool:
    body = _collapse_ws(text)
    token = _collapse_ws(surface)
    if not body:
        return False
    if not token:
        return len(body) > 12
    return len(body) > len(token) + 8 or body.lower() != token.lower()


def _finalize_appearance(
    *,
    source_id: Any,
    text: str,
    when: Any,
    authored: Any,
    kind: str,
    record_id: Any = None,
) -> Optional[Dict[str, Any]]:
    snippet = _scrub_identifier_shapes(_collapse_ws(text))[:APPEARANCE_EXCERPT]
    if not snippet:
        return None
    sid = str(source_id or "")
    return {
        "source_id": sid,
        "source_label": appearance_source_label(sid),
        "text": snippet,
        "at": str(when or "")[:19],
        "authored_by_owner": bool(authored),
        "kind": kind,
        "record_id": str(record_id or ""),
    }


def _appearance_mentions(
    conn: Any,
    nodes: Sequence[Dict[str, Any]],
    *,
    fetch: int,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    """Batched `entity_mentions` — every source_id, no connector filter."""
    by_entity: Dict[str, List[str]] = defaultdict(list)
    for node in nodes:
        if node.get("is_owner"):
            continue
        nid = str(node.get("node_id") or "")
        if not nid:
            continue
        eids = [str(node["entity_id"])] if node.get("entity_id") else []
        eids.extend(str(a) for a in (node.get("entity_id_aliases") or []) if a)
        for eid in eids:
            if eid and nid not in by_entity[eid]:
                by_entity[eid].append(nid)
    mentions: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    totals: Dict[str, int] = defaultdict(int)
    ids = list(by_entity)
    if not ids or not _appearance_table_exists(conn, "entity_mentions"):
        return mentions, totals

    cols = _appearance_columns(conn, "entity_mentions")
    has_surface = "surface_text" in cols
    has_record = "record_id" in cols
    has_event = "event_at" in cols
    has_created = "created_at" in cols
    has_authored = "authored_by_owner" in cols
    when_sql = "NULL"
    if has_event and has_created:
        when_sql = "COALESCE(event_at, created_at)"
    elif has_event:
        when_sql = "event_at"
    elif has_created:
        when_sql = "created_at"
    select = [
        "entity_id",
        "source_id",
        "surface_text" if has_surface else "NULL",
        "record_id" if has_record else "NULL",
        when_sql,
        "COALESCE(authored_by_owner, 0)" if has_authored else "0",
    ]

    raw: Dict[str, List[Tuple[Any, ...]]] = defaultdict(list)
    for chunk in _appearance_chunks(ids):
        placeholders = ",".join("?" for _ in chunk)
        try:
            for entity_id, n in conn.execute(
                f"SELECT entity_id, COUNT(*) FROM entity_mentions "
                f"WHERE entity_id IN ({placeholders}) GROUP BY entity_id",
                chunk,
            ).fetchall():
                for nid in by_entity.get(str(entity_id), ()):
                    totals[nid] += int(n or 0)
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute(
                f"SELECT {', '.join(select)} FROM entity_mentions "
                f"WHERE entity_id IN ({placeholders}) "
                f"ORDER BY {when_sql} DESC",
                chunk,
            ).fetchall()
        except sqlite3.Error:
            continue
        for entity_id, source_id, surface, record_id, when, authored in rows:
            for nid in by_entity.get(str(entity_id), ()):
                bucket = raw[nid]
                if len(bucket) >= fetch:
                    continue
                bucket.append((source_id, surface, record_id, when, authored))

    record_ids = [
        str(record_id) for bucket in raw.values()
        for _, _, record_id, _, _ in bucket if record_id
    ]
    texts = _load_appearance_record_texts(conn, record_ids)

    for nid, bucket in raw.items():
        built: List[Dict[str, Any]] = []
        for source_id, surface, record_id, when, authored in bucket:
            token = _collapse_ws(surface)
            full = texts.get(str(record_id or ""), "")
            text = _excerpt_around(full, token) if full else token
            row = _finalize_appearance(
                source_id=source_id, text=text, when=when, authored=authored,
                kind=APPEARANCE_MENTIONED, record_id=record_id,
            )
            if not row:
                continue
            row["_rich"] = _appearance_is_rich(row["text"], token)
            built.append(row)
        mentions[nid] = built
    return mentions, totals


def _peer_handle_keys(node: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    seen = set()
    for raw in list(node.get("messenger_keys") or []):
        key = str(raw or "").strip()
        if not key or key.lower() == "self" or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def _appearance_participation(
    conn: Any,
    nodes: Sequence[Dict[str, Any]],
    *,
    fetch: int,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    """Batched participation from canonical conversation tables. No source_id filter.

    A future messenger connector that writes `conversation_messages` /
    `conversation_participants` appears here with zero edits. `source_id` on
    the row is passed through as data.
    """
    rows_out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    totals: Dict[str, int] = defaultdict(int)
    if not _appearance_table_exists(conn, "conversation_messages"):
        return rows_out, totals

    key_to_nid: Dict[str, str] = {}
    cid_to_nid: Dict[str, str] = {}
    nid_keys: Dict[str, Set[str]] = defaultdict(set)
    for node in nodes:
        if node.get("is_owner"):
            continue
        nid = str(node.get("node_id") or "")
        if not nid:
            continue
        for key in _peer_handle_keys(node):
            key_to_nid.setdefault(key, nid)
            nid_keys[nid].add(key)
        cid = str(node.get("contact_id") or "").strip()
        if cid:
            cid_to_nid.setdefault(cid, nid)

    if cid_to_nid and _appearance_table_exists(conn, "contact_identifiers"):
        for chunk in _appearance_chunks(list(cid_to_nid)):
            placeholders = ",".join("?" for _ in chunk)
            try:
                idents = conn.execute(
                    f"SELECT contact_id, identifier FROM contact_identifiers "
                    f"WHERE contact_id IN ({placeholders})",
                    chunk,
                ).fetchall()
            except sqlite3.Error:
                idents = []
            for cid, ident in idents:
                key = str(ident or "").strip()
                nid = cid_to_nid.get(str(cid))
                if key and nid:
                    key_to_nid.setdefault(key, nid)
                    nid_keys[nid].add(key)

    if not key_to_nid and not cid_to_nid:
        return rows_out, totals

    conv_to_nids: Dict[str, Set[str]] = defaultdict(set)
    if cid_to_nid and _appearance_table_exists(conn, "conversation_participants"):
        for chunk in _appearance_chunks(list(cid_to_nid)):
            placeholders = ",".join("?" for _ in chunk)
            try:
                for conv_id, cid in conn.execute(
                    f"SELECT conversation_id, contact_id FROM conversation_participants "
                    f"WHERE contact_id IN ({placeholders})",
                    chunk,
                ).fetchall():
                    nid = cid_to_nid.get(str(cid))
                    if nid and conv_id:
                        conv_to_nids[str(conv_id)].add(nid)
            except sqlite3.Error:
                pass

    cols = _appearance_columns(conn, "conversation_messages")
    has_self = "is_from_self" in cols
    has_event = "event_at" in cols
    has_created = "created_at" in cols
    has_content = "content" in cols
    has_source = "source_id" in cols
    has_sender = "sender_id" in cols
    has_conv = "conversation_id" in cols
    if not has_sender or not has_content:
        return rows_out, totals
    when_sql = "NULL"
    if has_event and has_created:
        when_sql = "COALESCE(event_at, created_at)"
    elif has_event:
        when_sql = "event_at"
    elif has_created:
        when_sql = "created_at"
    self_sql = "COALESCE(is_from_self, 0)" if has_self else "0"
    source_sql = "source_id" if has_source else "NULL"
    conv_sql = "conversation_id" if has_conv else "NULL"
    select_sql = (
        f"message_id, {conv_sql}, sender_id, content, {when_sql}, {source_sql}, {self_sql}"
    )

    sent_ids: Dict[str, Set[str]] = defaultdict(set)
    fetched: Dict[str, List[Tuple[Any, ...]]] = defaultdict(list)
    collect_cap = max(fetch * 2, fetch)

    def take(nid: str, row: Tuple[Any, ...]) -> None:
        message_id = str(row[0] or "")
        if message_id:
            if message_id in sent_ids[nid]:
                return
            sent_ids[nid].add(message_id)
        if len(fetched[nid]) < collect_cap:
            fetched[nid].append(row)

    if key_to_nid:
        for chunk in _appearance_chunks(list(key_to_nid)):
            placeholders = ",".join("?" for _ in chunk)
            try:
                for sender, n in conn.execute(
                    f"SELECT sender_id, COUNT(*) FROM conversation_messages "
                    f"WHERE sender_id IN ({placeholders}) GROUP BY sender_id",
                    chunk,
                ).fetchall():
                    nid = key_to_nid.get(str(sender))
                    if nid:
                        totals[nid] += int(n or 0)
            except sqlite3.Error:
                pass
            try:
                msg_rows = conn.execute(
                    f"SELECT {select_sql} FROM conversation_messages "
                    f"WHERE sender_id IN ({placeholders}) "
                    f"ORDER BY {when_sql} DESC",
                    chunk,
                ).fetchall()
            except sqlite3.Error:
                msg_rows = []
            for row in msg_rows:
                nid = key_to_nid.get(str(row[2] or ""))
                if not nid:
                    continue
                if has_conv and row[1]:
                    conv_to_nids[str(row[1])].add(nid)
                take(nid, row)

    # 1:1 threads: the owner is a party too, so their messages belong on the card.
    dyadic: Set[str] = set()
    if conv_to_nids and has_conv:
        conv_ids = list(conv_to_nids)
        participant_n: Dict[str, int] = {}
        if _appearance_table_exists(conn, "conversation_participants"):
            for chunk in _appearance_chunks(conv_ids):
                placeholders = ",".join("?" for _ in chunk)
                try:
                    for conv_id, n in conn.execute(
                        f"SELECT conversation_id, COUNT(DISTINCT contact_id) "
                        f"FROM conversation_participants "
                        f"WHERE conversation_id IN ({placeholders}) "
                        f"GROUP BY conversation_id",
                        chunk,
                    ).fetchall():
                        participant_n[str(conv_id)] = int(n or 0)
                except sqlite3.Error:
                    pass
        sender_n: Dict[str, int] = {}
        if has_self:
            for chunk in _appearance_chunks(conv_ids):
                placeholders = ",".join("?" for _ in chunk)
                try:
                    for conv_id, n in conn.execute(
                        f"SELECT conversation_id, COUNT(DISTINCT sender_id) "
                        f"FROM conversation_messages "
                        f"WHERE conversation_id IN ({placeholders}) "
                        f"AND COALESCE(is_from_self, 0)=0 "
                        f"AND sender_id IS NOT NULL AND sender_id != '' "
                        f"AND lower(sender_id) != 'self' "
                        f"GROUP BY conversation_id",
                        chunk,
                    ).fetchall():
                        sender_n[str(conv_id)] = int(n or 0)
                except sqlite3.Error:
                    pass
        for conv_id in conv_ids:
            n_part = participant_n.get(conv_id)
            n_send = sender_n.get(conv_id)
            if n_part is not None:
                if n_part <= 2:
                    dyadic.add(conv_id)
            elif n_send is not None and n_send <= 1:
                dyadic.add(conv_id)

    if dyadic and has_self:
        for chunk in _appearance_chunks(list(dyadic)):
            placeholders = ",".join("?" for _ in chunk)
            try:
                for conv_id, n in conn.execute(
                    f"SELECT conversation_id, COUNT(*) FROM conversation_messages "
                    f"WHERE conversation_id IN ({placeholders}) "
                    f"AND COALESCE(is_from_self, 0)=1 "
                    f"GROUP BY conversation_id",
                    chunk,
                ).fetchall():
                    for nid in conv_to_nids.get(str(conv_id), ()):
                        totals[nid] += int(n or 0)
            except sqlite3.Error:
                pass
            try:
                owner_rows = conn.execute(
                    f"SELECT {select_sql} FROM conversation_messages "
                    f"WHERE conversation_id IN ({placeholders}) "
                    f"AND COALESCE(is_from_self, 0)=1 "
                    f"ORDER BY {when_sql} DESC",
                    chunk,
                ).fetchall()
            except sqlite3.Error:
                owner_rows = []
            for row in owner_rows:
                conv_id = str(row[1] or "")
                for nid in conv_to_nids.get(conv_id, ()):
                    take(nid, row)

    for nid, bucket in fetched.items():
        bucket.sort(key=lambda r: str(r[4] or ""), reverse=True)
        built: List[Dict[str, Any]] = []
        for message_id, _conv, _sender, content, when, source_id, authored in bucket[:fetch]:
            text = _excerpt_around(str(content or ""), "")
            row = _finalize_appearance(
                source_id=source_id, text=text, when=when, authored=authored,
                kind=APPEARANCE_PARTICIPATED, record_id=message_id,
            )
            if row:
                built.append(row)
        rows_out[nid] = built
    return rows_out, totals


def _merge_appearances(
    mentioned: List[Dict[str, Any]],
    mention_total: int,
    participated: List[Dict[str, Any]],
    participation_total: int,
    *,
    show: int,
) -> Tuple[List[Dict[str, Any]], int]:
    merged: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str]] = set()
    seen_records: Set[str] = set()

    def consider(row: Dict[str, Any]) -> None:
        rid = str(row.get("record_id") or "")
        if rid and rid in seen_records:
            return
        key = (
            str(row.get("source_id") or ""),
            str(row.get("at") or "")[:10],
            str(row.get("text") or "")[:80].lower(),
        )
        if key in seen:
            return
        seen.add(key)
        if rid:
            seen_records.add(rid)
        merged.append(row)

    rich = [row for row in mentioned if row.get("_rich")]
    thin = [row for row in mentioned if not row.get("_rich")]
    for row in rich + participated + thin:
        consider(row)
    merged.sort(key=lambda row: str(row.get("at") or ""), reverse=True)
    overlap = 0
    mention_ids = {str(r.get("record_id") or "") for r in mentioned if r.get("record_id")}
    part_ids = {str(r.get("record_id") or "") for r in participated if r.get("record_id")}
    if mention_ids and part_ids:
        overlap = len(mention_ids & part_ids)
    total = int(mention_total or 0) + int(participation_total or 0) - overlap
    chosen = merged[:show]
    for row in chosen:
        row.pop("_rich", None)
        row.pop("record_id", None)
    return chosen, max(total, len(merged))


def batch_person_appearances(
    conn: Any,
    nodes: Sequence[Dict[str, Any]],
    *,
    show: int = APPEARANCE_SHOW,
    fetch: int = APPEARANCE_FETCH,
) -> Dict[str, Dict[str, Any]]:
    """Connector-agnostic appearances for many people. Batched IN queries, never N+1.

    A new connector that writes `entity_mentions` or the canonical conversation
    tables shows up here with no call-site edits.
    """
    mentioned, mention_totals = _appearance_mentions(conn, nodes, fetch=fetch)
    participated, part_totals = _appearance_participation(conn, nodes, fetch=fetch)
    out: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        nid = str(node.get("node_id") or "")
        if not nid:
            continue
        mentions, total = _merge_appearances(
            mentioned.get(nid, []),
            mention_totals.get(nid, 0),
            participated.get(nid, []),
            part_totals.get(nid, 0),
            show=show,
        )
        out[nid] = {
            "mentions": mentions,
            "total": total,
            "mention_total": int(mention_totals.get(nid, 0)),
            "participation_total": int(part_totals.get(nid, 0)),
        }
    return out


def person_provenance(conn: Any, node: Dict[str, Any], *, limit: int = 20) -> Dict[str, Any]:
    """"Why is this person here?" — mentioned in text, or a party to a record.

    Mentioned and participated are collected together so a DM peer with no NER
    hit is not an empty card, and a journal-only name still shows the line they
    were named in. `source_id` is data on each row, never a switch.
    """
    packed = batch_person_appearances(
        conn, [node], show=int(limit), fetch=max(int(limit), APPEARANCE_FETCH),
    )
    item = packed.get(str(node.get("node_id") or ""), {}) or {}
    return {
        "node_id": node.get("node_id"),
        "label": node.get("label"),
        "band": node.get("band"),
        "band_reason": node.get("band_reason"),
        "messenger_keys": node.get("messenger_keys", []),
        "mentions": item.get("mentions") or [],
        "coverage": {
            "basis": (
                "records that named this person, or that they participated in, "
                "most recent first"
            ),
            "mentioned": item.get("mention_total", 0),
            "participated": item.get("participation_total", 0),
        },
    }


# --------------------------------------------------------------------------- structure

#: Edge types that are evidence two PEOPLE are connected to each other.
#:
#: `semantic_affinity` is deliberately absent. It measures how alike two people's records
#: read, which is a statement about text, not about acquaintance — rendering it as a social
#: tie would put a line between two strangers who happen to write similarly. On this corpus
#: it is 146 of 444 peer edges, so including it would have inflated the network by a third
#: with relationships nobody has.
STRUCTURAL_EDGE_TYPES = ("communicates_with", "co_occurrence")

#: Below this, a component is too small for "community" to mean anything; its members are
#: reported as unclustered rather than each being called a community of one.
MIN_COMMUNITY_SIZE = 3

#: Betweenness in a component this small is arithmetic, not insight: the middle node of a
#: three-person path scores a perfect 1.0. Measured here, that let November Romeo and a head of state top
#: the broker list ahead of every real contact. Below this the score is computed but not
#: offered as a finding.
MIN_COMPONENT_FOR_BROKERAGE = 6

#: Bands whose members belong in the STRUCTURE. Ambient is excluded: a celebrity seen on a
#: web page is not part of the owner's network, and letting one broker between two others
#: says something false about the owner's life. This is a display-structure decision, not a
#: deletion — the node is still on the graph and still searchable.
STRUCTURAL_BANDS = (BAND_CORE, BAND_NAMED, BAND_DISCUSSED)


def structural_metrics(conn: Any, dataset_id: str, nodes: List[Dict[str, Any]], *,
                       include_third_party: bool = False) -> Dict[str, Any]:
    """Communities, degree and betweenness on the EGO-REMOVED person network.

    Ego removal is what makes any of this mean something. The owner is connected to
    everybody on their own graph — leaving them in makes them the only broker, collapses
    every community into one blob, and inflates every centrality score. Measured here: with
    the owner in, 522 of 538 edges touch them and the layout is a featureless ring.

    Betweenness is the number worth having. It answers "who connects parts of my world that
    would otherwise not touch", which is exactly what a person cannot see from the inside and
    what neither message volume nor recency reveals.
    """
    import networkx as nx

    structural = [n for n in nodes
                  if not n.get("is_owner") and not n.get("dismissed")
                  and n.get("band", BAND_AMBIENT) in STRUCTURAL_BANDS]
    eligible = {str(n["node_id"]) for n in structural}
    by_entity = {str(n["entity_id"]): n["node_id"] for n in structural if n.get("entity_id")}
    owner_ids = {str(n["node_id"]) for n in nodes if n.get("is_owner")}

    # Built from EDGES ONLY. Adding every node first puts ~280 isolates in the graph, and
    # betweenness normalises by (n-1)(n-2)/2 — with n=436 the real brokers came out at 0.001
    # instead of 0.53, i.e. the strongest structural signal on the graph rounded to nothing.
    # A person with no measured connections has no betweenness to compute, not a tiny one.
    graph = nx.Graph()

    # 1. group co-presence — the owner was in the room, so this is first-party.
    for edge in build_person_edges(conn, dataset_id, nodes,
                                   include_third_party=include_third_party):
        if edge["attribution"] != ATTRIBUTION_CO_PRESENT:
            continue
        if edge["source"] not in eligible or edge["target"] not in eligible:
            continue
        graph.add_edge(edge["source"], edge["target"],
                       weight=float(edge.get("weight") or 1))

    # 2. the canonical person-to-person edges the KG already holds.
    placeholders = ",".join("?" for _ in STRUCTURAL_EDGE_TYPES)
    try:
        rows = conn.execute(
            f"SELECT src_entity_id, dst_entity_id, weight FROM entity_edges"
            f" WHERE edge_type IN ({placeholders})", STRUCTURAL_EDGE_TYPES).fetchall()
    except sqlite3.Error:
        rows = []
    for src, dst, weight in rows:
        a, b = by_entity.get(str(src)), by_entity.get(str(dst))
        if not a or not b or a == b or a not in eligible or b not in eligible:
            continue
        graph.add_edge(a, b, weight=float(weight or 1))

    if graph.number_of_edges() == 0:
        return {"communities": {}, "degree": {}, "betweenness": {},
                "coverage": {"reason": "no measured connections between your people yet"}}

    degree = nx.degree_centrality(graph)
    # Per-component: betweenness compares how much of the traffic INSIDE a person's own
    # corner of the network flows through them. Normalising across disconnected components
    # would let a large component's ordinary member outrank a small component's linchpin.
    betweenness: Dict[str, float] = {}
    brokerage_ok: Dict[str, bool] = {}
    for component in nx.connected_components(graph):
        big_enough = len(component) >= MIN_COMPONENT_FOR_BROKERAGE
        if len(component) < 3:
            scores = {str(x): 0.0 for x in component}
        else:
            scores = nx.betweenness_centrality(graph.subgraph(component), weight=None)
        betweenness.update(scores)
        for member in component:
            brokerage_ok[str(member)] = big_enough

    # Communities from greedy modularity, falling back to connected components — the point
    # is a stable grouping to lay out by, not a claim about social clubs.
    communities: Dict[str, int] = {}
    try:
        groups = list(nx.community.greedy_modularity_communities(graph))
    except Exception:  # noqa: BLE001
        groups = [set(component) for component in nx.connected_components(graph)]
    kept = 0
    for group in sorted(groups, key=len, reverse=True):
        if len(group) < MIN_COMMUNITY_SIZE:
            continue  # a pair is not a community, and calling it one crowds the legend
        kept += 1
        for member in group:
            communities[str(member)] = kept
    return {
        "communities": communities,
        "degree": {k: round(v, 5) for k, v in degree.items()},
        "betweenness": {k: round(v, 5) for k, v in betweenness.items()},
        # A score from a four-person component is arithmetic; the flag says which ones are
        # worth showing as a finding rather than as a number.
        "brokerage_meaningful": brokerage_ok,
        "coverage": {
            "basis": ("connections BETWEEN your people, with you removed — you are connected "
                      "to everyone here, so leaving you in makes you the only broker and "
                      "flattens every community"),
            "excluded": ("semantic similarity between two people is not evidence they know "
                         "each other"),
            "nodes": graph.number_of_nodes(),
            "edges": graph.number_of_edges(),
            "communities": kept,
        },
    }


# --------------------------------------------------------------------------- closeness

#: How close a tie reads from its STATE. Reciprocity, not volume, is what closeness is made
#: of: 98 of this node's 151 human ties are `broadcast_only` — high message counts flowing one
#: way, which is a mailing list, not a friendship. Sorting by volume would put those ahead of
#: the people the owner actually talks with.
TIE_STATE_CLOSENESS = {
    "active": 1.00,
    "cooling": 0.60,
    "dormant": 0.35,
    "one_sided": 0.20,
    "broadcast_only": 0.05,
}

#: Reciprocal months beyond this add nothing — the difference between talking back and forth
#: for six months and for a year is not the difference between a friend and a stranger.
RECIPROCITY_SATURATION = 6


def relationship_closeness(row: Dict[str, Any]) -> Dict[str, Any]:
    """How close a person is to the owner, 0..1, with the reason in words.

    Deliberately NOT the same axis as the structural metrics. Betweenness and community say
    how someone sits among the owner's OTHER people, with the owner removed. This says how
    they sit with the owner — and the two answer different questions, so the graph can use
    one for angle and the other for radius instead of muddling them into a single blob.

    Three ingredients, in the order they matter:

    1. **Reciprocity.** Whether it goes both ways, and for how many months. A tie that only
       ever carries one direction stops being a relationship, whatever its volume.
    2. **Recency.** A close tie that has gone quiet is not the same as a close tie.
    3. **Volume**, log-scaled and last. It breaks ties between people who are otherwise
       alike; it never promotes a broadcaster over a friend.
    """
    state = str(row.get("tie_state") or "").strip().lower()
    base = TIE_STATE_CLOSENESS.get(state, 0.3)

    reciprocal = float(row.get("reciprocal_periods") or 0)
    reciprocity = min(1.0, reciprocal / RECIPROCITY_SATURATION)

    total = float(row.get("total_msgs") or 0)
    volume = min(1.0, math.log10(total + 1) / 3.0) if total > 0 else 0.0

    recent_gap = row.get("recent_gap_days")
    try:
        gap = float(recent_gap) if recent_gap is not None else None
    except (TypeError, ValueError):
        gap = None
    # Half a year of silence halves it; the curve is gentle because people go quiet for
    # reasons that have nothing to do with closeness.
    recency = 1.0 if gap is None else max(0.35, 1.0 - (gap / 365.0))

    score = (0.50 * base + 0.30 * reciprocity + 0.20 * volume) * recency
    score = max(0.0, min(1.0, round(score, 4)))

    if state == "broadcast_only":
        reason = "messages arrive but nothing comes back"
    elif reciprocal >= 3:
        reason = f"back and forth across {int(reciprocal)} months"
    elif reciprocal >= 1:
        reason = f"back and forth in {int(reciprocal)} month{'s' if reciprocal > 1 else ''}"
    elif state in ("dormant", "cooling"):
        reason = f"{state}, {int(gap)} days quiet" if gap else state
    else:
        reason = "little back and forth recorded"
    return {"closeness": score, "closeness_reason": reason, "tie_state": state or None}


def attach_closeness(conn: Any, dataset_id: str, nodes: List[Dict[str, Any]]) -> None:
    """Stamp closeness onto every node that has a messaging tie with the owner."""
    from .messenger_directed import MESSENGER_DYAD_STATS_TABLE, SELF_KEY

    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({MESSENGER_DYAD_STATS_TABLE})")}
    except sqlite3.Error:
        return
    wanted = [c for c in ("total_msgs", "reciprocal_periods", "recent_gap_days", "tie_state")
              if c in cols]
    if not wanted:
        return
    try:
        rows = conn.execute(
            f"SELECT CASE WHEN a_key=? THEN b_key ELSE a_key END, {', '.join(wanted)}"
            f"  FROM {MESSENGER_DYAD_STATS_TABLE}"
            f" WHERE dataset_id=? AND involves_self=1", (SELF_KEY, dataset_id)).fetchall()
    except sqlite3.Error:
        return
    by_peer = {str(r[0]): dict(zip(wanted, r[1:])) for r in rows}

    for node in nodes:
        if node.get("is_owner"):
            node["closeness"] = 1.0
            node["closeness_reason"] = "this is you"
            continue
        best = None
        for key in node.get("messenger_keys", []):
            row = by_peer.get(str(key))
            if not row:
                continue
            scored = relationship_closeness(row)
            if best is None or scored["closeness"] > best["closeness"]:
                best = scored
        if best:
            node.update(best)
        else:
            # No messaging tie: closeness is UNKNOWN, not zero. A zero would place someone
            # the owner has never texted at the same distance as someone who ignores them.
            node["closeness"] = None
            node["closeness_reason"] = "no messages exchanged, so closeness is unknown"


# --------------------------------------------------------------------------- duplicates

#: Strongest first. A person who appears in two bands is ONE person seen two ways, and the
#: stronger sighting is the true one: someone you message and also mention is a core contact
#: who happens to also turn up in your journal, not an ambient name who happens to text you.
BAND_STRENGTH = {BAND_CORE: 3, BAND_NAMED: 2, BAND_DISCUSSED: 1, BAND_AMBIENT: 0}


def _identities_conflict(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """True when these cannot be the same person.

    Only CONTACT ids count. Two different contacts sharing a name are two address-book
    entries the owner (or their phone) kept apart, which is real evidence of two people.

    Two different ENTITY ids are not: extraction routinely emits one human twice, which is
    the whole duplicate problem. Dasha exists as `ent_4e1a089c…` (606 messages) and
    `ent_612aa44f…` (4 mentions), and treating that split as proof of two Dashas is exactly
    backwards.

    The name-collision danger — `Bravo Yankee` versus `Wendel Yankee` — is handled where it
    belongs: those have DIFFERENT names, so they never reach this function. Two people with
    genuinely identical names remain possible, which is why the link is derived at read,
    shown on the node, and undone by a `split`.
    """
    x, y = a.get("contact_id"), b.get("contact_id")
    return bool(x and y and str(x) != str(y))


def auto_link_duplicates(nodes: List[Dict[str, Any]], *, split_ids=()) -> List[Dict[str, Any]]:
    """Fold same-name nodes with COMPLEMENTARY evidence into one, at read time.

    On the live corpus every duplicate has the same shape: one `core` node holding the
    messaging identity and one `named` node holding the extracted entity — Dasha (606
    messages) beside Dasha (4 mentions). They are one person, and showing them twice makes
    the graph look careless and understates both halves of the relationship.

    Two guards keep this from inventing people:

    * **Complementary evidence only.** Two nodes the owner MESSAGES are two phone numbers
      that may well be two humans; those stay separate and go to the merge queue for a human
      to confirm. Folding happens only when one side is messaged and the other mentioned.
    * **No conflicting identity.** Different entity ids or different contact ids mean
      extraction already decided they are distinct.

    Derived at read, never written: `split_ids` (owner `split` overlay rows) suppress a link,
    so the owner can always pull one apart and it stays pulled apart.
    """
    split = {str(x) for x in (split_ids or ())}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for node in nodes:
        if node.get("is_owner") or node.get("needs_name") or node.get("dismissed"):
            continue
        # Display name AND the spine's aliases. Folding on the display name alone catches
        # only the exact-duplicate shape (one display name, twice) and misses the
        # commonest one: a person messaged under a full name and written down by first name.
        # Measured on the live node — "Rowan Alvestad" (498 messages, contact-linked) and
        # "Rowan" (12 mentions, the journal's name for the same human) sat as two nodes, so
        # every reading derived from the journal landed on the half of him with no card.
        #
        # An alias is the spine's own claim, not a guess made here, and the existing guards
        # still apply: only one messaged node may absorb, differing contact ids still block,
        # and a `split` still pulls it apart and keeps it apart.
        keys = {_normalized_name(node.get("label"))}
        keys.update(_normalized_name(a) for a in (node.get("aliases") or []))
        for key in keys:
            if len(key) < 3:
                continue  # a two-letter nickname is not evidence of anything
            groups.setdefault(key, []).append(node)

    absorbed: Set[str] = set()
    for key, group in groups.items():
        if len(group) < 2 or key in split:
            continue
        messaged = [n for n in group if n["evidence"]["messaged"]]
        mentioned = [n for n in group if not n["evidence"]["messaged"]]
        if len(messaged) != 1 or not mentioned:
            continue  # two messaged nodes may be two people — that is a question, not a fact
        keep = messaged[0]
        for other in mentioned:
            if str(other["node_id"]) in split or _identities_conflict(keep, other):
                continue
            # A node reachable by both its name and an alias appears in two groups; without
            # this its mention_count would be added to the keeper twice.
            if str(other["node_id"]) in absorbed:
                continue
            keep["mention_count"] = int(keep.get("mention_count", 0)) + int(other.get("mention_count", 0))
            keep["evidence"]["mentioned"] = True
            if not keep.get("entity_id") and other.get("entity_id"):
                keep["entity_id"] = other["entity_id"]
            keep["sources"] = sorted(set(keep.get("sources", [])) | set(other.get("sources", [])))
            keep.setdefault("linked_from", []).append(
                {"node_id": other["node_id"], "label": other.get("label"),
                 "band": other.get("band")})
            absorbed.add(str(other["node_id"]))
            # The strongest sighting wins the band (owner instruction 2026-08-27): someone
            # you message AND mention is a core contact, not an ambient name.
            if BAND_STRENGTH.get(str(other.get("band")), 0) > BAND_STRENGTH.get(str(keep.get("band")), 0):
                keep["band"] = other["band"]
                keep["band_reason"] = other.get("band_reason", "")
        if keep.get("linked_from"):
            keep["auto_linked"] = True
            keep["band_reason"] = (f"{keep.get('band_reason', '')}"
                                   f" · also mentioned in your records").strip(" ·")
    return [n for n in nodes if str(n["node_id"]) not in absorbed]


# --------------------------------------------------------------------------- fact closeness

#: Closeness tiers the relationships pack assigns, strongest first. This is the vocabulary the
#: owner already thinks in — inner circle, close, regular, peripheral.
TIER_CLOSENESS = {
    "inner_circle": 0.92,
    "close": 0.78,
    "regular": 0.55,
    "peripheral": 0.30,
}

#: Relationship events the pack records. `loss` and `conflict` are NOT lowered: a falling-out
#: happens between people who matter to each other, and the record does not say it ended.
EVENT_CLOSENESS = {
    "met": 0.62,
    "reconnected": 0.72,
    "loss": 0.70,
    "conflict": 0.66,
}

#: Facts the OWNER stated, as against ones synthesis inferred.
#:
#: This distinction is load-bearing and cost a correction. All 50 `rel.closeness_tier` facts
#: here carry `altitude: inferred` with `source_refs` pointing at `messenger_dyad_stats` —
#: they are a model's reading of the very message statistics the closeness score is already
#: built from. Letting them move the score is the graph agreeing with itself and calling that
#: corroboration, and worse, it means they can never surface the case that motivated this: a
#: mother texted monthly. The 33 `stated` facts come from journal entries the owner wrote,
#: carry a quote, and say things volume cannot.
STATED_ALTITUDE = "stated"

#: A relationship fact about somebody never messaged is weaker than the same fact about
#: somebody they text. `Echo Victor` carries a relationship_event purely from being
#: written about; uncapped he lands in the inner ring beside actual friends.
FACT_CAP_WITHOUT_INTERACTION = 0.66


#: Bounded so a card showing six facts cannot ship six whole journal entries.
EVIDENCE_TEXT_CHARS = 320

#: The source tables a fact's ref can be resolved back to a READABLE record in, and how.
#: Anything not listed here is a derived row, not a document — see `_attach_evidence`.
_EVIDENCE_TABLES: Dict[str, Dict[str, str]] = {
    "journal_entries": {
        "pk": "entry_id", "at": "entry_at", "text": "content",
        "where": "place_name", "label": "Journal entry",
    },
    "conversation_messages": {
        "pk": "message_id", "at": "event_at", "text": "content",
        "where": "", "label": "Message",
    },
}


def _attach_evidence(
    conn: Any, pending: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]
) -> None:
    """Resolve each fact's source refs into something the owner can actually read.

    Two kinds arrive here and only one of them is a document.

    A STATED fact points at a journal entry or a message — real text, worth opening, and the
    thing that makes a fact checkable rather than merely asserted.

    An INFERRED fact points at a DERIVED row, and its `record_id` is not that table's key.
    Measured on the live node: 0 of 23 `entity_edges` refs resolve by `edge_id`, and
    `messenger_dyad_stats` has a composite primary key with no single record id at all. Those
    refs carry their evidence in the ref's own `note` ("550 msgs, balance -0.16, 5 reciprocal
    periods"), which IS the statistic. It is shown as one, not dressed up as a quote — the
    card already distinguishes "you wrote this" from "inferred from your data", and evidence
    that looked like a source document would undo that distinction.

    A ref that resolves to NOTHING is reported as missing rather than dropped. Silently
    omitting it would leave the card showing fewer sources than the fact was built from,
    which reads as a smaller claim instead of a broken link.
    """
    wanted: Dict[str, set] = {}
    for _entry, refs in pending:
        for ref in refs:
            table = str(ref.get("table") or "")
            if table in _EVIDENCE_TABLES and ref.get("record_id"):
                wanted.setdefault(table, set()).add(str(ref["record_id"]))

    found: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for table, ids in wanted.items():
        spec = _EVIDENCE_TABLES[table]
        cols = [spec["pk"], spec["at"], spec["text"]]
        if spec["where"]:
            cols.append(spec["where"])
        id_list = list(ids)
        # Chunked: SQLite's default variable limit is 999 and a busy node can carry more
        # refs than that once several people are selected in one session.
        for start in range(0, len(id_list), 500):
            chunk = id_list[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            try:
                rows = conn.execute(
                    f"SELECT {', '.join(cols)} FROM {table} "  # noqa: S608 - names are ours
                    f"WHERE {spec['pk']} IN ({placeholders})",
                    chunk,
                ).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                found[(table, str(row[0]))] = {
                    "at": row[1],
                    "text": row[2],
                    "where": row[3] if spec["where"] and len(row) > 3 else None,
                }

    for entry, refs in pending:
        sources: List[Dict[str, Any]] = []
        for ref in refs:
            table = str(ref.get("table") or "")
            record_id = str(ref.get("record_id") or "")
            note = ref.get("note")
            spec = _EVIDENCE_TABLES.get(table)
            hit = found.get((table, record_id)) if spec else None
            if hit:
                text = str(hit.get("text") or "").strip()
                sources.append({
                    "kind": "record",
                    "label": spec["label"],
                    "at": hit.get("at"),
                    "text": text[:EVIDENCE_TEXT_CHARS],
                    "truncated": len(text) > EVIDENCE_TEXT_CHARS,
                    "where": hit.get("where") or None,
                    "table": table,
                    "record_id": record_id,
                })
            elif note:
                sources.append({
                    "kind": "measure",
                    "label": "Measured from your messages",
                    "detail": str(note),
                    "table": table,
                })
            else:
                # Named, and honestly unavailable. 3 of 21 journal refs and 1 of 9 message
                # refs on the live node point at records that are no longer there.
                sources.append({
                    "kind": "missing",
                    "label": "Source no longer in your database",
                    "table": table,
                    "record_id": record_id,
                })
        entry["sources"] = sources


#: A theme, not a coincidence. One entity in common is two people mentioning Texas once.
SHARED_OWNER_MIN_ENTITIES = 2
#: And it has to have been said more than in passing.
SHARED_OWNER_MIN_MENTIONS = 4
#: At least one of them has to RECUR. Measured, the two gates above still admitted a case of
#: four entities at one mention each — four coincidences, not a shared interest.
#:
#: Was 3, lowered to 2 after measuring the whole ladder on a live node (427 people). The join
#: reaches 34 of them AT ALL, so no gate setting buys reach — the ceiling is how many people
#: have entities extracted from what they sent. Within that ceiling the settings read:
#:     (2,4,3) → 3 people, all place      (2,4,2) → 5, all place
#:     (2,3,2) → 7, but two of them junk: "Asian, French" typed as orgs (they are cuisines),
#:               and an AI assistant beside a musician, both typed as shared PEOPLE.
#: So 2 is the last honest step: +2 real readings, nothing mis-typed admitted. A wider
#: co-occurrence signal — other owner entities in any record that mentions this person —
#: was measured too: 86 nodes reached, but its survivors are extraction artefacts. Three
#: misspellings of one musician came back as three separate PEOPLE in common; a venue named
#: after a second musician came back as a fourth; a wiki tool came back as a fifth. What it
#: offered as shared ground for the rest was two employers everyone in the record works
#: with. More reach, worse precision, so it is not the lane.
SHARED_OWNER_MIN_TOP_MENTIONS = 2
SHARED_OWNER_TOP = 3

#: How each entity type reads on a card. The card answers "what do we have in common" and
#: the answer should be a word, not a schema value.
_SHARED_OWNER_KIND_WORDS: Dict[str, str] = {
    "place": "Places",
    "org": "Organisations",
    "project": "Projects",
    "topic": "Topics",
    "person": "People",
    "product": "Things",
    "event": "Events",
    "work_of_art": "Works",
    "goal": "Goals",
}


#: One session together is co-presence, not collaboration. Two is the floor at which
#: "you work on this together" is a description of a pattern rather than of an evening.
#:
#: Measured on the live journal (2026-08-28): 56 people share at least one declared
#: session with the owner, 19 share two or more, across 23 person×project pairs. The
#: floor costs 37 readings and every one it drops is a single co-occurrence.
COACTIVITY_MIN_SESSIONS = 2

#: Kinds that can be WORKED ON. A person co-occurring with a place is where they were,
#: not what they were doing, and `shared_with_owner` already says that better.
COACTIVITY_KINDS = ("project", "org")


def attach_coactivity(conn: Any, nodes: List[Dict[str, Any]]) -> Dict[str, int]:
    """What this person and the owner DO together, from the owner's own declaration.

    The card could say how much a relationship happens — volume, balance, recency — and
    never what it was about, because every reading on it was a transform of a message
    count. This is the other witness: the owner writes a journal entry naming who they
    were with and what they were working on, in two adjacent columns, and the entity
    spine folds both into one record's co-occurrence bucket.

    So this reads the SPINE, not the artifact: `entity_edges` already carries a resolved
    person↔project edge with an evidence count, and reading it here means the number on
    the card is the number of sessions the owner logged. Matching the artifact's name
    slug instead would have to guess which "Rowan" it meant, and on this node it would
    guess wrong — there are two.

    Measured before this existed: the owner's closest person by every message statistic
    (0.9224 of 428) had ten declared Topos sessions across three months, and his card
    said "Warm · Inner circle" and nothing else.
    """
    from ..features.entities.structured_fields import STRUCTURED_CONFIDENCE

    if not _appearance_table_exists(conn, "entity_mentions"):
        return {"attached": 0}

    by_entity = {
        str(n["entity_id"]): n
        for n in nodes
        if n.get("entity_id") and not n.get("is_owner")
    }
    if not by_entity:
        return {"attached": 0}

    # Counted from the JOURNAL RECORDS themselves, not from `entity_edges`.
    #
    # Reading the co_occurrence edge was the obvious implementation and it was wrong: that
    # edge is minted wherever two entities land in one record of ANY kind, so a browsing
    # session and a news article count the same as a working afternoon. Run against the
    # live node it attached 42 readings, and the top of the list was "a head of state · a nationality · 6
    # sessions", "a public figure · ChatGPT · 4" and the owner co-occurring with a jobs board.
    # None of those is a session anybody logged, and the word "sessions" made the number a
    # claim rather than a count.
    #
    # So: both ends must be mentioned in the SAME journal entry, and the person must be
    # there because the owner DECLARED them — STRUCTURED_CONFIDENCE, the participant column
    # — not because a model found a name in the prose. That is what makes "10 sessions"
    # mean the thing a reader assumes it means.
    kinds = ",".join("?" * len(COACTIVITY_KINDS))
    try:
        rows = conn.execute(
            "SELECT pm.entity_id, om.entity_id, oe.canonical_name, oe.entity_type,"
            "       COUNT(DISTINCT pm.record_id), MAX(pm.event_at)"
            "  FROM entity_mentions pm"
            "  JOIN entity_mentions om ON om.record_id = pm.record_id"
            "                         AND om.canonical_table = 'journal_entries'"
            "  JOIN entities oe ON oe.entity_id = om.entity_id"
            " WHERE pm.canonical_table = 'journal_entries'"
            "   AND pm.confidence >= ?"
            "   AND pm.entity_id <> om.entity_id"
            f"   AND oe.entity_type IN ({kinds})"
            " GROUP BY 1, 2, 3, 4",
            (STRUCTURED_CONFIDENCE, *COACTIVITY_KINDS),
        ).fetchall()
    except sqlite3.Error:
        return {"attached": 0}

    #: node_id -> [(sessions, label, kind, last_at), ...]
    per_node: Dict[str, List[Tuple[int, str, str, Any]]] = defaultdict(list)
    for person_id, _other_id, label, kind, sessions, at in rows:
        node = by_entity.get(str(person_id))
        if node is None:
            continue
        sessions = int(sessions or 0)
        if sessions < COACTIVITY_MIN_SESSIONS:
            continue
        per_node[str(node["node_id"])].append(
            (sessions, str(label or ""), str(kind or ""), at)
        )

    by_node = {str(n.get("node_id") or ""): n for n in nodes}
    attached = 0
    for node_id, entries in per_node.items():
        node = by_node.get(node_id)
        if node is None:
            continue
        entries.sort(key=lambda e: (-e[0], e[1]))
        sessions, label, kind, last_at = entries[0]
        node["coactivity"] = {
            "label": label,
            "kind": kind,
            "sessions": sessions,
            "last_at": last_at,
            # The owner typed this into a column; it is not a model's reading of prose,
            # and the card must be able to say so — a declared fact and an inferred one
            # are not two witnesses agreeing.
            "declared": True,
            # Everything else they work on together, so a tooltip can be honest about
            # what the headline leaves out.
            "also": [
                {"label": lbl, "sessions": n} for n, lbl, _k, _at in entries[1:4]
            ],
        }
        attached += 1
    return {"attached": attached}


def attach_shared_with_owner(conn: Any, nodes: List[Dict[str, Any]]) -> Dict[str, int]:
    """What the owner and this person BOTH engage with, and what KIND of thing it is.

    The graph already computes affinity between two OTHER people (`shared_context_affinity`),
    and measured on the live node not one of its 45 rows involves the owner — so a card could
    say two of your contacts share a subject, and never what YOU share with the person you
    are looking at. That is the more useful of the two, and it was missing.

    An entity counts only if the owner has WRITTEN about it (`authored_by_owner`) and it also
    appears in a record this person took part in. That direction matters: an entity the owner
    merely received is not evidence of a shared interest, it is evidence of being told.

    Gated hard, because the tail is noise. Three shapes measured on a live node:
      · the strong case — 13 shared entities over 42 mentions, the top three at x11, x10
        and x5, all of one kind. That is a real "you talk about the same places".
      · the tie case — 4 entities, every one of them x1, spread across five kinds. A
        dominant "kind" there is an artefact of ties, not a shared interest.
      · the thin case — two `org` rows that are actually cuisines. Sparse AND mis-typed.
    So: at least two distinct entities of the dominant kind, at least four mentions behind
    them, and the kind must actually dominate. Otherwise the node says nothing, which is the
    correct output for "nothing in common that the record can show".
    """
    if not _appearance_table_exists(conn, "entity_mentions"):
        return {"attached": 0}

    try:
        owner_entity_ids = [
            str(r[0]) for r in conn.execute(
                "SELECT DISTINCT entity_id FROM entity_mentions "
                "WHERE authored_by_owner = 1 AND entity_id IS NOT NULL")
        ]
    except sqlite3.Error:
        return {"attached": 0}
    if not owner_entity_ids:
        return {"attached": 0}

    names: Dict[str, str] = {}
    kinds: Dict[str, str] = {}
    try:
        for eid, cname, etype in conn.execute(
                "SELECT entity_id, canonical_name, entity_type FROM entities"):
            names[str(eid)] = str(cname or "")
            kinds[str(eid)] = str(etype or "")
    except sqlite3.Error:
        return {"attached": 0}

    # sender handle -> node, the same keying the appearance lane uses
    key_to_nid: Dict[str, str] = {}
    for node in nodes:
        if node.get("is_owner"):
            continue
        nid = str(node.get("node_id") or "")
        if not nid:
            continue
        for key in _peer_handle_keys(node):
            key_to_nid.setdefault(key, nid)
    if not key_to_nid:
        return {"attached": 0}

    # Query by SENDER and filter the owner set in Python. Both lists are long — 381 owner
    # entities against every handle on the graph — and one query carrying both would run
    # past SQLite's 999-variable limit on a busy node, silently, as an sqlite3.Error that
    # this function is written to swallow.
    owner_set = set(owner_entity_ids)
    per_node: Dict[str, Counter] = defaultdict(Counter)
    for chunk in _appearance_chunks(list(key_to_nid), 400):
        placeholders = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                "SELECT cm.sender_id, em.entity_id, COUNT(*) "
                "FROM conversation_messages cm "
                "JOIN entity_mentions em ON em.record_id = cm.message_id "
                f"WHERE cm.sender_id IN ({placeholders}) "
                "GROUP BY 1, 2",
                chunk,
            ).fetchall()
        except sqlite3.Error:
            continue
        for sender, eid, n in rows:
            eid = str(eid or "")
            if eid not in owner_set:
                continue
            nid = key_to_nid.get(str(sender or ""))
            if not nid:
                continue
            per_node[nid][eid] += int(n or 0)

    by_node = {str(n.get("node_id") or ""): n for n in nodes}
    attached = 0
    for nid, hits in per_node.items():
        node = by_node.get(nid)
        if node is None:
            continue
        by_kind: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
        for eid, n in hits.items():
            kind = kinds.get(eid) or ""
            if not kind:
                continue
            by_kind[kind].append((names.get(eid) or eid, n))
        if not by_kind:
            continue
        kind, entries = max(
            by_kind.items(), key=lambda kv: (len(kv[1]), sum(n for _, n in kv[1])))
        total = sum(n for _, n in entries)
        entries.sort(key=lambda e: (-e[1], e[0]))
        if (len(entries) < SHARED_OWNER_MIN_ENTITIES
                or total < SHARED_OWNER_MIN_MENTIONS
                or entries[0][1] < SHARED_OWNER_MIN_TOP_MENTIONS):
            continue
        # NAMED examples must have been seen more than once. The gate above measures the
        # BREADTH of shared ground and lets a kind through on the strength of one repeated
        # entity; the names on the chip are a different promise, and a single co-mention is
        # a coincidence wearing a shared interest's clothes.
        #
        # Reported by the owner, 2026-08-28, looking at his closest collaborator's card:
        # "Halden Vry appeared in Rowan's, which I would rank way less important". The set
        # behind that chip was one person at ×2 and four entities at ×1 —
        # ties at one, so the second and third names were alphabetical noise, and one of
        # them is a PLACE the extractor had typed as a person.
        #
        # Raising the gate instead was measured and rejected: MIN_TOP_MENTIONS 2→3 halves
        # the feature, 12 people to 6, to fix a presentation problem. This keeps every
        # reading and drops only the names that were never evidence — Kim's "Arlington×1",
        # Mom's "the Statue of Liberty park×1", Rowan's Halden Vry.
        evidenced = [(name, n) for name, n in entries if n >= SHARED_OWNER_MIN_TOP_MENTIONS]
        node["shared_with_owner"] = {
            "kind": kind,
            "label": _SHARED_OWNER_KIND_WORDS.get(kind, kind.replace("_", " ").title()),
            "examples": [name for name, _ in evidenced[:SHARED_OWNER_TOP]],
            "entity_count": len(entries),
            # How many of those were seen more than once — the ones the chip is willing to
            # NAME. Carried so the tooltip can say "5 in common, 1 more than once" instead
            # of claiming five and listing one.
            "evidenced_count": len(evidenced),
            "mention_count": total,
        }
        attached += 1
    return {"attached": attached}


def person_relationship_facts(conn: Any, nodes: List[Dict[str, Any]]) -> Dict[str, int]:
    """Attach the owner's relationship facts to the people they are about.

    These belong on the person's card: a tier the owner can disagree with, an event with the
    sentence that produced it, and whether the owner said it or a pack inferred it. A card
    showing a closeness number without the fact behind it asks to be trusted rather than read.
    """
    by_entity = {str(n["entity_id"]): n for n in nodes if n.get("entity_id")}
    by_name: Dict[str, Dict[str, Any]] = {}
    for n in nodes:
        key = _normalized_name(n.get("label"))
        if key and not n.get("is_owner"):
            by_name.setdefault(key, n)
    # Degrades to UNDATED facts rather than to none. The `except sqlite3.Error` below used to
    # swallow the whole read, so adding `valid_from` to the SELECT silently dropped every
    # relationship fact anywhere the column was absent — the fixture caught it here, but on a
    # node it would have emptied the card with nothing logged. A fact without its date is
    # still the fact; losing all 83 of them to one missing column is not a trade worth making.
    rows: List[Any] = []
    dated = True
    try:
        rows = conn.execute(
            "SELECT payload_json, confidence, source_refs_json, valid_from "
            "FROM signal_objects").fetchall()
    except sqlite3.Error:
        dated = False
        try:
            rows = [
                (payload, confidence, refs, None)
                for payload, confidence, refs in conn.execute(
                    "SELECT payload_json, confidence, source_refs_json FROM signal_objects")
            ]
        except sqlite3.Error:
            return {"attached": 0}

    attached = 0
    #: (fact, its refs) — resolved in ONE batched pass after the scan rather than a query
    #: per fact, which would put ~60 lookups on a read that answers in single-digit ms.
    pending: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    for payload, confidence, refs, valid_from in rows:
        try:
            fact = json.loads(payload or "{}")
        except (TypeError, ValueError):
            continue
        predicate = str(fact.get("predicate") or "")
        if not predicate.startswith("rel."):
            continue
        struct = fact.get("value_struct") or {}
        # The fact names its person. `object_entity_id` is often absent, and where present it
        # suffers the same duplicate-entity split the graph already folds.
        node = by_entity.get(str(fact.get("object_entity_id") or ""))
        if node is None:
            node = by_name.get(_normalized_name(struct.get("person")))
        if node is None or node.get("is_owner"):
            continue
        parsed: List[Dict[str, Any]] = []
        try:
            loaded = json.loads(refs or "[]")
            if isinstance(loaded, list):
                parsed = [r for r in loaded if isinstance(r, dict)]
        except (TypeError, ValueError):
            parsed = []
        note = None
        if parsed:
            note = parsed[0].get("note") or parsed[0].get("table")
        entry = {
            "predicate": predicate,
            "tier": struct.get("tier"),
            "event": struct.get("event"),
            "quote": fact.get("quote"),
            "confidence": confidence,
            "altitude": fact.get("altitude"),
            "stated_by_owner": str(fact.get("asserted_by") or "") == "owner",
            "pack": fact.get("pack"),
            "evidence": note,
            # WHEN the fact is about, not when the row was written. `valid_from` is populated
            # on all 83 rel.* facts on the live node with 76 distinct values, so it carries
            # real signal; `created_at` is extraction time and would date every fact to the
            # day the pack last ran.
            "at": valid_from,
            "sources": [],
        }
        node.setdefault("facts", []).append(entry)
        pending.append((entry, parsed))
        attached += 1
    _attach_evidence(conn, pending)
    for node in nodes:
        for fact in node.get("facts", []):
            if fact.get("tier"):
                node["relationship_tier"] = fact["tier"]
                break
    return {"attached": attached, "dated": dated}


def attach_fact_closeness(conn: Any, nodes: List[Dict[str, Any]]) -> Dict[str, int]:
    """Raise closeness where a STATED relationship fact says more than the traffic does.

    A floor, never a replacement, and it can only pull a person IN: absence of a relationship
    fact is silence, not evidence of distance. Inferred tiers still ride along on the card,
    where the owner can see and disagree with them — they just do not move the number.
    """
    person_relationship_facts(conn, nodes)
    applied = 0
    for node in nodes:
        if node.get("is_owner"):
            continue
        best, reason = None, None
        for fact in node.get("facts", []):
            if str(fact.get("altitude")) != STATED_ALTITUDE:
                continue
            if fact.get("tier") in TIER_CLOSENESS:
                candidate = TIER_CLOSENESS[str(fact["tier"])]
                text = f"you place them in your {str(fact['tier']).replace('_', ' ')}"
            elif str(fact.get("predicate")) == "rel.caregiving":
                candidate, text = 0.90, "your records show you caring for them"
            elif fact.get("event") in EVENT_CLOSENESS:
                candidate = EVENT_CLOSENESS[str(fact["event"])]
                text = f"you wrote about {fact['event']} with them"
            else:
                continue
            if best is None or candidate > best:
                best, reason = candidate, text
        if best is None:
            continue
        if not node.get("evidence", {}).get("messaged"):
            best = min(best, FACT_CAP_WITHOUT_INTERACTION)
            reason = f"{reason} — though you have not messaged them"
        current = node.get("closeness")
        if current is not None and current >= best:
            continue
        node["closeness"] = round(best, 4)
        node["closeness_source"] = "facts"
        node["closeness_reason"] = reason
        applied += 1
    for n in nodes:
        n.setdefault("closeness_source", "messages" if n.get("closeness") is not None else None)
    return {"applied": applied}





# --------------------------------------------------------------------------- ambient groups

#: Search engines and mail. Everyone the owner ever looked up passes through these, so a
#: domain grouping built on them is a bucket of unrelated strangers — google.com alone holds
#: 42 of this node's ambient people.
NON_TOPICAL_DOMAINS = frozenset({
    "google.com", "mail.google.com", "docs.google.com", "duckduckgo.com", "bing.com",
    "search.yahoo.com", "chatgpt.com", "chat.openai.com",
})

#: A pair is not a cluster. Below this the members are left ungrouped rather than each being
#: called a group of one — 23 topic clusters here hold exactly one ambient person.
MIN_AMBIENT_GROUP = 3

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def _tokens(text: Any) -> Set[str]:
    return {t for t in _WORD_SPLIT.split(str(text or "").lower())
            if len(t) > 2 and t not in ("com", "org", "net", "www", "the", "and")}


def _domain_of(record_id: Any) -> Optional[str]:
    match = re.search(r"https?://([^/]+)", str(record_id or ""))
    return match.group(1).replace("www.", "").lower() if match else None


def group_ambient_people(conn: Any, nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Cluster the ambient names by what they were seen ALONGSIDE.

    Ambient is 173 of 437 people here and reads as an undifferentiated fringe, but the fringe
    is not one thing: it holds classical poets, GitHub collaborators, LinkedIn contacts, film
    actors and several pieces of software that extraction mistook for people. Two signals in
    the record already separate them, and neither needs anything embedded.

    1. **Topic cluster** — a person's records are already assigned to labelled topic clusters,
       reachable by joining on `record_id`. Covers 153 of 173.
    2. **Domain** — failing that, the site the name was read on. Covers 87 more loosely.

    Site-name clusters are rejected by a DERIVED test rather than a list: a cluster whose
    label echoes the domain its members were browsing is describing a website, not a subject.
    That catches `Google Trends` (google.com) and `YouTube Studio` (youtube.com), which
    between them held 44 of the 153 and would have been the two largest groups on screen.

    Known limitation, accepted deliberately: a genuinely topical site NAMED after its topic
    would be rejected too. The obvious alternative — requiring the cluster to draw from one
    dominant domain — was measured and is worse: `Monologues of a Native Prince` is 100%
    single-domain and is a real subject, while `Google Trends` spans eleven domains and is
    not. Concentration says nothing here; the label does.
    """
    ambient = {str(n["entity_id"]): n for n in nodes
               if n.get("band") == BAND_AMBIENT and n.get("entity_id") and not n.get("is_owner")}
    if not ambient:
        return {"grouped": 0, "groups": 0, "coverage": {"reason": "no ambient people"}}

    placeholders = ",".join("?" for _ in ambient)
    try:
        rows = conn.execute(
            f"SELECT m.entity_id, m.record_id, tcm.cluster_id FROM entity_mentions m"
            f"  JOIN topic_cluster_members tcm ON tcm.record_id = m.record_id"
            f" WHERE m.entity_id IN ({placeholders})", list(ambient)).fetchall()
    except sqlite3.Error:
        rows = []
    try:
        labels = {cid: lab for cid, lab in
                  conn.execute("SELECT cluster_id, label FROM topic_clusters")}
    except sqlite3.Error:
        labels = {}

    members: Dict[Any, Set[str]] = {}
    cluster_domains: Dict[Any, Counter] = {}
    for entity_id, record_id, cluster_id in rows:
        members.setdefault(cluster_id, set()).add(str(entity_id))
        domain = _domain_of(record_id)
        if domain:
            cluster_domains.setdefault(cluster_id, Counter())[domain] += 1

    def echoes_its_site(cluster_id: Any) -> bool:
        label_tokens = _tokens(labels.get(cluster_id))
        if not label_tokens:
            return True  # an unlabelled cluster cannot name itself on screen
        for domain, _n in (cluster_domains.get(cluster_id) or Counter()).most_common(3):
            if label_tokens & _tokens(domain):
                return True
        return False

    assigned: Dict[str, Dict[str, Any]] = {}
    # Largest first, so a person in several clusters lands in their most populated one and the
    # groups stay stable rather than depending on scan order. 27 of 153 sit in more than one.
    for cluster_id, group in sorted(members.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
        if len(group) < MIN_AMBIENT_GROUP or echoes_its_site(cluster_id):
            continue
        for entity_id in group:
            assigned.setdefault(entity_id, {
                "label": str(labels.get(cluster_id) or "").strip(),
                "kind": "topic", "key": f"topic:{cluster_id}"})

    # Domain fallback for whoever the topics missed.
    by_domain: Dict[str, Set[str]] = {}
    try:
        mention_rows = conn.execute(
            f"SELECT entity_id, record_id FROM entity_mentions"
            f" WHERE entity_id IN ({placeholders})", list(ambient)).fetchall()
    except sqlite3.Error:
        mention_rows = []
    for entity_id, record_id in mention_rows:
        if str(entity_id) in assigned:
            continue
        domain = _domain_of(record_id)
        if domain and domain not in NON_TOPICAL_DOMAINS:
            by_domain.setdefault(domain, set()).add(str(entity_id))
    for domain, group in sorted(by_domain.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(group) < MIN_AMBIENT_GROUP:
            continue
        for entity_id in group:
            assigned.setdefault(entity_id, {"label": domain, "kind": "site",
                                            "key": f"site:{domain}"})

    # A cluster passes the size test on its FULL membership, but larger clusters claim
    # shared members first — so a group that qualified can end up below the minimum once
    # everyone else has been taken. Live, that left "Notion integration" as a group of one.
    # Re-check on the final membership and release anyone left in a group too small to mean
    # anything; ungrouped is an honest answer, a group of one is not.
    final: Counter = Counter(a["key"] for a in assigned.values())
    assigned = {eid: a for eid, a in assigned.items()
                if final[a["key"]] >= MIN_AMBIENT_GROUP}

    keys = sorted({a["key"] for a in assigned.values()})
    index = {k: i for i, k in enumerate(keys)}
    for entity_id, group in assigned.items():
        node = ambient.get(entity_id)
        if not node:
            continue
        node["ambient_group"] = group["label"]
        node["ambient_group_kind"] = group["kind"]
        node["ambient_group_id"] = index[group["key"]]
    return {
        "grouped": len(assigned),
        "ungrouped": len(ambient) - len(assigned),
        "groups": len(keys),
        "coverage": {
            "basis": ("what each name was seen alongside — the topic cluster of the records "
                      "naming them, or failing that the site they were read on"),
            "excluded": ("clusters whose label echoes their own domain describe a website "
                         "rather than a subject, and search engines group strangers"),
            "min_group": MIN_AMBIENT_GROUP,
        },
    }


# --------------------------------------------------------------------------- SGU-13

#: A shared topic cluster wider than this fraction of the people it could cover is not a
#: subject, it is a stopword. Measured on the live node: at 0.30 it drops exactly two --
#: `Blackjack Team (always leave)` at 46 of 103 people and `Friends (movie)` at 35 -- and
#: keeps `Blue Hillbillies`, `Dialogues Technologies`, `Bandmates` and `CODAME ART+TECH`,
#: every one of which a size CAP would have thrown away. The cap was tried first: at 12 it
#: kept `Houseplants` and discarded the bands and the company, which is backwards.
CONTEXT_STOPWORD_SHARE = 0.30

#: ...but a share alone is meaningless on a small graph: with six people covered, 0.30 puts
#: the bound below two and EVERY subject is discarded as too broad, leaving a node with a
#: real answer being told it has none. A subject shared by fewer than this many people is
#: never a stopword however small the graph. On the live node the share bound is 30 people,
#: so this floor changes nothing there — it exists for the node that is just starting.
CONTEXT_STOPWORD_MIN_PEOPLE = 8

#: One shared topic is a coincidence; two is a pattern. Without this floor the strongest
#: score on the live node was 1.00 for pairs of unnamed numbers whose ONLY cluster was
#: `Clear Business Funding` -- a lead-generation blast. Cosine cannot tell a perfect match
#: from a match with nothing behind it, so the evidence floor has to be separate.
CONTEXT_MIN_SHARED = 2

#: Each person keeps only their strongest partners. A person in many clusters would otherwise
#: acquire an affinity to half the graph, and the layout would read their VOLUME as everyone
#: else's closeness -- the same mistake reciprocity-weighted closeness exists to avoid.
CONTEXT_TOP_PER_PERSON = 6


def _context_label_key(label: Any) -> str:
    """Fold near-duplicate cluster labels together.

    The cluster set itself contains `Friends`/`Friend` and `Blackjack Team`/`Blackjack Team
    (httpurl)`. Left alone each duplicate counts a pair twice, which is how a coincidence
    gets promoted to a pattern.
    """
    text = re.sub(r"\(.*?\)", " ", str(label or "")).lower()
    text = " ".join(re.sub(r"[^a-z0-9 ]+", " ", text).split())
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in text.split())


def shared_context_affinity(conn: Any, dataset_id: str,
                            nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """People who turn up in the same subjects, as a pull on the LAYOUT only.

    The graph places people by how they relate to the OWNER (distance) and to each other
    through messages (direction). Neither notices that two people belong to the same band,
    the same company or the same weekly thing unless they happen to have texted each other.
    Measured on the live node, that is nearly all of it: of 1,718 pairs who share a topic
    cluster, only 12 already have an edge drawn between them and 24 are already in the same
    detected community. About 98% of this is information the picture did not have.

    The join is the one `group_ambient_people` uses, pointed at the messaging people instead:
    person -> the conversations they are in -> those messages' topic clusters. Conversations
    come from `load_messages`, so peer keys get the same normalisation as everywhere else and
    no connector is named here.

    Scored as IDF-weighted cosine over each person's cluster set. Three failures were
    measured before this shape survived:

    * A plain sum of ``1/size`` ranked pairs by how MANY clusters each person appeared in,
      which is message volume wearing an affinity costume -- the top ten pairs were all
      driven by the same three broad clusters.
    * Cosine alone put the perfect 1.00 scores on people with a single cluster each, because
      two one-item vectors always match. Hence ``CONTEXT_MIN_SHARED``.
    * A cluster size cap discarded the real groups and kept the small talk. Hence a share of
      the population rather than a count.

    Returns pairs with the clusters that produced them. The graph does not draw these and
    never labels an edge with one, but a person's card can say what it found: a layout that
    moves people for reasons nothing can state is a layout that cannot be argued with.
    """
    people = {str(n["node_id"]): n for n in nodes
              if not n.get("is_owner") and n.get("band") != BAND_AMBIENT}
    if len(people) < 2:
        return {"pairs": [], "coverage": {"reason": "fewer than two people to relate"}}

    by_key: Dict[str, str] = {}
    for n in people.values():
        for mk in n.get("messenger_keys", []):
            by_key[str(mk)] = str(n["node_id"])

    from .messenger_directed import SELF_KEY, load_messages

    try:
        rows = load_messages(conn, dataset_id)
    except sqlite3.Error:
        rows = []
    # Everyone in a conversation owns that conversation's subjects. In a DM that is the one
    # peer; in a room it is everybody who spoke. The owner is excluded because they are in
    # every conversation, so counting them would make every subject universal.
    conv_people: Dict[str, Set[str]] = {}
    conv_messages: Dict[str, Set[str]] = {}
    for conv, message_id, sender, _at, from_self, _src, _reply in rows:
        conv_messages.setdefault(str(conv), set()).add(str(message_id))
        if from_self or not sender or str(sender) == SELF_KEY:
            continue
        node_id = by_key.get(str(sender))
        if node_id:
            conv_people.setdefault(str(conv), set()).add(node_id)

    message_ids = {m for conv in conv_people for m in conv_messages.get(conv, ())}
    if not message_ids:
        return {"pairs": [], "coverage": {"reason": "no conversations reach a person here"}}

    cluster_of: Dict[str, Any] = {}
    try:
        for chunk in _chunks(sorted(message_ids), 400):
            placeholders = ",".join("?" for _ in chunk)
            for record_id, cluster_id in conn.execute(
                    f"SELECT record_id, cluster_id FROM topic_cluster_members"
                    f" WHERE record_id IN ({placeholders})", chunk).fetchall():
                cluster_of[str(record_id)] = cluster_id
    except sqlite3.Error:
        return {"pairs": [], "coverage": {"reason": "this node has no topic clusters yet"}}
    try:
        labels = {cid: lab for cid, lab in
                  conn.execute("SELECT cluster_id, label FROM topic_clusters")}
    except sqlite3.Error:
        labels = {}

    # Fold duplicate labels before counting anyone, so a pair cannot be counted twice for
    # what is really one subject.
    members: Dict[str, Set[str]] = {}
    label_of: Dict[str, str] = {}
    for conv, folks in conv_people.items():
        for message_id in conv_messages.get(conv, ()):
            cluster_id = cluster_of.get(message_id)
            if cluster_id is None:
                continue
            raw = str(labels.get(cluster_id) or "").strip()
            key = _context_label_key(raw) or f"cluster:{cluster_id}"
            members.setdefault(key, set()).update(folks)
            label_of.setdefault(key, raw)

    covered = {p for folks in members.values() for p in folks}
    if len(covered) < 2:
        return {"pairs": [], "coverage": {"reason": "no subject reaches two people"}}

    too_broad = max(CONTEXT_STOPWORD_MIN_PEOPLE, CONTEXT_STOPWORD_SHARE * len(covered))
    subjects = {k: m for k, m in members.items() if 2 <= len(m) <= too_broad}
    dropped = sorted(((len(m), label_of.get(k, k)) for k, m in members.items()
                      if len(m) > too_broad), reverse=True)
    if not subjects:
        return {"pairs": [], "coverage": {
            "reason": "every shared subject was too broad to mean anything"}}

    # SMOOTHED, because the plain form is exactly zero for a subject that covers everyone
    # counted -- and on a small graph that is every subject, so two people sharing two niche
    # interests were told they share nothing. The niche is niche in the world; it only looks
    # universal because two people are all this node has yet. The +1s keep the ordering
    # (rarer still scores higher) and put a floor under it.
    idf = {k: math.log((len(covered) + 1) / (len(m) + 1)) + 1 for k, m in subjects.items()}
    of_person: Dict[str, Set[str]] = {}
    for key, folks in subjects.items():
        for person in folks:
            of_person.setdefault(person, set()).add(key)
    magnitude = {p: math.sqrt(sum(idf[k] ** 2 for k in keys))
                 for p, keys in of_person.items()}

    scored: List[Dict[str, Any]] = []
    ordered = sorted(of_person)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            shared = of_person[a] & of_person[b]
            if len(shared) < CONTEXT_MIN_SHARED:
                continue
            denominator = magnitude[a] * magnitude[b]
            if not denominator:
                continue
            # Shrunk by how much evidence is behind it. Cosine cannot tell a perfect match
            # from a match with nothing behind it: two people whose ONLY two subjects are
            # the same score 1.00, and on the live node the top of the list was three
            # unnamed numbers who had each received the same lead-generation blast. The
            # factor costs a well-evidenced pair almost nothing (27 shared subjects keeps
            # 93%) and halves a pair scraping the floor.
            overlap = len(shared)
            confidence = overlap / (overlap + CONTEXT_MIN_SHARED)
            score = confidence * sum(idf[k] ** 2 for k in shared) / denominator
            scored.append({
                "source": a, "target": b, "weight": round(score, 4),
                "shared": [label_of.get(k, k) for k in
                           sorted(shared, key=lambda k: -idf[k])[:4]],
                "shared_count": len(shared),
            })

    # MUTUAL: a pair survives only if each end is among the other's strongest. Keeping a
    # pair when EITHER end wanted it was tried first and does not hold: someone who turns up
    # in every subject is the strongest match FOR everybody, so every other person spends one
    # of their slots on them and the hub keeps a pull to the entire graph -- exactly the
    # volume-read-as-closeness this cap exists to prevent. Reciprocity is also the honest
    # relation: "we are each among the other's nearest" says something, "I am your nearest
    # and you are nowhere near mine" does not.
    top: Dict[str, Set[tuple]] = {}
    per_person: Dict[str, List[Dict[str, Any]]] = {}
    for pair in scored:
        per_person.setdefault(pair["source"], []).append(pair)
        per_person.setdefault(pair["target"], []).append(pair)
    for person, pairs in per_person.items():
        pairs.sort(key=lambda p: (-p["weight"], p["source"], p["target"]))
        top[person] = {(p["source"], p["target"]) for p in pairs[:CONTEXT_TOP_PER_PERSON]}
    out = [p for p in scored
           if (p["source"], p["target"]) in top.get(p["source"], ())
           and (p["source"], p["target"]) in top.get(p["target"], ())]
    out.sort(key=lambda p: (-p["weight"], p["source"], p["target"]))
    return {
        "pairs": out,
        "coverage": {
            "people": len(of_person),
            "subjects": len(subjects),
            "pairs_considered": len(scored),
            "meaning": ("people who turn up in the same subjects, pulled together in the "
                        "layout only — never drawn as an edge and never counted as a "
                        "community"),
            "dropped_as_too_broad": [f"{label} ({n} people)" for n, label in dropped[:6]],
            "min_shared": CONTEXT_MIN_SHARED,
        },
    }


def _chunks(items: List[Any], size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]

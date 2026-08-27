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

import math
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

    Two people genuinely can share a name (`Bravo Yankee` and `Charlie Yankee` are both real
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


def person_provenance(conn: Any, node: Dict[str, Any], *, limit: int = 20) -> Dict[str, Any]:
    """"Why is this person here?" — the records that produced the node.

    Nothing else makes an unfamiliar name judgeable, and without it band, merge and dismiss
    are guesses. Returns the mentions with their surface text, source and date.
    """
    entity_id = str(node.get("entity_id") or "")
    rows: List[Dict[str, Any]] = []
    if entity_id:
        try:
            for source_id, surface, when, authored in conn.execute(
                    "SELECT source_id, surface_text, COALESCE(event_at, created_at),"
                    "       COALESCE(authored_by_owner,0)"
                    "  FROM entity_mentions WHERE entity_id=?"
                    " ORDER BY COALESCE(event_at, created_at) DESC LIMIT ?",
                    (entity_id, int(limit))):
                rows.append({
                    "source_id": str(source_id or ""),
                    "text": clean_label(surface)[:200],
                    "at": str(when or ""),
                    "authored_by_owner": bool(authored),
                })
        except sqlite3.Error:
            rows = []
    return {
        "node_id": node.get("node_id"),
        "label": node.get("label"),
        "band": node.get("band"),
        "band_reason": node.get("band_reason"),
        "messenger_keys": node.get("messenger_keys", []),
        "mentions": rows,
        "coverage": {"basis": "the records that named this person, most recent first"},
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
#: three-person path scores a perfect 1.0. Measured here, that let November Romeo and Trump top
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

    The name-collision danger — `Bravo Yankee` versus `Charlie Yankee` — is handled where it
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
        key = _normalized_name(node.get("label"))
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

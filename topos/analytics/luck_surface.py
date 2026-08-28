"""LSU-1..4 — Doing x Telling per work item, and the shuffle control that keeps it honest.

Roberts's heuristic is `Luck = Doing x Telling`. This makes it computable over substrate
that exists: role shapes as work items (G5), owner-authored messages as telling, dated
journal/goal records as doing, and ego-removed communities as the breadth weight — because
telling three people across a structural hole reaches further than telling ten inside one
cluster.

Three rules, all from the research note and PLAN_SOCIAL_GRAPH §5a:

* **No score.** Every function here returns components with their evidence. The screen
  states them in words; nothing multiplies them into a number nobody can falsify.
* **Abstention over guessing.** A message that does not clearly belong to a work item is
  dropped, never assigned. A wrong join poisons every number downstream, and the honest
  fallback (owner-confirmed matches) is budgeted for, not argued against.
* **A shuffle control rides with every breadth figure.** If recipients are permuted at
  random and breadth does not fall, the number was counting the owner's messaging habits
  rather than a real spread — and the screen must say "indistinguishable from chance"
  instead of showing it.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

WORK_ITEMS_TABLE = "luck_work_items"
TELLING_EVENTS_TABLE = "luck_telling_events"
DOING_EVENTS_TABLE = "luck_doing_events"

#: Sources that record the owner DOING the work, as opposed to reading about it. Measured on
#: the live corpus, this is the whole discriminator: `topos` carries 293 github + 23 journal
#: mentions, while `the new york times` carries 145 browser visits and nothing else. Without
#: it the screen would list every org the owner ever read about as a body of their own work.
WORK_SOURCES = ("github_activity", "grow_journal", "grow_data_file")

#: Unambiguous evidence the owner AUTHORED work on something. A commit is a work record.
AUTHORED_WORK_SOURCES = ("github_activity",)

#: Evidence that the owner wrote something down — which is not the same thing. The growth
#: journal is a record of a LIFE, not of work: "The Lantern Cafe" and "the Greenmart" each
#: carry six journal mentions and were duly presented as bodies of work the owner had built
#: and told nobody about. Journal-only entities have to earn their place (see below).
JOURNAL_SOURCES = ("grow_journal", "grow_data_file")

#: How many of the owner's own goals must name a journal-only entity before it counts as
#: work they are pursuing. Matched on WORD boundaries: substring matching let "first" collect
#: 22 hits from "first draft" and "first pass" and promoted a truncation into a project.
#:
#: Set from the live corpus, not from taste. At 3 this dropped Mursion, TinyCloud and Yale
#: (2 goal mentions each) alongside the cafes it was aimed at; at 2 those return while
#: the Greenmart (0), Metro Fitness (0) and The Lantern Cafe (1) still do not. The journal is
#: a first-class doing source -- it is in WORK_SOURCES -- and this gate exists only to keep
#: places of daily life from being presented as bodies of the owner's work.
MIN_GOALS_FOR_JOURNAL_WORK = 2

#: Words extraction sometimes canonicalises into an entity. None of them is a body of work,
#: and each would otherwise ride in on goal text that merely uses the word.
GENERIC_ENTITY_NAMES = frozenset("""first second third next last new old good great big small
today tomorrow yesterday morning evening week month year time day home work life people
thing things stuff place places""".split())

#: Entity types that can BE a body of work. `place` and `person` are deliberately absent:
#: the owner journals from Northgate and about Albiona, which gives both authored-work
#: evidence, but neither is a thing they are building — and listing a friend as a work item
#: with "told 1 person" would be both wrong and unpleasant.
WORK_ENTITY_TYPES = ("project", "product", "org", "topic", "event")

#: A work item needs this much authored-work evidence before it is one.
MIN_DOING_EVENTS = 3

#: Below this, a work item has too little telling to characterise and the screen says so.
MIN_TELLING_EVENTS = 3

#: Entity names shorter than this cannot be matched in message text — a two- or three-letter
#: surface ("UI", "AI", "app") collides with ordinary words far more often than it names the
#: owner's work, and a false telling event is worse than a missing one.
MIN_SURFACE_LEN = 4

#: A surface appearing in more than this share of the owner's messages is a common word that
#: happens to be an entity name, not a reference to the work. Guards the case where extraction
#: canonicalises something like "Next" or "Signal" into an entity.
MAX_SURFACE_MESSAGE_SHARE = 0.20


def is_speakable(surface: str) -> bool:
    """Could a person plausibly type this into a message?

    `dialoguesai/topos-react-app` is a repo slug and its ONLY name — 287 authored-work events
    and not one speakable surface. Matching it against conversation always returns zero, and
    a screen that renders that zero as "you built this for three months and told nobody" is
    stating a naming artifact as a fact about the owner's life. Work with no speakable name
    is reported as not measurable instead.

    Also drops extraction noise: surfaces carrying newlines ("Topos\n\nAccomplished") are
    fragments of a record, not names.
    """
    text = str(surface or "").strip()
    if len(text) < MIN_SURFACE_LEN or "/" in text or "\n" in text or "\r" in text:
        return False
    # a bare lowercase-hyphen handle ("dev-ry") is an account name, not something said aloud
    return not (text.islower() and "-" in text and " " not in text)

_WORD = re.compile(r"[a-z][a-z0-9_-]{2,}")
_STOP = frozenset("""the and for with that this from have has had you your our are was were
just get got out not but all can will would should could about into over more some when
what who how they them their there here then than its it's i'm we're don't doesn't now
today tomorrow yesterday yeah okay ok thanks thank please sorry hey hi hello""".split())


def _terms(text: str) -> set:
    return {w for w in _WORD.findall(str(text or "").lower()) if w not in _STOP}


def create_luck_tables(conn: Any) -> None:
    """Feature-owned, additive DDL — never a registry migration.

    Same reasoning as the messenger tables: routing this through the registry would bump
    user_version past what an installed engine understands and fence the node out of every
    write (2026-08-25).
    """
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {WORK_ITEMS_TABLE} (
            work_item_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            terms_json TEXT NOT NULL,
            recurrence_weeks INTEGER NOT NULL DEFAULT 0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'role_shape',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {TELLING_EVENTS_TABLE} (
            work_item_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            recipient_key TEXT NOT NULL,
            community_id TEXT,
            event_at TEXT NOT NULL,
            period_key TEXT NOT NULL,
            overlap INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (work_item_id, message_id, recipient_key)
        )"""
    )
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {DOING_EVENTS_TABLE} (
            work_item_id TEXT NOT NULL,
            record_id TEXT NOT NULL,
            record_table TEXT NOT NULL,
            event_at TEXT NOT NULL,
            period_key TEXT NOT NULL,
            PRIMARY KEY (work_item_id, record_id)
        )"""
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{TELLING_EVENTS_TABLE}_period"
        f" ON {TELLING_EVENTS_TABLE}(work_item_id, period_key)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- LSU-1

def build_work_items(conn: Any) -> List[Dict[str, Any]]:
    """Work items from the engine's OWN canonical entities, not a parallel term heuristic.

    The first cut derived work items from topic-cluster labels and matched them against
    message text by term overlap. Measured, that resolved 2 of 2,802 owner messages: cluster
    labels are code vocabulary ("entityimportancepanel", "graphworkspacepage") and nobody
    types an identifier into iMessage. The mismatch was in the vocabulary, not the owner.

    Canonical entities already solve this. `entities` holds the work under the names people
    actually say — Topos, Dialogues — with aliases attached, and `entity_mentions` already
    links them to both the owner's github/journal record and their conversations. Riding on
    that extraction rather than inventing a second one is the same rule the fact lane follows.

    Rows are merged on `normalized_name`: extraction emits "Topos" as both a `project` (296
    doing, 0 telling) and an `org` (104 doing, 8 telling) — one body of work split in two,
    which would otherwise appear on the screen twice with contradictory numbers.
    """
    try:
        rows = conn.execute(
            "SELECT e.normalized_name, e.canonical_name, e.entity_type, e.aliases_json,"
            "       m.source_id, COUNT(*) "
            "  FROM entity_mentions m JOIN entities e ON e.entity_id = m.entity_id"
            f" WHERE e.is_self IS NOT 1 AND e.entity_type IN"
            f" ({','.join('?' for _ in WORK_ENTITY_TYPES)})"
            # grouped by SPELLING too: collapsing on normalized_name alone left SQLite to
            # pick a bare canonical_name for the group, so the label vote only ever saw one
            # candidate and "TOPOS" beat "Topos" 43-to-nothing
            "  GROUP BY e.normalized_name, m.source_id, e.canonical_name",
            WORK_ENTITY_TYPES).fetchall()
    except sqlite3.Error:
        return []

    goal_texts = _goal_texts(conn)
    merged: Dict[str, Dict[str, Any]] = {}
    for norm, canonical, etype, aliases_json, source_id, n in rows:
        if not norm:
            continue
        item = merged.setdefault(str(norm), {
            "normalized_name": str(norm), "label": str(canonical or norm),
            "types": set(), "surfaces": set(), "doing_events": 0,
            "by_source": Counter(), "label_votes": Counter(), "authored_events": 0})
        item["types"].add(str(etype or ""))
        # the most-mentioned spelling wins the label: extraction emits "Topos" and "TOPOS"
        # as separate rows, and taking whichever sorted last shouted the name on the screen
        item["label_votes"][str(canonical or norm)] += int(n or 0)
        item["by_source"][str(source_id or "")] += int(n or 0)
        if str(source_id) in WORK_SOURCES:
            item["doing_events"] += int(n or 0)
        if str(source_id) in AUTHORED_WORK_SOURCES:
            item["authored_events"] += int(n or 0)
        item["surfaces"].add(str(canonical or norm))
        try:
            for a in json.loads(aliases_json or "[]"):
                if a:
                    item["surfaces"].add(str(a))
        except (TypeError, ValueError):
            pass

    out: List[Dict[str, Any]] = []
    for norm, item in merged.items():
        if item["doing_events"] < MIN_DOING_EVENTS:
            continue  # read about, never worked on — not the owner's doing
        if norm in GENERIC_ENTITY_NAMES:
            continue  # a common word wearing an entity name
        if not item["authored_events"]:
            # journal-only: the owner wrote it down, which does not make it their work.
            # It qualifies only if their OWN goals keep naming it.
            if _goal_hits(norm, goal_texts) < MIN_GOALS_FOR_JOURNAL_WORK:
                continue
        surfaces = sorted({s for s in item["surfaces"] if len(s) >= MIN_SURFACE_LEN})
        if not surfaces:
            continue
        out.append({
            "work_item_id": "work:" + re.sub(r"[^a-z0-9]+", "-", norm).strip("-"),
            "label": item["label_votes"].most_common(1)[0][0],
            "surfaces": surfaces,
            "entity_types": sorted(t for t in item["types"] if t),
            "doing_events": item["doing_events"],
            "doing_by_source": dict(item["by_source"]),
            "authored_events": item["authored_events"],
        })
    out.sort(key=lambda r: -r["doing_events"])
    return out


def _goal_texts(conn: Any) -> List[str]:
    try:
        return [str(g or "").lower() for (g,) in conn.execute(
            "SELECT goal_text FROM user_goals WHERE goal_text IS NOT NULL")]
    except sqlite3.Error:
        return []


def _goal_hits(name: str, goal_texts: List[str]) -> int:
    if not goal_texts:
        return 0
    pattern = _surface_pattern(name)
    return sum(1 for g in goal_texts if pattern.search(g))


def _surface_pattern(surface: str) -> "re.Pattern":
    return re.compile(r"(?<![a-z0-9])" + re.escape(surface.lower()) + r"(?![a-z0-9])")


def resolve_message_to_work_item(text: str, items: List[Dict[str, Any]]) -> List[str]:
    """Every work item this message names. A list, not a best guess.

    The term-overlap version abstained on ties, because a tie meant "we cannot tell which
    item this is about". Named surfaces carry no such doubt: a message reading "shipped the
    Topos demo, told Dialogues" genuinely tells about both, and crediting only one would
    under-count telling on purpose. Ambiguity is gone, so abstention is no longer the right
    response to a second match.
    """
    low = str(text or "").lower()
    if not low:
        return []
    hits = []
    for item in items:
        if any(p.search(low) for p in item.get("_patterns", ())):
            hits.append(item["work_item_id"])
    return hits


def compile_surfaces(conn: Any, dataset_id: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach match patterns, dropping surfaces too common to mean the work.

    Runs once per rollup rather than per message; the share test needs the owner's whole
    message corpus, so computing it inside the resolver would re-scan for every row.
    """
    try:
        messages = [str(t or "").lower() for (t,) in conn.execute(
            "SELECT content FROM conversation_messages"
            " WHERE dataset_id=? AND is_from_self=1 AND content IS NOT NULL", (dataset_id,))]
    except sqlite3.Error:
        messages = []
    total = len(messages)
    for item in items:
        kept = []
        for surface in item["surfaces"]:
            if not is_speakable(surface):
                continue
            pattern = _surface_pattern(surface)
            if total:
                share = sum(1 for m in messages if pattern.search(m)) / total
                if share > MAX_SURFACE_MESSAGE_SHARE:
                    continue  # a common word wearing an entity name
            kept.append(pattern)
        item["_patterns"] = tuple(kept)
        item["matchable"] = bool(kept)
        item["speakable_surfaces"] = [pattern.pattern for pattern in kept]
    return items


# --------------------------------------------------------------------------- LSU-2/3

def _period(ts: str) -> str:
    return str(ts)[:7]


def build_telling_events(conn: Any, dataset_id: str, items: List[Dict[str, Any]]) -> List[Tuple]:
    """Owner-authored messages that name a work item, with their recipients.

    Telling is the OWNER speaking — `is_from_self=1`. A message the owner received about
    their own work is someone else telling them, which is a different fact and belongs on a
    different rail.

    DMs only. Group broadcast fans one message out to every speaker in the room, which would
    let a single message in a busy thread outweigh a year of real correspondence.
    """
    from .messenger_directed import (SELF_KEY, classify_conversations, load_messages,
                                     EDGE_KIND_DM)

    rows = load_messages(conn, dataset_id)
    kinds = classify_conversations(rows)
    peers: Dict[str, set] = {}
    for conv, _m, sender, _e, from_self, _src, _rt in rows:
        if not from_self and sender and str(sender) != SELF_KEY:
            peers.setdefault(conv, set()).add(str(sender))

    content = {}
    try:
        for mid, text in conn.execute(
                "SELECT message_id, content FROM conversation_messages"
                " WHERE dataset_id=? AND is_from_self=1 AND content IS NOT NULL", (dataset_id,)):
            content[str(mid)] = str(text)
    except sqlite3.Error:
        return []

    communities = _community_by_participant(conn, dataset_id)
    out: List[Tuple] = []
    for conv, mid, _sender, ea, is_self, _src, _rt in rows:
        if not is_self or kinds.get(conv) != EDGE_KIND_DM:
            continue
        text = content.get(str(mid))
        if not text:
            continue
        for work_item_id in resolve_message_to_work_item(text, items):
            for recipient in (peers.get(conv) or set()):
                out.append((work_item_id, str(mid), recipient,
                            communities.get(recipient), str(ea), _period(ea), 1))
    return out


def _community_by_participant(conn: Any, dataset_id: str) -> Dict[str, str]:
    """Ego-removed community per participant, latest period.

    The breadth weight depends on this: communities are computed with the owner REMOVED,
    so "different communities" means the people differ from each other, not merely that
    they all know the owner.
    """
    try:
        rows = conn.execute(
            "SELECT participant_id, community_id FROM messenger_communities"
            " WHERE dataset_id=? AND period_key=(SELECT MAX(period_key)"
            " FROM messenger_communities WHERE dataset_id=?)",
            (dataset_id, dataset_id)).fetchall()
    except sqlite3.Error:
        return {}
    by_contact = {str(r[0]): str(r[1]) for r in rows if r and r[0] is not None}
    if not by_contact:
        return {}
    # KEY-SPACE BRIDGE. messenger_communities keys on CONTACT IDs while telling recipients
    # are messenger keys (phones/handles) — measured, the two sets overlap on ZERO rows, so
    # a direct lookup silently returned "no community" for every recipient and made breadth
    # structurally zero. resolve_peer_identities already maps one to the other; using it
    # here is the difference between a breadth figure and a decorative 0.
    from .messenger_directed import resolve_peer_identities

    try:
        peer_keys = [str(r[0]) for r in conn.execute(
            f"SELECT DISTINCT from_key FROM messenger_directed_edges"
            f" WHERE dataset_id=? AND from_key != 'self'", (dataset_id,)).fetchall()]
        peer_keys += [str(r[0]) for r in conn.execute(
            f"SELECT DISTINCT to_key FROM messenger_directed_edges"
            f" WHERE dataset_id=? AND to_key != 'self'", (dataset_id,)).fetchall()]
    except sqlite3.Error:
        return by_contact
    idents = resolve_peer_identities(conn, sorted(set(peer_keys)))
    out = dict(by_contact)
    for peer, (contact_id, _eid, _dn) in idents.items():
        community = by_contact.get(str(contact_id or ""))
        if community:
            out[peer] = community
    return out


def build_doing_events(conn: Any, items: List[Dict[str, Any]]) -> List[Tuple]:
    """Dated records of the owner doing the work — canonical mentions in the work sources.

    Dated by `event_at`, falling back to `created_at`: a mention with no date cannot join a
    month and would silently collapse the recurrence figure the screen reports.
    """
    by_norm = {i["work_item_id"]: i for i in items}
    wanted = {}
    for i in items:
        wanted[i["work_item_id"]] = i
    placeholders = ",".join("?" for _ in WORK_SOURCES)
    try:
        rows = conn.execute(
            f"SELECT e.normalized_name, m.record_id, m.canonical_table, m.source_id,"
            f"       COALESCE(m.event_at, m.created_at)"
            f"  FROM entity_mentions m JOIN entities e ON e.entity_id = m.entity_id"
            f" WHERE m.source_id IN ({placeholders})"
            f"   AND e.entity_type IN ({','.join('?' for _ in WORK_ENTITY_TYPES)})",
            tuple(WORK_SOURCES) + tuple(WORK_ENTITY_TYPES)).fetchall()
    except sqlite3.Error:
        return []

    import re as _re
    out: List[Tuple] = []
    for norm, record_id, table, source_id, when in rows:
        if not norm or not when:
            continue
        work_item_id = "work:" + _re.sub(r"[^a-z0-9]+", "-", str(norm)).strip("-")
        if work_item_id not in wanted:
            continue
        out.append((work_item_id, str(record_id), str(table or source_id),
                    str(when), _period(str(when))))
    return out


# --------------------------------------------------------------------------- LSU-4

def _has_messaging_substrate(conn: Any, dataset_id: str) -> bool:
    from .dataset_resolution import has_messaging_substrate

    return has_messaging_substrate(conn, dataset_id)


def resolve_primary_dataset(conn: Any) -> str:
    from .dataset_resolution import resolve_primary_dataset as _resolve

    return _resolve(conn)


def messaging_population(conn: Any, dataset_id: str,
                         communities: Dict[str, str]) -> Dict[str, str]:
    """The subset of the community map the owner can actually reach by message.

    `_community_by_participant` deliberately holds each person TWICE — under their contact id
    and under their messenger key — because telling recipients arrive in the second space and
    the stored communities live in the first. That is right for lookup and wrong for counting:
    used as a population it doubles every person, and used as a denominator it offers
    communities the owner has no messaging path to. Both distort the control.

    So the population is the messenger-key side only: one row per person the owner has ever
    exchanged a DM with. "Reached 9 of 22 reachable" is a claim about this owner's actual
    social range; "9 of 37" quietly counted rooms with no door.
    """
    from .messenger_directed import MESSENGER_DIRECTED_EDGES_TABLE, SELF_KEY

    try:
        keys = {str(r[0]) for r in conn.execute(
            f"SELECT DISTINCT from_key FROM {MESSENGER_DIRECTED_EDGES_TABLE}"
            f" WHERE dataset_id=? AND from_key != ?", (dataset_id, SELF_KEY))}
        keys |= {str(r[0]) for r in conn.execute(
            f"SELECT DISTINCT to_key FROM {MESSENGER_DIRECTED_EDGES_TABLE}"
            f" WHERE dataset_id=? AND to_key != ?", (dataset_id, SELF_KEY))}
    except sqlite3.Error:
        return dict(communities)
    return {k: v for k, v in communities.items() if k in keys and v}


def shuffle_control_breadth(recipients: List[str], communities: Dict[str, str],
                            *, trials: int = 200) -> float:
    """Expected community breadth if the same number of people had been told AT RANDOM.

    The null model. If real breadth does not exceed this, the number reflects how many
    people the owner messages rather than how widely the work travelled — and the screen
    must say so rather than showing a figure that means nothing.

    The first version drew uniformly over COMMUNITIES, which put the control ABOVE observed
    breadth for every item on the live corpus (Topos reached 9, control said 10.4) and so
    declared every real spread "chance". Communities are nothing like equal — a handful hold
    most of the owner's contacts — so uniform-over-buckets overstates how far random telling
    would reach. Drawing people from the owner's actual messaging population is the honest
    null: it asks whether THESE recipients spread further than that many recipients usually
    would.

    Exact, not sampled: the probability a community is missed entirely is a hypergeometric
    tail, so the expectation has a closed form. `trials` is retained for call compatibility
    and deliberately unused — a sampled control would make a stored row differ from a
    recomputed one.
    """
    sized: Counter = Counter(c for c in communities.values() if c)
    population = sum(sized.values())
    n = len(recipients)
    if not sized or n <= 0 or population <= 0:
        return 0.0
    n = min(n, population)
    expected = 0.0
    for _community, size in sized.items():
        # P(at least one of the n drawn falls in this community), sampling without replacement
        if population - size < n:
            expected += 1.0
            continue
        p_miss = 1.0
        for i in range(n):
            p_miss *= (population - size - i) / (population - i)
        expected += 1.0 - p_miss
    return round(expected, 4)


def rollup(conn: Any, dataset_id: str) -> Dict[str, Any]:
    """Per work item: Doing, Telling, breadth, the shuffle control, and who never heard.

    Returns components — never a product. The screen says "you have built X for three
    months and told two people"; it does not say "your luck score is 74".
    """
    # An unnamed dataset is answered from the record rather than refused; the id used is
    # reported below so the screen can say which one it read.
    from .dataset_resolution import resolve_messaging_dataset

    requested_dataset = str(dataset_id or "").strip()
    dataset_id, resolved_by_engine = resolve_messaging_dataset(conn, requested_dataset)

    items = compile_surfaces(conn, dataset_id, build_work_items(conn))
    if not items:
        return {"dataset_id": dataset_id, "work_items": [], "coverage": {
            "reason": "no body of work with authored evidence has emerged from the record yet",
            "dataset_resolved_by_engine": resolved_by_engine,
            "dataset_requested": requested_dataset or None}}

    # DATASET-LEVEL LIBEL GUARD. Doing comes from entity_mentions, which is not scoped to a
    # dataset; telling comes from this dataset's messages. Point the read at a dataset with
    # no messaging substrate — a device stub, a fresh dataset, an id resolved by a race —
    # and every work item keeps its real doing count while telling collapses to zero. On
    # screen that reads "you built this and told nobody", which is a statement about the
    # owner's life derived from a wrong id. Measured 2026-08-27: 1,609 doing / 0 telling.
    messaging_present = _has_messaging_substrate(conn, dataset_id)

    telling = build_telling_events(conn, dataset_id, items) if messaging_present else []
    doing = build_doing_events(conn, items)
    communities = _community_by_participant(conn, dataset_id)
    population = messaging_population(conn, dataset_id, communities)
    all_communities = {c for c in population.values() if c}

    tell_by_item: Dict[str, List[Tuple]] = defaultdict(list)
    for t in telling:
        tell_by_item[t[0]].append(t)
    do_by_item: Dict[str, List[Tuple]] = defaultdict(list)
    for d in doing:
        do_by_item[d[0]].append(d)

    labels = _peer_labels(conn, dataset_id, sorted({t[2] for t in telling}))
    out = []
    for item in items:
        wid = item["work_item_id"]
        tells = tell_by_item.get(wid, [])
        dos = do_by_item.get(wid, [])
        recipients = sorted({t[2] for t in tells})
        reached = {t[3] for t in tells if t[3]}
        control = shuffle_control_breadth(recipients, population)
        breadth = len(reached)
        # The Roberts case, stated as data: built, and who never heard about it.
        unreached = sorted(all_communities - reached)
        out.append({
            "work_item_id": wid,
            "label": item["label"],
            "entity_types": item["entity_types"],
            "doing_events": len(dos),
            "doing_periods": len({d[4] for d in dos}),
            "telling_events": len(tells),
            "told_people": [{"key": r, "label": labels.get(r) or r} for r in recipients],
            "communities_reached": breadth,
            "communities_total": len(all_communities),
            "communities_unreached": len(unreached),
            "breadth_shuffle_control": control,
            # The display rule: a breadth at or below chance is not a finding.
            "breadth_beats_chance": bool(breadth > control),
            # only a claim when the name could have been matched at all
            "below_telling_floor": bool(item.get("matchable", True) and messaging_present
                                        and len(tells) < MIN_TELLING_EVENTS),
            # A name too common to match was dropped by the share guard; without this the
            # screen would report a confident "told nobody" that was never measurable.
            # The dataset guard folds in here for the same reason.
            "matchable": bool(item.get("matchable", True) and messaging_present),
        })
    out.sort(key=lambda w: (-w["doing_events"], -w["telling_events"]))
    return {
        "dataset_id": dataset_id,
        "work_items": out,
        "coverage": {
            "work_item_basis": ("canonical entities with authored-work evidence in "
                                + ", ".join(WORK_SOURCES)),
            "telling_basis": "owner-authored DM messages naming the entity or an alias",
            "resolver": (f"entity surface match, >= {MIN_SURFACE_LEN} chars, surfaces in "
                         f">{MAX_SURFACE_MESSAGE_SHARE:.0%} of messages dropped as common words"),
            "breadth_basis": "ego-removed communities of the recipients",
            "control_basis": ("expected reach if the same number of people were drawn at"
                              " random from everyone the owner exchanges DMs with"),
            "communities_known": len(all_communities),
            "reachable_people": len(population),
            "telling_measurable": messaging_present,
            "dataset_resolved_by_engine": resolved_by_engine,
            "dataset_requested": requested_dataset or None,
        },
    }


def _peer_labels(conn: Any, dataset_id: str, keys: List[str]) -> Dict[str, str]:
    from .relationship_reads import peer_labels

    return peer_labels(conn, dataset_id, keys)


# --------------------------------------------------------------------------- LSU-7/8

#: The explore↔exploit control (LSU-8). 1.0 ranks moves that reach a community nobody in it
#: has heard from; 0.0 ranks moves that deepen ties that already exist. The default sits in
#: the middle because neither is right in general — the research's whole point is that the
#: return on a weak tie depends on how covered you already are.
DEFAULT_EXPLORE = 0.5

#: No panel shows more than this. A list of twenty things to do is a list of nothing to do.
MAX_MOVES = 5


def _days(value: Any) -> str:
    """Whole days. A gap of 72.0959 days is a measurement pretending to be a fact."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return "Some time"
    if n <= 0:
        return "Less than a day"
    return f"{n} day" + ("" if n == 1 else "s")


def _marginal_benefit(new_communities: int, known_communities: int) -> float:
    """How much reach a move ADDS, not how much reach exists.

    Telling a sixth person inside a community that already knows adds nothing structural;
    telling the first person in a community that does not adds a whole channel. §2 requires
    this term to be visible in the ranking AND in the words on screen, so that a move is
    never presented as valuable without saying what makes it valuable.
    """
    if known_communities <= 0:
        return 0.0
    return round(min(1.0, new_communities / known_communities), 4)


def reach_score(marginal_benefit: float, explore: float) -> float:
    """Ranking weight for a move that reaches people who have not heard.

    Rises with the slider. Never falls to zero at explore=0: reaching a new circle still has
    value to someone who mostly wants to deepen what they have, it just stops leading.
    """
    return round(float(marginal_benefit) * (0.4 + 0.6 * float(explore)), 4)


def deepen_score(base: float, explore: float) -> float:
    """Ranking weight for a move that strengthens a tie that already exists. Falls as the
    slider moves toward reach — the mirror of `reach_score`, kept beside it so the two can
    never drift into agreeing."""
    return round(float(base) * (1.0 - float(explore)), 4)


def build_moves(conn: Any, dataset_id: str, *, explore: float = DEFAULT_EXPLORE,
                limit: int = MAX_MOVES) -> List[Dict[str, Any]]:
    """Ranked, evidence-carrying things the owner could actually do this week.

    Rule-based on purpose: every input is already measured and every move can name the row
    it came from. A model that ranked these could not say why, and "why" is the only reason
    to trust a suggestion about your own relationships.

    Owner-only by construction — these are judgements about named people, and §D-B keeps
    person-level judgement first-party.
    """
    explore = max(0.0, min(1.0, float(explore)))
    surface = rollup(conn, dataset_id)
    items = [w for w in surface.get("work_items", []) if w.get("matchable")]
    coverage = surface.get("coverage", {})
    communities_total = int(coverage.get("communities_known") or 0)
    reachable_people = int(coverage.get("reachable_people") or 0)

    moves: List[Dict[str, Any]] = []

    # 1. Tell a community that has not heard. The inverse of LSU-2, and the move the whole
    #    screen exists to produce.
    for item in items[:3]:
        unreached = int(item.get("communities_unreached") or 0)
        if unreached <= 0 or item.get("below_telling_floor"):
            continue
        benefit = _marginal_benefit(unreached, communities_total)
        # Quantify in CIRCLES only where breadth actually beat its control. On a corpus whose
        # communities are near-singletons (56 people across 37 of them) "28 circles have not
        # heard" sounds like a large structural opportunity while the same screen is saying,
        # correctly, that spread here is indistinguishable from chance. People are the
        # honest unit when the partition is that fine.
        untold_people = max(0, reachable_people - len(item.get("told_people") or []))
        if item.get("breadth_beats_chance"):
            reach_words = f"reaches up to {unreached} circles that know nothing about it"
            why_tail = f"{unreached} of {communities_total} circles you message have not heard."
        else:
            reach_words = f"reaches people among the {untold_people} who have not heard"
            why_tail = (f"{untold_people} of the {reachable_people} people you message have"
                        f" not heard about it.")
        moves.append({
            "kind": "tell_unexposed_community",
            "title": f"Tell someone outside your usual circle about {item['label']}",
            "why": f"{item['doing_events']} recorded work events, and {why_tail}",
            "marginal_benefit": benefit,
            "marginal_benefit_words": reach_words,
            "evidence": {"work_item_id": item["work_item_id"],
                         "communities_unreached": unreached},
            "score": reach_score(benefit, explore),
        })

    try:
        from .relationship_reads import read_relationship_signals, read_relationships

        signals = read_relationship_signals(conn, dataset_id=dataset_id, signal="all")
        relationships = read_relationships(conn, dataset_id=dataset_id, limit=200)
    except Exception:  # noqa: BLE001 — a missing rail costs its moves, not the panel
        signals, relationships = {}, {"relationships": []}

    # 2. Reconnect a tie that is cooling. Exploit-side: the relationship already exists, so
    #    the marginal structural gain is small even when the human value is large.
    for alarm in (signals.get("drift_alarms") or [])[:3]:
        label = alarm.get("label") or alarm.get("peer_key")
        if not label or not any(ch.isalpha() for ch in str(label)):
            continue  # never put a bare phone number in a suggestion
        moves.append({
            "kind": "reconnect_cooling_tie",
            "title": f"Reach out to {label}",
            # drift_alarms carries recent_gap_days and total_msgs, not a median gap; the
            # earlier phrasing asked for a field that is not there and rendered "you used to
            # talk about every — days", plus an unrounded 72.0959.
            "why": (f"{_days(alarm.get('recent_gap_days'))} since your last exchange, after "
                    f"{alarm.get('total_msgs', 0)} messages."),
            "marginal_benefit": 0.0,
            "marginal_benefit_words": "deepens a tie you already have; adds no new circle",
            "evidence": {"peer_key": alarm.get("peer_key")},
            "score": deepen_score(0.55, explore),
        })

    # 3. Name someone the node cannot name. Everything above degrades when the subject of a
    #    move is a phone number, so this is a prerequisite move rather than a cosmetic one.
    unnamed = [r for r in (relationships.get("relationships") or []) if r.get("needs_name")]
    unnamed.sort(key=lambda r: -(r.get("total_msgs") or 0))
    for row in unnamed[:2]:
        moves.append({
            "kind": "name_unknown_contact",
            "title": f"Put a name to {row.get('peer_key')}",
            "why": (f"{row.get('total_msgs')} messages exchanged and no name on file, so this"
                    f" person cannot appear in anything above."),
            "marginal_benefit": 0.0,
            "marginal_benefit_words": "unblocks every other suggestion about this person",
            "evidence": {"peer_key": row.get("peer_key"),
                         "total_msgs": row.get("total_msgs")},
            "score": 0.5,
        })

    # 4. Repay an imbalance. Reciprocity is the mechanism the research puts under weak-tie
    #    value: a tie that only ever carries one way stops carrying.
    for row in (signals.get("reciprocity") or [])[:3]:
        balance = row.get("balance")
        label = row.get("label") or row.get("peer_key")
        if balance is None or balance > -0.4:
            continue
        if not any(ch.isalpha() for ch in str(label)):
            continue
        moves.append({
            "kind": "repay_imbalance",
            "title": f"Answer {label}",
            "why": "They have carried this conversation for a while.",
            "marginal_benefit": 0.0,
            "marginal_benefit_words": "keeps an existing tie alive; adds no new circle",
            "evidence": {"peer_key": row.get("peer_key"), "balance": balance},
            "score": deepen_score(0.45, explore),
        })

    # Deterministic ordering: score, then kind, then title. LSU-8 requires the same slider
    # position to produce the same list every time, so no tie may fall to dict ordering.
    moves.sort(key=lambda m: (-m["score"], m["kind"], m["title"]))
    return moves[:limit]

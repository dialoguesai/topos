"""Reflection: moments worth bringing up again (product-iterations/reflection/REFLECTION_PROGRAM.md §2a).

The node finds candidates for reflection, appreciation and conflict resolution from the FACT ONTOLOGIES the owner
switched on (``pack_registry.enabled``), never from raw records directly. A sweep (:func:`sweep`) reads the active
``fact`` signal objects of those packs and applies deterministic rules (dated repeats, open threads with no closing
row, people who recur in peak moments, an earlier window against a recent one): no model call, on the catch-up lane,
never on the request path. Each candidate keeps the facts it rests on and their source references, so the
conversation door can show the owner's own words again.

Owner-only tables ``reflection_candidates`` and ``reflection_labels`` are created lazily on first use, like the
truth lane's (no migration), and listed in ``data_explorer_tables.OWNER_ONLY_TABLES``. A label writes one
``reflection_label`` signal object (dimension ``memory``, ``disclosure: owner_only``, DISCLOSURE_RULES DR-60): refused
below the owner tier whatever a scope grants, not indexed for search, never read by a share path.

Three doors (owner class only at the truth door): ``reflection_invitations`` and ``reflection_conversation`` are
reads and change nothing (freshness is the client's ``exclude`` list); ``reflection_label`` is the one write.
A node with no reflection pack enabled answers every starter with ``cards: []`` and ``packs_missing``.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..storage.db.write_gate import commit_connection, with_db_write
from .reflection_stances import STARTERS, compose

CANDIDATES_TABLE = "reflection_candidates"
LABELS_TABLE = "reflection_labels"
TABLES = (CANDIDATES_TABLE, LABELS_TABLE)

#: The packs the sweep reads. The two ``moments.*`` packs are this program's; the rest are bundled already.
REFLECTION_PACKS: Tuple[str, ...] = ("moments.joy", "moments.tension", "narrative.self", "life.events",
                                     "aspirations.goals", "obligations.commitments", "relationships.social")
LABELS = ("worth_it", "opened", "not_now", "not_true", "too_personal")
LABEL_CONFIDENCE = {"worth_it": 1.0, "not_now": 0.5, "not_true": 0.0, "too_personal": 0.0}
OBJECT_TYPE = "reflection_label"
OBJECT_DIMENSION = "memory"
EXTRACTOR_VERSION = "reflection_v1"
VERSION = "reflection/v1.0"
NOT_NOW_DAYS = 30
SWEEP_INTERVAL_HOURS = 6
MAX_LIMIT = 6
MAX_EXCLUDE = 200
MAX_NOTE_CHARS = 1000
EXCERPT_CHARS = 400
TEXT_CHARS = 240
JOB_KIND = "reflection_sweep"

_POSITIVE_LIFE = {"award_recognition", "graduation", "marriage", "engagement", "birth_of_child", "business_started"}
_HARD_LIFE = {"bereavement", "breakup_divorce", "accident_emergency", "business_ended", "pet_loss"}
_STALLED = {"stalled", "paused", "abandoned", "blocked"}
_OPEN = {"open", "unknown", "", None}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
#: An entity id as the spine writes them (``ent_<hex>``) or a bare UUID.
_ENTITY_ID = re.compile(r"^(?:ent_[0-9a-f]{6,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)
#: A raw handle: a phone number or an email address. Never a display name.
_HANDLE = re.compile(r"^(?:\+?[0-9][0-9 ().-]{5,}|[^@\s]+@[^@\s]+)$")
#: Anything id-, label- or handle-shaped that must never reach card text (the last line of defence). A phone number is
#: a run of digits, spaces, dots, dashes and brackets that does not start inside another number and is not an ISO date:
#: the first version matched every "2026-10-11" the cards print, so every card read "On someone you wrote".
_LEAK = re.compile(r"(?:\b(?:id|key):\S+|\bent_[0-9a-f]{6,}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
                   r"|(?<![0-9-])(?![0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9]))\+?[0-9][0-9 ().-]{8,}[0-9]"
                   r"|[^@\s]+@[^@\s]+\.[a-z]{2,})", re.I)

_DDL = (
    f"""CREATE TABLE IF NOT EXISTS {CANDIDATES_TABLE} (
        candidate_id TEXT PRIMARY KEY,
        candidate_key TEXT NOT NULL UNIQUE,
        rule TEXT NOT NULL,
        section_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        valence TEXT NOT NULL,
        person TEXT,
        text TEXT NOT NULL,
        observation TEXT NOT NULL DEFAULT '',
        interpretation TEXT NOT NULL DEFAULT '',
        evidence_refs_json TEXT NOT NULL DEFAULT '[]',
        fact_ids_json TEXT NOT NULL DEFAULT '[]',
        row_ids_json TEXT NOT NULL DEFAULT '[]',
        score REAL NOT NULL DEFAULT 0,
        event_at TEXT,
        built_at TEXT NOT NULL,
        seen_at TEXT NOT NULL,
        retired INTEGER NOT NULL DEFAULT 0,
        retire_reason TEXT,
        suppressed_until TEXT,
        opened_count INTEGER NOT NULL DEFAULT 0,
        engine_version TEXT
    )""",
    f"CREATE INDEX IF NOT EXISTS idx_{CANDIDATES_TABLE}_section ON {CANDIDATES_TABLE}(section_id, retired, score)",
    f"""CREATE TABLE IF NOT EXISTS {LABELS_TABLE} (
        label_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL CHECK (label IN ('worth_it', 'not_now', 'not_true', 'too_personal')),
        note TEXT,
        labelled_at TEXT NOT NULL
    )""",
)


class ReflectionRequestError(ValueError):
    """A request the store refuses; ``str(exc)`` is a fixed code, never the input."""


# --- tables and time ---------------------------------------------------------------------------------------------------
def _present(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def tables_present(conn: sqlite3.Connection) -> bool:
    return all(_present(conn, t) for t in TABLES)


def ensure_tables(conn: sqlite3.Connection) -> None:
    if tables_present(conn):
        return
    with with_db_write():
        for statement in _DDL:
            conn.execute(statement)
        commit_connection(conn)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _day(value: Any) -> str:
    parsed = _aware(value)
    return parsed.date().isoformat() if parsed else "undated"


def _days_ago(value: Any, now: datetime) -> float:
    parsed = _aware(value)
    return (now - parsed).total_seconds() / 86400 if parsed else 10_000.0


# --- packs and facts ----------------------------------------------------------------------------------------------------
def enabled_packs(conn: sqlite3.Connection) -> List[str]:
    """The reflection packs the owner switched on, in catalog order. No registry table means none on."""
    if not _present(conn, "pack_registry"):
        return []
    on = {str(r[0]) for r in conn.execute("SELECT pack_id FROM pack_registry WHERE enabled=1").fetchall()}
    return [p for p in REFLECTION_PACKS if p in on]


def pack_status(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    on = enabled_packs(conn)
    return {"packs_enabled": on, "packs_missing": [p for p in REFLECTION_PACKS if p not in on]}


def load_facts(conn: sqlite3.Connection, packs: Iterable[str]) -> List[Dict[str, Any]]:
    """Active ``fact`` objects of the given packs, flattened for the rules. Reads only what the derivation writer
    wrote: predicate, structured value, the owner's quote, source refs, actor role and date."""
    packs = list(packs)
    if not packs or not _present(conn, "signal_objects"):
        return []
    columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(signal_objects)").fetchall()}
    if "ontology_id" not in columns:
        return []
    marks = ",".join("?" for _ in packs)
    rows = conn.execute(
        f"SELECT object_id, object_key, payload_json, source_refs_json, valid_from, ontology_id FROM signal_objects "
        f"WHERE object_type='fact' AND valid_to IS NULL AND ontology_id IN ({marks})", packs).fetchall()
    facts = []
    for object_id, key, payload_json, refs_json, valid_from, pack in rows:
        try:
            payload = json.loads(payload_json or "{}")
            refs = json.loads(refs_json or "[]")
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        value = payload.get("value_struct") if isinstance(payload.get("value_struct"), dict) else {}
        facts.append({
            "id": str(object_id), "key": str(key), "pack": str(pack),
            "predicate": str(payload.get("predicate") or ""), "value": value,
            "object_value": str(payload.get("object_value") or ""), "quote": str(payload.get("quote") or ""),
            "actor_role": str(payload.get("actor_role") or ""), "at": valid_from,
            "refs": [r for r in refs if isinstance(r, dict) and r.get("table") and r.get("record_id")],
        })
    return facts


def _names(conn: sqlite3.Connection, ids: Iterable[str]) -> Dict[str, str]:
    wanted = [i for i in dict.fromkeys(ids) if i and _present(conn, "entities")]
    out: Dict[str, str] = {}
    for start in range(0, len(wanted), 100):
        chunk = wanted[start:start + 100]
        for entity_id, name, is_self in conn.execute(
                f"SELECT entity_id, canonical_name, is_self FROM entities WHERE entity_id IN "
                f"({','.join('?' for _ in chunk)})", chunk).fetchall():
            out[str(entity_id)] = "" if is_self else " ".join(str(name or "").split())
    return out


def _is_label(raw: str) -> bool:
    return raw.lower().startswith(("id:", "key:")) or bool(_ENTITY_ID.match(raw))


def resolve_people(conn: sqlite3.Connection, raws: Iterable[str]) -> Dict[str, str]:
    """Every raw person value a fact carries -> what a card may show: a display name, or "" (leave the person out).

    The derivation writer binds a message's recipient or sender as a RUNNER LABEL, ``id:<entity id>`` or
    ``key:<messenger key>`` (``template.is_runner_label``), not as a name. A label, an entity id or a raw handle is
    never shown: an ``id:`` label or a bare entity id resolves through the entity spine (the owner's own entity to
    nothing), a ``key:`` label through the owner's contacts, and anything that does not resolve to a name is dropped.
    A plain name passes as written."""
    from .truth_checks import _contact_name

    raws = [r for r in dict.fromkeys(raws) if r]
    entity_ids = {}
    for raw in raws:
        if raw.lower().startswith("id:"):
            entity_ids[raw] = raw.split(":", 1)[1].strip()
        elif _ENTITY_ID.match(raw):
            entity_ids[raw] = raw
    names = _names(conn, set(entity_ids.values()))
    cache: Dict[str, Optional[str]] = {}
    out: Dict[str, str] = {}
    for raw in raws:
        if raw in entity_ids:
            name = names.get(entity_ids[raw], "")
        elif raw.lower().startswith("key:"):
            key = raw.split(":", 1)[1].strip()
            name = _contact_name(conn, key, cache) or (
                _contact_name(conn, key.split(":", 1)[1], cache) if ":" in key else None) or ""
        else:
            name = raw
        name = " ".join(str(name).split())
        out[raw] = "" if (not name or _is_label(name) or _HANDLE.match(name)) else name
    return out


def _scrub(text: str) -> str:
    """Last line of defence: no id-, label- or handle-shaped token reaches card text."""
    return _LEAK.sub("someone", text or "")


def _people(fact: Dict[str, Any]) -> List[str]:
    """People named inside a structured value, as the owner named them (ids are resolved by the sweep)."""
    value = fact.get("value") or {}
    out: List[str] = []
    for field in ("with", "person", "counterparty"):
        raw = value.get(field)
        for item in (raw if isinstance(raw, list) else [raw]):
            if isinstance(item, dict):
                item = item.get("name") or item.get("entity_id") or item.get("id")
            text = str(item or "").strip()
            if text and text.lower() not in ("you", "me", "self", "owner"):
                out.append(text)
    return out


def _said(fact: Dict[str, Any]) -> str:
    """What to show for a fact: the owner's own words when the writer kept them, else the value's description."""
    quote = " ".join(fact.get("quote", "").split())
    if quote:
        return f"“{quote[:TEXT_CHARS]}”"
    value = fact.get("value") or {}
    for field in ("description", "what", "goal", "theme", "claim", "account", "to_state"):
        if value.get(field):
            return str(value[field])[:TEXT_CHARS]
    return fact.get("object_value", "")[:TEXT_CHARS]


def _recency(fact: Dict[str, Any], now: datetime) -> float:
    return 1.0 / (1.0 + _days_ago(fact["at"], now) / 30.0)


def _refs(facts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for f in facts:
        for r in f["refs"]:
            key = (str(r["table"]), str(r["record_id"]))
            if key in seen:
                continue
            seen.add(key)
            out.append({"table": key[0], "record_id": key[1], "date": _day(r.get("date") or f["at"]),
                        "role": str(r.get("role") or f.get("actor_role") or "")})
    return out


# --- the rules ----------------------------------------------------------------------------------------------------------
def _candidate(rule: str, key: str, section: str, kind: str, valence: str, text: str, observation: str,
               facts: List[Dict[str, Any]], now: datetime, *, interpretation: str = "", person: str = "",
               row_ids: Iterable[str] = (), score: Optional[float] = None) -> Dict[str, Any]:
    newest = max(facts, key=lambda f: _aware(f["at"]) or datetime.min.replace(tzinfo=timezone.utc))
    return {
        "candidate_key": f"{rule}:{key}", "rule": rule, "section_id": section, "kind": kind, "valence": valence,
        "person": (person if person and not _LEAK.search(person) else None),
        "text": _scrub(" ".join(text.split()))[:600], "observation": _scrub(" ".join(observation.split()))[:400],
        "interpretation": _scrub(" ".join(interpretation.split()))[:400], "evidence_refs": _refs(facts),
        "fact_ids": sorted(f["id"] for f in facts), "row_ids": sorted(set(row_ids)),
        "score": score if score is not None else _recency(newest, now) + 0.15 * (len(facts) - 1),
        "event_at": newest["at"],
    }


def rules(facts: List[Dict[str, Any]], now: datetime, names: Dict[str, str]) -> List[Dict[str, Any]]:
    """Every candidate the facts support. Pure: no connection, no clock but ``now``. ``names`` is
    :func:`resolve_people`'s map; a raw value missing from it is shown only when it is a plain name."""
    def name(raw: str) -> str:
        if raw in names:
            return names[raw]
        return "" if (_is_label(raw) or _HANDLE.match(raw)) else raw

    def persons(fact: Dict[str, Any]) -> List[str]:
        return [n for n in (name(p) for p in _people(fact)) if n]

    by_pred: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in facts:
        by_pred[f["predicate"]].append(f)
    out: List[Dict[str, Any]] = []

    # S4 — joy: every peak moment of the year is a recall; an accomplishment with one mention is under-celebrated.
    peaks = [f for f in by_pred.get("moment.peak", []) if _days_ago(f["at"], now) <= 365]
    for f in peaks:
        kind = str(f["value"].get("kind") or "other")
        people = persons(f)
        who = f" with {', '.join(people)}" if people else ""
        text = f"On {_day(f['at'])} you wrote {_said(f)}{who}."
        obs = f"A moment of kind {kind}, dated {_day(f['at'])}."
        out.append(_candidate("peak", f["key"], "S4", "recall", "positive", text, obs, [f], now,
                              person=", ".join(people), row_ids=["RF-032", "RF-033"]))
        if kind == "accomplished" and len(f["refs"]) <= 1:
            out.append(_candidate("uncelebrated", f["key"], "S4", "recognition", "positive",
                                  f"You wrote {_said(f)} on {_day(f['at'])} and nothing after it returns to it.",
                                  "One accomplished moment with a single mention and no later reflection.", [f], now,
                                  interpretation="Something wonderful you may not have fully appreciated.",
                                  row_ids=["RF-034", "RF-038"]))
    small = [f for f in peaks if str(f["value"].get("kind")) == "small_pleasure"]
    if len(small) >= 2:
        out.append(_candidate("small_pleasures", "all", "S4", "recognition", "positive",
                              f"Small pleasures you have written about {len(small)} times since {_day(min(f['at'] for f in small))}: "
                              + "; ".join(_said(f) for f in small[:4]) + ".",
                              f"{len(small)} small-pleasure moments between {_day(min(f['at'] for f in small))} and "
                              f"{_day(max(f['at'] for f in small))}.", small, now,
                              interpretation="A reliable small joy.", row_ids=["RF-039"]))

    # S2 — relationships: gratitude not yet said; the people who recur in peak moments; conflicts and drift without a
    # closing event; an avoided conversation with someone.
    for f in by_pred.get("moment.gratitude", []):
        if f["value"].get("acknowledged") is True:
            continue
        people = persons(f) or ["someone"]
        out.append(_candidate("gratitude", f["key"], "S2", "recognition", "positive",
                              f"On {_day(f['at'])} you credited {people[0]}: {_said(f)}. No message of thanks appears after it.",
                              f"One gratitude row dated {_day(f['at'])}; acknowledgement not recorded.", [f], now,
                              person=people[0], row_ids=["RF-015"]))
    company: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in peaks:
        for p in persons(f):
            company[p].append(f)
    for person, fs in company.items():
        if len(fs) >= 2:
            out.append(_candidate("company", person, "S2", "recognition", "positive",
                                  f"{person} was there for {len(fs)} of the moments you described as good, "
                                  f"between {_day(min(x['at'] for x in fs))} and {_day(max(x['at'] for x in fs))}.",
                                  f"{len(fs)} peak moments name {person}.", fs, now, person=person,
                                  interpretation="Someone who may bring out the best in you.", row_ids=["RF-019", "RF-042"]))
    events: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in by_pred.get("rel.relationship_event", []):
        people = persons(f)
        if people:
            events[people[0]].append(f)
    for person, fs in events.items():
        fs.sort(key=lambda x: _aware(x["at"]) or now)
        last = fs[-1]
        event = str(last["value"].get("event") or "")
        if event in ("conflict", "drifted"):
            difficult = event == "conflict"
            out.append(_candidate("rel_open", f"{person}:{event}", "S2", "recognition",
                                  "difficult" if difficult else "neutral",
                                  f"The last thing you recorded about {person} ({_day(last['at'])}) was a {event.replace('_', ' ')}: "
                                  f"{_said(last)}. Nothing after it says it was reconciled or that you reconnected.",
                                  f"Latest relationship event for {person}: {event}, dated {_day(last['at'])}; no closing event.",
                                  [last], now, person=person, row_ids=["RF-017"] if difficult else ["RF-011"]))

    # S3 — difficult: open tensions (an avoided conversation also lands in S2), recurring kinds, hard life events.
    tensions = by_pred.get("moment.tension", [])
    resolved_people = {p for f in by_pred.get("moment.tension_resolved", []) for p in persons(f)}
    for f in tensions:
        status = f["value"].get("status")
        if status not in _OPEN or _days_ago(f["at"], now) > 180:
            continue
        kind = str(f["value"].get("kind") or "other")
        people = persons(f)
        person = people[0] if people else ""
        if person and person in resolved_people:
            continue
        text = f"On {_day(f['at'])} you wrote {_said(f)}."
        if kind == "avoided_conversation" and person:
            out.append(_candidate("avoided", f["key"], "S2", "invitation", "difficult",
                                  f"{text} It reads as a conversation with {person} you have not had.",
                                  f"An avoided conversation dated {_day(f['at'])}, still open.", [f], now, person=person,
                                  row_ids=["RF-014", "RF-041"]))
        label = {"undecided_decision": "a decision you keep revisiting", "weighing_on_me": "something weighing on you",
                 "hard_to_accept": "something you are finding hard to accept", "felt_misunderstood": "a time you felt misunderstood",
                 "disappointment": "a disappointment", "misunderstanding": "a misunderstanding",
                 "recurring_frustration": "a frustration that keeps returning", "conflict": "a conflict"}.get(kind, "an unresolved situation")
        out.append(_candidate("tension", f["key"], "S3", "recognition", "difficult", f"{text} It reads as {label}.",
                              f"A tension of kind {kind}, dated {_day(f['at'])}, with no row after it saying it was worked through.",
                              [f], now, person=person, row_ids=["RF-038", "RF-033", "RF-036"]))
    by_kind: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in tensions:
        by_kind[str(f["value"].get("kind") or "other")].append(f)
    for kind, fs in by_kind.items():
        # Recurrence is read from time (two instances a month or more apart); the pack carries no setting field,
        # because a small model guessed one the record never stated (pack gate, 24 Oct 2026).
        span = max(_days_ago(f["at"], now) for f in fs) - min(_days_ago(f["at"], now) for f in fs)
        if len(fs) >= 2 and span >= 30:
            out.append(_candidate("recurring_tension", kind, "S3", "recognition", "difficult",
                                  f"The same kind of tension ({kind.replace('_', ' ')}) appears {len(fs)} times, "
                                  f"from {_day(min(f['at'] for f in fs))} to {_day(max(f['at'] for f in fs))}.",
                                  f"{len(fs)} dated instances of {kind}, a month or more apart.", fs, now,
                                  interpretation="A frustration recurring across situations.", row_ids=["RF-034", "RF-063"]))
        if len(fs) >= 2:
            recent = [f for f in fs if _days_ago(f["at"], now) <= 90]
            earlier = [f for f in fs if _days_ago(f["at"], now) > 90]
            if earlier and not recent and any(str(f["value"].get("status")) in ("resolved", "let_go", "processed") for f in earlier):
                out.append(_candidate("growth", kind, "S5", "recognition", "positive",
                                      f"You used to write about {kind.replace('_', ' ')} ({len(earlier)} times before "
                                      f"{_day(max(f['at'] for f in earlier))}); nothing in the last three months, and the last "
                                      f"row says it was worked through.", f"{len(earlier)} earlier rows, none in the recent window.",
                                      earlier, now, interpretation="A change in how you handle it.", row_ids=["RF-044", "RF-078"]))

    # life events: hard ones moved past quickly, good ones to celebrate, recent ones that mark a change.
    for f in by_pred.get("life.event", []):
        kind = str(f["value"].get("kind") or "other")
        age = _days_ago(f["at"], now)
        if kind in _HARD_LIFE and age <= 365:
            out.append(_candidate("hard_event", f["key"], "S3", "recall", "difficult",
                                  f"On {_day(f['at'])} you recorded {_said(f)}.",
                                  f"A life event of kind {kind.replace('_', ' ')}, dated {_day(f['at'])}.", [f], now,
                                  interpretation="A difficult experience that may deserve more room.", row_ids=["RF-033"]))
        elif kind in _POSITIVE_LIFE and age <= 365:
            out.append(_candidate("good_event", f["key"], "S4", "recognition", "positive",
                                  f"On {_day(f['at'])} you recorded {_said(f)}.",
                                  f"A life event of kind {kind.replace('_', ' ')}, dated {_day(f['at'])}.", [f], now,
                                  row_ids=["RF-038"]))
        elif age <= 90:
            out.append(_candidate("change", f["key"], "S1", "recognition", "neutral",
                                  f"Something that changed recently: {_said(f)} ({_day(f['at'])}).",
                                  f"A life event of kind {kind.replace('_', ' ')} in the last 90 days.", [f], now,
                                  row_ids=["RF-007"]))
    for f in by_pred.get("life.turning_point", []):
        out.append(_candidate("turning_point", f["key"], "S5", "recall", "neutral",
                              f"A turning point you described: {_said(f)} ({_day(f['at'])}).",
                              f"A stated turning point dated {_day(f['at'])}.", [f], now, row_ids=["RF-049"]))

    # S1 — discover: open promises, recurring themes.
    resolved_counterparties = {p for f in by_pred.get("commit.resolved", []) for p in persons(f)}
    for f in by_pred.get("commit.made", []):
        value = f["value"]
        if value.get("status") not in _OPEN or _days_ago(f["at"], now) < 14 or value.get("direction") == "owed_to_owner":
            continue
        people = persons(f)
        person = people[0] if people else ""
        if person and person in resolved_counterparties:
            continue
        out.append(_candidate("open_promise", f["key"], "S1", "recall", "neutral",
                              f"On {_day(f['at'])} you promised {person or 'someone'}: {_said(f)}. Nothing after it closes it.",
                              f"An open commitment dated {_day(f['at'])}" + (f", due {value.get('due')}" if value.get("due") else "") + ".",
                              [f], now, person=person, row_ids=["RF-005", "RF-008"]))
    for f in by_pred.get("self.recurring_theme", []):
        out.append(_candidate("theme", f["key"], "S1", "recognition", "neutral",
                              f"A theme you keep returning to: {_said(f)}.",
                              f"Recorded as a recurring theme on {_day(f['at'])}.", [f], now,
                              interpretation="Something mentioned repeatedly without being unpacked.", row_ids=["RF-002", "RF-006"]))

    # S5 / S6 — self and work.
    for f in by_pred.get("self.stated_change", []):
        v = f["value"]
        change = f"from {v.get('from_state')} to {v.get('to_state')}" if v.get("from_state") or v.get("to_state") else _said(f)
        out.append(_candidate("stated_change", f["key"], "S5", "recognition", "neutral",
                              f"In your own words you changed {change} ({_day(f['at'])}).",
                              f"A stated change in {v.get('domain') or 'a domain'}, dated {_day(f['at'])}.", [f], now,
                              row_ids=["RF-043", "RF-048"]))
    for f in by_pred.get("self.aspirational_identity", []):
        v = f["value"]
        out.append(_candidate("aspiration_identity", f["key"], "S5", "recognition", "positive",
                              f"Who you said you want to become: {v.get('identity') or _said(f)} ({v.get('stage') or 'stage unstated'}, {_day(f['at'])}).",
                              f"An aspirational identity dated {_day(f['at'])}.", [f], now, row_ids=["RF-050"]))
    for f in by_pred.get("asp.goal", []):
        v = f["value"]
        if str(v.get("status") or "") in _STALLED:
            section = "S6" if str(v.get("domain") or "") == "work" else "S5"
            out.append(_candidate("stalled_goal", f["key"], section, "recognition", "difficult",
                                  f"A goal you marked {v.get('status')}: {v.get('goal') or _said(f)} ({_day(f['at'])}).",
                                  f"Goal status {v.get('status')}, dated {_day(f['at'])}.", [f], now,
                                  row_ids=["RF-060", "RF-057"] if section == "S6" else ["RF-052"]))
    milestones = by_pred.get("asp.milestone", [])
    for f in by_pred.get("asp.aspiration", []):
        if _days_ago(f["at"], now) <= 365 and not milestones:
            out.append(_candidate("aspiration", f["key"], "S5", "recognition", "neutral",
                                  f"Something you said you want: {_said(f)} ({_day(f['at'])}), with no milestone recorded since.",
                                  f"An aspiration dated {_day(f['at'])}; no milestone rows.", [f], now,
                                  interpretation="A desire stated and not yet started.", row_ids=["RF-052"]))
    for f in milestones:
        if _days_ago(f["at"], now) <= 90:
            out.append(_candidate("milestone", f["key"], "S6", "recognition", "positive",
                                  f"Progress you recorded: {_said(f)} ({_day(f['at'])}).",
                                  f"A milestone dated {_day(f['at'])}.", [f], now,
                                  interpretation="Progress that may not have been appreciated yet.", row_ids=["RF-058", "RF-066"]))

    # S7 — connect: a person on both sides; places or activities that recur in peak moments.
    tense_people = {p: f for f in tensions for p in persons(f)}
    for person, fs in company.items():
        if person in tense_people:
            t = tense_people[person]
            out.append(_candidate("both_ends", person, "S7", "recognition", "neutral",
                                  f"{person} appears in a moment you described as good ({_day(fs[0]['at'])}) and in a tension "
                                  f"({_day(t['at'])}).", "The same person on both sides of your records.", [fs[0], t], now,
                                  person=person, interpretation="Two ends that may be related.", row_ids=["RF-072", "RF-076"]))
    where: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in peaks:
        for field in ("where", "activity"):
            raw = f["value"].get(field)
            raw = raw.get("name") if isinstance(raw, dict) else raw
            if raw:
                where[str(raw).strip().lower()].append(f)
    for place, fs in where.items():
        if len(fs) >= 2:
            out.append(_candidate("energy", place, "S7", "recognition", "positive",
                                  f"'{place}' appears in {len(fs)} of the moments you described as good.",
                                  f"{len(fs)} peak moments share it.", fs, now,
                                  interpretation="A place or activity that reliably fills you with energy.", row_ids=["RF-042", "RF-075"]))
    return out


# --- the sweep ---------------------------------------------------------------------------------------------------------
def sweep(conn: sqlite3.Connection, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Rebuild the candidates from the facts of the enabled reflection packs. Upserts by ``candidate_key`` so a
    candidate keeps its id, its label, its suppression and its opened count across sweeps; a candidate the facts no
    longer support is retired (``gone``) rather than deleted, so a label on it keeps its row. Counts only."""
    moment = now or now_utc()
    packs = enabled_packs(conn)
    facts = load_facts(conn, packs)
    produced = rules(facts, moment, resolve_people(conn, [p for f in facts for p in _people(f)])) if facts else []
    stamp = moment.isoformat()
    ensure_tables(conn)
    with with_db_write():
        existing = {str(r[0]): str(r[1]) for r in conn.execute(
            f"SELECT candidate_key, candidate_id FROM {CANDIDATES_TABLE}").fetchall()}
        kept = set()
        for c in produced:
            kept.add(c["candidate_key"])
            if c["candidate_key"] in existing:
                conn.execute(
                    f"UPDATE {CANDIDATES_TABLE} SET section_id=?, kind=?, valence=?, person=?, text=?, observation=?, "
                    f"interpretation=?, evidence_refs_json=?, fact_ids_json=?, row_ids_json=?, score=?, event_at=?, "
                    f"seen_at=?, engine_version=?, retired=CASE WHEN retire_reason='gone' THEN 0 ELSE retired END, "
                    f"retire_reason=CASE WHEN retire_reason='gone' THEN NULL ELSE retire_reason END WHERE candidate_key=?",
                    (c["section_id"], c["kind"], c["valence"], c["person"], c["text"], c["observation"], c["interpretation"],
                     json.dumps(c["evidence_refs"]), json.dumps(c["fact_ids"]), json.dumps(c["row_ids"]), c["score"],
                     c["event_at"], stamp, VERSION, c["candidate_key"]))
            else:
                conn.execute(
                    f"INSERT INTO {CANDIDATES_TABLE} (candidate_id, candidate_key, rule, section_id, kind, valence, person, "
                    f"text, observation, interpretation, evidence_refs_json, fact_ids_json, row_ids_json, score, event_at, "
                    f"built_at, seen_at, engine_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, c["candidate_key"], c["rule"], c["section_id"], c["kind"], c["valence"], c["person"],
                     c["text"], c["observation"], c["interpretation"], json.dumps(c["evidence_refs"]),
                     json.dumps(c["fact_ids"]), json.dumps(c["row_ids"]), c["score"], c["event_at"], stamp, stamp, VERSION))
        gone = [k for k in existing if k not in kept]
        for start in range(0, len(gone), 500):
            chunk = gone[start:start + 500]
            conn.execute(f"UPDATE {CANDIDATES_TABLE} SET retired=1, retire_reason='gone' WHERE candidate_key IN "
                         f"({','.join('?' for _ in chunk)}) AND retired=0", chunk)
        commit_connection(conn)
    by_section: Dict[str, int] = defaultdict(int)
    for c in produced:
        by_section[c["section_id"]] += 1
    return {"packs": packs, "facts": len(facts), "candidates": len(produced), "retired": len(gone),
            "by_section": dict(sorted(by_section.items())), "built_at": stamp}


def last_sweep_at(conn: sqlite3.Connection) -> Optional[datetime]:
    if not _present(conn, CANDIDATES_TABLE):
        return None
    found = conn.execute(f"SELECT MAX(seen_at) FROM {CANDIDATES_TABLE}").fetchone()
    return _aware(found[0]) if found and found[0] else None


def sweep_due(conn: sqlite3.Connection, *, now: Optional[datetime] = None) -> bool:
    """A sweep is owed when a reflection pack is on and no sweep ran in the last interval."""
    if not enabled_packs(conn):
        return False
    last = last_sweep_at(conn)
    moment = now or now_utc()
    return last is None or moment - last >= timedelta(hours=SWEEP_INTERVAL_HOURS)


def enqueue_sweep(conn: Any, *, now: Optional[datetime] = None) -> Optional[str]:
    """Queue one sweep on the catch-up lane when one is due and none is in flight. Returns the job id."""
    from ..pipeline.job_store import enqueue_job, ensure_pipeline_jobs_schema

    if conn is None or not sweep_due(conn, now=now):
        return None
    ensure_pipeline_jobs_schema(conn)
    with with_db_write():
        if conn.execute("SELECT 1 FROM pipeline_jobs WHERE kind=? AND status IN ('queued','running') LIMIT 1",
                        (JOB_KIND,)).fetchone():
            return None
        return enqueue_job(conn, kind=JOB_KIND, payload={"reason": "due"})


async def execute_sweep(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The job executor: one sweep on a worker thread's connection; the result is counts only."""
    import asyncio

    from ..core.state import get_db_connection

    def run() -> Dict[str, Any]:
        conn = get_db_connection()
        if conn is None:
            return {"status": "error", "error": "no database"}
        return {"status": "ok", **sweep(conn)}

    return await asyncio.to_thread(run)


# --- the owner's reads ----------------------------------------------------------------------------------------------
def balance(cards: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Interleave valences so a set never opens with a run of difficult cards: positive, neutral, difficult, round
    robin, each bucket best score first."""
    buckets: Dict[str, List[Dict[str, Any]]] = {"positive": [], "neutral": [], "difficult": []}
    for c in cards:
        buckets.setdefault(c.get("valence", "neutral"), buckets["neutral"]).append(c)
    order = [buckets["positive"], buckets["neutral"], buckets["difficult"]]
    out: List[Dict[str, Any]] = []
    while len(out) < limit and any(order):
        for bucket in order:
            if bucket and len(out) < limit:
                out.append(bucket.pop(0))
    return out


def _card(row: Dict[str, Any]) -> Dict[str, Any]:
    refs = json.loads(row.get("evidence_refs_json") or "[]")
    return {
        "candidate_id": row["candidate_id"], "section_id": row["section_id"], "kind": row["kind"],
        "valence": row["valence"], "text": row["text"], "observation": row["observation"],
        "interpretation": row["interpretation"], "person": row.get("person"),
        "evidence": [{"source": r.get("table"), "date": r.get("date"), "role": r.get("role")} for r in refs],
        "row_ids": json.loads(row.get("row_ids_json") or "[]"), "event_at": row.get("event_at"),
    }


def _rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    cursor = conn.execute(sql, tuple(params))
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, r)) for r in cursor.fetchall()]


def invitations(conn: sqlite3.Connection, starter: Any = None, limit: Any = 5, exclude: Any = None, *,
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """Balanced candidates for one starter (or across every section when ``starter`` is None: the home list).
    A read: nothing is written; the client passes the ids it showed recently as ``exclude``."""
    if starter is not None and starter not in STARTERS:
        raise ReflectionRequestError("invalid_reflection_request")
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ReflectionRequestError("invalid_reflection_request")
    if exclude is None:
        exclude = []
    if not isinstance(exclude, list) or len(exclude) > MAX_EXCLUDE or not all(isinstance(e, str) for e in exclude):
        raise ReflectionRequestError("invalid_reflection_request")
    status = pack_status(conn)
    if not status["packs_enabled"] or not _present(conn, CANDIDATES_TABLE):
        return {"cards": [], **status, "built_at": None}
    moment = (now or now_utc()).isoformat()
    sections = list(STARTERS[starter]) if starter else []
    where = ["retired=0", "(suppressed_until IS NULL OR suppressed_until < ?)"]
    params: List[Any] = [moment]
    if sections:
        where.append(f"section_id IN ({','.join('?' for _ in sections)})")
        params += sections
    rows = _rows(conn, f"SELECT * FROM {CANDIDATES_TABLE} WHERE {' AND '.join(where)} ORDER BY score DESC, event_at DESC "
                       f"LIMIT 200", params)
    excluded = set(exclude)
    fresh = [r for r in rows if r["candidate_id"] not in excluded]
    chosen = balance([_card(r) for r in (fresh if len(fresh) >= limit else rows)], limit)
    last = last_sweep_at(conn)
    return {"cards": chosen, **status, "built_at": last.isoformat() if last else None}


def _record_excerpt(conn: sqlite3.Connection, table: str, record_id: str) -> Dict[str, Any]:
    """A source record as evidence: its date and source always; its text only when the owner wrote it."""
    from .truth_checks import load_record, with_offset

    record = load_record(conn, table, record_id)
    if record is None:
        return {"source": table, "date": None, "role": "", "excerpt": "", "present": False}
    excerpt = " ".join(record["text"].split())[:EXCERPT_CHARS] if record["owner"] else ""
    return {"source": record["source"], "date": _day(record["at"]) if record["at"] else None,
            "role": "authored" if record["owner"] else "other", "excerpt": excerpt, "present": True}


def _candidate_row(conn: sqlite3.Connection, candidate_id: Any) -> Dict[str, Any]:
    if not isinstance(candidate_id, str) or not candidate_id.strip() or len(candidate_id) > 64:
        raise ReflectionRequestError("invalid_reflection_request")
    if not _present(conn, CANDIDATES_TABLE):
        raise ReflectionRequestError("unknown_candidate")
    rows = _rows(conn, f"SELECT * FROM {CANDIDATES_TABLE} WHERE candidate_id=?", (candidate_id.strip(),))
    if not rows:
        raise ReflectionRequestError("unknown_candidate")
    return rows[0]


def conversation(conn: sqlite3.Connection, candidate_id: Any) -> Dict[str, Any]:
    """The prepared conversation for one candidate: character, prompt, opening, questions and the evidence with the
    owner's own words. A read: nothing is written (the app records ``opened`` through the label door)."""
    row = _candidate_row(conn, candidate_id)
    refs = json.loads(row.get("evidence_refs_json") or "[]")
    evidence = []
    for ref in refs[:8]:
        found = _record_excerpt(conn, str(ref.get("table")), str(ref.get("record_id")))
        if not found["present"]:
            continue
        evidence.append({"source": found["source"], "date": found["date"] or ref.get("date"), "role": found["role"],
                         "excerpt": found["excerpt"]})
    if not evidence:
        # The facts' own words, when the records are not of a table the lane reads (or are gone): still the owner's.
        for ref in refs[:8]:
            evidence.append({"source": ref.get("table"), "date": ref.get("date"), "role": ref.get("role") or "", "excerpt": ""})
    prepared = compose(row, evidence)
    return {"candidate_id": row["candidate_id"], "section_id": row["section_id"], "kind": row["kind"],
            "valence": row["valence"], "evidence": evidence, **prepared}


# --- the owner's write ----------------------------------------------------------------------------------------------
def label(conn: sqlite3.Connection, candidate_id: Any, label_value: Any, note: Any = None, *,
          now: Optional[datetime] = None) -> Dict[str, Any]:
    """The owner's label of one candidate. ``opened`` counts a conversation started and writes no label row;
    the others replace the previous label, apply their suppression, and write the owner-only ``reflection_label``
    object (replaced in place on relabel)."""
    if label_value not in LABELS:
        raise ReflectionRequestError("invalid_reflection_label")
    if note is not None and not isinstance(note, str):
        raise ReflectionRequestError("invalid_reflection_label")
    note = " ".join((note or "").split())[:MAX_NOTE_CHARS] or None
    row = _candidate_row(conn, candidate_id)
    moment = now or now_utc()
    stamp = moment.isoformat()
    with with_db_write():
        if label_value == "opened":
            conn.execute(f"UPDATE {CANDIDATES_TABLE} SET opened_count=opened_count+1 WHERE candidate_id=?", (row["candidate_id"],))
            commit_connection(conn)
            return {"ok": True, "label_id": None, "opened_count": int(row["opened_count"]) + 1}
        existing = conn.execute(f"SELECT label_id FROM {LABELS_TABLE} WHERE candidate_id=?", (row["candidate_id"],)).fetchone()
        label_id = str(existing[0]) if existing else uuid.uuid4().hex
        conn.execute(
            f"INSERT INTO {LABELS_TABLE} (label_id, candidate_id, label, note, labelled_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET label=excluded.label, note=excluded.note, labelled_at=excluded.labelled_at",
            (label_id, row["candidate_id"], label_value, note, stamp))
        if label_value == "not_now":
            conn.execute(f"UPDATE {CANDIDATES_TABLE} SET suppressed_until=? WHERE candidate_id=?",
                         ((moment + timedelta(days=NOT_NOW_DAYS)).isoformat(), row["candidate_id"]))
        elif label_value == "not_true":
            conn.execute(f"UPDATE {CANDIDATES_TABLE} SET retired=1, retire_reason='not_true' WHERE candidate_id=?",
                         (row["candidate_id"],))
        elif label_value == "too_personal":
            fact_ids = set(json.loads(row.get("fact_ids_json") or "[]"))
            for other in _rows(conn, f"SELECT candidate_id, person, fact_ids_json FROM {CANDIDATES_TABLE} WHERE retired=0"):
                same_person = bool(row.get("person")) and other.get("person") == row.get("person")
                shared = fact_ids & set(json.loads(other.get("fact_ids_json") or "[]"))
                if other["candidate_id"] == row["candidate_id"] or same_person or shared:
                    conn.execute(f"UPDATE {CANDIDATES_TABLE} SET retired=1, retire_reason='too_personal' WHERE candidate_id=?",
                                 (other["candidate_id"],))
        elif label_value == "worth_it":
            conn.execute(f"UPDATE {CANDIDATES_TABLE} SET suppressed_until=NULL WHERE candidate_id=?", (row["candidate_id"],))
        payload = {"candidate_id": row["candidate_id"], "section_id": row["section_id"], "rule": row["rule"],
                   "label": label_value, "note": note, "valence": row["valence"], "text": row["text"],
                   "disclosure": "owner_only"}
        refs = [{"table": r.get("table"), "record_id": r.get("record_id")}
                for r in json.loads(row.get("evidence_refs_json") or "[]")]
        _write_label_object(conn, f"reflection:{row['candidate_id']}", payload, LABEL_CONFIDENCE[label_value], refs,
                            str(row.get("event_at") or stamp), stamp)
        commit_connection(conn)
    return {"ok": True, "label_id": label_id}


def _write_label_object(conn: sqlite3.Connection, key: str, payload: Dict[str, Any], confidence: float,
                        refs: List[Dict[str, Any]], valid_from: str, now: str) -> None:
    """Inside the caller's gate; the truth lane's ``verified_claim`` shape, for the same reasons."""
    found = conn.execute(
        "SELECT object_id FROM signal_objects WHERE signal_dimension=? AND object_type=? AND object_key=? AND valid_to IS NULL",
        (OBJECT_DIMENSION, OBJECT_TYPE, key)).fetchone()
    if found:
        conn.execute("UPDATE signal_objects SET payload_json=?, confidence=?, source_refs_json=?, valid_from=?, updated_at=? "
                     "WHERE object_id=?", (json.dumps(payload), confidence, json.dumps(refs), valid_from, now, found[0]))
        return
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, confidence, "
        "source_refs_json, valid_from, valid_to, extractor_version, created_at, updated_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'owner')",
        (str(uuid.uuid4()), OBJECT_DIMENSION, OBJECT_TYPE, key, json.dumps(payload), confidence, json.dumps(refs),
         valid_from, EXTRACTOR_VERSION, now, now))


def weekly_counts(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Aggregates for the program's weekly readout (no door yet): candidates by section and valence, labels by value,
    conversations opened."""
    if not _present(conn, CANDIDATES_TABLE):
        return {"candidates": 0, "by_section": {}, "by_valence": {}, "labels": {}, "opened": 0}
    out: Dict[str, Any] = {"candidates": 0, "by_section": defaultdict(int), "by_valence": defaultdict(int),
                           "labels": defaultdict(int), "opened": 0}
    for section, valence, opened in conn.execute(
            f"SELECT section_id, valence, opened_count FROM {CANDIDATES_TABLE} WHERE retired=0 OR retire_reason!='gone'"):
        out["candidates"] += 1
        out["by_section"][section] += 1
        out["by_valence"][valence] += 1
        out["opened"] += int(opened or 0)
    if _present(conn, LABELS_TABLE):
        for value, n in conn.execute(f"SELECT label, COUNT(*) FROM {LABELS_TABLE} GROUP BY label"):
            out["labels"][value] = int(n)
    return {k: (dict(v) if isinstance(v, defaultdict) else v) for k, v in out.items()}

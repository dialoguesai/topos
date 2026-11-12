"""``pack_facts``: an app's read of the facts in the packs its grant names (DISCLOSURE_RULES DR-61, DR-66).

The owner ruled on 24 Oct 2026 that the node is queried by an app through a grant, and that the app composes everything.
This module is the node's whole part of that: the ACTIVE facts of the packs the owner switched on
(``pack_registry.enabled``), each with its structured value, the owner's own words and where it came from. No candidate,
no ranking, no prompt, no write.

Who may read it (``core/handlers/query.py``): the owner's app, and an owner agent on the socket whose grant names packs
(``mcp_clients.AppGrant``), for those packs. Every other class is refused before the payload is read.

An owner agent gets the shape below with every filter applied. The owner's app gets the same shape with no Off-limits
and no special-class filter, as on every owner read (owner questions read with full visibility):

* only active facts of the packs asked for, and only of packs the owner enabled;
* special-class facts held back: a fact whose pack or predicate is ``special`` by the pack definition, whose stamp
  says so, or whose predicate the current pack no longer declares (``narrative.self``'s ``self.attribution_pattern``
  today). No switch opens special-class kinds to an agent yet (``query/packet_resolution``);
* Off-limits applied by absence, never by denial (``blackhole_guard`` D5): a fact is left out when its subject, its
  object or any person or entity it binds is protected, when a name it shows is protected, when its own text names a
  protected person, when a source record it rests on is protected or mentions one, or when it has no source record to
  judge. While an Off-limits rebuild is pending nothing is returned (withheld, never stale). A protected record takes
  out the facts resting on it and no others: a pack fact carries its lineage (``source_refs``), unlike the prose stores
  the guard's blanket withholding was built for (``withhold_pending_rebuild``). Nothing counts what was left out;
* people and entities as display names only (:func:`resolve_people`): never an entity id, a runner label (``id:``,
  ``key:``) or a raw phone number or email. A reference that does not resolve to a name is left out, the writer's bound
  ``*_entity_id(s)`` keys are dropped, and any id- or handle-shaped token left in text becomes "someone";
* the fact's quote, and a source record's text, only when the owner wrote it. A record someone else wrote gives its
  source and date only. Record ids never leave.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "pack_facts/v1"

DEFAULT_LIMIT = 500
MAX_PACKS = 100
MAX_LIMIT = 1000
MAX_FACT_IDS = 100
MAX_SOURCES = 8
EXCERPT_CHARS = 400
QUOTE_CHARS = 200
#: The one refusal code for a payload this read does not take; never the input.
INVALID = "invalid_pack_facts_request"
_PAYLOAD_KEYS = frozenset({"packs", "fact_ids", "since", "limit", "include_excerpts"})
#: How a value names the owner. Shown as "you", never counted as a person.
_SELF_WORDS = frozenset({"you", "me", "self", "owner", "myself"})
#: Person-valued fields whatever their declared type (``derivation.template._PERSON_FIELDS``).
_PERSON_FLOOR = ("person", "member")

#: An entity id as the spine writes them (``ent_<hex>``) or a bare UUID.
_ENTITY_ID = re.compile(r"^(?:ent_[0-9a-f]{6,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)
#: A raw handle: a phone number or an email address. Never a display name.
_HANDLE = re.compile(r"^(?:\+?[0-9][0-9 ().-]{5,}|[^@\s]+@[^@\s]+)$")
#: Anything id-, label- or handle-shaped that must never reach shown text (the last line of defence). A phone number is
#: a run of digits, spaces, dots, dashes and brackets that does not start inside another number and is not an ISO date
#: (the first version read every date a card printed as a phone number).
_LEAK = re.compile(r"(?:\b(?:id|key):\S+|\bent_[0-9a-f]{6,}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
                   r"|(?<![0-9-])(?![0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9]))\+?[0-9][0-9 ().-]{8,}[0-9]"
                   r"|[^@\s]+@[^@\s]+\.[a-z]{2,})", re.I)


class PackFactsRequestError(ValueError):
    """A payload this read refuses; ``str(exc)`` is always :data:`INVALID`."""


# --- small readers -----------------------------------------------------------------------------------------------------
def _present(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


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


def enabled_packs(conn: sqlite3.Connection) -> List[str]:
    """The packs the owner switched on. No registry table means none on."""
    if not _present(conn, "pack_registry"):
        return []
    return sorted(str(r[0]) for r in conn.execute("SELECT pack_id FROM pack_registry WHERE enabled=1").fetchall())


def pack_status(conn: sqlite3.Connection, granted: Optional[Tuple[str, ...]] = None) -> Dict[str, List[str]]:
    """Which packs this reader can use now, and which its grant names that are switched off. ``granted`` None: the owner's
    own read, every pack that is on."""
    on = enabled_packs(conn)
    if granted is None:
        return {"packs_enabled": on, "packs_missing": []}
    return {"packs_enabled": [p for p in granted if p in on], "packs_missing": [p for p in granted if p not in on]}


# --- people --------------------------------------------------------------------------------------------------------------
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


def _entity_id_of(raw: str) -> Optional[str]:
    """The entity id a reference names (an ``id:`` label or a bare id), else None."""
    if raw.lower().startswith("id:"):
        return raw.split(":", 1)[1].strip() or None
    return raw if _ENTITY_ID.match(raw) else None


def resolve_people(conn: sqlite3.Connection, raws: Iterable[str]) -> Dict[str, str]:
    """Every raw person value a fact carries -> what may be shown: a display name, or "" (leave the person out).

    The derivation writer binds a message's recipient or sender as a RUNNER LABEL, ``id:<entity id>`` or
    ``key:<messenger key>`` (``template.is_runner_label``), not as a name. A label, an entity id or a raw handle is
    never shown: an ``id:`` label or a bare entity id resolves through the entity spine (the owner's own entity to
    nothing), a ``key:`` label through the owner's contacts, and anything that does not resolve to a name is dropped.
    A plain name passes as written."""
    from .truth_checks import _contact_name

    raws = [r for r in dict.fromkeys(raws) if r]
    entity_ids = {raw: eid for raw in raws if (eid := _entity_id_of(raw))}
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
    """Last line of defence: no id-, label- or handle-shaped token reaches shown text."""
    return _LEAK.sub("someone", text or "")


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _scrub_value(v) for k, v in value.items()
                if not str(k).endswith(("_entity_id", "_entity_ids"))}
    return value


def _strings(value: Any) -> List[str]:
    """Every string inside a value, keys left out (what the Off-limits text scan reads)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    return []


# --- pack definitions -----------------------------------------------------------------------------------------------
@lru_cache(maxsize=8)
def _definitions(pack_ids: Tuple[str, ...], signature: Tuple[Tuple[str, float], ...]) -> Dict[str, Any]:
    from ..features.derivation.packs import load_packs
    from ..features.derivation.registry import bundled_pack_dir

    return load_packs(bundled_pack_dir(), only=list(pack_ids))


def pack_definitions(pack_ids: Iterable[str]) -> Dict[str, Any]:
    """The bundled definitions of these packs, reloaded when a file changes. Raises when they cannot be loaded: the
    read then fails closed rather than guessing which fields name people or which predicates are special."""
    from ..features.derivation.registry import bundled_pack_dir

    base = bundled_pack_dir()
    ids = tuple(sorted(set(pack_ids)))
    signature = tuple((p, (base / f"{p}.yaml").stat().st_mtime) for p in ids)
    return _definitions(ids, signature)


def _field_kinds(definition: Any, predicate: str) -> Dict[str, str]:
    """field -> "person" or "entity", for every reference field the predicate declares (by its TYPE, as the writer
    binds them), plus the person floor."""
    from ..features.derivation.packs import ENTITY_REF_VALUE_TYPES, PERSON_REF_VALUE_TYPES

    declared = getattr(definition, "predicates", {}).get(predicate) if definition is not None else None
    out: Dict[str, str] = {}
    for field, spec in (getattr(declared, "value_schema", None) or {}).items():
        kind = str(spec).strip() if isinstance(spec, str) else ""
        if kind in PERSON_REF_VALUE_TYPES:
            out[str(field)] = "person"
        elif kind in ENTITY_REF_VALUE_TYPES:
            out[str(field)] = "entity"
    for field in _PERSON_FLOOR:
        out.setdefault(field, "person")
    return out


def _special(definition: Any, predicate: str, payload: Dict[str, Any]) -> bool:
    """Special-class for an agent: by the fact's stamp, by the current definition, or unclassifiable."""
    if str(payload.get("sensitivity") or "").strip().lower() == "special":
        return True
    if definition is None or predicate not in getattr(definition, "predicates", {}):
        return True
    return definition.effective_sensitivity(predicate) == "special"


# --- the request ---------------------------------------------------------------------------------------------------------
def parse_request(payload: Any, granted: Optional[Tuple[str, ...]] = None) -> Dict[str, Any]:
    """``{"packs"?: [pack ids], "fact_ids"?: [ids], "since"?: "YYYY-MM-DD", "limit"?: 1..1000,
    "include_excerpts"?: bool}`` -> the request, or :class:`PackFactsRequestError`. A pack outside ``granted`` is refused
    (None: the owner's own read, any pack)."""
    if payload is None:
        payload = {}
    if not isinstance(payload, dict) or not set(payload) <= _PAYLOAD_KEYS:
        raise PackFactsRequestError(INVALID)
    packs = payload.get("packs")
    if packs is not None and (not isinstance(packs, list) or not packs or len(packs) > MAX_PACKS
                              or not all(isinstance(p, str) and (granted is None or p in granted) for p in packs)):
        raise PackFactsRequestError(INVALID)
    fact_ids = payload.get("fact_ids")
    if fact_ids is not None and (not isinstance(fact_ids, list) or not fact_ids or len(fact_ids) > MAX_FACT_IDS
                                 or not all(isinstance(f, str) and 0 < len(f) <= 64 for f in fact_ids)):
        raise PackFactsRequestError(INVALID)
    since = payload.get("since")
    if since is not None:
        if not isinstance(since, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", since):
            raise PackFactsRequestError(INVALID)
        try:
            date.fromisoformat(since)
        except ValueError:
            raise PackFactsRequestError(INVALID) from None
    limit = payload.get("limit", DEFAULT_LIMIT)
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise PackFactsRequestError(INVALID)
    include = payload.get("include_excerpts", False)
    if type(include) is not bool:
        raise PackFactsRequestError(INVALID)
    return {"packs": list(packs) if packs else None, "fact_ids": list(dict.fromkeys(fact_ids)) if fact_ids else None,
            "since": since, "limit": limit, "include_excerpts": include}


def _rows(conn: sqlite3.Connection, packs: List[str], fact_ids: Optional[List[str]],
          since: Optional[str]) -> List[Tuple[Any, ...]]:
    if not packs or not _present(conn, "signal_objects"):
        return []
    columns = {str(r[1]) for r in conn.execute("PRAGMA table_info(signal_objects)").fetchall()}
    if "ontology_id" not in columns:
        return []
    version = "ontology_version" if "ontology_version" in columns else "NULL"
    altitude = "altitude" if "altitude" in columns else "NULL"
    where = ["object_type='fact'", "valid_to IS NULL", f"ontology_id IN ({','.join('?' for _ in packs)})"]
    params: List[Any] = list(packs)
    if fact_ids:
        where.append(f"object_id IN ({','.join('?' for _ in fact_ids)})")
        params += fact_ids
    if since:
        where.append("valid_from >= ?")
        params.append(since)
    return conn.execute(
        f"SELECT object_id, payload_json, source_refs_json, valid_from, ontology_id, {version}, {altitude} "
        f"FROM signal_objects WHERE {' AND '.join(where)} ORDER BY valid_from DESC, object_id", params).fetchall()


def _reference(item: Any) -> Tuple[str, Optional[str]]:
    """(the raw reference to resolve, an entity id the item carries beside it)."""
    if isinstance(item, dict):
        eid = str(item.get("entity_id") or item.get("id") or "").strip() or None
        return str(item.get("name") or eid or "").strip(), eid
    return str(item if item is not None else "").strip(), None


def _references(value: Dict[str, Any], kinds: Dict[str, str]) -> Tuple[List[str], List[str]]:
    """(every raw reference in the value's reference fields, every entity id the value binds)."""
    raws: List[str] = []
    ids: List[str] = []
    for key, field_value in value.items():
        key = str(key)
        if key.endswith(("_entity_id", "_entity_ids")):
            ids += [str(x) for x in (field_value if isinstance(field_value, list) else [field_value]) if x]
            continue
        if key not in kinds:
            continue
        for item in (field_value if isinstance(field_value, list) else [field_value]):
            raw, eid = _reference(item)
            if eid:
                ids.append(eid)
            if raw and raw.lower() not in _SELF_WORDS:
                raws.append(raw)
                found = _entity_id_of(raw)
                if found:
                    ids.append(found)
    return raws, ids


def _shown_value(value: Dict[str, Any], kinds: Dict[str, str], names: Dict[str, str]) -> Tuple[Dict[str, Any], List[str]]:
    """(the value as an app may see it, the people it names as display names)."""
    shown: Dict[str, Any] = {}
    people: List[str] = []
    for key, field_value in value.items():
        key = str(key)
        if key.endswith(("_entity_id", "_entity_ids")):
            continue
        kind = kinds.get(key)
        if kind is None:
            shown[key] = _scrub_value(field_value)
            continue
        items: List[str] = []
        for item in (field_value if isinstance(field_value, list) else [field_value]):
            raw, _ = _reference(item)
            if not raw:
                continue
            if raw.lower() in _SELF_WORDS:
                items.append("you")
                continue
            name = names.get(raw, "")
            if name:
                items.append(name)
                if kind == "person":
                    people.append(name)
        shown[key] = items if isinstance(field_value, list) else (items[0] if items else None)
    return shown, list(dict.fromkeys(people))


def _sources(conn: sqlite3.Connection, refs: List[Dict[str, Any]],
             cache: Dict[str, Optional[str]]) -> List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    """Each source reference with its record when the record is one the node reads (else None)."""
    from .truth_checks import load_record

    out = []
    for ref in refs:
        try:
            record = load_record(conn, str(ref["table"]), str(ref["record_id"]), names=cache)
        except sqlite3.Error:
            record = None
        out.append((ref, record))
    return out


def _withheld(guard: Any, payload: Dict[str, Any], ids: List[str], raws: List[str], names: List[str],
              texts: List[str], sources: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]) -> bool:
    """Whether Off-limits leaves this fact out of an agent's read (see the module docstring)."""
    if not sources:
        return True
    for entity_id in [payload.get("subject_entity_id"), payload.get("object_entity_id"), *ids]:
        if entity_id and guard.blocks_entity_id(str(entity_id)):
            return True
    if any(guard.blocks_name(name) for name in names):
        return True
    if guard.text_mentions_blackholed("\n".join([*texts, *raws, *names])):
        return True
    for ref, record in sources:
        if guard.blocks_record_id(str(ref["record_id"])):
            return True
        if record is not None and guard.text_mentions_blackholed(record.get("text") or ""):
            return True
    return False


# --- the read ------------------------------------------------------------------------------------------------------------
def read(conn: sqlite3.Connection, request: Dict[str, Any], *, agent: bool, guard: Any = None,
         granted: Optional[Tuple[str, ...]] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The facts for one parsed request. ``agent`` applies the special-class hold-back and Off-limits through
    ``guard`` (required for an agent); the owner's app reads unfiltered. Raises when the pack definitions cannot be
    loaded or the protected-record lineage cannot be read (the handler answers a fixed code)."""
    if agent and guard is None:
        raise ValueError("an agent read needs its Off-limits guard")
    status = pack_status(conn, granted)
    wanted = request.get("packs")
    packs = [p for p in status["packs_enabled"] if wanted is None or p in wanted]
    result: Dict[str, Any] = {"version": VERSION, **status, "facts": [], "truncated": False,
                              "as_of": (now or datetime.now(timezone.utc)).isoformat()}
    if not packs:
        return result
    filtering = bool(agent and guard.active)
    if filtering:
        from ..features.lifecycle.blackhole import BlackholeStore

        if BlackholeStore(conn).has_pending_rebuild(view=guard.view):
            return result
    definitions = pack_definitions(packs)
    parsed = []
    all_raws: List[str] = []
    for object_id, payload_json, refs_json, valid_from, pack, version, altitude in _rows(
            conn, packs, request.get("fact_ids"), request.get("since")):
        try:
            payload = json.loads(payload_json or "{}")
            refs = json.loads(refs_json or "[]")
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        predicate = str(payload.get("predicate") or "")
        definition = definitions.get(str(pack))
        if agent and _special(definition, predicate, payload):
            continue
        value = payload.get("value_struct") if isinstance(payload.get("value_struct"), dict) else {}
        kinds = _field_kinds(definition, predicate)
        raws, ids = _references(value, kinds)
        all_raws += raws
        refs = [r for r in (refs if isinstance(refs, list) else [])
                if isinstance(r, dict) and r.get("table") and r.get("record_id")]
        parsed.append((object_id, payload, refs, valid_from, str(pack), version, altitude, predicate, value, kinds,
                       raws, ids))
    names = resolve_people(conn, all_raws)
    cache: Dict[str, Optional[str]] = {}
    limit = int(request.get("limit") or DEFAULT_LIMIT)
    for object_id, payload, refs, valid_from, pack, version, altitude, predicate, value, kinds, raws, ids in parsed:
        shown, people = _shown_value(value, kinds, names)
        resolved = [names[r] for r in raws if names.get(r)]
        authored = str(payload.get("actor_role") or "") == "authored"
        sources = _sources(conn, refs, cache)
        if filtering:
            texts = [*_strings(value), str(payload.get("quote") or ""), str(payload.get("object_value") or "")]
            if _withheld(guard, payload, ids, raws, resolved, texts, sources):
                continue
        if len(result["facts"]) >= limit:
            result["truncated"] = True
            break
        quote = " ".join(str(payload.get("quote") or "").split())
        result["facts"].append({
            "id": str(object_id), "pack": pack, "pack_version": version, "predicate": predicate,
            "value": shown, "people": people,
            "quote": _scrub(quote)[:QUOTE_CHARS] if authored else "",
            "authored": authored, "altitude": altitude or payload.get("altitude"),
            "at": valid_from, "day": _day(valid_from),
            "sources": [_source(ref, record, valid_from, request.get("include_excerpts", False))
                        for ref, record in sources[:MAX_SOURCES]],
        })
    return result


def _source(ref: Dict[str, Any], record: Optional[Dict[str, Any]], at: Any, include_excerpt: bool) -> Dict[str, Any]:
    """Where a fact came from: its source and date always; the text only when asked and the owner wrote it."""
    if record is None:
        return {"source": str(ref.get("table")), "date": _day(ref.get("date") or at), "authored": False, "excerpt": ""}
    owner = bool(record.get("owner"))
    text = " ".join(str(record.get("text") or "").split()) if (include_excerpt and owner) else ""
    return {"source": str(record.get("source") or ref.get("table")),
            "date": _day(record.get("at") or ref.get("date") or at), "authored": owner,
            "excerpt": _scrub(text)[:EXCERPT_CHARS]}

"""Which Off-limits entry the share boundary cannot be built over, and what kind of value it is (review R3-M3).

Every share read builds `permissions_v2.entity_boundary.EntityBoundary` first, and a list it cannot read turns every
share on the node off: an unreadable value is never taken for "nobody". The upgrade step that carries the older
per-person excludes (`contact_excludes`) writes entries that reach contacts the boundary never read before, so the
step is the first moment a node can find that one of its contacts holds such a value (a username list that is not a
list of strings, a saved name that is not text). The boundary says only that it refuses. This module says where,
so that the owner can be told which entry to remove and a pre-flight on a copy can say which kind of value it is.

It decides nothing about any share. Nothing is read through the objects it builds, and its answer is checked by the
boundary itself: an entry is named only when the boundary's own construction succeeds over the list without it.

  - `_Over`: the boundary's own construction over some of the entries (one alone, or all but a few);
  - `_values`: each stored value of the kinds the construction reads, given to the construction's own readers;
  - `unreadable`: the two together.
Kinds are fixed words (KINDS), never data.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Set

from ...permissions_v2.canonical import PolicyError
from ...permissions_v2.entity_boundary import (EntityBoundary, _decode, _handle_keys, name_spellings, skeleton)

#: What kind of value could not be read. `other`: the boundary refuses and no value of these kinds explains it.
KINDS = ("entry_name", "entry_names", "entry_identifiers", "entry_entity", "contact_name", "contact_usernames",
         "contact_handle", "entity_names", "entity_identifiers", "other")
#: Above this many entries each is not tried alone (a boundary is built per entry): the values still say which.
MAX_TRIED_ALONE = 200


class _Over(EntityBoundary):
    """The share boundary's own construction over some of the Off-limits entries. For this module only."""

    def __init__(self, conn, *, only: Optional[str] = None, without=()):
        self._only, self._without = only, set(without)
        super().__init__(conn)

    def _table(self, table, required, **kwargs):
        rows = super()._table(table, required, **kwargs)
        if table == "entity_blackholes":
            rows = [row for row in rows if row.get("blackhole_id") not in self._without
                    and (self._only is None or row.get("blackhole_id") == self._only)]
        return rows


def builds(conn: sqlite3.Connection, **over) -> bool:
    """Whether the boundary's construction succeeds over the whole list, over `only` one entry, or `without` some."""
    try:
        _Over(conn, **over)
    except Exception:  # noqa: BLE001 -- every refusal, whatever raised it
        return False
    return True


def _refuses(read, *args) -> bool:
    try:
        return read(*args) is False
    except Exception:  # noqa: BLE001 -- the construction would have refused here
        return True


def _a_list_of_strings(raw, *, handles=False) -> bool:
    """The construction's own rule for a username list and, with `handles`, for an entity's identifier list, each
    of which must also give a key (`EntityBoundary._handle`)."""
    if not raw:
        return True
    values = _decode(raw)
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        return False
    return not handles or all(_handle_keys(value) for value in values)


def _names_read(row) -> bool:
    EntityBoundary._name_values(row)
    return True


def _name_read(value) -> bool:
    for spelling in name_spellings(value or ""):
        skeleton(spelling)
    return True


def _handle_read(value) -> bool:
    # A handle with no letter and no digit is passed over (review R2-H2); any other that gives no key refuses.
    return bool(_handle_keys(value)) or (isinstance(value, str) and not skeleton(value))


def _rows(conn: sqlite3.Connection, table: str, columns: tuple) -> List[Dict[str, Any]]:
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    wanted = [column for column in columns if column in present]
    if not wanted:
        return []
    return [dict(zip(wanted, row)) for row in conn.execute(f"SELECT {', '.join(wanted)} FROM {table}")]


def _values(conn: sqlite3.Connection) -> List[tuple]:
    """(kind, "entry" | "contact" | "entity", key) for each stored value the construction's own readers refuse.
    The key is the entry's id, the contact's id, or for an entity its id and the contact it is linked to."""
    found: List[tuple] = []
    for row in _rows(conn, "entity_blackholes", ("blackhole_id", "entity_id", "normalized_name", "canonical_name",
                                                  "aliases_json", "identifier_aliases_json")):
        entry = row.get("blackhole_id")
        if _refuses(lambda: bool(skeleton(row.get("normalized_name")))):
            found.append(("entry_name", "entry", entry))
        if not isinstance(row.get("entity_id"), str):
            found.append(("entry_entity", "entry", entry))
        if _refuses(lambda: EntityBoundary._identifier_keys(row) is not None):
            found.append(("entry_identifiers", "entry", entry))
        if _refuses(_names_read, row):
            found.append(("entry_names", "entry", entry))
    for row in _rows(conn, "contacts", ("contact_id", "display_name", "known_usernames_json")):
        if _refuses(_name_read, row.get("display_name")):
            found.append(("contact_name", "contact", row.get("contact_id")))
        if _refuses(_a_list_of_strings, row.get("known_usernames_json")):
            found.append(("contact_usernames", "contact", row.get("contact_id")))
    for row in _rows(conn, "contact_identifiers", ("contact_id", "identifier")):
        if _refuses(_handle_read, row.get("identifier")):
            found.append(("contact_handle", "contact", row.get("contact_id")))
    for table, key in (("entities", "entity_id"), ("entity_merge_tombstones", "absorbed_entity_id")):
        for row in _rows(conn, table, (key, "contact_id", "normalized_name", "canonical_name", "aliases_json",
                                       "identifiers_json")):
            where = (row.get(key), row.get("contact_id"))
            if _refuses(_names_read, row):
                found.append(("entity_names", "entity", where))
            if _refuses(lambda: _a_list_of_strings(row.get("identifiers_json"), handles=True)):
                found.append(("entity_identifiers", "entity", where))
    return found


def _entry_of(conn: sqlite3.Connection, entries: List[Dict[str, Any]], where: str, key: Any) -> Optional[str]:
    """The entry a value belongs to: itself, the one the step made for that contact, or the one linked to that entity."""
    from .contact_excludes import CARRIES_TABLE

    ids = {entry["blackhole_id"] for entry in entries}
    if where == "entry":
        return key if key in ids else None
    if where == "entity":
        entity_id, key = key
        linked = next((entry["blackhole_id"] for entry in entries if entity_id and entry.get("entity_id") == entity_id),
                      None)
        if linked is not None or not key:
            return linked
    try:
        row = conn.execute(f"SELECT blackhole_id FROM {CARRIES_TABLE} WHERE contact_id=?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] in ids else None


def unreadable(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Why the boundary refuses over the Off-limits list as it is, for a caller that has just found that it does.

    `entries`: the entries a refusal is traced to, each `{"blackhole_id", "position", "kinds"}`, in the order the
    list is read (`position` from 1). `of`: how many entries there are. `kinds`: every kind found, traced or not.
    `enough`: whether the boundary's own construction succeeds over the list WITHOUT those entries, which is the
    only ground for telling the owner that removing them brings sharing back. A fault in a value the construction
    reads for every contact or entity (a saved name that is not text, an entity's names) is not undone by removing
    one entry while another is left: `enough` is then False and nothing is promised."""
    from .blackhole import BlackholeStore

    entries = BlackholeStore(conn).list()
    order = [entry["blackhole_id"] for entry in entries]
    kinds: Dict[str, Set[str]] = {}
    every: Set[str] = set()
    try:
        values = _values(conn)
    except Exception:  # noqa: BLE001 -- a table that cannot be read at all: no value is to blame
        values = []
    for kind, where, key in values:
        every.add(kind)
        entry = _entry_of(conn, entries, where, key)
        if entry is not None:
            kinds.setdefault(entry, set()).add(kind)
    if len(order) <= MAX_TRIED_ALONE:
        alone = [entry for entry in order if not builds(conn, only=entry)]
        if len(alone) < len(order) or len(order) == 1:
            for entry in alone:
                kinds.setdefault(entry, set())
    suspects = [entry for entry in order if entry in kinds]
    enough = bool(suspects) and builds(conn, without=suspects)
    return {"entries": [{"blackhole_id": entry, "position": order.index(entry) + 1,
                         "kinds": sorted(kinds[entry]) or ["other"]} for entry in suspects],
            "of": len(order), "kinds": sorted(every) or ["other"], "enough": enough}

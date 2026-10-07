"""The owner's Off-limits list as his app is given it.

One place for both doors (the local route and the relay handler), so they cannot drift: what a row says about an
entry the upgrade carried and the label it is shown under. Nothing here decides a protection: it describes entries.

A row adds to the store's record (third fix round, review R2-H3):

  carried_waiting          carried by the upgrade, the owner has not acted: never shared, nothing else changed
  carried_waiting_aliases  which of its names and identifiers wait (all of them, or on a full entry the added ones)
  display_label            what to show as its name: never a raw contact id
  names, identifiers       its aliases, split; the contact id is in neither
  clean_up                 the clean-up's state in true words, and how many of its terms a clean-up can look for
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional

from .blackhole import ENTRY_ID, BlackholeStore, normalize_entity_name

#: The label of an entry that has nothing an owner would recognise: no saved name, no handle, no linked entity.
NAMELESS = "A contact with no saved name"
#: `rebuild_state` in the words a row is shown with (`clean_up.state`).
CLEAN_UP_STATES = {"pending": "not_started", "running": "running", "complete": "done", "failed": "failed"}
#: A contact id as `normalize_entity_name` leaves it: "<dataset words> contact <digest>".
_CONTACT_ID = re.compile(r"(?:^|\s)contact\s+\S+$")


def _is_contact_id(value: str) -> bool:
    text = str(value or "")
    return ":contact:" in text or bool(_CONTACT_ID.search(text) and text.count(" ") >= 2)


def carried_contacts(conn: sqlite3.Connection, blackhole_id: str) -> List[Dict[str, Any]]:
    """The contacts the upgrade step carried into this entry, each with the name the owner saved it under, its
    handles and whether the step made the entry for it. Empty for an entry the step never touched, and on a
    database the step never ran on."""
    from .contact_excludes import CARRIES_TABLE

    try:
        rows = conn.execute(
            f"SELECT contact_id, outcome FROM {CARRIES_TABLE} WHERE blackhole_id=? ORDER BY contact_id",
            (str(blackhole_id),)).fetchall()
    except sqlite3.OperationalError:
        return []
    found = []
    for contact_id, outcome in rows:
        saved, handles = "", []
        try:
            row = conn.execute("SELECT display_name FROM contacts WHERE contact_id=?", (contact_id,)).fetchone()
            saved = str(row[0] or "").strip() if row else ""
            handles = sorted({str(handle[0]).strip() for handle in conn.execute(
                "SELECT identifier FROM contact_identifiers WHERE contact_id=?", (contact_id,))
                if handle[0] and str(handle[0]).strip()})
        except sqlite3.OperationalError:
            pass
        found.append({"contact_id": str(contact_id), "made_the_entry": outcome == "carried", "saved_name": saved,
                      "handles": handles})
    return found


def display_label(conn: sqlite3.Connection, record: Dict[str, Any]) -> str:
    """What an entry is shown as. For an entry the upgrade step made: the name the owner saved the contact under,
    else the contact's first handle, else the entry's own name (the linked entity's name; or the saved name or
    handle the step named the entry by, when the contact's own row has since gone), else NAMELESS. For any other
    entry its own name, as before. Never a contact id.

    The entry's own name is the last resort for every carried entry since the fourth round (the third round's own
    B10): it used to be read only for an entry with a linked entity, so an entry the step had named by the saved
    name of a contact that was later removed at its source was shown under the fixed words while it held that
    name."""
    own = str(record.get("canonical_name") or record.get("normalized_name") or "")
    made_for = [contact for contact in carried_contacts(conn, str(record.get("blackhole_id") or ""))
                if contact["made_the_entry"]]
    if made_for:
        contact = made_for[0]
        label = contact["saved_name"] or next(iter(contact["handles"]), "") or own
    else:
        label = own
    return NAMELESS if not label or _is_contact_id(label) else label


def written_terms(conn: sqlite3.Connection, record: Dict[str, Any]) -> set:
    """The names, handles and usernames of an entry: what a person could be written as in the owner's summaries.
    The contact id is left out: no text holds one, so "the clean-up looked for it" says nothing."""
    contact_ids = {normalize_entity_name(contact["contact_id"])
                   for contact in carried_contacts(conn, str(record.get("blackhole_id") or ""))}
    terms = {str(record.get("normalized_name") or ""), *(record.get("aliases") or [])} - {""}
    return {term for term in terms if term not in contact_ids and not _is_contact_id(term)}


def describe(conn: sqlite3.Connection, record: Dict[str, Any]) -> Dict[str, Any]:
    """One row of the list: the store's record and the fields the module docstring names."""
    from .blackhole_rebuild import searchable

    identifiers = set(record.get("identifier_aliases") or [])
    written = written_terms(conn, record)
    looks_for, too_short = searchable(written)
    return {
        **record,
        "display_label": display_label(conn, record),
        "names": sorted(term for term in written - identifiers if term != record.get("normalized_name")),
        "identifiers": sorted(written & identifiers),
        "clean_up": {"state": CLEAN_UP_STATES.get(str(record.get("rebuild_state")), "not_started"),
                     "looks_for": looks_for, "too_short": too_short},
    }


def listing(conn: sqlite3.Connection, *, preview: Optional[str] = None) -> Dict[str, Any]:
    """The owner's whole list: every entry described, the open notices, and how many entries are carried and
    waiting. With `preview` (an entry's own id), also what that entry's clean-up would withdraw, counted without
    writing anything; no `preview` key when there is no such entry."""
    from .record_protection import RecordProtectionStore

    store = BlackholeStore(conn)
    rows = [describe(conn, record) for record in store.list()]
    records = RecordProtectionStore(conn)
    out: Dict[str, Any] = {
        "blackholes": rows,
        "notifications": store.notifications(state="open"),
        "carried": {"waiting": sum(1 for row in rows if row["carried_waiting"])},
        "record_protection_supported": True,
        "record_protection_tables": records.supported_tables(),
        "records": records.list(),
    }
    wanted = str(preview or "").strip()
    if wanted and ENTRY_ID.match(wanted) and any(row["blackhole_id"] == wanted for row in rows):
        from .blackhole_rebuild import preview_for_blackhole

        out["preview"] = preview_for_blackhole(conn, wanted)
    return out


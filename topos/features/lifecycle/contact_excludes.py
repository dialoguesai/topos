"""Carry the older sharing model's per-person "exclude" choices into the Off-limits list (N6, decision D24).

In the older model a contact row carries `sharing_policy_json` (`uma_contact_enrichment._parse_sharing_policy`):
`row_visibility` "exclude_from_grants" or "normal", and `name_visibility` "hidden" or "normal". The owner set it per
contact in the app ("Include" / "Exclude", "Yes" / "Hidden"); a grant that inherited contact defaults then dropped every
message whose thread had an excluded participant. The new model has no per-person setting: Off-limits (the
`entity_blackholes` list, read by `permissions_v2.entity_boundary`) is the node's one never-share list. So before the
older lane is removed, every EXPLICIT exclude becomes an Off-limits entry.

What counts, and why:
  - an explicit exclude is a stored policy that parses to an object whose `row_visibility` is "exclude_from_grants".
    A contact with no stored policy is excluded only by the older model's default; the new model includes by default
    (design principle 1), so a default is not carried. A stored object without `row_visibility`, or one that does not
    parse, is a default too, counted apart;
  - "Hidden" (name_visibility) with rows included is not carried: the older lane released those rows with the name
    blanked; the new lane releases only the owner's own text and never labels a sender, and an Off-limits entry would
    withhold every row naming the person, which the owner did not ask for. It is counted;
  - the owner's own contact card is never carried (review R1 node, R-M6). The older app stored both fields whenever
    either toggle was touched, so hiding one's own name stored an exclude; an Off-limits entry for the owner would
    withhold every message the owner wrote. It is counted.

Each carried contact becomes one entry, named by what the node knows of it: the entity linked to it (the most
mentioned; the entry keeps that entity's id), else its display name, else a handle, else its id, taking the first of
those that is a usable name (R-H2: a contact saved under an emoji is named by a handle or by its id). The entry's
further NAMES are the display name and the linked entities' names. Its handles (phone, email), usernames and the
contact id are written as IDENTIFIERS (`blackhole.IDENTIFIERS_COLUMN`): the boundary reads them where it reads a
contact's handles, whole, so its closure reaches the contact itself (an Off-limits term equal to a contact's handle
or id protects that contact, boundary v8) and with it every conversation it takes part in, as the older exclude did;
they are never read as names, so the words of an id or of an address withhold nothing (R-M5). The entry is written
through `BlackholeStore`, as the owner's own flag is: it raises the owner's notice and purges the search indexes.

Protect without destroying (R-B1; the owner's decision of 7 Oct): this step runs unasked at the first start on
1.5.0, so it never runs the clean-up of derived text (`blackhole_rebuild`). That job deletes index rows, blanks
briefs and overwrites the owner's own home-chat turns, and nothing but a whole-database backup brings them back.
Each entry is left `pending`: the share boundary withholds the person from that moment, summaries, briefs and
digests are withheld from everyone but the owner while any entry waits (`BlackholeGuard.withhold_pending_rebuild`),
and the owner starts the clean-up per person. An entry the owner had already made gains the contact's names and
identifiers and nothing else (its tier and note stay); if its clean-up had completed it waits again, with a notice
(R-L4).

One contact that cannot be carried does not stop the others (R-H2): it is counted, the rest are carried, and the
runner's entry (`dispatch`) then fails the step, so the ledger says `failed` with the counts and the next start tries
again. Trying again never undoes the owner (R-L5): every contact the step has dealt with is remembered in
`CARRIES_TABLE`, by id, and is skipped from then on, so an entry the owner removed is not put back. The stored older
choice is never altered. Read-only with `dry_run`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger("topos.features.lifecycle.contact_excludes")

STEP_ID = "carry-contact-excludes-to-off-limits"
ENDPOINT = "/v1/privacy/off-limits/carry-contact-excludes"
EXCLUDE = "exclude_from_grants"
NOTE = ("Carried over from your earlier sharing setting that excluded this contact from what you share "
        "(Topos 1.5.0). Off-limits keeps them out of everything you share; remove them here to change that.")
#: What the owner is shown for each carried contact, in place of the store's own words for a new entry (which say a
#: rebuild is needed before the name disappears; here none runs until the owner starts it). `{name}` is the name the
#: owner saved the contact under.
NOTICE = ("'{name}' is now Off-limits. You excluded them from sharing in an earlier version of Topos, and Topos 1.5.0 "
          "carried that over: nothing that names them, and nothing from a conversation with them, is shared. "
          "Summaries, briefs and digests written before may still name them; those are withheld from everyone but "
          "you until you start the clean-up for them in Off-limits settings. To share them again, remove them from "
          "Off-limits there.")
#: The same, for an entry the owner had already made and whose clean-up had finished.
NOTICE_ADDED = ("'{name}' was already Off-limits. You had also excluded them from sharing in an earlier version of "
                "Topos, and Topos 1.5.0 added that contact's names and addresses to the entry. Summaries, briefs and "
                "digests written before may still name them that way; those are withheld from everyone but you "
                "until you start the clean-up for them in Off-limits settings.")
NAMELESS = "A contact with no saved name"

#: Which contacts this step has dealt with: ids and an outcome, never a name. A row outlives the entry, so that a
#: later run does not put back an entry the owner removed. Created by the step's first real run, with no migration
#: number; nothing else reads it and it is no input to any protection decision.
CARRIES_TABLE = "off_limits_contact_carries"
_CARRIES_SQL = (f"CREATE TABLE IF NOT EXISTS {CARRIES_TABLE} (contact_id TEXT NOT NULL PRIMARY KEY, "
                "blackhole_id TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL, "
                "carried_at TEXT NOT NULL DEFAULT (datetime('now')))")


class CarryIncomplete(RuntimeError):
    """Some explicit excludes could not be carried. The rest were; the runner ledgers the step `failed` and the next
    start tries the remaining ones again. The message holds counts only."""


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _policy(raw: Any) -> tuple:
    """(kind, policy): "none" for no stored policy, "unreadable" for one that is not a JSON object, else "stored"."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "none", None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return "unreadable", None
    if not isinstance(value, dict):
        return "unreadable", None
    return "stored", value


def _identity(conn: sqlite3.Connection, contact: Dict[str, Any]) -> Dict[str, Any]:
    """What the node knows of one contact: display name, usernames, handles and linked entities."""
    contact_id = contact["contact_id"]
    display = str(contact.get("display_name") or "").strip()
    usernames: List[str] = []
    raw = contact.get("known_usernames_json")
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = []
        if isinstance(parsed, list):
            usernames = [str(name).strip() for name in parsed if isinstance(name, str) and name.strip()]
    handles: List[str] = []
    if _columns(conn, "contact_identifiers") >= {"contact_id", "identifier"}:
        where, args = "WHERE contact_id=?", [contact_id]
        if contact.get("dataset_id") is not None and "dataset_id" in _columns(conn, "contact_identifiers"):
            where += " AND dataset_id=?"
            args.append(contact["dataset_id"])
        handles = sorted({str(row[0]).strip() for row in conn.execute(
            f"SELECT identifier FROM contact_identifiers {where}", args) if row[0] and str(row[0]).strip()})
    entities: List[Dict[str, Any]] = []
    entity_columns = _columns(conn, "entities")
    if {"entity_id", "contact_id", "canonical_name"} <= entity_columns:
        order = "COALESCE(mention_count, 0) DESC, entity_id" if "mention_count" in entity_columns else "entity_id"
        aliases = "aliases_json" if "aliases_json" in entity_columns else "NULL"
        for entity_id, canonical, aliases_json in conn.execute(
                f"SELECT entity_id, canonical_name, {aliases} FROM entities WHERE contact_id=? ORDER BY {order}",
                (contact_id,)):
            names = [str(canonical).strip()] if canonical and str(canonical).strip() else []
            try:
                more = json.loads(aliases_json) if aliases_json else []
            except (TypeError, ValueError):
                more = []
            if isinstance(more, list):
                names += [str(alias).strip() for alias in more if isinstance(alias, str) and alias.strip()]
            entities.append({"entity_id": str(entity_id), "names": names})
    return {"contact_id": contact_id, "display": display, "usernames": usernames, "handles": handles,
            "entities": entities}


def _usable(value: str) -> bool:
    """Whether the Off-limits store and the share boundary can both name an entry by this: it normalises to
    something with a letter or a digit. A name of symbols alone (an emoji, a dash) is refused by the store or, worse,
    accepted by it and unreadable to the boundary, which then withholds every share on the node."""
    from ...permissions_v2.entity_boundary import skeleton
    from .blackhole import normalize_entity_name

    return bool(skeleton(normalize_entity_name(value)))


def _entry(identity: Dict[str, Any]) -> Dict[str, Any]:
    """The Off-limits entry for one contact: what it is named by, its further names and its identifiers.

    Named by the first of these that is usable (`_usable`): the most-mentioned linked entity (the store then names
    the entry by that entity's canonical name and keeps its id), the display name, a handle, the contact id."""
    linked = identity["entities"]
    contact_id = str(identity["contact_id"])
    names = [identity["display"], *(name for entity in linked for name in entity["names"])]
    identifiers = [*identity["usernames"], *identity["handles"], contact_id]
    if linked and linked[0]["names"] and _usable(linked[0]["names"][0]):
        named_by, entity_ref, name = "linked_entity", linked[0]["entity_id"], linked[0]["names"][0]
    elif _usable(identity["display"]):
        named_by, entity_ref, name = "name", identity["display"], identity["display"]
    else:
        handle = next((value for value in identity["handles"] if _usable(value)), None)
        if handle is not None:
            named_by, entity_ref, name = "handle", handle, handle
        else:
            named_by, entity_ref, name = "contact_id_only", contact_id, contact_id
    saved = identity["display"] or next(iter(identity["handles"]), "") or NAMELESS
    return {"entity_ref": entity_ref, "name": name, "named_by": named_by, "saved_name": saved,
            "names": sorted({value for value in names if value}),
            "identifiers": sorted({value for value in identifiers if value and _usable(value)})}


def explicit_choices(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Read only: every contact's stored choice, as counts, and the explicit excludes to carry (each with `is_self`)."""
    counts: Dict[str, int] = {"contacts": 0, "no_stored_choice": 0, "unreadable": 0, "stored_without_row_choice": 0,
                              "explicit_excludes": 0, "explicit_includes": 0, "hidden_names": 0}
    excludes: List[Dict[str, Any]] = []
    present = _columns(conn, "contacts")
    if "sharing_policy_json" not in present:
        return {"counts": counts, "excludes": excludes}
    fields = [column for column in ("contact_id", "dataset_id", "display_name", "known_usernames_json", "is_self")
              if column in present]
    cursor = conn.execute(f"SELECT {', '.join(fields)}, sharing_policy_json FROM contacts ORDER BY contact_id")
    for row in cursor.fetchall():
        contact = dict(zip(fields, row[:-1]))
        counts["contacts"] += 1
        kind, policy = _policy(row[-1])
        if kind == "none":
            counts["no_stored_choice"] += 1
            continue
        if kind == "unreadable":
            counts["unreadable"] += 1
            continue
        if policy.get("name_visibility") == "hidden":
            counts["hidden_names"] += 1
        if "row_visibility" not in policy:
            counts["stored_without_row_choice"] += 1
        elif policy.get("row_visibility") == EXCLUDE:
            counts["explicit_excludes"] += 1
            excludes.append(contact)
        else:
            counts["explicit_includes"] += 1
    return {"counts": counts, "excludes": excludes}


def _remembered(conn: sqlite3.Connection) -> set:
    """The contacts an earlier run dealt with (CARRIES_TABLE); empty before the first real run."""
    try:
        return {str(row[0]) for row in conn.execute(f"SELECT contact_id FROM {CARRIES_TABLE}")}
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return set()
        raise


def _carry_one(conn: sqlite3.Connection, store: Any, contact_id: str, entry: Dict[str, Any]) -> str:
    """Write one contact's entry and remember the contact, in one transaction. Returns the outcome."""
    from ...storage.db.write_gate import batched_writes

    with batched_writes(conn):
        # An entry is the same one whether it is found by the linked entity's id or by the name it would be given
        # (the owner may have protected the name before any entity existed for it).
        found = next((ref for ref in (entry["entity_ref"], entry["name"]) if store.get(ref) is not None), None)
        if found is None:
            result = store.blackhole_entity(entity_ref=entry["entity_ref"], note=NOTE, aliases=entry["names"],
                                            identifiers=entry["identifiers"],
                                            notice=NOTICE.format(name=entry["saved_name"]))
            outcome = "carried"
        else:
            # Names and identifiers only: the owner's own tier and note stay (`blackhole_entity` would reset them).
            result = store.add_aliases(entity_ref=found, aliases=entry["names"], identifiers=entry["identifiers"],
                                       notice=NOTICE_ADDED.format(name=entry["saved_name"]))
            outcome = ("added_and_waiting" if result["requeued"] else "added_to_existing" if result["grew"]
                       else "already_off_limits")
            if entry["named_by"] == "linked_entity" and not result.get("entity_id"):
                store.bind_entity_id(normalized_name=result["normalized_name"], entity_id=entry["entity_ref"])
        conn.execute(f"INSERT OR REPLACE INTO {CARRIES_TABLE} (contact_id, blackhole_id, outcome) VALUES (?,?,?)",
                     (contact_id, str(result.get("blackhole_id") or ""), outcome))
    return outcome


def carry_contact_excludes(conn: sqlite3.Connection, *, dry_run: bool = False) -> Dict[str, Any]:
    """The upgrade step (STEP_ID): every explicit exclude becomes an Off-limits entry. Returns counts only.

    `carried`: new entries. `already_off_limits`: excludes whose entry was there already, of which
    `added_to_existing` gained names or identifiers. `carried_before`: contacts an earlier run dealt with, skipped.
    `own_card_skipped`: the owner's own card. `failed`: contacts that could not be carried this time.
    `clean_ups_waiting`: entries this run left waiting for the owner to start their clean-up."""
    empty = {"step": STEP_ID, "dry_run": dry_run, "counts": {"contacts": 0}, "carried": 0, "already_off_limits": 0,
             "added_to_existing": 0, "carried_before": 0, "own_card_skipped": 0, "failed": 0, "named_by": {},
             "clean_ups_waiting": 0}
    try:
        found = explicit_choices(conn)
    except sqlite3.OperationalError as exc:
        if "no such table: contacts" in str(exc).lower():
            return empty
        raise
    out = {**empty, "counts": found["counts"]}
    remembered = _remembered(conn)
    store = None
    if not dry_run and found["excludes"]:
        from ...storage.db.write_gate import commit_connection, with_db_write
        from .blackhole import BlackholeStore

        store = BlackholeStore(conn)
        with with_db_write():
            conn.execute(_CARRIES_SQL)
            commit_connection(conn)
    named_by: Dict[str, int] = {}
    for contact in found["excludes"]:
        contact_id = str(contact["contact_id"])
        if contact.get("is_self"):
            out["own_card_skipped"] += 1
            continue
        if contact_id in remembered:
            out["carried_before"] += 1
            continue
        outcome = None
        try:
            entry = _entry(_identity(conn, contact))
            if not dry_run:
                outcome = _carry_one(conn, store, contact_id, entry)
        except Exception as exc:  # noqa: BLE001 -- one contact must not stop the others; counted, tried again next start
            out["failed"] += 1
            logger.warning("carry contact excludes: one contact could not be carried (%s)", type(exc).__name__)
            continue
        named_by[entry["named_by"]] = named_by.get(entry["named_by"], 0) + 1
        if outcome is None:
            continue
        if outcome == "carried":
            out["carried"] += 1
            out["clean_ups_waiting"] += 1
            continue
        out["already_off_limits"] += 1
        if outcome != "already_off_limits":
            out["added_to_existing"] += 1
        if outcome == "added_and_waiting":
            out["clean_ups_waiting"] += 1
    out["named_by"] = named_by
    return out


def dispatch(conn: sqlite3.Connection, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The upgrade runner's entry (`_exec_engine_endpoint`, ENDPOINT).

    A run in which some contact could not be carried raises, after every other contact was carried: the runner then
    ledgers the step `failed` with this message (counts, never a name) and runs it again at the next start, when the
    contacts already dealt with are skipped. A run with no failure returns its counts, which the ledger row keeps."""
    out = carry_contact_excludes(conn, dry_run=bool((params or {}).get("dry_run", False)))
    if out["failed"]:
        raise CarryIncomplete(
            f"{out['failed']} of {out['counts'].get('explicit_excludes', 0)} explicit excludes could not be carried "
            f"(carried {out['carried']}, already off-limits {out['already_off_limits']}, carried before "
            f"{out['carried_before']}, own card {out['own_card_skipped']}); the step runs again at the next start")
    return out

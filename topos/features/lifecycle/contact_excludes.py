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
    withhold every row naming the person, which the owner did not ask for. It is counted.

Each carried contact becomes one entry, named by what the node knows of it: the entity linked to it (the most
mentioned; the entry keeps that entity's id), else its display name, else its first handle, else its id. The entry's aliases are every other name,
handle (phone, email), username and linked entity name, and the contact id, so the boundary's closure reaches the
contact itself (an Off-limits name equal to a contact's handle or id protects that contact, boundary v8) and with it
every conversation it takes part in, as the older exclude did. The entry is written through `BlackholeStore`, as the
owner's own flag is: it raises the owner's notification and purges the search indexes.

Protect without destroying (review R1 node, R-B1; the owner's decision of 7 Oct): this step runs unasked at the first
start on 1.5.0, so it never runs the clean-up of derived text (`blackhole_rebuild`). That job deletes index rows,
blanks briefs and overwrites the owner's own home-chat turns, and nothing but a whole-database backup brings them
back. Each entry is left `pending`: the share boundary withholds the person from that moment, summaries, briefs and
digests are withheld from everyone but the owner while any entry waits (`BlackholeGuard.withhold_pending_rebuild`),
and the owner starts the clean-up per person from the entry's notice.

Idempotent and resumable: an entry that exists is merged (aliases only grow), never duplicated, and a second run
writes nothing new. Read-only with `dry_run`.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

STEP_ID = "carry-contact-excludes-to-off-limits"
ENDPOINT = "/v1/privacy/off-limits/carry-contact-excludes"
EXCLUDE = "exclude_from_grants"
NOTE = ("Carried over from your earlier sharing setting that excluded this contact from what you share "
        "(Topos 1.5.0). Off-limits keeps them out of everything you share; remove them here to change that.")


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


def _entry(identity: Dict[str, Any]) -> Dict[str, Any]:
    """The Off-limits entry for one contact: entity_ref (an entity id, or the name), aliases, and how it is named."""
    linked = identity["entities"]
    names = [identity["display"], *identity["usernames"], *identity["handles"],
             *(name for entity in linked for name in entity["names"]), str(identity["contact_id"])]
    if linked and linked[0]["names"]:          # the store names an entity's entry by the entity's canonical name
        named_by, name = "linked_entity", linked[0]["names"][0]
    elif identity["display"]:
        named_by, name = "name", identity["display"]
    elif identity["handles"]:
        named_by, name = "handle", identity["handles"][0]
    else:
        named_by, name = "contact_id_only", str(identity["contact_id"])
    entity_ref = linked[0]["entity_id"] if linked else name
    # Every name, the entry's own included: with a linked entity the store names the entry by the entity's canonical
    # name, and the contact's display name must not be lost beside it.
    aliases = sorted({value for value in names if value})
    return {"entity_ref": entity_ref, "name": name, "aliases": aliases, "named_by": named_by}


def explicit_choices(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Read only: every contact's stored choice, as counts, and the explicit excludes to carry."""
    counts: Dict[str, int] = {"contacts": 0, "no_stored_choice": 0, "unreadable": 0, "stored_without_row_choice": 0,
                              "explicit_excludes": 0, "explicit_includes": 0, "hidden_names": 0}
    excludes: List[Dict[str, Any]] = []
    if "sharing_policy_json" not in _columns(conn, "contacts"):
        return {"counts": counts, "excludes": excludes}
    fields = [column for column in ("contact_id", "dataset_id", "display_name", "known_usernames_json")
              if column in _columns(conn, "contacts")]
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


def carry_contact_excludes(conn: sqlite3.Connection, *, dry_run: bool = False) -> Dict[str, Any]:
    """The upgrade step (STEP_ID): every explicit exclude becomes an Off-limits entry. Returns counts only."""
    try:
        found = explicit_choices(conn)
    except sqlite3.OperationalError as exc:
        if "no such table: contacts" in str(exc).lower():
            return {"step": STEP_ID, "dry_run": dry_run, "counts": {"contacts": 0}, "carried": 0,
                    "already_off_limits": 0, "named_by": {}, "clean_ups_waiting": 0}
        raise
    named_by: Dict[str, int] = {}
    carried = already = 0
    store = None
    if not dry_run:
        from .blackhole import BlackholeStore, normalize_entity_name
        store = BlackholeStore(conn)
    for contact in found["excludes"]:
        entry = _entry(_identity(conn, contact))
        named_by[entry["named_by"]] = named_by.get(entry["named_by"], 0) + 1
        if dry_run:
            continue
        existing = store.get(entry["entity_ref"])
        if existing is not None and {normalize_entity_name(alias) for alias in entry["aliases"]} - {""} <= set(
                existing["aliases"]):
            already += 1                           # carried before (or the owner's own): nothing to write
            continue
        result = store.blackhole_entity(entity_ref=entry["entity_ref"], note=NOTE, aliases=entry["aliases"])
        if result.get("already_blackholed"):
            already += 1
            continue
        # No clean-up here (module docstring): the new entry stays `pending`, which withholds, and waits for the owner.
        carried += 1
    return {"step": STEP_ID, "dry_run": dry_run, "counts": found["counts"], "carried": carried,
            "already_off_limits": already, "named_by": named_by, "clean_ups_waiting": carried}


def dispatch(conn: sqlite3.Connection, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The upgrade runner's entry (`_exec_engine_endpoint`, ENDPOINT)."""
    return carry_contact_excludes(conn, dry_run=bool((params or {}).get("dry_run", False)))

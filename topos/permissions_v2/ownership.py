"""``permissions_v2_ownership`` (A2A-3 §7.4; decision D4; A2A-5 §5.4; N4): what counts as the owner's, over the relay.

A row of AI chat, journal or browsing is shared only when the node can prove it is the owner's own. Two existing
modules hold that proof, and until now only the owner's socket could reach them (E1 §5):

- ``ai_chat_capture`` (OD-39): the owner's capture app's AI-chat prompts. Rows stamped by an app on the node's capture
  list (``TOPOS_OWNER_CAPTURE_APP_IDS``, mirroring the control plane's) count for the OD-39 source; any other app's,
  and rows written before the node recorded writers, count once the owner attests them (a receipt);
- ``capture_receipts`` (OD-50, OD-52): the same for journal entries, browser visits and an AI-chat export import.

This module puts both behind one relayed owner message, in the screens' terms:

- ``list``: ``apps``, one entry per (app, kind) for every app whose stamped writes landed in one of the three tables
  and every app a live receipt names; ``yours`` when the node accepts that app's rows now (first-party, D4: on the
  capture list for its source, or vouched for by a live receipt), ``not_yours`` when the owner's latest word on it is
  "not mine", ``needs_ok`` otherwise. ``older``: one group per (source, kind) of the rows written before the node
  recorded writers that a receipt could still cover, with ``before``, the day the node began recording writers on
  that table. Each with the module's fixed statement and its preview digest over exactly the rows a "mine" would
  cover now (``null`` for a first-party app, and for an entry nothing can cover now).
- ``confirm`` ``mine``: the existing preview-digest-then-attest flow, for exactly the previewed rows.
  ``not_mine``: recorded in ``share_ownership_decisions`` (NEW) so the item stops asking; it proves nothing, and it
  withdraws the entry's live receipts, so the owner's latest word is the one that holds.
- ``withdraw``: the existing revoke, with the other live receipts the same entry holds (its app on its table, or
  the same older group), so an entry is withdrawn whole.

An app's receipt over an entry spanning several sources of one kind is one receipt per source; the reply names the
first. Pre-stamp rows carry no app: a receipt over them names the family's import door (``owner_import``, never a
client a relay stamp can name, so it vouches for no app's later writes), or the OD-39 capture app for that source.

Codes: ``preview_stale`` (409), ``item_unknown`` (400), ``receipt_unknown`` (400), ``receipt_revoked`` (409). Counts,
ids and the modules' fixed sentences only: never a row, a title, a name or a URL. The modules' own checks run
unchanged; nothing here re-labels a row.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Optional

from . import ai_chat_capture, capture_receipts
from .canonical import PolicyError, digest

#: The tables whose rows a receipt can prove, and their kinds (A2A-5 §4.2's words).
TABLE_KINDS = {"ai_chat_messages": "ai_chats", "journal_entries": "journal_entries", "activity_events": "interests"}
KIND_ORDER = ("ai_chats", "journal_entries", "interests")
IMPORT_APP = "owner_import"
DECISIONS = "share_ownership_decisions"
#: A module's refusal -> the closed code of the relayed message.
CODES = {"capture_attestation_preview_stale": "preview_stale", "capture_receipt_unknown": "receipt_unknown",
         "capture_receipt_revoked": "receipt_revoked", "capture_attestation_invalid": "item_unknown",
         "capture_attestation_unconfirmed": "item_unknown", "capture_attestation_dataset_unknown": "item_unknown",
         "capture_attestation_dataset_not_this_node": "item_unknown",
         "capture_attestation_dataset_posture_unknown": "item_unknown"}


@dataclass(frozen=True)
class Unit:
    """What one receipt covers: a table, a source, and the app it vouches for."""
    table: str
    source_id: str
    app_id: str

    @property
    def capture(self) -> bool:
        """OD-39's module (an AI-chat capture source), else the generalised receipts."""
        return self.table == "ai_chat_messages" and not capture_receipts.ai_chat_export_source(self.source_id)


@dataclass
class Entry:
    item_type: str                  # "app" | "older"
    kind: str
    units: list
    items: int
    statement: str
    preview_digest: Optional[str]
    state: str = "needs_ok"
    first_party: bool = False
    group_id: Optional[str] = None
    before: Optional[str] = None

    @property
    def app_id(self) -> str:
        return self.units[0].app_id

    @property
    def source_id(self) -> str:
        return self.units[0].source_id


class Refused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- reading -----------------------------------------------------------------------------------------------------

def _exists(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _columns(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _writer_tables(conn) -> list:
    return [table for table in TABLE_KINDS if _exists(conn, table)
            and {"source_id", "writer_class", "writer_app_id"} <= _columns(conn, table)]


def _stamped(conn) -> dict:
    """(table, source, app) -> rows an app's door wrote as the owner's app (stamped ``owner_app``)."""
    found = {}
    for table in _writer_tables(conn):
        for source_id, app_id, rows in conn.execute(
                f"SELECT source_id, writer_app_id, COUNT(*) FROM {table} WHERE writer_class='owner_app' "
                "AND writer_app_id IS NOT NULL GROUP BY source_id, writer_app_id"):
            if isinstance(source_id, str) and source_id and isinstance(app_id, str) and app_id:
                found[(table, source_id, app_id)] = int(rows)
    return found


def _receipts(conn, owner_id: str) -> list:
    """Every receipt of this owner, both modules: (receipt id, table, source, app, attested_at, revoked_at, rows)."""
    found = []
    if ai_chat_capture.installed(conn):
        for row in conn.execute(f"SELECT t.receipt_id, t.source_id, t.app_id, t.attested_at, t.revoked_at, "
                                f"(SELECT COUNT(*) FROM {ai_chat_capture.RECEIPT_ROWS} r WHERE r.receipt_id=t.receipt_id) "
                                f"FROM {ai_chat_capture.RECEIPTS} t WHERE t.owner_id=?", (owner_id,)):
            found.append((row[0], "ai_chat_messages", row[1], row[2], row[3], row[4], int(row[5])))
    if capture_receipts.installed(conn):
        for row in conn.execute(f"SELECT t.receipt_id, t.canonical_table, t.source_id, t.app_id, t.attested_at, "
                                f"t.revoked_at, (SELECT COUNT(*) FROM {capture_receipts.RECEIPT_ROWS} r "
                                f"WHERE r.receipt_id=t.receipt_id) FROM {capture_receipts.RECEIPTS} t WHERE t.owner_id=?",
                                (owner_id,)):
            if row[1] in TABLE_KINDS:
                found.append((row[0], row[1], row[2], row[3], row[4], row[5], int(row[6])))
    return found


def _decisions(conn, owner_id: str) -> list:
    if not _exists(conn, DECISIONS):
        return []
    return conn.execute(f"SELECT item_type, canonical_table, source_id, app_id, decided_at FROM {DECISIONS} "
                        "WHERE owner_id=? AND decision='not_mine'", (owner_id,)).fetchall()


def _first_party(unit: Unit) -> bool:
    """D4: on the node's capture list for its source. The node honours that list for OD-39's source only."""
    return (unit.table == "ai_chat_messages" and unit.source_id == ai_chat_capture.OD39_SOURCE_ID
            and unit.app_id in ai_chat_capture._od39_app_ids())


def _accepted(conn, owner_id: str, unit: Unit) -> bool:
    """Whether the node counts this app's stamped rows of this source as the owner's now (the modules' own rule)."""
    if unit.capture:
        return unit.app_id in ai_chat_capture.capture_sources(conn, owner_id).get(unit.source_id, frozenset())
    return unit.app_id in capture_receipts.capture_apps(conn, owner_id=owner_id, table=unit.table,
                                                         source_id=unit.source_id)


def _preview(conn, owner_id: str, unit: Unit, resource_id) -> Optional[dict]:
    """The module's preview, or None when no receipt could cover this unit now."""
    try:
        if unit.capture:
            return ai_chat_capture.preview(conn, owner_id=owner_id, source_id=unit.source_id, app_id=unit.app_id)
        found = capture_receipts.preview(conn, owner_id=owner_id, table=unit.table, source_id=unit.source_id,
                                         app_id=unit.app_id, resource_id=resource_id)
    except PolicyError:
        return None
    # The generalised receipts bind rows to the one install of their source: with none, an attestation is refused.
    return found if found.get("dataset_certified") else None


def _combined(digests: list) -> Optional[str]:
    if not digests or any(value is None for value in digests):
        return None
    return digests[0] if len(digests) == 1 else digest({"units": digests})


def _day(value, *, after: bool = False) -> Optional[str]:
    """The UTC day (YYYY-MM-DD) of a stored time, or of the day after it."""
    from .fact_eligibility import canonical_utc_microseconds
    stamp = canonical_utc_microseconds(value) if value is not None else None
    if stamp is None:
        return None
    return time.strftime("%Y-%m-%d", time.gmtime(stamp // 1_000_000 + (86_400 if after else 0)))


def _before(conn, table: str) -> Optional[str]:
    """The UTC day the node began recording writers on ``table``: its first stamped row's ingest day; with no
    stamped row yet, the day after its last unstamped one."""
    if "ingested_at" not in _columns(conn, table):
        return None
    first = conn.execute(f"SELECT MIN(ingested_at) FROM {table} WHERE writer_class IS NOT NULL").fetchone()[0]
    if first is not None:
        return _day(first)
    return _day(conn.execute(f"SELECT MAX(ingested_at) FROM {table} WHERE writer_class IS NULL").fetchone()[0],
                after=True)


def _older_app(conn, owner_id: str, table: str, source_id: str, stamped: dict, receipts: list) -> Optional[str]:
    """The app a receipt over this source's pre-stamp rows names (module docstring), or None when none can."""
    if table != "ai_chat_messages" or capture_receipts.ai_chat_export_source(source_id):
        return IMPORT_APP
    if source_id == ai_chat_capture.OD39_SOURCE_ID:
        apps = sorted(ai_chat_capture._od39_app_ids())
        if apps:
            return apps[0]
    known = {app for (t, s, app) in stamped if t == table and s == source_id}
    known |= {app for (_r, t, s, app, _a, revoked, _n) in receipts if t == table and s == source_id and revoked is None}
    known.discard(IMPORT_APP)
    return next(iter(known)) if len(known) == 1 else None


def _group_id(table: str, source_id: str) -> str:
    return "older-" + digest({"table": table, "source_id": source_id})[:24]


def entries(conn, *, owner_id: str, resource_id) -> list:
    """Every app entry and older group, with its state, in the screens' order."""
    stamped, receipts, decisions = _stamped(conn), _receipts(conn, owner_id), _decisions(conn, owner_id)
    # apps: (app, kind) -> units
    units = {}
    for (table, source_id, app_id) in stamped:
        units.setdefault((app_id, TABLE_KINDS[table]), set()).add(Unit(table, source_id, app_id))
    for (_receipt, table, source_id, app_id, _at, revoked, _rows) in receipts:
        if revoked is None and app_id != IMPORT_APP:
            units.setdefault((app_id, TABLE_KINDS[table]), set()).add(Unit(table, source_id, app_id))
    found = []
    for (app_id, kind), members in units.items():
        members = sorted(members, key=lambda unit: unit.source_id)
        listed = sum(rows for (_r, table, source_id, app, _a, revoked, rows) in receipts
                     if revoked is None and app == app_id and Unit(table, source_id, app) in members)
        items = sum(stamped.get((unit.table, unit.source_id, app_id), 0) for unit in members) + listed
        first_party = all(_first_party(unit) for unit in members)
        previews = [None if first_party else _preview(conn, owner_id, unit, resource_id) for unit in members]
        statement = (ai_chat_capture.STATEMENT if members[0].capture
                     else capture_receipts.family_of(members[0].table).statement)
        entry = Entry(item_type="app", kind=kind, units=members, items=items, statement=statement,
                      preview_digest=None if first_party else _combined(
                          [preview["preview_digest"] if preview else None for preview in previews]),
                      first_party=first_party)
        if first_party or all(_accepted(conn, owner_id, unit) for unit in members):
            entry.state = "yours"
        else:
            latest_receipt = max((at for (_r, table, source_id, app, at, _rev, _n) in receipts
                                  if app == app_id and TABLE_KINDS.get(table) == kind), default=None)
            said = [at for (item_type, table, _s, app, at) in decisions
                    if item_type == "app" and app == app_id and TABLE_KINDS.get(table) == kind]
            if said and (latest_receipt is None or max(said) >= latest_receipt):
                entry.state = "not_yours"
        found.append(entry)
    # older: pre-stamp rows a receipt could still cover, per (source, kind)
    for table in _writer_tables(conn):
        for (source_id,) in conn.execute(f"SELECT DISTINCT source_id FROM {table} WHERE writer_class IS NULL "
                                         "AND source_id IS NOT NULL ORDER BY source_id"):
            app_id = _older_app(conn, owner_id, table, source_id, stamped, receipts)
            if app_id is None:
                continue
            unit = Unit(table, source_id, app_id)
            preview = _preview(conn, owner_id, unit, resource_id)
            if preview is None or not preview.get("row_count"):
                continue
            declined = [at for (item_type, t, s, _app, at) in decisions
                        if item_type == "older" and t == table and s == source_id]
            if declined:
                continue                      # the owner said "not mine": the group stops asking
            found.append(Entry(item_type="older", kind=TABLE_KINDS[table], units=[unit],
                               items=int(preview["row_count"]), statement=preview["statement"],
                               preview_digest=preview["preview_digest"], group_id=_group_id(table, source_id),
                               before=_before(conn, table)))
    found.sort(key=lambda entry: (entry.item_type != "app", KIND_ORDER.index(entry.kind),
                                  entry.app_id if entry.item_type == "app" else entry.source_id))
    return found


def listing(conn, *, owner_id: str, resource_id) -> dict:
    apps, older = [], []
    for entry in entries(conn, owner_id=owner_id, resource_id=resource_id):
        if entry.item_type == "app":
            apps.append({"app_id": entry.app_id, "kind": entry.kind, "items": entry.items, "state": entry.state,
                         "statement": entry.statement, "preview_digest": entry.preview_digest})
        else:
            older.append({"group_id": entry.group_id, "source_id": entry.source_id, "kind": entry.kind,
                          "before": entry.before, "items": entry.items, "statement": entry.statement,
                          "preview_digest": entry.preview_digest})
    return {"apps": apps, "older": older}


# --- the owner's word ----------------------------------------------------------------------------------------------

def _install_decisions(conn) -> None:
    """Create the decision table. Append-only: a later "mine" (a receipt) supersedes a decision; nothing edits one."""
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {DECISIONS} (
        decision_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
        item_type TEXT NOT NULL CHECK (item_type IN ('app', 'older')), canonical_table TEXT NOT NULL,
        source_id TEXT, app_id TEXT, decision TEXT NOT NULL CHECK (decision = 'not_mine'),
        decided_at INTEGER NOT NULL)""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {DECISIONS}_immutable BEFORE UPDATE ON {DECISIONS}
        BEGIN SELECT RAISE(ABORT, 'share_ownership_decision_immutable'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {DECISIONS}_kept BEFORE DELETE ON {DECISIONS}
        BEGIN SELECT RAISE(ABORT, 'share_ownership_decision_immutable'); END""")


def _record_not_mine(conn, owner_id: str, entry: Entry, now: int) -> None:
    _install_decisions(conn)
    unit = entry.units[0]
    conn.execute(f"INSERT INTO {DECISIONS} (decision_id, owner_id, item_type, canonical_table, source_id, app_id, "
                 "decision, decided_at) VALUES (?,?,?,?,?,?,'not_mine',?)",
                 ("own-" + uuid.uuid4().hex, owner_id, entry.item_type, unit.table,
                  unit.source_id if entry.item_type == "older" else None,
                  unit.app_id if entry.item_type == "app" else None, now))


def _revoke(conn, owner_id: str, receipt_id: str, table: str, now: int) -> None:
    module = ai_chat_capture if receipt_id.startswith("aicap-") else capture_receipts
    module.revoke(conn, owner_id=owner_id, receipt_id=receipt_id, now=now)


def _live_receipts_of(conn, owner_id: str, entry: Entry) -> list:
    """The live receipts an entry holds: its app's on its table (an app entry), or its group's (an older one)."""
    unit = entry.units[0]
    out = []
    for (receipt_id, table, source_id, app_id, _at, revoked, _rows) in _receipts(conn, owner_id):
        if revoked is not None or table != unit.table:
            continue
        if entry.item_type == "app" and app_id == unit.app_id:
            out.append((receipt_id, table))
        elif entry.item_type == "older" and source_id == unit.source_id and app_id == unit.app_id:
            out.append((receipt_id, table))
    return out


def _attest(conn, owner_id: str, unit: Unit, preview_digest: str, resource_id, now: int) -> str:
    if unit.capture:
        return ai_chat_capture.attest(conn, owner_id=owner_id, source_id=unit.source_id, app_id=unit.app_id,
                                      preview_digest=preview_digest, confirm=True, now=now)["receipt_id"]
    return capture_receipts.attest(conn, owner_id=owner_id, table=unit.table, source_id=unit.source_id,
                                   app_id=unit.app_id, preview_digest=preview_digest, confirm=True, now=now,
                                   resource_id=resource_id)["receipt_id"]


def confirm(conn, *, owner_id: str, resource_id, item_type: str, item_id: str, decision: str,
            preview_digest, now: Optional[int] = None) -> dict:
    """The owner's "mine" or "not mine" on one entry. The caller holds the write gate and commits."""
    now = int(time.time() if now is None else now)
    current = entries(conn, owner_id=owner_id, resource_id=resource_id)
    if item_type == "app":
        candidates = [entry for entry in current if entry.item_type == "app" and entry.app_id == item_id]
    else:
        candidates = [entry for entry in current if entry.item_type == "older" and entry.group_id == item_id]
    if not candidates:
        raise Refused("item_unknown")
    entry = next((entry for entry in candidates if entry.preview_digest == preview_digest), None)
    if entry is None:
        raise Refused("preview_stale")
    if entry.first_party:
        if decision == "not_mine":
            raise Refused("item_unknown")   # installing a first-party app is the statement (D4)
        return {"state": "yours", "receipt_id": None}
    if decision == "not_mine":
        for receipt_id, table in _live_receipts_of(conn, owner_id, entry):
            _revoke(conn, owner_id, receipt_id, table, now)
        _record_not_mine(conn, owner_id, entry, now)
        return {"state": "not_yours", "receipt_id": None}
    receipt_ids = []
    try:
        for unit in entry.units:
            preview = _preview(conn, owner_id, unit, resource_id)
            if preview is None:
                raise Refused("preview_stale")
            receipt_ids.append(_attest(conn, owner_id, unit, preview["preview_digest"], resource_id, now))
    except PolicyError as exc:
        raise Refused(CODES.get(exc.code, "item_unknown")) from None
    return {"state": "yours", "receipt_id": receipt_ids[0]}


def withdraw(conn, *, owner_id: str, resource_id, receipt_id, now: Optional[int] = None) -> dict:
    """The existing revoke of one receipt, with the other live receipts of the same entry. The caller commits."""
    now = int(time.time() if now is None else now)
    if not isinstance(receipt_id, str) or not receipt_id:
        raise Refused("receipt_unknown")
    found = next((row for row in _receipts(conn, owner_id) if row[0] == receipt_id), None)
    if found is None:
        raise Refused("receipt_unknown")
    if found[5] is not None:
        raise Refused("receipt_revoked")
    _receipt, table, source_id, app_id, _at, _revoked, _rows = found
    # A first-party app's entry never needs a receipt, so one naming it (or the import door) covers older rows.
    if app_id == IMPORT_APP or _first_party(Unit(table, source_id, app_id)):
        entry = Entry(item_type="older", kind=TABLE_KINDS[table], units=[Unit(table, source_id, app_id)], items=0,
                      statement="", preview_digest=None)
    else:
        entry = Entry(item_type="app", kind=TABLE_KINDS[table], units=[Unit(table, source_id, app_id)], items=0,
                      statement="", preview_digest=None)
    try:
        _revoke(conn, owner_id, receipt_id, table, now)
        for sibling, sibling_table in _live_receipts_of(conn, owner_id, entry):
            _revoke(conn, owner_id, sibling, sibling_table, now)
    except PolicyError as exc:
        raise Refused(CODES.get(exc.code, "receipt_unknown")) from None
    if entry.item_type == "older":
        return {"state": "needs_ok"}
    after = next((item for item in entries(conn, owner_id=owner_id, resource_id=resource_id)
                  if item.item_type == "app" and item.app_id == app_id and item.kind == entry.kind), None)
    return {"state": after.state if after is not None else "needs_ok"}


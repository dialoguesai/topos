"""Facts a pack wrote from a reaction the owner sent in Messages: count them, and end them on the owner's yes.

Messages stores a reaction ("tapback") as a message from whoever reacted, and its text is the message
reacted to. Until the packs and the commitments lane learned to skip such a row
(`derivation_job._owner_reaction`, `commitments.promise_shaped_records`), an owner-only pack read the
other person's sentence as the owner's own and could store a fact from it. Skipping the row stops new
ones; it does not touch a fact already stored. This module is the owner's way to end those.

What it finds: a standing pack fact (`valid_to IS NULL`, an `ontology_id`) whose EVERY source record
is a reaction the owner sent. The test is the one the lane that wrote the fact now applies, imported
and not restated: the packs' for every pack, and the commitments lane's as well for its own pack.
So what is ended is what a pack would no longer read. A fact with any other source (a journal entry,
a message the owner typed, a contact's message, a record that is no longer stored, a source list this
cannot read) is left alone: it has evidence the reaction rule says nothing about.

Three more kinds are left although every record they cite is such a reaction:
- a fact an owner action made (confirmed, corrected, revised or promoted in the app, declared or
  overridden): that is the owner's own statement and a batch does not undo it. Counted apart.
- a fact that cites its records for when they were sent, not for what they say (`actor_role`
  `synthesis`, the rhythm kernel): a reaction was sent by the owner at that time whatever it quotes.
- a fact whose source list is not the whole story. The writer replaces an event's source list when
  the event is told again in another record, and changes a fact's fields from a record it does not
  add to the list, so a fact first read from a journal entry can end up citing only a reaction. The
  derivation ledger keeps the record of every stored assertion; a fact it shows was also stored from
  another record, or an event told again whose stores it does not fully account for, is left.
  Counted apart.

What ending means: the row's `valid_to` is set and `closed_reason` is stamped in its payload, the way
`DerivationWriter._close` ends a superseded fact. No row is deleted and no source record is touched,
so the fact stays in the belief history and the reaction stays where it was. No tombstone is written
either: the value may be true of the owner from another record, and a pack that later reads it there
may store it again. The reaction itself is never read again (the history walk leaves it out), so a
later backfill cannot bring the ended fact back from it.

`find` and `count` read; only `retract` writes, in one gated, committed batch with nothing else open
on the connection. Its caller holds the owner's confirmation (the route in `api/signal.py` checks the
confirmed socket peer); nothing calls it by itself, on upgrade or otherwise.

What is returned and logged: pack ids and counts. Never a fact's value, a quote or a message's text.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable, Dict, List, NamedTuple, Optional

logger = logging.getLogger("topos.derivation.reaction_retract")

MESSAGES = "conversation_messages"
#: Stamped in the ended fact's payload. Not one of the re-derivation reasons
#: (`permissions_v2/message_evidence.py` REDERIVED_REASONS), so the share side keeps treating the
#: ended fact as it treats any closure it has no ruling for: it goes on withholding what it cites
#: (pinned in `tests/permissions_v2/test_closed_fact_floor.py`).
CLOSED_REASON = "owner_reaction_source"
_CHUNK = 400
#: `DerivationWriter._corroborate` keeps this many `extractions` entries (`hist[-12:]`).
_EXTRACTIONS_KEPT = 12


class RetractRefused(ValueError):
    """The standing set is not the one the owner was shown: nothing was written."""

    code = "reaction_facts_changed"


class _Fact(NamedTuple):
    object_id: str
    pack_id: str
    source_refs_json: str
    payload_json: str


class Found(NamedTuple):
    #: The facts a yes would end.
    facts: List[_Fact]
    #: Per pack: facts that qualify by their sources and are left because an owner action made them.
    kept_confirmed: Dict[str, int]
    #: Per pack: facts that qualify by the sources they list and are left because the ledger shows, or
    #: cannot rule out, a record that is not a reaction among the ones they were stored from.
    kept_other_record: Dict[str, int]

    def by_pack(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for fact in self.facts:
            counts[fact.pack_id] = counts.get(fact.pack_id, 0) + 1
        return counts


def _message_ids(source_refs_json: Any) -> Optional[List[str]]:
    """The message ids a fact cites, or None when any of its sources is not a stored message row
    this can name (another table, a malformed entry, an empty or unreadable list)."""
    try:
        refs = json.loads(source_refs_json or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(refs, list) or not refs:
        return None
    ids = []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("table") != MESSAGES or ref.get("record_id") in (None, ""):
            return None
        ids.append(str(ref["record_id"]))
    return ids


class _Reaction(NamedTuple):
    #: The packs' test: the row's role resolves to `authored` and it is a reaction
    #: (`derivation_job._record_role_for`, then `_owner_reaction`).
    by_role: bool
    #: The commitments lane's test: it picks rows by the sender flag alone and feeds them as authored
    #: whatever the source's posture (`commitments.promise_shaped_records`: `is_from_self=1`, then
    #: `is_reaction`).
    by_flag: bool


def _owner_reactions(conn: sqlite3.Connection, message_ids: List[str],
                     posture_for: Optional[Callable[[Dict[str, Any]], str]] = None) -> Dict[str, _Reaction]:
    """For each stored message id: whether the row is a reaction the owner sent, by each lane's own
    test. An id with no stored row is absent from the answer."""
    from ...enrichment.jobs.canonical.derivation_job import _owner_reaction, _record_role_for
    from ..facts.reactions import is_reaction, stored_metadata_column

    if posture_for is None:
        from ..provenance.posture import make_posture_resolver
        posture_for = make_posture_resolver(conn)
    columns = ("message_id", "content", "actor_role", "is_from_self", "sender_id", "sender_type",
               "source_id", "dataset_id")
    out: Dict[str, _Reaction] = {}
    wanted = sorted(set(message_ids))
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start:start + _CHUNK]
        rows = conn.execute(
            f"SELECT {', '.join(columns)}, {stored_metadata_column(conn)} FROM {MESSAGES}"
            f" WHERE message_id IN ({','.join('?' for _ in chunk)})", chunk).fetchall()
        for values in rows:
            row = dict(zip(columns, values[:-1]))
            marker, text = {"metadata_json": values[-1]}, row["content"]
            by_role = _owner_reaction(_record_role_for(row, MESSAGES, posture_for), MESSAGES, marker, text)
            by_flag = row["is_from_self"] in (1, True, "1") and is_reaction(marker, text)
            out[str(row["message_id"])] = _Reaction(bool(by_role), bool(by_flag))
    return out


def _payload(payload_json: Any) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(payload_json or "{}")
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _owner_made(created_by: Any, extractor_version: Any, payload: Optional[Dict[str, Any]]) -> bool:
    """An owner action made this fact, by the mark each of the owner's doors leaves on the row. A
    payload this cannot read counts as the owner's: it cannot show otherwise.

    The list is stated here, where a fact is about to be ended, and not taken from the share side's
    copy of it (`permissions_v2/message_evidence.py` `closed_fact_release`): a new owner door belongs
    in both. Checking two payload keys alone let a fact the owner revised or promoted be ended."""
    if payload is None:
        return True
    if created_by not in (None, "system") or str(extractor_version or "").startswith("owner"):
        return True                           # SignalObjectStore.owner_override, rows the owner declared
    if payload.get("verified_by_owner") or "corrected_from" in payload:
        return True                           # facts/verdicts.py: confirm_fact, edit_fact
    extractor = payload.get("extractor")
    model = extractor.get("model") if isinstance(extractor, dict) else extractor
    # surfaces.py: revise_fact (owner-revise), promote_conflict (owner-promote), the informant door
    return model is not None and (not isinstance(model, str) or model.startswith("owner"))


def _stored_from(conn: sqlite3.Connection, object_ids: List[str]) -> Dict[str, List[tuple]]:
    """Per fact: the `(table, record id)` of every stored assertion the derivation ledger holds for it
    (`derivation_job._ledger_write`: one row each time a record's assertion was written to, merged
    into or changed that fact). Empty for a node that keeps no ledger."""
    out: Dict[str, List[tuple]] = {}
    try:
        for start in range(0, len(object_ids), _CHUNK):
            chunk = object_ids[start:start + _CHUNK]
            for object_id, table, record_id in conn.execute(
                    "SELECT written_object_id, source_table, record_id FROM derivation_training_ledger"
                    f" WHERE written_object_id IN ({','.join('?' for _ in chunk)})", chunk):
                out.setdefault(str(object_id), []).append((table, record_id))
    except sqlite3.OperationalError:
        return {}
    return out


def _list_may_be_replaced(packs: Dict[str, Any], pack_id: str, payload: Dict[str, Any], stores: int) -> bool:
    """Whether a later reading may have replaced this fact's source list without the ledger showing
    every record behind it. `stores` is the number of ledger rows for the fact.

    `DerivationWriter._corroborate` appends one `extractions` entry per reading it merges in. For an
    event told again under another date (`_episodic_retelling`) it writes the retelling's records
    over the fact's own, so the list then names only the last record. Only an episodic predicate
    takes that path; a predicate this cannot look up is treated as one. The ledger accounts for the
    fact when it holds one row for the first store and one per merge."""
    told = payload.get("extractions")
    if not told:
        return False
    from ..facts.store import normalize_predicate
    name = str(payload.get("predicate") or "")
    pack = packs.get(pack_id)
    pred = next((p for n, p in pack.predicates.items() if normalize_predicate(n) == name), None) if pack else None
    if pred is not None and (pred.temporal != "episodic" or getattr(pred, "event_identity", "") == "dated"):
        return False
    # The writer keeps the last twelve entries: a full list may have dropped earlier ones.
    if not isinstance(told, list) or len(told) >= _EXTRACTIONS_KEPT:
        return True
    return stores != 1 + len(told)


def _bundled_packs() -> Dict[str, Any]:
    from .packs import load_packs
    from .registry import bundled_pack_dir
    try:
        return load_packs(bundled_pack_dir())
    except Exception:  # noqa: BLE001 - an unreadable pack file must not end a fact: every predicate is then unknown
        logger.warning("could not load the fact packs; an event told again is left", exc_info=True)
        return {}


def find(conn: sqlite3.Connection, *,
         posture_for: Optional[Callable[[Dict[str, Any]], str]] = None) -> Found:
    """The standing pack facts whose every source record is a reaction the owner sent. Reads only.

    A source counts when the packs' own test says so. For the commitments pack the lane's test counts
    too, because that lane picks its records itself. A row only the sender flag calls the owner's
    (a source the owner marked ambient) is still read by a pack that takes observed text, so a fact
    another pack holds from it is not one this ends: the next backfill could store it again.

    Of the facts that pass, the module's three kinds are left: an owner action made it; it cites its
    records for their timing; the ledger shows or cannot rule out another record behind it.

    A database that has no pack facts yet (no `ontology_id` column) or no messages table holds none."""
    from .commitments import PACK_ID as COMMITMENTS_PACK
    none = Found([], {}, {})
    try:
        rows = conn.execute(
            "SELECT object_id, ontology_id, source_refs_json, payload_json, created_by, extractor_version"
            " FROM signal_objects"
            " WHERE object_type='fact' AND valid_to IS NULL AND ontology_id IS NOT NULL AND ontology_id<>''"
            " ORDER BY ontology_id, object_id").fetchall()
    except sqlite3.OperationalError:
        return none
    cited: Dict[str, List[str]] = {}
    for object_id, _pack, refs_json, *_rest in rows:
        ids = _message_ids(refs_json)
        if ids is not None:
            cited[str(object_id)] = ids
    if not cited:
        return none
    try:
        reaction = _owner_reactions(conn, [mid for ids in cited.values() for mid in ids], posture_for)
    except sqlite3.OperationalError:
        return none

    def owner_reaction(pack_id: str, message_id: str) -> bool:
        judged = reaction.get(message_id)
        return judged is not None and (judged.by_role or (pack_id == COMMITMENTS_PACK and judged.by_flag))

    kept: Dict[str, int] = {}
    candidates: List[tuple] = []
    for object_id, pack_id, refs_json, payload_json, created_by, extractor_version in rows:
        ids, pack_id = cited.get(str(object_id)), str(pack_id)
        if ids is None or not all(owner_reaction(pack_id, mid) for mid in ids):
            continue
        payload = _payload(payload_json)
        if _owner_made(created_by, extractor_version, payload):
            kept[pack_id] = kept.get(pack_id, 0) + 1
        elif payload.get("actor_role") != "synthesis":   # synthesize.py: a sample of records, for their timing
            candidates.append((_Fact(str(object_id), pack_id, refs_json, payload_json), payload))

    # What the ledger says each one was stored from: the source list can be short of it.
    ledger = _stored_from(conn, [fact.object_id for fact, _ in candidates])
    unjudged = sorted({str(rid) for records in ledger.values() for table, rid in records
                       if table == MESSAGES and rid not in (None, "") and str(rid) not in reaction})
    if unjudged:
        reaction.update(_owner_reactions(conn, unjudged, posture_for))
    packs: Optional[Dict[str, Any]] = None
    facts: List[_Fact] = []
    other: Dict[str, int] = {}
    for fact, payload in candidates:
        records = ledger.get(fact.object_id, [])
        unsure = any(table != MESSAGES or rid in (None, "") or not owner_reaction(fact.pack_id, str(rid))
                     for table, rid in records)
        if not unsure and payload.get("extractions"):
            packs = _bundled_packs() if packs is None else packs
            # Every ledger row names a reaction (checked above); a store the ledger does not hold
            # may have been from another record.
            unsure = _list_may_be_replaced(packs, fact.pack_id, payload, len(records))
        if unsure:
            other[fact.pack_id] = other.get(fact.pack_id, 0) + 1
        else:
            facts.append(fact)
    return Found(facts, kept, other)


def _summary(by_pack: Dict[str, int], found: Found) -> Dict[str, Any]:
    return {"packs": [{"pack_id": pid, "facts": by_pack[pid]} for pid in sorted(by_pack)],
            "total": sum(by_pack.values()),
            "kept_confirmed": sum(found.kept_confirmed.values()),
            "kept_other_record": sum(found.kept_other_record.values())}


def count(conn: sqlite3.Connection) -> Dict[str, Any]:
    """`{packs: [{pack_id, facts}], total, kept_confirmed, kept_other_record}`: what a yes would end,
    per pack, and how many facts that cite only such reactions stay: because an owner action made
    them, and because another record is or may be behind them. Writes nothing."""
    found = find(conn)
    return _summary(found.by_pack(), found)


def retract(conn: sqlite3.Connection, *, expected_total: Optional[int] = None) -> Dict[str, Any]:
    """End every fact `find` names. The caller holds the owner's confirmation.

    `expected_total` is the count the owner was shown before the yes; a different count now raises
    `RetractRefused` with nothing written, so the yes never covers facts it was not asked about.

    One gated batch, one commit, and nothing open on this connection when the gate is asked for:
    waiting for the write gate while holding SQLite's write lock stalls every gated writer for as
    long as the wait lasts (topos#279). Each row is ended only if it is still standing with the
    sources and payload that were judged; a fact that changed in between (a second source added, the
    owner's verdict) is left and counted under `changed`.

    Returns the count's shape plus `retracted` and `changed`."""
    from ...storage.db.write_gate import batched_writes
    from .writer import _now_iso

    found = find(conn)
    if expected_total is not None and len(found.facts) != expected_total:
        raise RetractRefused("the facts written from reactions are not the ones that were counted; count again")
    ended: Dict[str, int] = {}
    changed = 0
    if found.facts:
        if getattr(conn, "in_transaction", False):
            conn.commit()
        with batched_writes(conn):
            now = _now_iso()
            for fact in found.facts:
                payload = json.loads(fact.payload_json or "{}")
                payload["closed_reason"] = CLOSED_REASON
                cursor = conn.execute(
                    "UPDATE signal_objects SET valid_to=?, payload_json=?, updated_at=?"
                    " WHERE object_id=? AND object_type='fact' AND valid_to IS NULL"
                    " AND source_refs_json IS ? AND payload_json IS ?",
                    (now, json.dumps(payload, default=str), now, fact.object_id,
                     fact.source_refs_json, fact.payload_json))
                if cursor.rowcount == 1:
                    ended[fact.pack_id] = ended.get(fact.pack_id, 0) + 1
                else:
                    changed += 1
    out = _summary(ended, found)
    out["retracted"] = out["total"]
    out["changed"] = changed
    logger.info("facts written from the owner's reactions: %d ended in %d pack(s) by the owner, %d changed "
                "since the count and left, %d made by the owner and left, %d with another record behind them left",
                out["retracted"], len(ended), changed, out["kept_confirmed"], out["kept_other_record"])
    return out

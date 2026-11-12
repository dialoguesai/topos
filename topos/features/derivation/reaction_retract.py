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

Four more kinds are left although every record they cite is such a reaction:
- a fact an owner action made (confirmed, corrected, revised or promoted in the app, declared or
  overridden, its attribution repaired): that is the owner's own statement and a batch does not undo
  it. Counted apart.
- a fact the owner marked in an app (a row in `fact_feedback`: their own reaction or rank on the
  fact). The owner looked at it and said something about it; whether that should keep it is theirs to
  rule, so it is kept. Counted apart.
- a fact that cites its records for when they were sent, not for what they say (`actor_role`
  `synthesis`, the rhythm kernel): a reaction was sent by the owner at that time whatever it quotes.
- a fact whose source list is not the whole story, or cannot be shown to be. The list can be short
  of what the fact rests on in three ways: the writer replaces an event's list when the event is
  told again in another record; it changes a fact's fields from a record it does not add to the
  list; and it stores nothing at all for the same value read again within seven days by the same
  model (`noop`), so a fact stored from a reaction and then read in a message the owner typed still
  cites the reaction alone. The derivation ledger has a row for every assertion that reached the
  writer, stored or not. A fact is ended only when the ledger shows the whole of it: a store for the
  fact's first write and for each later merge, every one of them from such a reaction, and no
  reading from any other record that was a repeat of the fact and stored nowhere. A fact the ledger
  holds no store for (no ledger, a row made some other way) is left: nothing shows what it rests on.
  Counted apart.

What ending means: the row's `valid_to` is set and `closed_reason` is stamped in its payload, the way
`DerivationWriter._close` ends a superseded fact. No row is deleted and no source record is touched,
so the fact stays in the belief history and the reaction stays where it was. No tombstone is written
either: the value may be true of the owner from another record, and a pack that later reads it there
may store it again. The reaction itself is never read again (the history walk leaves it out), so a
later backfill cannot bring the ended fact back from it.

What ending does not do: put anything back. A fact that was closed when a reaction's value took its
place (`superseded`, `correction`, a `closes` rule) stays closed, and a fact with another source
whose fields a reaction changed keeps those fields and is not ended. Finding and restoring those is
not built here.

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
#: The owner's reaction and rank on a pack fact, keyed by `fact_id` (`query/fact_feedback.py`). That
#: module creates the table on its first write and is not in every build, so the table is looked for
#: by name and never imported.
FEEDBACK_TABLE = "fact_feedback"


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
    #: Per pack: facts that qualify by their sources and are left because the owner marked them in an
    #: app (a row in `fact_feedback`).
    kept_marked: Dict[str, int]
    #: Per pack: facts that qualify by the sources they list and are left because the ledger shows, or
    #: cannot rule out, a record that is not a reaction among the ones they rest on.
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
        return True                           # facts/verdicts.py: confirm_fact, edit_fact with a new value
    # facts/verdicts.py edit_fact with `asserted_by` alone repairs the attribution in place and leaves
    # no other mark. What `DerivationWriter._insert` stamps follows from the role; anything else was
    # set afterwards (a payload without the writer's two stamps is not shown to be the writer's).
    role = payload.get("actor_role")
    if payload.get("asserted_by") != ("owner" if role == "authored" else f"extracted:{role}"):
        return True
    extractor = payload.get("extractor")
    model = extractor.get("model") if isinstance(extractor, dict) else extractor
    # surfaces.py: revise_fact (owner-revise), promote_conflict (owner-promote), the informant door
    if model is not None and (not isinstance(model, str) or model.startswith("owner")):
        return True
    # The same doors landing on a fact that already stood with that value: the writer merges them in
    # (`_corroborate`) and the only mark is the model of the reading it appends.
    told = payload.get("extractions")
    return any(isinstance(entry, dict) and str(entry.get("model") or "").startswith("owner")
               for entry in (told if isinstance(told, list) else ()))


def _marked_in_an_app(conn: sqlite3.Connection, object_ids: List[str]) -> set:
    """Which of these facts have a row in `fact_feedback`. A database without that table has none.
    A table of that name this cannot read by `fact_id` marks them all: it cannot show otherwise."""
    if not object_ids or conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                      (FEEDBACK_TABLE,)).fetchone() is None:
        return set()
    marked: set = set()
    try:
        for start in range(0, len(object_ids), _CHUNK):
            chunk = object_ids[start:start + _CHUNK]
            marked.update(str(row[0]) for row in conn.execute(
                f"SELECT fact_id FROM {FEEDBACK_TABLE} WHERE fact_id IN ({','.join('?' for _ in chunk)})", chunk))
    except sqlite3.OperationalError:
        return set(object_ids)
    return marked


def _stored_from(conn: sqlite3.Connection, object_ids: List[str]) -> Dict[str, List[tuple]]:
    """Per fact: the `(table, record id)` of every stored assertion the derivation ledger holds for it
    (`derivation_job._ledger_write`: one row each time a record's assertion was written to, merged
    into or changed that fact). Empty for a node that keeps no ledger.

    Not every reading the fact rests on: a repeat the writer stored nowhere has a row with no fact id
    (`_repeats_not_stored`)."""
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


def _repeats_not_stored(conn: sqlite3.Connection, pack_ids: List[str]) -> Dict[tuple, List[tuple]]:
    """Per `(pack id, predicate)`: the `(table, record id, value json)` of every reading in the ledger
    that reached the writer and was stored nowhere.

    The lanes write one ledger row per judged assertion. One the verifier rejected never reaches the
    writer. Every other one does, and its row carries the fact id only for an outcome that wrote
    (`derivation_job.run_derivation_batch`, `_drip_catchup`, `surfaces.run_pack_backfill`: `stored`).
    `noop` is the outcome that matters here: the same value as a standing fact, read from another
    record within the writer's window, adds no source and leaves only this row. The trial lane's
    accepted rows are of this shape too (read, judged true of the record, stored nowhere) and count
    the same: the record says it."""
    from ..facts.store import normalize_predicate
    out: Dict[tuple, List[tuple]] = {}
    try:
        for start in range(0, len(pack_ids), _CHUNK):
            chunk = pack_ids[start:start + _CHUNK]
            for pack_id, predicate, table, record_id, value_json in conn.execute(
                    "SELECT pack_id, predicate, source_table, record_id, value_json"
                    " FROM derivation_training_ledger"
                    " WHERE written_object_id IS NULL AND vstatus IS NOT 'rejected'"
                    f" AND pack_id IN ({','.join('?' for _ in chunk)})", chunk):
                out.setdefault((str(pack_id), normalize_predicate(predicate)), []).append(
                    (table, record_id, value_json))
    except sqlite3.OperationalError:
        return {}
    return out


def _comparable(value: Any) -> str:
    """A value for the writer's own equality (`_canon_value`, what `assert_pack_fact` compares before
    it answers `noop`), without the person ids the writer binds into it (`_bind_entity_refs`): an
    entity merge rewrites those in the fact's payload and not in the ledger, so a row that was equal
    when it was judged would no longer look it."""
    from .writer import _canon_value
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if not str(k).endswith(("_entity_id", "_entity_ids"))}
    return _canon_value(value)


def _same_value(value_json: Any, comparable: str) -> bool:
    """Whether a ledger row's value is the fact's. A value this cannot read counts as the same: it
    cannot show otherwise."""
    try:
        return _comparable(json.loads(value_json)) == comparable
    except (TypeError, ValueError):
        return True


def _ledger_is_short(payload: Dict[str, Any], stores: int) -> bool:
    """Whether the ledger holds fewer stores for a fact than the fact's own row records: one for its
    first write, and one for each reading merged into it (`DerivationWriter._corroborate` appends an
    `extractions` entry per merge and keeps the last twelve, so a full list may have dropped some).

    Every lane that stores a pack fact from a record has written the ledger since the ledger existed,
    so a store it does not hold is one nothing here can name: a ledger that was emptied, a row made
    by hand or copied in. Such a fact is left."""
    told = payload.get("extractions") or []
    if not isinstance(told, list) or len(told) >= _EXTRACTIONS_KEPT:
        return True
    return stores < 1 + len(told)


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

    Of the facts that pass, the module's four kinds are left: an owner action made it; the owner
    marked it in an app; it cites its records for their timing; the ledger shows, or cannot rule
    out, another record behind it (a store from one, a repeat read in one and stored nowhere, or no
    ledger row for a store the fact records).

    A database that has no pack facts yet (no `ontology_id` column) or no messages table holds none."""
    from .commitments import PACK_ID as COMMITMENTS_PACK
    none = Found([], {}, {}, {})
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

    # The owner's own reaction or rank on the fact, recorded through an app.
    in_an_app = _marked_in_an_app(conn, [fact.object_id for fact, _ in candidates])
    marked: Dict[str, int] = {}
    for fact, _ in candidates:
        if fact.object_id in in_an_app:
            marked[fact.pack_id] = marked.get(fact.pack_id, 0) + 1
    candidates = [(fact, payload) for fact, payload in candidates if fact.object_id not in in_an_app]

    # What the ledger says each one rests on: the source list can be short of it.
    ledger = _stored_from(conn, [fact.object_id for fact, _ in candidates])
    repeats = _repeats_not_stored(conn, sorted({fact.pack_id for fact, _ in candidates}))
    behind: Dict[str, List[tuple]] = {}
    for fact, payload in candidates:
        records = ledger.get(fact.object_id, [])
        told = payload.get("extractions") or []
        # More stores than a first write and its merges: a record changed the fact's fields
        # (`_update_fields` leaves no other trace), so the value a repeat was compared with may not
        # be the value the fact has now. Any reading of the predicate that was not stored then counts.
        refielded = isinstance(told, list) and len(records) > 1 + len(told)
        value = _comparable(payload.get("value_struct") or payload.get("object_value"))
        behind[fact.object_id] = records + [
            (table, rid) for table, rid, value_json
            in repeats.get((fact.pack_id, str(payload.get("predicate") or "")), [])
            if refielded or _same_value(value_json, value)]
    unjudged = sorted({str(rid) for records in behind.values() for table, rid in records
                       if table == MESSAGES and rid not in (None, "") and str(rid) not in reaction})
    if unjudged:
        reaction.update(_owner_reactions(conn, unjudged, posture_for))
    packs: Optional[Dict[str, Any]] = None
    facts: List[_Fact] = []
    other: Dict[str, int] = {}
    for fact, payload in candidates:
        stores = len(ledger.get(fact.object_id, []))
        unsure = _ledger_is_short(payload, stores) or any(
            table != MESSAGES or rid in (None, "") or not owner_reaction(fact.pack_id, str(rid))
            for table, rid in behind[fact.object_id])
        if not unsure and payload.get("extractions"):
            packs = _bundled_packs() if packs is None else packs
            # Every ledger row names a reaction (checked above); an event told again whose stores
            # the ledger does not match one for one may have had its list replaced.
            unsure = _list_may_be_replaced(packs, fact.pack_id, payload, stores)
        if unsure:
            other[fact.pack_id] = other.get(fact.pack_id, 0) + 1
        else:
            facts.append(fact)
    return Found(facts, kept, marked, other)


def _summary(by_pack: Dict[str, int], found: Found) -> Dict[str, Any]:
    return {"packs": [{"pack_id": pid, "facts": by_pack[pid]} for pid in sorted(by_pack)],
            "total": sum(by_pack.values()),
            "kept_confirmed": sum(found.kept_confirmed.values()),
            "kept_marked": sum(found.kept_marked.values()),
            "kept_other_record": sum(found.kept_other_record.values())}


def count(conn: sqlite3.Connection) -> Dict[str, Any]:
    """`{packs: [{pack_id, facts}], total, kept_confirmed, kept_marked, kept_other_record}`: what a
    yes would end, per pack, and how many facts that cite only such reactions stay: because an owner
    action made them, because the owner marked them in an app, and because another record is or may
    be behind them. Writes nothing."""
    found = find(conn)
    return _summary(found.by_pack(), found)


def retract(conn: sqlite3.Connection, *, expected_total: Optional[int] = None) -> Dict[str, Any]:
    """End every fact `find` names. The caller holds the owner's confirmation.

    `expected_total` is the count the owner was shown before the yes; a different count now raises
    `RetractRefused` with nothing written, so the yes never covers facts it was not asked about.

    One gated batch, one commit, and nothing open on this connection when the gate is asked for:
    waiting for the write gate while holding SQLite's write lock stalls every gated writer for as
    long as the wait lasts (topos#279). The wait can be long, and what keeps a fact is not all on its
    own row: a repeat read in a typed message leaves only a ledger row, a mark made in an app only a
    `fact_feedback` row. So the facts are judged again inside the gate, where no gated writer can
    add either, and a fact is ended only if it is named both times and is still standing with the
    sources and payload that were judged. One that is not (a second source added, the owner's
    verdict, a repeat or a mark that arrived during the wait) is left and counted under `changed`.

    Nothing is put back: a fact one of these replaced stays closed (the module docstring).

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
            shown, found = found, find(conn)
            still = {fact.object_id: fact for fact in found.facts}
            now = _now_iso()
            for fact in shown.facts:
                fact = still.get(fact.object_id)
                if fact is None:
                    changed += 1
                    continue
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
                "since the count and left, %d made by the owner and left, %d marked in an app and left, "
                "%d with another record behind them left",
                out["retracted"], len(ended), changed, out["kept_confirmed"], out["kept_marked"],
                out["kept_other_record"])
    return out

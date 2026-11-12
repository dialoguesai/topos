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
cannot read) is left alone: it has evidence the reaction rule says nothing about. So is a fact the
owner confirmed or corrected in the app: that is the owner's own statement and a batch does not undo
it.

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
#: ended fact as it treats any closure it has no ruling for: it goes on withholding what it cites.
CLOSED_REASON = "owner_reaction_source"
_CHUNK = 400


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
    #: Per pack: facts that qualify by their sources and are left because the owner confirmed or
    #: corrected them.
    kept_confirmed: Dict[str, int]

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


def _owner_made(payload_json: Any) -> bool:
    """The owner confirmed this fact or corrected it to this value (`facts/verdicts.py`). A payload
    this cannot read counts as the owner's: it cannot show otherwise."""
    try:
        payload = json.loads(payload_json or "{}")
    except (TypeError, ValueError):
        return True
    if not isinstance(payload, dict):
        return True
    return bool(payload.get("verified_by_owner")) or "corrected_from" in payload


def find(conn: sqlite3.Connection, *,
         posture_for: Optional[Callable[[Dict[str, Any]], str]] = None) -> Found:
    """The standing pack facts whose every source record is a reaction the owner sent. Reads only.

    A source counts when the packs' own test says so. For the commitments pack the lane's test counts
    too, because that lane picks its records itself. A row only the sender flag calls the owner's
    (a source the owner marked ambient) is still read by a pack that takes observed text, so a fact
    another pack holds from it is not one this ends: the next backfill could store it again.

    A database that has no pack facts yet (no `ontology_id` column) or no messages table holds none."""
    from .commitments import PACK_ID as COMMITMENTS_PACK
    try:
        rows = conn.execute(
            "SELECT object_id, ontology_id, source_refs_json, payload_json FROM signal_objects"
            " WHERE object_type='fact' AND valid_to IS NULL AND ontology_id IS NOT NULL AND ontology_id<>''"
            " ORDER BY ontology_id, object_id").fetchall()
    except sqlite3.OperationalError:
        return Found([], {})
    cited: Dict[str, List[str]] = {}
    for object_id, _pack, refs_json, _payload in rows:
        ids = _message_ids(refs_json)
        if ids is not None:
            cited[str(object_id)] = ids
    if not cited:
        return Found([], {})
    try:
        reaction = _owner_reactions(conn, [mid for ids in cited.values() for mid in ids], posture_for)
    except sqlite3.OperationalError:
        return Found([], {})
    facts: List[_Fact] = []
    kept: Dict[str, int] = {}
    for object_id, pack_id, refs_json, payload_json in rows:
        ids = cited.get(str(object_id))
        if ids is None:
            continue
        by_flag_too = str(pack_id) == COMMITMENTS_PACK
        judged = [reaction.get(mid) for mid in ids]
        if not all(r is not None and (r.by_role or (by_flag_too and r.by_flag)) for r in judged):
            continue
        if _owner_made(payload_json):
            kept[str(pack_id)] = kept.get(str(pack_id), 0) + 1
            continue
        facts.append(_Fact(str(object_id), str(pack_id), refs_json, payload_json))
    return Found(facts, kept)


def _summary(by_pack: Dict[str, int], kept: Dict[str, int]) -> Dict[str, Any]:
    return {"packs": [{"pack_id": pid, "facts": by_pack[pid]} for pid in sorted(by_pack)],
            "total": sum(by_pack.values()),
            "kept_confirmed": sum(kept.values())}


def count(conn: sqlite3.Connection) -> Dict[str, Any]:
    """`{packs: [{pack_id, facts}], total, kept_confirmed}`: what a yes would end, per pack, and how
    many facts of the same kind stay because the owner confirmed them. Writes nothing."""
    found = find(conn)
    return _summary(found.by_pack(), found.kept_confirmed)


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
    out = _summary(ended, found.kept_confirmed)
    out["retracted"] = out["total"]
    out["changed"] = changed
    logger.info("facts written from the owner's reactions: %d ended in %d pack(s) by the owner, %d changed "
                "since the count and left, %d confirmed by the owner and left",
                out["retracted"], len(ended), changed, out["kept_confirmed"])
    return out

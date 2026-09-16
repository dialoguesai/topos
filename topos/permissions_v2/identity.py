"""Who the owner is, for permissions v2: one frozen legacy rule and one attested rule.

Producers write the owner's entity id as a fact subject, and a node legitimately
holds several `is_self` rows, so the first fact family could only ever release
the literal `"self"` subject that no production producer writes. The owner may
now attest, explicitly and per entity, which entities denote them. That
attestation grants nothing by itself: every fact still needs the owner's
evidence review, the owner's output review and a grant of the separate
`permissions-beta/p2b-v3` capability.

Two sets, never one. The permit set decides what may be released and contains
only what the owner attested (plus the literal `"self"`). The restriction set
decides what a tombstone, exclusion or copy check matches, and contains every
spelling of the owner this node has ever seen: current `is_self` rows, ids ever
attested or revoked, and ids linked by a merge tombstone. The permit set is
always a subset of the restriction set, so widening whom the owner may release
about can never narrow what an owner restriction covers.

Nothing here authenticates the owner, loads a review, or decides a policy. The
attestation ledger is authenticated by its own signed-command service, and the
canonical floor detects a ledger that moved outside it.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Literal

from .canonical import PolicyError, digest
from .protection_clock import EVENTS, LEDGER, REGISTRY, identity_event_key, rekey_event_key

LEGACY_CONTRACT = "legacy_single_self_v1"
ATTESTED_CONTRACT = "owner_attested_v1"
SUBJECT_CONTRACTS = (LEGACY_CONTRACT, ATTESTED_CONTRACT)
SUBJECT_CONTRACT_BY_CAPABILITY = {
    "permissions-beta/p2a-v1": LEGACY_CONTRACT,
    "permissions-beta/p2b-v1": LEGACY_CONTRACT,
    "permissions-beta/p2b-v2": LEGACY_CONTRACT,
    "permissions-beta/p2b-v3": ATTESTED_CONTRACT,
}
# The exact sentence the owner confirms. Changing it is a new statement version.
ATTESTATION_STATEMENT = "owner-identity-attestation/v1"
SELF = "self"
MAX_ACTIVE_ATTESTATIONS = 16
MAX_RESTRICTION_SUBJECTS = 512
EntryState = Literal["active", "stale", "revoked"]


@dataclass(frozen=True)
class IdentityEntry:
    """One entity's current attestation state, derived inside one read transaction."""
    entity_id: str
    entry_id: str
    generation: int
    state: EntryState
    reason: str | None


def _rows(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        raise PolicyError("identity_state_unavailable") from None


def _identifier(value) -> bool:
    return type(value) is str and 0 < len(value) <= 200


def legacy_owner_subjects(conn) -> set[str]:
    """The frozen pre-binding rule: `{"self", E}` only when exactly one self row exists.

    This is the permit rule of every capability that existed before identity
    binding, and it must keep raising the same codes in the same order. It never
    reads an attestation.
    """
    try:
        rows = conn.execute("SELECT entity_id FROM entities WHERE is_self=1").fetchmany(2)
    except sqlite3.OperationalError:
        raise PolicyError("owner_subject_unknown") from None
    if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        raise PolicyError("owner_subject_ambiguous")
    return {SELF, rows[0][0]}


def self_entity_ids(conn) -> set[str]:
    """Every current `is_self` row. Restriction input only; never a permit."""
    return {row[0] for row in _rows(conn, "SELECT entity_id FROM entities WHERE is_self=1") if _identifier(row[0])}


def literal_self_shadowed(conn) -> bool:
    """True when an entity row literally named `self` shadows the producer constant.

    One producer writes the literal subject `"self"`. If an entity ever carries
    that id, the two meanings are no longer distinguishable, so the attested
    contract withholds the literal rather than guessing which one a fact meant.
    """
    return bool(_rows(conn, "SELECT 1 FROM entities WHERE entity_id=? LIMIT 1", (SELF,)))


def registry_ids(conn) -> set[str]:
    """Every id the registry has ever recorded as an owner spelling."""
    return {row[0] for row in _rows(conn, f"SELECT entity_id FROM {REGISTRY}") if _identifier(row[0])}


def restriction_subjects(conn) -> set[str]:
    """Every spelling of the owner a restriction may be keyed by (`R`).

    Deliberately wider than any permit set and never raising on ambiguity: an
    owner who excluded `<entity>:prefers` before a merge must keep that veto
    afterwards, and a node with four self rows must still match a tombstone
    keyed by the one the producers stopped using. Merge tombstones extend the
    set transitively, because a merge re-keys facts across the pair.
    """
    subjects = {SELF} | self_entity_ids(conn) | registry_ids(conn)
    pairs = [(row[0], row[1]) for row in
             _rows(conn, "SELECT absorbed_entity_id, merged_into FROM entity_merge_tombstones")
             if _identifier(row[0]) and _identifier(row[1])] if _has_table(conn, "entity_merge_tombstones") else []
    changed = True
    while changed:
        changed = False
        for absorbed, kept in pairs:
            if absorbed in subjects and kept not in subjects:
                subjects.add(kept); changed = True
            elif kept in subjects and absorbed not in subjects:
                subjects.add(absorbed); changed = True
        if len(subjects) > MAX_RESTRICTION_SUBJECTS:
            raise PolicyError("identity_restriction_unbounded")
    return subjects


def _has_table(conn, name) -> bool:
    return bool(_rows(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)))


def composition_revision(conn, entity_id: str) -> str:
    """What an attested entity is made of, excluding every column native jobs rewrite.

    Names, aliases, identifiers, mention counts and metadata change on every
    enrichment batch, so binding them would decay consent. Merge membership
    does not change on its own: it changes when this entity absorbs another or
    is absorbed, which is exactly when the owner should look again.
    """
    absorbed = sorted(row[0] for row in _rows(conn,
        "SELECT absorbed_entity_id FROM entity_merge_tombstones WHERE merged_into=?", (entity_id,))
        if _identifier(row[0])) if _has_table(conn, "entity_merge_tombstones") else []
    merged_into = sorted(row[0] for row in _rows(conn,
        "SELECT merged_into FROM entity_merge_tombstones WHERE absorbed_entity_id=?", (entity_id,))
        if _identifier(row[0])) if _has_table(conn, "entity_merge_tombstones") else []
    return digest({"version": "entity-composition/v1", "entity_id": entity_id,
                   "absorbed": absorbed, "merged_into": merged_into})


def _entity_pins(conn, entity_id: str):
    rows = _rows(conn, "SELECT entity_type, is_self, contact_id FROM entities WHERE entity_id=?", (entity_id,))
    if len(rows) != 1:
        return None
    return {"entity_type": rows[0][0], "is_self": rows[0][1], "contact_id": rows[0][2]}


def last_identity_event(conn, entity_id: str) -> int:
    value = _rows(conn, f"SELECT max(generation) FROM {EVENTS} WHERE artifact_key=?",
                  (identity_event_key(entity_id),))
    found = value[0][0] if value else None
    return found if type(found) is int else 0


def identity_event_count(conn, entity_id: str) -> int:
    """How many identity events this entity has, not just the newest generation.

    Some identity churn is logged without advancing the clock, so a merge that
    moves twenty mentions leaves twenty events at one generation. Counting them
    is what makes each one change a closure that names the entity.
    """
    value = _rows(conn, f"SELECT count(*) FROM {EVENTS} WHERE artifact_key=?", (identity_event_key(entity_id),))
    found = value[0][0] if value else 0
    return found if type(found) is int else 0


def entries(conn) -> dict[str, IdentityEntry]:
    """Current attestation state per entity, folded from the append-only ledger.

    An entry is `active` only while every pinned identity column still matches,
    its composition is unchanged, and no identity event landed after the
    attestation itself. Anything else is `stale`: the owner attested something
    that has since changed, and only the owner can say whether it still denotes
    them. A revoked entry stays revoked; re-attesting mints a new entry.
    """
    folded: dict[str, dict] = {}
    for row in _rows(conn, f"SELECT sequence, entry_id, action, entity_id, target_entry_id, entity_type, "
                           f"is_self, contact_id, composition_revision, generation FROM {LEDGER} ORDER BY sequence"):
        (_sequence, entry_id, action, entity_id, target, entity_type, is_self, contact_id,
         composition, generation) = row
        if not _identifier(entity_id) or not _identifier(entry_id):
            raise PolicyError("identity_state_unavailable")
        if action == "attest":
            folded[entity_id] = {"entry_id": entry_id, "generation": generation, "revoked": False,
                                 "pins": {"entity_type": entity_type, "is_self": is_self, "contact_id": contact_id},
                                 "composition": composition}
        elif action == "revoke":
            current = folded.get(entity_id)
            if current is None or current["entry_id"] != target:
                raise PolicyError("identity_state_unavailable")
            current["revoked"] = True
        else:
            raise PolicyError("identity_state_unavailable")
    result = {}
    for entity_id, value in folded.items():
        if value["revoked"]:
            result[entity_id] = IdentityEntry(entity_id, value["entry_id"], value["generation"], "revoked", None)
            continue
        reason = None
        pins = _entity_pins(conn, entity_id)
        if pins is None:
            reason = "entity_missing"
        elif pins != value["pins"]:
            reason = "identity_columns_changed"
        elif composition_revision(conn, entity_id) != value["composition"]:
            reason = "composition_changed"
        elif last_identity_event(conn, entity_id) != value["generation"]:
            reason = "identity_event_after_attestation"
        result[entity_id] = IdentityEntry(entity_id, value["entry_id"], value["generation"],
                                          "active" if reason is None else "stale", reason)
    return result


def attested_subjects(conn) -> set[str]:
    """Entity ids the owner currently attests and whose identity has not moved."""
    return {entity_id for entity_id, entry in entries(conn).items() if entry.state == "active"}


def permit_subjects(conn, *, contract: str) -> set[str]:
    """The only subjects a release may be about, under the contract its capability fixed."""
    if contract == LEGACY_CONTRACT:
        return legacy_owner_subjects(conn)
    if contract != ATTESTED_CONTRACT:
        raise PolicyError("subject_contract_unknown")
    subjects = attested_subjects(conn)
    if not literal_self_shadowed(conn):
        subjects = subjects | {SELF}
    return subjects


def rekeyed_facts(conn, fact_ids) -> set[str]:
    """Facts whose subject was rewritten in place, which is the overlay signature.

    A merge re-keys the absorbed entity's facts onto the surviving entity. If
    the survivor is the owner, another person's fact becomes an owner-subject
    fact with the owner's own messages as its evidence. Such a fact is never
    eligible under the attested contract; the owner can state the claim again,
    which writes a new fact with its own lineage.
    """
    tainted = set()
    for fact_id in sorted({str(value) for value in fact_ids}):
        if _rows(conn, f"SELECT 1 FROM {EVENTS} WHERE artifact_key=? LIMIT 1",
                 (rekey_event_key(fact_id),)):
            tainted.add(fact_id)
    return tainted


def closure_identity(conn, *, subjects, fact_ids) -> dict:
    """The identity state one review depends on, and nothing else on the node.

    Attesting an unrelated entity, or a merge elsewhere, leaves this value
    unchanged; any change to a subject this closure actually names changes it
    permanently, because the entry generation and the last identity event both
    move forward.
    """
    current = entries(conn)
    registry = registry_ids(conn)
    state = []
    for subject in sorted({str(value) for value in subjects} - {SELF, ""}):
        entry = current.get(subject)
        state.append({"subject": subject, "registered": subject in registry,
                      "last_event": last_identity_event(conn, subject),
                      "events": identity_event_count(conn, subject),
                      "entry": None if entry is None else
                               {"entry_id": entry.entry_id, "generation": entry.generation, "state": entry.state}})
    return {"version": "closure-identity/v1", "subjects": state,
            "literal_self_shadowed": literal_self_shadowed(conn),
            "rekeyed_facts": sorted(rekeyed_facts(conn, fact_ids))}


def identity_fingerprint(conn) -> str:
    """Node-wide identity state for signed authority: consent rows and the registry.

    Signed authority changes on any attestation or identity event, for every
    capability, so a recipient cannot tell an identity change from an Off-limits
    change by which of their grants went stale.
    """
    ledger = [list(row) for row in _rows(conn, f"SELECT sequence, entry_id, action, entity_id, target_entry_id, "
                                               f"generation FROM {LEDGER} ORDER BY sequence")]
    registry = [list(row) for row in _rows(conn, f"SELECT entity_id, basis, first_generation FROM {REGISTRY} "
                                                 f"ORDER BY entity_id")]
    return digest({"version": "owner-identity-fingerprint/v1", "ledger": ledger, "registry": registry})

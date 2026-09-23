"""The only writer of the owner identity attestation ledger.

Everything this service does is the owner saying something about themselves. It
grants nothing: an attestation decides which entities denote the owner, and a
fact about one of them still needs the owner's evidence review, the owner's
output review, and a grant of a capability whose contract reads attestations.

The write path is deliberately narrow. One owner principal, one signed command
consumed exactly once by the ledger's own uniqueness constraint, one canonical
transaction, and the external floor published pending before it and active
after. A crash between the two leaves the floor closed, which stops reads until
a deliberate recovery rather than letting a half-written consent record serve.
"""
from __future__ import annotations

from pathlib import Path
import re
import secrets
import sqlite3

from .canonical import PolicyError, digest
from .canonical_floor import CanonicalFloorStore
from .evidence import EvidenceResolver, _owner
from .identity import (ATTESTED_CONTRACT, SELF, composition_revision, entries, identity_event_count,
    last_identity_event, literal_self_shadowed, permit_subjects, registry_ids, rekeyed_facts,
    restriction_subjects, self_entity_ids)
from .identity_protocol import (AttestIdentity, DescribeIdentity, IdentityEntryMetadata,
    IdentityState, IdentitySubject, RevokeIdentity)
from .protection_clock import (ATTESTATION_STATEMENT, LEDGER, REGISTRY, TABLE, clock_state)
from topos.storage.db.write_gate import with_db_write

MAX_SUBJECTS = 512


def _hash(value, code):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise PolicyError(code)
    return value


class IdentityAttestationService:
    """Owner-only. Never reachable from a recipient request or a policy."""

    def __init__(self, *, resolver: EvidenceResolver, floor: CanonicalFloorStore):
        self.resolver = resolver
        self.floor = floor

    # --- connections --------------------------------------------------------

    def _writable(self):
        try:
            conn = sqlite3.connect(self.resolver.path)
        except sqlite3.Error:
            raise PolicyError("identity_state_unavailable") from None
        # Explicit transaction control: BEGIN IMMEDIATE below must be the one
        # that opens the write, not a second one inside an implicit transaction.
        conn.isolation_level = None
        return conn

    # --- read ---------------------------------------------------------------

    def describe(self, request: DescribeIdentity) -> IdentityState:
        """What the owner's review surface shows. Reads only; installs nothing."""
        _owner(self.resolver.binding)
        DescribeIdentity.parse(request.model_dump())
        with self.resolver._read() as (conn, _floor):
            self.floor.check(conn)
            return self._state(conn)

    def _state(self, conn) -> IdentityState:
        _clock_id, generation = clock_state(conn)
        current = entries(conn)
        registry = {row[0]: row[1] for row in conn.execute(f"SELECT entity_id,basis FROM {REGISTRY}")}
        selves = self_entity_ids(conn)
        restrictions = restriction_subjects(conn)
        permits = permit_subjects(conn, contract=ATTESTED_CONTRACT)
        candidates = sorted((set(registry) | set(current) | selves) - {SELF})
        if len(candidates) > MAX_SUBJECTS:
            raise PolicyError("identity_state_unavailable")
        subjects = []
        has_entities = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entities' LIMIT 1").fetchone())
        for entity_id in candidates:
            row = conn.execute("SELECT entity_type,is_self,contact_id FROM entities WHERE entity_id=?",
                               (entity_id,)).fetchone() if has_entities else None
            entry = current.get(entity_id)
            subjects.append(IdentitySubject(
                entity_id=entity_id, exists=row is not None, is_self=bool(row and row[1] == 1),
                basis=registry.get(entity_id), entry_id=entry.entry_id if entry else None,
                entry_state=entry.state if entry else None, entry_reason=entry.reason if entry else None,
                entity_type=row[0] if row else None, contact_id=row[2] if row else None,
                composition_revision=composition_revision(conn, entity_id) if row else None,
                last_identity_event=last_identity_event(conn, entity_id),
                rekeyed_facts=self._rekeyed_for(conn, entity_id)))
        return IdentityState(version="topos-owner-identity-state/v1", contract=ATTESTED_CONTRACT,
            statement_version=ATTESTATION_STATEMENT, literal_self_shadowed=literal_self_shadowed(conn),
            generation=generation, subjects=subjects, permitted_count=len(permits),
            restricted_count=len(restrictions))

    @staticmethod
    def _rekeyed_for(conn, entity_id) -> int:
        """How many of this entity's current facts arrived by a subject rewrite."""
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_objects' "
                            "LIMIT 1").fetchone():
            return 0
        try:
            ids = [row[0] for row in conn.execute(
                "SELECT object_id FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL "
                "AND json_extract(payload_json,'$.subject_entity_id')=?", (entity_id,))]
        except sqlite3.Error:
            raise PolicyError("identity_state_unavailable") from None
        return len(rekeyed_facts(conn, ids))

    # --- write --------------------------------------------------------------

    def attest(self, request: AttestIdentity, *, command_id: str, command_hash: str) -> IdentityEntryMetadata:
        """Record that the owner confirmed this entity, exactly as they saw it."""
        # Re-parsed, so the exact sentence and statement version are enforced by
        # the contract itself; a hand-built model never reaches the ledger.
        request = AttestIdentity.parse(request.model_dump())

        def apply(conn):
            row = conn.execute("SELECT entity_type,is_self,contact_id FROM entities WHERE entity_id=?",
                               (request.entity_id,)).fetchone()
            if row is None:
                raise PolicyError("identity_subject_unknown")
            # What the owner had in front of them, or nothing. An entity that
            # moved between the describe and the confirmation is a different
            # statement, and the owner has not made it.
            if (row[0] != request.expected_entity_type or row[1] != request.expected_is_self
                or row[2] != request.expected_contact_id
                or composition_revision(conn, request.entity_id) != request.expected_composition_revision):
                raise PolicyError("identity_subject_moved")
            live = entries(conn).get(request.entity_id)
            if live is not None and live.state != "revoked":
                if request.replaces_entry_id != live.entry_id:
                    raise PolicyError("identity_attestation_conflict")
                self._append(conn, action="revoke", entity_id=request.entity_id, entry_id=self._entry_id(),
                             target_entry_id=live.entry_id, command_id=command_id + ":replace",
                             command_hash=command_hash)
            elif request.replaces_entry_id is not None:
                raise PolicyError("identity_attestation_conflict")
            entry_id = self._entry_id()
            self._append(conn, action="attest", entity_id=request.entity_id, entry_id=entry_id,
                         entity_type=row[0], is_self=row[1], contact_id=row[2],
                         composition=composition_revision(conn, request.entity_id),
                         command_id=command_id, command_hash=command_hash)
            return entry_id

        entry_id = self._commit(apply, command_id=command_id, command_hash=command_hash)
        with self.resolver._read() as (conn, _floor):
            entry = entries(conn).get(request.entity_id)
            if entry is None or entry.entry_id != entry_id:
                raise PolicyError("identity_state_unavailable")
            return IdentityEntryMetadata(entity_id=request.entity_id, entry_id=entry_id, state="active",
                                         generation=entry.generation)

    def revoke(self, request: RevokeIdentity, *, command_id: str, command_hash: str) -> IdentityEntryMetadata:
        """Withdraw a statement. Terminal: re-confirming mints a new entry."""
        request = RevokeIdentity.parse(request.model_dump())

        def apply(conn):
            live = entries(conn).get(request.entity_id)
            if live is None or live.state == "revoked" or live.entry_id != request.entry_id:
                raise PolicyError("identity_attestation_conflict")
            entry_id = self._entry_id()
            self._append(conn, action="revoke", entity_id=request.entity_id, entry_id=entry_id,
                         target_entry_id=request.entry_id, command_id=command_id, command_hash=command_hash)
            return entry_id

        self._commit(apply, command_id=command_id, command_hash=command_hash)
        with self.resolver._read() as (conn, _floor):
            entry = entries(conn).get(request.entity_id)
            if entry is None or entry.state != "revoked":
                raise PolicyError("identity_state_unavailable")
            return IdentityEntryMetadata(entity_id=request.entity_id, entry_id=request.entry_id,
                                         state="revoked", generation=entry.generation)

    # --- the shared write shape ---------------------------------------------

    @staticmethod
    def _entry_id() -> str:
        return "entry-" + secrets.token_hex(16)

    def _append(self, conn, *, action, entity_id, entry_id, command_id, command_hash,
                target_entry_id=None, entity_type=None, is_self=None, contact_id=None, composition=None):
        generation = conn.execute(f"SELECT generation FROM {TABLE} WHERE singleton=1").fetchone()[0]
        conn.execute(
            f"INSERT INTO {LEDGER}(entry_id,action,entity_id,target_entry_id,entity_type,is_self,contact_id,"
            "composition_revision,statement_version,command_id,command_hash,generation) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, action, entity_id, target_entry_id, entity_type, is_self, contact_id, composition,
             ATTESTATION_STATEMENT, command_id, command_hash, generation + 1))

    def _commit(self, apply, *, command_id: str, command_hash: str):
        """One owner, one command, one transaction, one floor publication.

        The command is burned by the ledger's own uniqueness constraint rather
        than by a separate table, so a replay cannot be accepted and then found
        to be a duplicate afterwards.
        """
        _owner(self.resolver.binding)
        if type(command_id) is not str or not 0 < len(command_id) <= 180:
            raise PolicyError("identity_command_invalid")
        _hash(command_hash, "identity_command_invalid")
        with with_db_write():
            conn = self._writable()
            try:
                conn.execute("BEGIN IMMEDIATE")
                clock_state(conn)
                pending = self.floor.publish_pending(conn)
                try:
                    result = apply(conn)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    # Nothing was written, so the floor may reopen at exactly
                    # the ledger it was pinned to. If that is not true any more,
                    # abort_pending refuses and the node stays closed.
                    self.floor.abort_pending(conn)
                    raise
                self.floor.publish_active(conn)
                return result
            except sqlite3.IntegrityError as exc:
                raise PolicyError("identity_attestation_conflict"
                                  if "already active" in str(exc) or "not the current attestation" in str(exc)
                                  else "identity_command_replayed") from None
            except sqlite3.Error:
                raise PolicyError("identity_state_unavailable") from None
            finally:
                conn.close()

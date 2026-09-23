"""Node-local P2a effective policy, epoch, replay and decision receipt ledger.

All mutations serialize via SQLite IMMEDIATE transactions. No environment DB,
network, model, public telemetry, or existing query route is consulted here.
Receipts are private checkpoints, never disclosure authorization tokens.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from topos.principal import OWNER_APP, current_principal
from topos.storage.db.write_gate import with_db_write

from .canonical import MAX_INTEGER, PolicyError, canonical_bytes, digest, parse_json
from .contract import Hash, Identifier, Number, Only, StrictModel
from .registry import Policy, PolicyDecision, Disclosure, parse_policy, parse_decision, parse_disclosure
from .fact_contract import FactPolicyV2
from .ledger_retention import EXPIRY_INDEX, compact_expired
from .signing import (AnyAuthorityBinding, AnyRequestContext, parse_authority,
    parse_envelope, parse_request_context, verify_current_signature, verify_envelope)


class NodeIdentity(StrictModel):
    environment_id: Identifier
    node_id: Identifier
    resource_id: Identifier
    owner_id: Identifier


class Lease(StrictModel):
    request_id: Identifier
    envelope_hash: Hash
    node_epoch: Number


class Admission:
    """One verified delivery of one envelope, with nothing written for it yet.

    `verify` builds it -- request binding, signature, authority, policy time -- and
    writes no row, so a door can run its floors before the node pays for the
    envelope. Exactly one of `admit_verified` and `refuse` then claims the request
    id: the `SELECT` and the `INSERT` under the primary key, inside one
    `BEGIN IMMEDIATE`, before any response leaves. That is where replay protection
    has always lived and it has not moved; what E2 changes is what the row costs
    when the floors say no (`BOOKKEEPING_BATCH_4.md` §3).

    `encoded` is exactly the envelope `admit_verified` would store, so a refusal
    checkpoint reads the same bytes from memory that a permit reads from the row.
    `status` is this delivery's own record of which claim it made; a door may
    therefore call `refuse` again from an outer handler and get a no-op rather than
    a second row or a `request_replay` over its own.
    """
    __slots__ = ("request", "envelope", "encoded", "lease", "status")

    def __init__(self, *, request, envelope, encoded: str, lease: Lease):
        self.request, self.envelope, self.encoded, self.lease = request, envelope, encoded, lease
        self.status: str | None = None


_DDL = (
    "CREATE TABLE IF NOT EXISTS p2a_node (singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity_json TEXT NOT NULL, epoch INTEGER NOT NULL, protection_revision TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS p2a_policies (version_id TEXT PRIMARY KEY, policy_hash TEXT NOT NULL, policy_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS p2a_grants (grant_id TEXT PRIMARY KEY, assignment_id TEXT UNIQUE NOT NULL, grant_generation INTEGER NOT NULL, assignment_generation INTEGER NOT NULL, version_id TEXT NOT NULL, active INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS p2a_mutations (command_id TEXT PRIMARY KEY, command_hash TEXT NOT NULL, result_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS p2a_requests (request_id TEXT PRIMARY KEY, envelope_hash TEXT NOT NULL, envelope_json TEXT NOT NULL, status TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS p2a_receipts (request_id TEXT PRIMARY KEY, receipt_json TEXT NOT NULL, decision_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS p2a_grant_bindings (grant_id TEXT PRIMARY KEY, binding_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS p2a_grant_authorities (grant_id TEXT PRIMARY KEY, authority_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS p2a_policy_commitments (version_id TEXT PRIMARY KEY, policy_hash TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS p2a_protocol_commands (command_id TEXT PRIMARY KEY, command_hash TEXT NOT NULL, receipt_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS p2a_protection_observation (singleton INTEGER PRIMARY KEY CHECK(singleton=1), clock_id TEXT NOT NULL, generation INTEGER NOT NULL)",
    # The external canonical floor, mirrored so that deleting the floor file is
    # a rollback rather than a fresh install. Present only on a node that has
    # ever had one; absent on a node that never enabled identity attestations.
    "CREATE TABLE IF NOT EXISTS p2a_canonical_floor (singleton INTEGER PRIMARY KEY CHECK(singleton=1), clock_id TEXT NOT NULL, revision INTEGER NOT NULL, floor_digest TEXT NOT NULL)",
    # Finds the envelopes ledger_retention may drop; on the ledger's own schema, never a canonical one.
    EXPIRY_INDEX,
)


class PolicyLedger:
    def __init__(self, path: Path, *, identity: NodeIdentity, protection_revision: str, trusted_keys: Mapping[str, bytes]):
        self.path = Path(path)
        self.identity = NodeIdentity.parse(identity.model_dump())
        self.trusted_keys = dict(trusted_keys)
        self._validate_revision(protection_revision)
        # Caller supplies an explicitly isolated node-private file, never a
        # default application DB. Creation permissions precede SQLite writes.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._transaction() as conn:
            for sql in _DDL:
                conn.execute(sql)
            row = conn.execute("SELECT * FROM p2a_node WHERE singleton=1").fetchone()
            identity_json = canonical_bytes(self.identity.model_dump()).decode("ascii")
            if row is None:
                conn.execute("INSERT INTO p2a_node VALUES (1, ?, 0, ?)", (identity_json, protection_revision))
            elif row["identity_json"] != identity_json:
                raise PolicyError("ledger_identity")
            elif row["protection_revision"] != protection_revision:
                raise PolicyError("ledger_protection_revision")

    @contextmanager
    def _transaction(self):
        with with_db_write():
            conn = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def _validate_revision(revision: str) -> None:
        class Revision(StrictModel):
            value: Hash
        Revision.parse({"value": revision})

    def _owner(self) -> None:
        principal = current_principal()
        if principal is None or principal.cls != OWNER_APP:
            raise PolicyError("owner_required")
        if principal.channel not in {"uds", "cp_relay"}:
            raise PolicyError("owner_channel")
        if (principal.acting_user and principal.acting_user != self.identity.owner_id) or (principal.channel == "cp_relay" and principal.acting_user != self.identity.owner_id):
            raise PolicyError("owner_binding")

    @staticmethod
    def _integer(value: int) -> None:
        if type(value) is not int or not 0 <= value <= MAX_INTEGER:
            raise PolicyError("integer_invalid")

    @staticmethod
    def _node(conn):
        return conn.execute("SELECT * FROM p2a_node WHERE singleton=1").fetchone()

    def _cas(self, conn, expected_epoch: int):
        self._integer(expected_epoch)
        node = self._node(conn)
        if node["epoch"] != expected_epoch:
            raise PolicyError("epoch_conflict")
        if expected_epoch == MAX_INTEGER:
            raise PolicyError("epoch_exhausted")
        return node

    @staticmethod
    def _retry(conn, command_id: str, body: dict) -> dict | None:
        class CommandId(StrictModel):
            value: Identifier
        CommandId.parse({"value": command_id})
        row = conn.execute("SELECT * FROM p2a_mutations WHERE command_id=?", (command_id,)).fetchone()
        if row:
            if row["command_hash"] != digest(body):
                raise PolicyError("idempotency_conflict")
            return parse_json(row["result_json"])
        return None

    @staticmethod
    def _mutated(conn, command_id: str, body: dict, expected_epoch: int) -> dict:
        result = {"applied_epoch": expected_epoch + 1, "command_id": command_id}
        # The WHERE is deliberately retained even though IMMEDIATE serializes.
        changed = conn.execute("UPDATE p2a_node SET epoch=? WHERE singleton=1 AND epoch=?", (expected_epoch + 1, expected_epoch)).rowcount
        if changed != 1:
            raise PolicyError("epoch_conflict")
        conn.execute("INSERT INTO p2a_mutations VALUES (?, ?, ?)", (command_id, digest(body), canonical_bytes(result).decode("ascii")))
        return result

    def activate(self, raw_policy: bytes | str | dict, *, grant_generation: int, assignment_generation: int, expected_epoch: int, command_id: str, now: int) -> dict:
        """Owner-only local coordination hook; grantee signatures cannot activate.

        P2a supports one immutable assignment identity per grant. Every update
        advances both generations; revocation retains the tombstone. A future
        CP synchronization envelope must use a distinct signature domain.
        """
        self._owner()
        policy = parse_policy(raw_policy)
        self._integer(now)
        for generation in (grant_generation, assignment_generation):
            self._integer(generation)
            if generation == 0:
                raise PolicyError("generation_invalid")
        for key, value in self.identity.model_dump().items():
            if getattr(policy.binding, key) != value:
                raise PolicyError("policy_binding")
        body = {"operation": "activate", "policy": policy.model_dump(), "grant_generation": grant_generation, "assignment_generation": assignment_generation, "expected_epoch": expected_epoch}
        with self._transaction() as conn:
            previous = self._retry(conn, command_id, body)
            if previous is not None:
                return previous
            self._cas(conn, expected_epoch)
            if not policy.validity.starts_at <= now < policy.validity.expires_at:
                raise PolicyError("policy_time")
            old = conn.execute("SELECT * FROM p2a_grants WHERE grant_id=?", (policy.binding.grant_id,)).fetchone()
            other = conn.execute("SELECT grant_id FROM p2a_grants WHERE assignment_id=?", (policy.binding.assignment_id,)).fetchone()
            if other and other["grant_id"] != policy.binding.grant_id:
                raise PolicyError("assignment_binding")
            if old:
                if self._grant_capability(conn, old) != policy.versions.capability:
                    raise PolicyError("capability_change_requires_new_grant")
                if old["assignment_id"] != policy.binding.assignment_id:
                    raise PolicyError("assignment_binding")
                if grant_generation <= old["grant_generation"] or assignment_generation <= old["assignment_generation"]:
                    raise PolicyError("generation_stale")
                old_binding = conn.execute("SELECT binding_json FROM p2a_grant_bindings WHERE grant_id=?", (policy.binding.grant_id,)).fetchone()
                bound = parse_json(old_binding["binding_json"]) if old_binding else self._policy(conn, old["version_id"]).binding.model_dump()
                if bound != policy.binding.model_dump():
                    raise PolicyError("policy_binding")
            elif grant_generation != 1 or assignment_generation != 1:
                raise PolicyError("initial_generation")
            encoded = canonical_bytes(policy.model_dump()).decode("ascii")
            hashed = digest(policy.model_dump())
            immutable = conn.execute("SELECT policy_hash FROM p2a_policies WHERE version_id=?", (policy.policy_version_id,)).fetchone()
            if immutable and immutable["policy_hash"] != hashed:
                raise PolicyError("immutable_policy")
            committed = conn.execute("SELECT policy_hash FROM p2a_policy_commitments WHERE version_id=?", (policy.policy_version_id,)).fetchone()
            if committed and committed["policy_hash"] != hashed:
                raise PolicyError("immutable_policy")
            conn.execute("INSERT OR IGNORE INTO p2a_policies VALUES (?, ?, ?)", (policy.policy_version_id, hashed, encoded))
            conn.execute("INSERT OR IGNORE INTO p2a_policy_commitments VALUES (?, ?)", (policy.policy_version_id, hashed))
            conn.execute("INSERT OR IGNORE INTO p2a_grant_bindings VALUES (?, ?)", (policy.binding.grant_id, canonical_bytes(policy.binding.model_dump()).decode("ascii")))
            conn.execute("INSERT INTO p2a_grants VALUES (?, ?, ?, ?, ?, 1) ON CONFLICT(grant_id) DO UPDATE SET grant_generation=excluded.grant_generation, assignment_generation=excluded.assignment_generation, version_id=excluded.version_id, active=1", (policy.binding.grant_id, policy.binding.assignment_id, grant_generation, assignment_generation, policy.policy_version_id))
            return self._mutated(conn, command_id, body, expected_epoch)

    def revoke(self, grant_id: str, *, expected_epoch: int, command_id: str) -> dict:
        self._owner()
        body = {"operation": "revoke", "grant_id": grant_id, "expected_epoch": expected_epoch}
        with self._transaction() as conn:
            previous = self._retry(conn, command_id, body)
            if previous is not None:
                return previous
            self._cas(conn, expected_epoch)
            row = conn.execute("SELECT * FROM p2a_grants WHERE grant_id=?", (grant_id,)).fetchone()
            if row is None:
                raise PolicyError("grant_unknown")
            if max(row["grant_generation"], row["assignment_generation"]) == MAX_INTEGER:
                raise PolicyError("generation_exhausted")
            conn.execute("UPDATE p2a_grants SET active=0, grant_generation=grant_generation+1, assignment_generation=assignment_generation+1 WHERE grant_id=?", (grant_id,))
            return self._mutated(conn, command_id, body, expected_epoch)

    def update_protection(self, revision: str, *, expected_epoch: int, command_id: str) -> dict:
        self._owner()
        self._validate_revision(revision)
        body = {"operation": "protection", "revision": revision, "expected_epoch": expected_epoch}
        with self._transaction() as conn:
            previous = self._retry(conn, command_id, body)
            if previous is not None:
                return previous
            self._cas(conn, expected_epoch)
            conn.execute("UPDATE p2a_node SET protection_revision=? WHERE singleton=1", (revision,))
            return self._mutated(conn, command_id, body, expected_epoch)

    def _grant_capability(self, conn, grant) -> str:
        saved = conn.execute("SELECT authority_json FROM p2a_grant_authorities WHERE grant_id=?", (grant["grant_id"],)).fetchone()
        if saved:
            authority = parse_authority(saved["authority_json"])
            if authority.policy_version_id == grant["version_id"]:
                return authority.capability_version
        return self._policy(conn, grant["version_id"]).versions.capability

    @staticmethod
    def _policy(conn, version_id: str) -> Policy:
        row = conn.execute("SELECT * FROM p2a_policies WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise PolicyError("policy_unknown")
        policy = parse_policy(row["policy_json"])
        if digest(policy.model_dump()) != row["policy_hash"]:
            raise PolicyError("policy_integrity")
        return policy

    def _authority(self, conn, grant_id: str, now: int) -> tuple[AnyAuthorityBinding, Policy]:
        self._integer(now)
        grant = conn.execute("SELECT * FROM p2a_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if grant is None or not grant["active"]:
            raise PolicyError("grant_inactive")
        policy = self._policy(conn, grant["version_id"])
        if not policy.validity.starts_at <= now < policy.validity.expires_at:
            raise PolicyError("policy_time")
        node = self._node(conn)
        authority = parse_authority({**policy.binding.model_dump(), "grant_generation": grant["grant_generation"], "assignment_generation": grant["assignment_generation"], "policy_version_id": policy.policy_version_id, "policy_hash": digest(policy.model_dump()), "capability_version": policy.versions.capability, "protection_revision": node["protection_revision"], "node_epoch": node["epoch"]})
        return authority, policy

    def authority_snapshot(self, grant_id: str, *, now: int) -> AnyAuthorityBinding:
        """Owner-only local signer coordination. Never a public token endpoint."""
        self._owner()
        with self._transaction() as conn:
            authority, _ = self._authority(conn, grant_id, now)
            return authority

    def _bound_request(self, request: AnyRequestContext) -> AnyRequestContext:
        # Reparse trusted typed inputs against accidental mutation too.
        request = parse_request_context(request)
        if any(getattr(request, key) != value for key, value in self.identity.model_dump().items()):
            raise PolicyError("request_binding")
        return request

    def _verify(self, conn, raw_envelope: bytes | str | dict, *, request: AnyRequestContext, payload: Any,
                now: int) -> Admission:
        """Signature, authority and time. Reads the ledger, writes nothing to it."""
        authority, policy = self._authority(conn, request.grant_id, now)
        envelope = verify_envelope(raw_envelope, trusted_keys=self.trusted_keys, expected_authority=authority, request=request, payload=payload, now=now)
        if (envelope.issued_at < policy.validity.starts_at
            or envelope.expires_at > policy.validity.expires_at):
            raise PolicyError("envelope_policy_time")
        if conn.execute("SELECT 1 FROM p2a_requests WHERE request_id=?", (request.request_id,)).fetchone():
            # Early and NOT authoritative: the claim's own SELECT and INSERT under the
            # primary key are still the decision, and two deliveries that pass here
            # still leave exactly one row. This is here so a replay is turned away
            # before the floors read a row, as it was when the claim happened here --
            # and so every door answers `request_replay` for one, not the uniform
            # refusal its floors would have raised on the way past.
            raise PolicyError("request_replay")
        encoded = canonical_bytes(envelope.model_dump()).decode("ascii")
        envelope_hash = digest(envelope.model_dump())
        return Admission(request=request, envelope=envelope, encoded=encoded,
                         lease=Lease(request_id=request.request_id, envelope_hash=envelope_hash,
                                     node_epoch=envelope.node_epoch))

    def _claim(self, conn, admission: Admission, *, envelope_json: str, status: str, now: int) -> Lease:
        """Take the request id for this envelope, once, under the primary key."""
        if conn.execute("SELECT 1 FROM p2a_requests WHERE request_id=?", (admission.lease.request_id,)).fetchone():
            raise PolicyError("request_replay")
        conn.execute("INSERT INTO p2a_requests VALUES (?, ?, ?, ?)",
                     (admission.lease.request_id, admission.lease.envelope_hash, envelope_json, status))
        compact_expired(conn, now=now)
        return admission.lease

    def admit(self, raw_envelope: bytes | str | dict, *, request: AnyRequestContext, payload: Any, now: int) -> Lease:
        """Verify and claim in one transaction: the form for a caller with no floors of its own."""
        request = self._bound_request(request)
        with self._transaction() as conn:
            admission = self._verify(conn, raw_envelope, request=request, payload=payload, now=now)
            lease = self._claim(conn, admission, envelope_json=admission.encoded, status="admitted", now=now)
        admission.status = "admitted"
        return lease

    def verify(self, raw_envelope: bytes | str | dict, *, request: AnyRequestContext, payload: Any,
               now: int) -> Admission:
        """`admit` without the write, so a door can run its floors before the node stores 2.8 KB.

        The id is still unclaimed when this returns, so the caller MUST claim it --
        `admit_verified` for a read that reaches release, `refuse` for one the floors
        turn away, and `refuse` again from a handler for any other exit -- before a
        response leaves. Between the two calls the effective authority may move; a
        stale one is caught at the checkpoint, which re-reads it, so the split is
        fail-closed rather than atomic.
        """
        request = self._bound_request(request)
        with self._transaction() as conn:
            return self._verify(conn, raw_envelope, request=request, payload=payload, now=now)

    def admit_verified(self, admission: Admission, *, now: int) -> Lease:
        """Claim the id for a read that reached release: the whole envelope, `admitted`."""
        with self._transaction() as conn:
            lease = self._claim(conn, admission, envelope_json=admission.encoded, status="admitted", now=now)
        admission.status = "admitted"
        return lease

    def refuse(self, admission: Admission, raw_decision: bytes | str | dict | None = None, *,
               candidate_revision: str | None = None, members: list | None = None, now: int) -> dict | None:
        """Claim the id for a read the floors refused: the tombstone, and the deny receipt.

        The row is `(request_id, envelope_hash, '', 'refused')`, the shape
        `ledger_retention.compact_expired` leaves behind -- about 120 bytes where an
        admitted envelope is ~2.8 KB. The id is burnt exactly as `admitted` burns it,
        so a duplicate delivery still loses at the primary key, and the row is
        terminal: `checkpoint_decision` refuses any status but `admitted`, so a
        refused read replayed after a protection change cannot become a permitted
        one. The receipt is the owner's audit trail and is written here byte for byte
        as the checkpoint wrote it before the split.

        Returns None when this delivery has already claimed its id, so a door may
        call this from an outer handler without writing a second row or raising over
        its own first one. With no decision it writes the tombstone alone, which is
        what a door that failed before deciding used to leave behind.
        """
        if admission.status is not None:
            return None
        with self._transaction() as conn:
            self._claim(conn, admission, envelope_json="", status="refused", now=now)
            receipt = None
            if raw_decision is not None:
                receipt = self._checkpoint(conn, envelope=admission.envelope, lease=admission.lease,
                                           raw_decision=raw_decision, candidate_revision=candidate_revision,
                                           output=None, members=members, now=now)
        admission.status = "refused"
        return receipt

    @staticmethod
    def _permit_shape(policy: Policy, decision: PolicyDecision, output: Disclosure) -> None:
        # A single clause's evidence and release tuple must authorize this
        # output shape. This is necessary but not sufficient for disclosure:
        # actual predicate, provenance and Off-limits proof await an adapter.
        if len(decision.matched_allow_clause_ids) != 1 or decision.matched_deny_clause_ids or decision.missing_context_codes or decision.reason_code != "rule_permit":
            raise PolicyError("decision_inconsistent")
        rule = next((rule for rule in policy.rules if rule.rule_id == decision.matched_allow_clause_ids[0]), None)
        ceilings = {"summary", "raw"} if isinstance(policy, FactPolicyV2) else {"raw"}
        if rule is None or rule.effect != "permit" or rule.release.ceiling not in ceilings or "owner-engine-local" not in rule.evidence_use.processors.values:
            raise PolicyError("rule_binding")
        sources = rule.evidence_use.sources.values if isinstance(rule.evidence_use.sources, Only) else policy.source_universe.source_ids
        if isinstance(policy, FactPolicyV2):
            if "signal_objects" not in rule.evidence_use.tables or not (set(rule.evidence_use.tables) & {"conversation_messages", "ai_chat_messages"}):
                raise PolicyError("rule_binding")
        else:
            for record in output.records:
                if record.source_id not in sources or not any(record.canonical_table in form.tables for form in rule.release.forms):
                    raise PolicyError("rule_binding")
        if not rule.release.forms or not sources:
            raise PolicyError("rule_binding")

    def _checkpoint(self, conn, *, envelope, lease: Lease, raw_decision: bytes | str | dict, candidate_revision: str,
                    output: dict | None, members: list | None, now: int) -> dict:
        """Bind one decision to one envelope and write the one receipt for it.

        The body every door's checkpoint shares, in one place, so the refusal path
        E2 added cannot drift from the release path: the epoch and expiry check, the
        current-signature check, the authority equality, the decision binding, the
        shape check, and the receipt. It writes no request row; the caller decides
        whether the id was claimed as `admitted` (and is now `checkpointed`) or as
        the `refused` tombstone.

        `envelope` is whatever the caller verified -- the row's bytes on the release
        path, the admission's identical bytes when the floors refused before the row
        was ever paid for -- and the set shape follows the capability that was
        signed, never the caller's word for it.
        """
        from .search_contract import CAPABILITY_SEARCH
        self._validate_revision(candidate_revision)
        set_level = envelope.capability_version == CAPABILITY_SEARCH
        members = list(members or [])
        decision = parse_decision(raw_decision, capability=envelope.capability_version)
        if envelope.node_epoch != lease.node_epoch or envelope.expires_at <= now or envelope.issued_at > now:
            raise PolicyError("lease_expired")
        verify_current_signature(envelope, trusted_keys=self.trusted_keys, now=now)
        authority, policy = self._authority(conn, envelope.grant_id, now)
        if any(getattr(envelope, key) != value for key, value in authority.model_dump().items()):
            raise PolicyError("authority_stale")
        if decision.policy_hash != authority.policy_hash or decision.candidate_revision != candidate_revision or decision.stage != "output_release":
            raise PolicyError("decision_binding")
        output_hash = None
        if decision.verdict == "permit":
            if output is None:
                raise PolicyError("projection_required")
            parsed_output = parse_disclosure(output, capability=envelope.capability_version)
            if set_level:
                self._search_shape(policy, decision, parsed_output, members)
            else:
                if decision.required_projection_id != parsed_output.view_id:
                    raise PolicyError("projection_required")
                self._permit_shape(policy, decision, parsed_output)
            output_hash = digest(parsed_output.model_dump())
        elif output is not None or members:
            raise PolicyError("denied_output")
        receipt = {"version": "topos-local-receipt/v2", "request_id": lease.request_id, "envelope_hash": lease.envelope_hash, "policy_hash": authority.policy_hash, "node_epoch": authority.node_epoch, "protection_revision": authority.protection_revision, "candidate_revision": candidate_revision, "decision_hash": digest(decision.model_dump()), "output_hash": output_hash, "verdict": decision.verdict, "checked_at": now, "execution_enabled": False}
        if set_level:
            receipt = {**receipt, "version": "topos-local-receipt/v3", "record_count": len(members),
                       "members_digest": digest([m if isinstance(m, dict) else m.model_dump() for m in members])}
        conn.execute("INSERT INTO p2a_receipts VALUES (?, ?, ?)", (lease.request_id, canonical_bytes(receipt).decode("ascii"), canonical_bytes(decision.model_dump()).decode("ascii")))
        return receipt

    def _leased_envelope(self, conn, lease: Lease):
        """The envelope of an admitted, not yet checkpointed request. Never a tombstone's."""
        row = conn.execute("SELECT * FROM p2a_requests WHERE request_id=?", (lease.request_id,)).fetchone()
        if row is None or row["envelope_hash"] != lease.envelope_hash:
            raise PolicyError("lease_unknown")
        if row["status"] != "admitted":
            # `checkpointed` is a second checkpoint; `refused` is a read the floors
            # turned away, whose receipt `refuse` already wrote inside the same
            # transaction that wrote its tombstone. Neither may be checkpointed here,
            # which is what stops a refused read becoming a permitted one.
            raise PolicyError("request_replay")
        return parse_envelope(row["envelope_json"])

    def checkpoint_decision(self, lease: Lease, raw_decision: bytes | str | dict, *, candidate_revision: str, output: dict | None, now: int) -> dict:
        """Atomic final epoch check and PRIVATE receipt hook, no data release.

        A future adapter must prove predicates/lineage and perform this check
        immediately before its transport release. No adapter is enabled in
        P2a. This method returns no candidate content or reusable permit.
        """
        self._validate_revision(candidate_revision)
        lease = Lease.parse(lease.model_dump())
        with self._transaction() as conn:
            envelope = self._leased_envelope(conn, lease)
            if envelope.capability_version == "permissions-beta/p2c-v1":
                raise PolicyError("unsupported_capability")  # a search is checkpointed only as a set
            receipt = self._checkpoint(conn, envelope=envelope, lease=lease, raw_decision=raw_decision,
                                       candidate_revision=candidate_revision, output=output, members=None, now=now)
            conn.execute("UPDATE p2a_requests SET status='checkpointed' WHERE request_id=?", (lease.request_id,))
            return receipt

    # -- p2c-v1: one set-level decision per search (additive; the methods above are unchanged) --

    @staticmethod
    def _search_shape(policy, decision, output, members) -> None:
        """Every released record is covered by its own member's permit rule, as p2a's shape check requires per read."""
        from .search_contract import SearchMemberBinding, SearchPolicy, VIEW_SEARCH
        if not isinstance(policy, SearchPolicy) or decision.required_projection_id != VIEW_SEARCH:
            raise PolicyError("rule_binding")
        members = [SearchMemberBinding.parse(member if isinstance(member, dict) else member.model_dump()) for member in members]
        if len(members) != len(output.records) or decision.member_count != len(members):
            raise PolicyError("decision_inconsistent")
        if decision.matched_allow_clause_ids != sorted({member.allow_clause_id for member in members}):
            raise PolicyError("decision_inconsistent")
        for member, record in zip(members, output.records):
            rule = next((rule for rule in policy.rules if rule.rule_id == member.allow_clause_id), None)
            if (rule is None or rule.effect != "permit" or rule.release.ceiling != "raw"
                or "owner-engine-local" not in rule.evidence_use.processors.values):
                raise PolicyError("rule_binding")
            sources = rule.evidence_use.sources.values if isinstance(rule.evidence_use.sources, Only) else policy.source_universe.source_ids
            if (member.source_id != record.source_id or member.table != record.canonical_table
                or record.source_id not in sources or record.canonical_table not in policy.search.tables
                or not any(record.canonical_table in form.tables for form in rule.release.forms)):
                raise PolicyError("rule_binding")

    def checkpoint_set_decision(self, lease: Lease, raw_decision, *, candidate_revision: str, output: dict | None,
                                members: list, now: int) -> dict:
        """p2c-v1's private checkpoint: one decision and one receipt (v3) for the whole result set."""
        from .search_contract import CAPABILITY_SEARCH
        self._validate_revision(candidate_revision)
        lease = Lease.parse(lease.model_dump())
        with self._transaction() as conn:
            envelope = self._leased_envelope(conn, lease)
            if envelope.capability_version != CAPABILITY_SEARCH:
                raise PolicyError("unsupported_capability")
            receipt = self._checkpoint(conn, envelope=envelope, lease=lease, raw_decision=raw_decision,
                                       candidate_revision=candidate_revision, output=output, members=members, now=now)
            conn.execute("UPDATE p2a_requests SET status='checkpointed' WHERE request_id=?", (lease.request_id,))
            return receipt

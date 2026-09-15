"""Node-side authenticated policy synchronization, still no data execution.

Canonical protection is read from the real node DB under the same write gate as
record/entity protection mutations. Before reporting effective state, a changed
floor advances the ledger epoch. No caller-supplied fingerprint is authoritative.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from topos.storage.db.write_gate import with_db_write

from .canonical import MAX_INTEGER, PolicyError, canonical_bytes, digest, parse_json
from .contract import Binding
from .ledger import PolicyLedger
from .protocol import AckBody, AppliedCommandReceipt, NodeGrantState, SignedAck, SignedMutation, SignedStatusRequest, binding_of, command_digest, sign_ack, verify_mutation, verify_status_request
from .signing import AuthorityBinding
from .protection_clock import clock_state, current_protection_revision, ensure_protection_clock


class NodePolicyProtocol:
    def __init__(self, ledger: PolicyLedger, *, canonical_database: Path, cp_issuer_id: str, frontend_client_id: str, trusted_cp_keys: Mapping[str, bytes], node_signing_kid: str, node_signing_key: Ed25519PrivateKey):
        self.ledger = ledger
        self.canonical_database = Path(canonical_database).resolve(strict=True)
        if self.canonical_database == ledger.path.resolve() or not self.canonical_database.is_file():
            raise PolicyError("canonical_database_invalid")
        self.cp_issuer_id = cp_issuer_id
        self.frontend_client_id = frontend_client_id
        self.trusted_cp_keys = dict(trusted_cp_keys)
        self.node_signing_kid = node_signing_kid
        self.node_signing_key = node_signing_key
        with ledger._transaction() as conn:
            previous = conn.execute("SELECT * FROM p2a_protection_observation WHERE singleton=1").fetchone()
            ensure_protection_clock(self.canonical_database, owner_id=ledger.identity.owner_id, allow_install=previous is None)
            with sqlite3.connect(self.canonical_database.as_uri() + "?mode=ro", uri=True) as canonical:
                clock_id, generation = clock_state(canonical)
            if previous is not None and (previous["clock_id"] != clock_id or previous["generation"] > generation):
                raise PolicyError("protection_clock_rollback")
            conn.execute("INSERT OR IGNORE INTO p2a_protection_observation VALUES (1,?,?)", (clock_id, generation))

    def _protection_revision(self, ledger_conn) -> str:
        # This process must share the write gate with the actual owner controls.
        # A fresh read avoids an old cached connection's transaction snapshot.
        with sqlite3.connect(self.canonical_database.as_uri() + "?mode=ro", uri=True) as canonical:
            canonical.execute("BEGIN")
            revision = current_protection_revision(canonical, owner_id=self.ledger.identity.owner_id)
            clock_id, generation = clock_state(canonical)
            previous = ledger_conn.execute("SELECT * FROM p2a_protection_observation WHERE singleton=1").fetchone()
            if previous is None or previous["clock_id"] != clock_id or previous["generation"] > generation:
                raise PolicyError("protection_clock_rollback")
            ledger_conn.execute("UPDATE p2a_protection_observation SET generation=? WHERE singleton=1", (generation,))
            return revision

    def _sync_protection(self, conn) -> None:
        actual = self._protection_revision(conn)
        node = self.ledger._node(conn)
        if actual != node["protection_revision"]:
            if node["epoch"] == MAX_INTEGER:
                raise PolicyError("epoch_exhausted")
            conn.execute("UPDATE p2a_node SET protection_revision=?, epoch=epoch+1 WHERE singleton=1 AND epoch=?", (actual, node["epoch"]))

    def _bound_grant(self, conn, binding: Binding):
        grant = conn.execute("SELECT * FROM p2a_grants WHERE grant_id=?", (binding.grant_id,)).fetchone()
        if grant is not None:
            row = conn.execute("SELECT binding_json FROM p2a_grant_bindings WHERE grant_id=?", (binding.grant_id,)).fetchone()
            old_binding = parse_json(row["binding_json"]) if row else self.ledger._policy(conn, grant["version_id"]).binding.model_dump()
            if old_binding != binding.model_dump():
                raise PolicyError("binding_conflict")
        other = conn.execute("SELECT grant_id FROM p2a_grants WHERE assignment_id=?", (binding.assignment_id,)).fetchone()
        if other and other["grant_id"] != binding.grant_id:
            raise PolicyError("binding_conflict")
        return grant

    def _state(self, conn, binding: Binding) -> NodeGrantState:
        grant = self._bound_grant(conn, binding)
        node = self.ledger._node(conn)
        authority = None
        if grant:
            saved = conn.execute("SELECT authority_json FROM p2a_grant_authorities WHERE grant_id=?", (binding.grant_id,)).fetchone()
            if saved and parse_json(saved["authority_json"])["policy_version_id"] == grant["version_id"]:
                authority_body = parse_json(saved["authority_json"])
            else:
                policy = self.ledger._policy(conn, grant["version_id"])
                authority_body = {**policy.binding.model_dump(), "policy_version_id": policy.policy_version_id, "policy_hash": digest(policy.model_dump()), "capability_version": policy.versions.capability}
            authority_body.update(grant_generation=grant["grant_generation"], assignment_generation=grant["assignment_generation"], node_epoch=node["epoch"], protection_revision=node["protection_revision"])
            authority = AuthorityBinding.parse(authority_body)
        return NodeGrantState.parse({"identity": self.ledger.identity.model_dump(), "node_epoch": node["epoch"], "protection_revision": node["protection_revision"], "grant_state": "absent" if grant is None else "active" if grant["active"] else "revoked", "authority": authority.model_dump() if authority else None})

    @staticmethod
    def _receipt(conn, command_id: str | None, command_hash: str | None) -> AppliedCommandReceipt | None:
        if command_id is None:
            return None
        row = conn.execute("SELECT * FROM p2a_protocol_commands WHERE command_id=?", (command_id,)).fetchone()
        if row is None:
            return None
        if row["command_hash"] != command_hash:
            raise PolicyError("command_conflict")
        return AppliedCommandReceipt.parse(row["receipt_json"])

    def _apply(self, conn, command: SignedMutation, now: int) -> tuple[str, AppliedCommandReceipt]:
        hashed = command_digest(command)
        previous = self._receipt(conn, command.command_id, hashed)
        if previous is not None:
            return "already_applied", previous
        if conn.execute("SELECT 1 FROM p2a_mutations WHERE command_id=?", (command.command_id,)).fetchone():
            raise PolicyError("command_conflict")
        current = self.ledger._cas(conn, command.expected_epoch)
        target = command.authority
        if target.protection_revision != current["protection_revision"]:
            raise PolicyError("protection_changed")
        binding = binding_of(target)
        grant = self._bound_grant(conn, binding)
        if grant:
            if target.grant_generation <= grant["grant_generation"] or target.assignment_generation <= grant["assignment_generation"]:
                raise PolicyError("generation_stale")
        elif command.operation == "activate":
            if target.grant_generation != 1 or target.assignment_generation != 1:
                raise PolicyError("generation_stale")
        elif min(target.grant_generation, target.assignment_generation) < 2:
            # Revoke-before-activate tombstones cancel an intended generation1.
            raise PolicyError("generation_stale")
        for table in ("p2a_policy_commitments", "p2a_policies"):
            old = conn.execute(f"SELECT policy_hash FROM {table} WHERE version_id=?", (target.policy_version_id,)).fetchone()
            if old and old["policy_hash"] != target.policy_hash:
                raise PolicyError("immutable_policy")
        if command.operation == "activate":
            policy = command.policy  # Required and hash/binding checked by schema.
            if not policy.validity.starts_at <= now < policy.validity.expires_at:
                raise PolicyError("policy_time")
            conn.execute("INSERT OR IGNORE INTO p2a_policies VALUES (?, ?, ?)", (target.policy_version_id, target.policy_hash, canonical_bytes(policy.model_dump()).decode("ascii")))
        conn.execute("INSERT OR IGNORE INTO p2a_policy_commitments VALUES (?, ?)", (target.policy_version_id, target.policy_hash))
        conn.execute("INSERT OR IGNORE INTO p2a_grant_bindings VALUES (?, ?)", (binding.grant_id, canonical_bytes(binding.model_dump()).decode("ascii")))
        conn.execute("INSERT INTO p2a_grants VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(grant_id) DO UPDATE SET grant_generation=excluded.grant_generation, assignment_generation=excluded.assignment_generation, version_id=excluded.version_id, active=excluded.active", (binding.grant_id, binding.assignment_id, target.grant_generation, target.assignment_generation, target.policy_version_id, int(command.operation == "activate")))
        conn.execute("INSERT INTO p2a_grant_authorities VALUES (?, ?) ON CONFLICT(grant_id) DO UPDATE SET authority_json=excluded.authority_json", (binding.grant_id, canonical_bytes(target.model_dump()).decode("ascii")))
        conn.execute("UPDATE p2a_node SET epoch=? WHERE singleton=1 AND epoch=?", (target.node_epoch, command.expected_epoch))
        receipt = AppliedCommandReceipt(command_id=command.command_id, command_hash=hashed, operation=command.operation, authority=target, applied_at=now)
        conn.execute("INSERT INTO p2a_protocol_commands VALUES (?, ?, ?)", (command.command_id, hashed, canonical_bytes(receipt.model_dump()).decode("ascii")))
        return "applied", receipt

    def _ack(self, request: SignedMutation | SignedStatusRequest, *, outcome: str, reason_code: str, receipt: AppliedCommandReceipt | None, state: NodeGrantState, now: int) -> SignedAck:
        mutation = isinstance(request, SignedMutation)
        return sign_ack(AckBody.parse({"version": "topos-policy-ack/v2", "kid": self.node_signing_kid, "issuer_id": self.ledger.identity.node_id, "audience_id": self.cp_issuer_id, "response_to": digest(request.model_dump()), "request_kind": "mutation" if mutation else "status", "request_id": request.command_id if mutation else request.request_id, "command_id": request.command_id, "command_hash": command_digest(request) if mutation else request.command_hash, "outcome": outcome, "reason_code": reason_code, "receipt": receipt.model_dump() if receipt else None, "state": state.model_dump(), "issued_at": now, "expires_at": now + 120}), self.node_signing_key)

    def mutate(self, raw, *, now: int) -> SignedAck:
        command = verify_mutation(raw, trusted_keys=self.trusted_cp_keys, issuer_id=self.cp_issuer_id, identity=self.ledger.identity, frontend_client_id=self.frontend_client_id, now=now)
        with self.ledger._transaction() as conn:
            self._sync_protection(conn)
            binding = binding_of(command.authority)
            self._bound_grant(conn, binding)
            conn.execute("SAVEPOINT policy_command")
            receipt = None
            try:
                outcome, receipt = self._apply(conn, command, now)
                reason = "ok"
                conn.execute("RELEASE policy_command")
            except PolicyError as exc:
                conn.execute("ROLLBACK TO policy_command")
                conn.execute("RELEASE policy_command")
                outcome = "rejected"
                reason = exc.code if exc.code in {"epoch_conflict", "generation_stale", "binding_conflict", "immutable_policy", "policy_time", "protection_changed", "command_conflict"} else "command_invalid"
            return self._ack(command, outcome=outcome, reason_code=reason, receipt=receipt, state=self._state(conn, binding), now=now)

    def status(self, raw, *, now: int) -> SignedAck:
        request = verify_status_request(raw, trusted_keys=self.trusted_cp_keys, issuer_id=self.cp_issuer_id, identity=self.ledger.identity, now=now)
        with self.ledger._transaction() as conn:
            self._sync_protection(conn)
            state = self._state(conn, request.binding)
            try:
                receipt = self._receipt(conn, request.command_id, request.command_hash)
                reason = "command_unknown" if request.command_id is not None and receipt is None else "ok"
            except PolicyError:
                receipt, reason = None, "command_conflict"
            if receipt and binding_of(receipt.authority) != request.binding:
                raise PolicyError("binding_conflict")
            return self._ack(request, outcome="status", reason_code=reason, receipt=receipt, state=state, now=now)

    def admit(self, raw, **kwargs):
        """Future adapter hook: actual protection sync precedes grantee admission."""
        with with_db_write():
            with self.ledger._transaction() as conn:
                self._sync_protection(conn)
            return self.ledger.admit(raw, **kwargs)

    def checkpoint_decision(self, lease, decision, **kwargs):
        """Still private/non-executing; actual protection changes invalidate work."""
        with with_db_write():
            with self.ledger._transaction() as conn:
                self._sync_protection(conn)
            return self.ledger.checkpoint_decision(lease, decision, **kwargs)

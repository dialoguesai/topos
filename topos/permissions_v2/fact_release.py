"""Signed P2b scalar release through current owner evidence/output review gates."""
from __future__ import annotations

from collections.abc import Callable

from topos.principal import THIRD_PARTY, current_principal
from topos.storage.db.write_gate import with_db_write

from .canonical import PolicyError, canonical_bytes, digest
from .contract import Binding
from .fact_contract import FAMILY_BY_CAPABILITY, OUTPUT_FAMILIES, FactPolicyV2
from .fact_policy import fact_projection_decision
from .forwarding import ReleaseBody, sign_node_result
from .identity import SUBJECT_CONTRACT_BY_CAPABILITY
from .node_protocol import NodePolicyProtocol
from .projection_reviews import ProjectionReviewService
from .release import SourceMessageIntent
from .signing import (AuthorityBinding, FactRequestContext, SignedFactEnvelope,
    parse_authority, verify_current_signature)

MAX_FACT_DISCLOSURE_BYTES = 8192


class FactProjectionRelease:
    """The trusted send callback executes before evidence/review gates release.

    Only the exact reviewed six-field scalar is serialized. This path invokes
    neither the raw-source transport nor a model or generic query fallback.
    Failed or uncertain sends consume the signed request permanently.
    """
    def __init__(self, *, protocol: NodePolicyProtocol, projections: ProjectionReviewService,
                 clock: Callable[[], int]):
        if (protocol.ledger.identity.model_dump() != projections.resolver.binding.model_dump()
            or protocol.canonical_database != projections.resolver.path.resolve(strict=True)):
            raise PolicyError("release_service_binding")
        self.protocol, self.projections, self.clock = protocol, projections, clock

    def dispatch(self, *, envelope: dict, payload: dict, request_id: str, send: Callable) -> None:
        principal = current_principal()
        if (principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay"
            or not principal.acting_user or not principal.client_id):
            raise PolicyError("recipient_relay_required")
        intent = SourceMessageIntent.parse(payload)
        fact_id = intent.fact_id()
        # This parser deliberately rejects every P2a request/capability tuple.
        signed = SignedFactEnvelope.parse(envelope)
        ledger = self.protocol.ledger
        request = FactRequestContext.parse({**ledger.identity.model_dump(), "actor_id": principal.acting_user,
            "client_id": principal.client_id, "grant_id": signed.grant_id, "assignment_id": signed.assignment_id,
            "request_id": request_id, "request_type": "permissions.v2.fact.read"})
        # The owner-identity rule AND the output family come from the signed
        # capability, before any evidence is resolved or any row is read. If the
        # envelope named a capability the grant does not carry, the policy loaded
        # inside the callback will not match the contract the evidence was
        # qualified under, and the decision refuses rather than releasing under
        # the wrong rule. Neither is ever inferred from the candidate.
        contract = SUBJECT_CONTRACT_BY_CAPABILITY.get(signed.capability_version)
        family = FAMILY_BY_CAPABILITY.get(signed.capability_version)
        if contract is None or family is None:
            raise PolicyError("unsupported_capability")
        with with_db_write():
            with ledger._transaction() as db:
                self.protocol._sync_protection(db)
            # E2, as the locator door: verified here, claimed after the floors, so a
            # refused fact read costs the tombstone and not the envelope.
            admission = ledger.verify(signed.model_dump(), request=request, payload=intent.model_dump(), now=self.clock())

            def release(evidence, reviewed, rows, permits):
                with ledger._transaction() as db:
                    self.protocol._sync_protection(db)
                    authority, policy = ledger._authority(db, signed.grant_id, self.clock())
                    expected = parse_authority({field: getattr(signed, field) for field in AuthorityBinding.model_fields})
                    floor = self.projections.resolver.current_floor
                    if (authority != expected or not isinstance(policy, FactPolicyV2)
                        or floor is None or floor != authority.protection_revision):
                        raise PolicyError("authority_stale")
                # Extract only after full verified authority equality above.
                binding = Binding.parse({field: getattr(authority, field) for field in Binding.model_fields})
                decision = fact_projection_decision(policy=policy, evidence=evidence, projection=reviewed,
                    rows=rows, binding=binding, request_as_of=signed.issued_at, now=self.clock(),
                    permitted_subjects=permits)
                if decision.verdict != "permit":
                    ledger.refuse(admission, decision.model_dump(), candidate_revision=decision.candidate_revision,
                                  now=self.clock())
                    raise PolicyError("permission_denied")
                # The disclosure class comes from the capability that was signed,
                # not from the candidate in hand: a candidate that does not fit
                # the granted family is refused here rather than re-labelled.
                output = OUTPUT_FAMILIES[family][2].parse(reviewed.candidate.output.model_dump())
                if len(canonical_bytes(output.model_dump())) > MAX_FACT_DISCLOSURE_BYTES:
                    raise PolicyError("disclosure_budget")
                lease = ledger.admit_verified(admission, now=self.clock())
                ledger.checkpoint_decision(lease, decision.model_dump(), candidate_revision=decision.candidate_revision,
                    output=output.model_dump(), now=self.clock())
                checked_at = self.clock()
                verify_current_signature(signed, trusted_keys=ledger.trusted_keys, now=checked_at)
                result = sign_node_result(ReleaseBody(version="topos-node-disclosure/v1",
                    kid=self.protocol.node_signing_kid, envelope_hash=digest(signed.model_dump()),
                    request_id=request_id, request_hash=signed.request_hash, authority=authority,
                    output_hash=digest(output.model_dump()), checked_at=checked_at, expires_at=signed.expires_at),
                    self.protocol.node_signing_key)
                return result, output, authority

            try:
                result, output, checkpointed = self.projections.with_reviewed(fact_id, now=self.clock(),
                    callback=release, contract=contract, family=family)
            except BaseException:
                # Every other exit from the floors spent the id under the old order,
                # because the row was written before them; it still does, as the
                # tombstone. A no-op once the branch above refused.
                try:
                    ledger.refuse(admission, now=self.clock())
                except Exception:  # noqa: BLE001
                    pass
                raise

        # Every node gate is released here; the checkpoint above is the linearization point
        # (design §7 R12, as the locator door). One brief ledger transaction re-syncs
        # protection and re-reads authority, so anything committed since refuses the send.
        if self._authority_after_checkpoint(signed) != checkpointed:
            raise PolicyError("authority_stale")
        send(result.model_dump(), output.model_dump())

    def _authority_after_checkpoint(self, signed):
        ledger = self.protocol.ledger
        now = self.clock()
        with ledger._transaction() as db:
            self.protocol._sync_protection(db)
            return ledger._authority(db, signed.grant_id, now)[0]

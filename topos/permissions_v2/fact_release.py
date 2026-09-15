"""Signed P2b scalar release through current owner evidence/output review gates."""
from __future__ import annotations

from collections.abc import Callable

from topos.principal import THIRD_PARTY, current_principal
from topos.storage.db.write_gate import with_db_write

from .canonical import PolicyError, canonical_bytes, digest
from .contract import Binding
from .fact_contract import FactPolicyV2, FactScalarDisclosure
from .fact_policy import fact_projection_decision
from .forwarding import ReleaseBody, sign_node_result
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
        with with_db_write():
            with ledger._transaction() as db:
                self.protocol._sync_protection(db)
            lease = ledger.admit(signed.model_dump(), request=request, payload=intent.model_dump(), now=self.clock())

            def release(evidence, reviewed, rows):
                with ledger._transaction() as db:
                    self.protocol._sync_protection(db)
                    authority, policy = ledger._authority(db, signed.grant_id, self.clock())
                    expected = parse_authority({field: getattr(signed, field) for field in AuthorityBinding.model_fields})
                    if (authority != expected or not isinstance(policy, FactPolicyV2)
                        or evidence.snapshot.protection_revision != authority.protection_revision):
                        raise PolicyError("authority_stale")
                # Extract only after full verified authority equality above.
                binding = Binding.parse({field: getattr(authority, field) for field in Binding.model_fields})
                decision = fact_projection_decision(policy=policy, evidence=evidence, projection=reviewed,
                    rows=rows, binding=binding, request_as_of=signed.issued_at, now=self.clock())
                if decision.verdict != "permit":
                    ledger.checkpoint_decision(lease, decision.model_dump(), candidate_revision=decision.candidate_revision,
                        output=None, now=self.clock())
                    raise PolicyError("permission_denied")
                output = FactScalarDisclosure.parse(reviewed.candidate.output.model_dump())
                if len(canonical_bytes(output.model_dump())) > MAX_FACT_DISCLOSURE_BYTES:
                    raise PolicyError("disclosure_budget")
                ledger.checkpoint_decision(lease, decision.model_dump(), candidate_revision=decision.candidate_revision,
                    output=output.model_dump(), now=self.clock())
                checked_at = self.clock()
                verify_current_signature(signed, trusted_keys=ledger.trusted_keys, now=checked_at)
                result = sign_node_result(ReleaseBody(version="topos-node-disclosure/v1",
                    kid=self.protocol.node_signing_kid, envelope_hash=digest(signed.model_dump()),
                    request_id=request_id, request_hash=signed.request_hash, authority=authority,
                    output_hash=digest(output.model_dump()), checked_at=checked_at, expires_at=signed.expires_at),
                    self.protocol.node_signing_key)
                send(result.model_dump(), output.model_dump())

            self.projections.with_reviewed(fact_id, now=self.clock(), callback=release)

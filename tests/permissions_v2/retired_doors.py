"""Test-only drivers: the locator door and the fact door as they were before N8 removed them from the node.

Nothing under ``topos/`` imports this module, constructs these classes or routes a message to them: the node has
no ``permissions_v2_source_read`` or ``permissions_v2_fact_read`` handler, transport or switch any more, and
``tests/permissions_v2/test_n8_retired_doors_guard.py`` keeps it that way. They are kept here, unchanged apart
from the removed shadow-audit hook, for one reason: a large part of this suite proves SHARED checks through them
(the evidence floors and the sibling-fact floor in ``evidence.py``, Off-limits, attested identity, native
authorship proof, the ledger's verify / claim / tombstone / checkpoint order, read budgets, opaque ids), and the
mutation battery names those tests as the killers for mutants in that shared code. The knowledge search door
calls the same functions. Until each of those tests is re-homed onto the search door, these adapters are the
way they reach the code they protect.

Do not add behaviour here, and do not test this module's own logic: a property that lives only in these two
classes left the product with them.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from pydantic import StringConstraints

from topos.disclosure.content_policy import is_record_nsfw
from topos.permissions_v2.canonical import PolicyError, canonical_bytes, digest, parse_json
from topos.permissions_v2.contract import (CAPABILITY, CAPABILITY_ATTESTED, CAPABILITY_OPAQUE, Binding, Identifier,
    StrictModel)
from topos.permissions_v2.evidence import EvidenceResolver, EvidenceReviewStore, _key
from topos.permissions_v2.fact_contract import FAMILY_BY_CAPABILITY, OUTPUT_FAMILIES, FactPolicyV2
from topos.permissions_v2.fact_policy import fact_projection_decision
from topos.permissions_v2.forwarding import ReleaseBody, sign_node_result
from topos.permissions_v2.identity import SUBJECT_CONTRACT_BY_CAPABILITY
from topos.permissions_v2.node_protocol import NodePolicyProtocol
from topos.permissions_v2.opaque_ids import RecordKeys, opaque_record_id
from topos.permissions_v2.projection_reviews import ProjectionReviewService
from topos.permissions_v2.release import MAX_DISCLOSURE_BYTES, SOURCE_VIEWS, source_message_decision
from topos.permissions_v2.signing import (AuthorityBinding, FactRequestContext, RequestContext,
    SignedAttestedSourceEnvelope, SignedEnvelope, SignedFactEnvelope, SignedOpaqueSourceEnvelope, parse_authority,
    verify_current_signature)
from topos.principal import THIRD_PARTY, current_principal
from topos.storage.db.write_gate import with_db_write

# Their view's record_id is the canonical counter (`imessage:<ROWID>`), which tells a recipient
# how many messages lie between two it holds. D20 makes that a release-blocking leak, so the node
# releases nothing under them; they still parse, so stored policies, grants and receipts verify.
RETIRED_SOURCE_CAPABILITIES = frozenset({CAPABILITY, CAPABILITY_ATTESTED})
# Where the per-grant id keys live, relative to the canonical database: the one store the
# search stream's `runtime.record_keys_root()` names, so both doors derive the same id.
RECORD_KEYS_PATH = ("permissions-v2", "message-search")


def record_keys_root(canonical_database) -> Path:
    return Path(canonical_database).parent.joinpath(*RECORD_KEYS_PATH)


def parse_source_envelope(raw) -> SignedEnvelope | SignedAttestedSourceEnvelope:
    """A raw message read's signed envelope, p2a-v1 or p2a-v2, never a fact envelope.

    Only an envelope that names p2a-v2 takes the new class. Everything else parses
    exactly as it did before that capability existed, with the same refusals.
    """
    value = parse_json(raw) if isinstance(raw, (str, bytes)) else raw
    if isinstance(value, dict) and value.get("capability_version") == CAPABILITY_ATTESTED:
        return SignedAttestedSourceEnvelope.parse(value)
    if isinstance(value, dict) and value.get("capability_version") == CAPABILITY_OPAQUE:
        return SignedOpaqueSourceEnvelope.parse(value)
    return SignedEnvelope.parse(raw)


class SourceMessageIntent(StrictModel):
    query: Annotated[str, StringConstraints(strict=True, max_length=8_000)]

    def fact_id(self) -> str:
        class Locator(StrictModel):
            value: Identifier
        if not self.query.startswith("fact:"):
            raise PolicyError("unsupported_query")
        return Locator.parse({"value": self.query[5:]}).value


class SourceMessageRelease:
    """Single-process node adapter; constructed with trusted runtime services.

    send(result, output) must perform the bounded CP transport dispatch before
    returning. It must not enqueue a later sender. It is called with no node gate
    held, after the checkpoint; revocations and protection writes committed before
    the post-checkpoint authority re-read win. A failed or uncertain send consumes
    the request, and a retry needs a fresh CP issuance.
    No model, fallback query engine or source search is reachable here.
    """
    def __init__(self, *, protocol: NodePolicyProtocol, resolver: EvidenceResolver,
                 reviews: EvidenceReviewStore, clock: Callable[[], int]):
        if (protocol.ledger.identity.model_dump() != resolver.binding.model_dump()
            or protocol.canonical_database != resolver.path.resolve(strict=True)
            or reviews.binding != resolver.binding):
            raise PolicyError("release_service_binding")
        self.protocol, self.resolver, self.reviews, self.clock = protocol, resolver, reviews, clock
        self.record_keys = record_keys_root(protocol.canonical_database)
        self.retired = RETIRED_SOURCE_CAPABILITIES

    def dispatch(self, *, envelope: dict, payload: dict, request_id: str, send: Callable) -> None:
        principal = current_principal()
        if (principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay"
            or not principal.acting_user or not principal.client_id):
            raise PolicyError("recipient_relay_required")
        intent = SourceMessageIntent.parse(payload)
        fact_id = intent.fact_id()
        signed = parse_source_envelope(envelope)
        if signed.request_type != "permissions.v2.read":
            raise PolicyError("unsupported_query")
        # The owner-identity rule comes from the signed capability, before any
        # evidence is resolved; never from the request body or the candidate row.
        contract = SUBJECT_CONTRACT_BY_CAPABILITY.get(signed.capability_version)
        if contract is None:
            raise PolicyError("unsupported_capability")
        if signed.capability_version in self.retired:
            raise PolicyError("capability_retired")
        if signed.capability_version not in SOURCE_VIEWS:
            # An allowlist, not the shared fallback: `source_view` answers the locator view
            # for any capability, because another stream's evaluator asks it about its own
            # (p2c-v1 re-decides its members through `source_message_decision`). If such a
            # capability ever reached THIS door, that default would build canonical ids.
            raise PolicyError("unsupported_capability")
        view, disclosure = SOURCE_VIEWS[signed.capability_version]
        ledger = self.protocol.ledger
        request = RequestContext.parse({**ledger.identity.model_dump(), "actor_id": principal.acting_user,
            "client_id": principal.client_id, "grant_id": signed.grant_id, "assignment_id": signed.assignment_id,
            "request_id": request_id, "request_type": "permissions.v2.read"})
        with with_db_write():
            # Protection changes advance effective authority before admission.
            with ledger._transaction() as db:
                self.protocol._sync_protection(db)
            # E2: verified here, claimed after the floors. A read the floors refuse
            # costs the owner the tombstone, not the ~2.8 KB envelope; the claim is
            # still one SELECT and one INSERT under the primary key, still before any
            # response leaves. Every exit below spends the id exactly once.
            admission = ledger.verify(envelope, request=request, payload=intent.model_dump(), now=self.clock())

            def release(qualified, rows):
                with ledger._transaction() as db:
                    self.protocol._sync_protection(db)
                    authority, policy = ledger._authority(db, signed.grant_id, self.clock())
                    # The snapshot binds its closure's protection history; signed
                    # authority binds the node-wide revision of this very read.
                    if (authority != parse_authority({field: getattr(signed, field) for field in AuthorityBinding.model_fields})
                        or self.resolver.current_floor is None or self.resolver.current_floor != authority.protection_revision):
                        raise PolicyError("authority_stale")
                decision = source_message_decision(policy, qualified)
                if decision.verdict != "permit":
                    ledger.refuse(admission, decision.model_dump(), candidate_revision=decision.candidate_revision,
                                  now=self.clock())
                    raise PolicyError("permission_denied")
                key = self._record_key(signed) if signed.capability_version == CAPABILITY_OPAQUE else None
                released = []
                for ref in qualified.snapshot.leaves:
                    row = rows[_key(ref.identity)]
                    if is_record_nsfw(row):
                        # The owner's NSFW decision withholds a message from every share, and this door
                        # releases the whole message. Same hard withhold, same reason, as p2c-v3's.
                        raise PolicyError("unsupported_message_content")
                    identity = ref.identity
                    record_id = identity.record_id if key is None else opaque_record_id(key, grant_id=signed.grant_id,
                        table=identity.table, source_id=identity.source_id, dataset_id=identity.dataset_id,
                        record_id=identity.record_id)
                    released.append(({"record_id": record_id, "source_id": identity.source_id,
                                      "canonical_table": identity.table, "content": row.get("content")}, identity))
                if key is not None:
                    # The canonical ids are gone from the ids, but the LIST was still ordered by
                    # them (the snapshot sorts leaves by their canonical identity), which ranks
                    # the records the owner's store holds. Under an opaque view the order is the
                    # opaque one, which says nothing a recipient did not already hold. Each record
                    # keeps its own identity through the sort.
                    released.sort(key=lambda pair: pair[0]["record_id"])
                records = [record for record, _identity in released]
                output = disclosure.parse({"family": "canonical_record", "operation": "read", "view_id": view, "records": records})
                if not records or len(canonical_bytes(output.model_dump())) > MAX_DISCLOSURE_BYTES:
                    raise PolicyError("disclosure_budget")
                # This durable one-shot checkpoint precedes transport so an
                # uncertain send cannot be replayed after restart. The outer
                # resolver callback still holds evidence/review/write gates. The
                # request id is claimed here, immediately before it, so nothing can
                # be released under an envelope whose id was not spent first.
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

            # p2a-v1 keeps the frozen legacy rule; p2a-v2 reads the owner's
            # attestations, as the fact labels do. A fact grant cannot reach this
            # adapter. Both release whole messages, so every other fact citing
            # one of them must be scoped too, checked inside the same read.
            try:
                result, output, checkpointed = self.resolver.with_qualified(fact_id, reviews=self.reviews,
                    callback=release, contract=contract, discloses_sources=True)
            except BaseException:
                # Any other exit from the floors -- an unknown fact, an unreviewed
                # record, the off-limits floor, a stale authority -- spent the id
                # under the old order too, because the row was already written. It
                # still does, as the tombstone. A no-op when the branch above already
                # refused; best effort, so a failure here cannot mask the refusal.
                try:
                    ledger.refuse(admission, now=self.clock())
                except Exception:  # noqa: BLE001
                    pass
                raise

        # Every node gate is released here (design §7 R12): the checkpoint above is the
        # linearization point, and an owner write no longer waits out the send. What the
        # gap re-opens is narrowed by one brief ledger transaction: protection re-synced,
        # then the grant's authority re-read, so a revoke, expiry, re-policy or protection
        # change committed since the checkpoint refuses the send. One committed after it
        # races only the bounded send, as a write after the send always could.
        if self._authority_after_checkpoint(signed) != checkpointed:
            raise PolicyError("authority_stale")
        send(result.model_dump(), output.model_dump())

    def _record_key(self, signed) -> bytes:
        """This grant's opaque-id key. Any failure refuses: there is no id to fall back to.

        The canonical id is exactly what this view stops releasing, so a missing durable
        directory, a loosened mode or an unreadable store must not quietly produce one.
        """
        try:
            return RecordKeys(self.record_keys).get(signed.grant_id, create=True)
        except PolicyError:
            raise
        except Exception:
            raise PolicyError("record_key_unavailable") from None

    def _authority_after_checkpoint(self, signed):
        ledger = self.protocol.ledger
        now = self.clock()
        with ledger._transaction() as db:
            self.protocol._sync_protection(db)
            return ledger._authority(db, signed.grant_id, now)[0]


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

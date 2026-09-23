"""Bounded source-message disclosure through the signed P2a policy boundary.

The locator is a scoped, owner-reviewed fact, but the output is its complete set
of terminal canonical messages. This does not add a fact, summary or NL form.
The callback is the trusted transport send itself, never a permit consumer.
Three capabilities name this door. p2a-v1 (frozen legacy subject rule) and p2a-v2
(owner-attested rule) release `canonical.message_disclosure.v1`, whose record ids are
the canonical counter; the node now refuses both (RETIRED_SOURCE_CAPABILITIES). p2a-v3
is p2a-v2 with `canonical.message_disclosure.v2`, whose record ids are opaque per grant
(`opaque_ids`). The rule is always the signed capability's.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from pydantic import StringConstraints

from topos.principal import THIRD_PARTY, current_principal
from topos.storage.db.write_gate import with_db_write

from .canonical import PolicyError, canonical_bytes, digest, parse_json
from .contract import (CAPABILITY, CAPABILITY_ATTESTED, CAPABILITY_OPAQUE, EVALUATOR_ATTESTED, EVALUATOR_OPAQUE,
    Decision, MessageDisclosure, Only, PolicyV2, StrictModel, VIEW, VIEW_OPAQUE, evaluate_predicate)
from .evidence import EvidenceResolver, EvidenceReviewStore, QualifiedEvidence, _key
from .forwarding import ReleaseBody, sign_node_result
from .identity import SUBJECT_CONTRACT_BY_CAPABILITY
from .node_protocol import NodePolicyProtocol
from .opaque_ids import RecordKeys, opaque_record_id
from .registry import AttestedSubjectSourceDecision, OpaqueMessageDisclosure, OpaqueSubjectSourceDecision
from .search_contract import CAPABILITY_SEARCH, EVALUATOR_SEARCH, SearchMemberDecision
from .signing import (AuthorityBinding, RequestContext, SignedAttestedSourceEnvelope, SignedEnvelope,
    SignedOpaqueSourceEnvelope, parse_authority, verify_current_signature)

VOCABULARY = "owner-review-vocabulary/v1"
MAX_DISCLOSURE_BYTES = 256_000
# capability -> (decision class, evaluator version). Closed: a policy of any other
# capability, fact capabilities included, has no raw message decision at all.
SOURCE_DECISIONS = {CAPABILITY: (Decision, "hard-rules/p2a-v1"),
                    CAPABILITY_ATTESTED: (AttestedSubjectSourceDecision, EVALUATOR_ATTESTED),
                    CAPABILITY_OPAQUE: (OpaqueSubjectSourceDecision, EVALUATOR_OPAQUE),
                    # p2c-v1 search re-decides each returned record's fact with this very function.
                    CAPABILITY_SEARCH: (SearchMemberDecision, EVALUATOR_SEARCH)}
# capability -> (view id, disclosure class). Only p2a-v3's view carries opaque record ids.
# Read through `source_view`, never by subscript: `source_message_decision` is also the
# function the p2c-v1 search re-decides each member with (its SOURCE_DECISIONS entry), and
# that capability's member decision carries the locator view. A subscript here made every
# search index rebuild fail with a KeyError once the two branches were merged.
SOURCE_VIEWS = {CAPABILITY: (VIEW, MessageDisclosure), CAPABILITY_ATTESTED: (VIEW, MessageDisclosure),
                CAPABILITY_OPAQUE: (VIEW_OPAQUE, OpaqueMessageDisclosure)}


def source_view(capability: str) -> tuple:
    return SOURCE_VIEWS.get(capability, (VIEW, MessageDisclosure))
# Their view's record_id is the canonical counter (`imessage:<ROWID>`), which tells a recipient
# how many messages lie between two it holds. D20 makes that a release-blocking leak, so the node
# releases nothing under them; they still parse, so stored policies, grants and receipts verify.
RETIRED_SOURCE_CAPABILITIES = frozenset({CAPABILITY, CAPABILITY_ATTESTED})
# Where the per-grant id keys live, relative to the canonical database: the one store the
# search stream's `runtime.record_keys_root()` names, so both doors derive the same id.
RECORD_KEYS_PATH = ("permissions-v2", "message-search")


def record_keys_root(canonical_database) -> "Path":
    from pathlib import Path
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
        from .contract import Identifier

        class Locator(StrictModel):
            value: Identifier
        if not self.query.startswith("fact:"):
            raise PolicyError("unsupported_query")
        return Locator.parse({"value": self.query[5:]}).value


def _attributes(classification) -> dict[str, list[str]]:
    # Qualification independently proves native owner authorship and owner-only
    # subjects. These are documented vocabulary values, never pack-name guesses.
    return {"domain": classification.domains, "actor_role": ["authored"],
            "subject": ["owner"], "sensitivity": [classification.sensitivity]}


def _rule_sources(rule, policy):
    selection = rule.evidence_use.sources
    return set(selection.values if isinstance(selection, Only) else policy.source_universe.source_ids)


def _tables(rule):
    return {table for form in rule.release.forms for table in form.tables}


def source_message_decision(policy: PolicyV2, evidence: QualifiedEvidence) -> Decision:
    """Evaluate whole correlated clauses against every qualified contributing row.

    This helper cannot grant access: only the service obtains trusted evidence.
    A denial on any selected terminal source withholds the whole unredacted
    result. For derived artifacts, exclusions apply conservatively to the entire
    contributing closure when a deny source overlaps; no partial recomputation.
    Evidence qualified under a subject rule other than the one the policy's
    capability selects is refused, never evaluated.
    """
    if policy.versions.vocabulary != VOCABULARY:
        raise PolicyError("unsupported_vocabulary")
    capability = policy.versions.capability
    if capability not in SOURCE_DECISIONS:
        raise PolicyError("unsupported_capability")
    if evidence.subject_contract != SUBJECT_CONTRACT_BY_CAPABILITY[capability]:
        raise PolicyError("subject_contract_mismatch")
    decision_class, evaluator_version = SOURCE_DECISIONS[capability]
    snapshot = evidence.snapshot
    labels = {_key(item.evidence.identity): _attributes(item) for item in evidence.classifications}
    closure = snapshot.artifacts + snapshot.leaves
    if not snapshot.leaves or len(labels) != len(closure):
        raise PolicyError("classification_incomplete")
    selected_keys = {_key(item.identity) for item in closure}
    if set(labels) != selected_keys:
        raise PolicyError("classification_incomplete")
    all_sources = {item.identity.source_id for item in snapshot.leaves}
    all_tables = {item.identity.table for item in snapshot.leaves}
    allows, denies, unknown_deny, unknown_allow = [], [], False, False
    for rule in policy.rules:
        sources, tables = _rule_sources(rule, policy), _tables(rule)
        if "owner-engine-local" not in rule.evidence_use.processors.values:
            continue
        if rule.effect == "permit":
            if (rule.release.ceiling != "raw" or not sources or not tables
                or not all_sources <= sources or not all_tables <= tables):
                continue
            values = [evaluate_predicate(rule.evidence_use.predicate, labels[_key(item.identity)]) for item in closure]
            values += [evaluate_predicate(rule.release.predicate, labels[_key(item.identity)]) for item in snapshot.leaves]
            if all(value is True for value in values):
                allows.append(rule.rule_id)
            elif False not in values and None in values:
                unknown_allow = True
        else:
            selected = [item for item in snapshot.leaves if item.identity.source_id in sources and item.identity.table in tables]
            if not selected:
                continue
            values = [evaluate_predicate(rule.evidence_use.predicate, labels[_key(item.identity)]) for item in closure]
            values += [evaluate_predicate(rule.release.predicate, labels[_key(item.identity)]) for item in selected]
            if True in values:
                denies.append(rule.rule_id)
            elif None in values:
                unknown_deny = True
    verdict = "deny" if denies else "indeterminate" if unknown_deny else "permit" if allows else "indeterminate" if unknown_allow else "deny"
    return decision_class(stage="output_release", verdict=verdict, policy_hash=digest(policy.model_dump()),
        candidate_revision=digest({"snapshot": snapshot.model_dump(), "review_revision": evidence.review_revision}),
        evaluator_version=evaluator_version, matched_allow_clause_ids=allows[:1] if verdict == "permit" else [],
        matched_deny_clause_ids=denies, reason_code="rule_permit" if verdict == "permit" else "rule_deny" if verdict == "deny" else "unknown_context",
        required_projection_id=source_view(capability)[0] if verdict == "permit" else None,
        missing_context_codes=["classification"] if verdict == "indeterminate" else [])


def _file_shadow_index(ledger, *, request_id, grant_id, records, record_key, now) -> None:
    """File this release in the node's shadow index, if the node keeps one. Never raises into a release.

    Placed here rather than inside `_checkpoint` because the grant's record key lives on this side: the ledger
    has no key and must not grow one. A release whose index write fails is counted by `shadow_index.failures()`
    and shows up later as an `records_unavailable` re-score, which is the visible form of the hole.
    """
    from . import shadow_index
    if not shadow_index.enabled():
        return
    try:
        with ledger._transaction() as conn:
            shadow_index.record_release(conn, request_id=request_id, grant_id=grant_id, records=records,
                                        record_key=record_key, now=now)
    except Exception:  # noqa: BLE001 -- an unauditable read is still a correct read
        shadow_index._count_failure()


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
                records = []
                for ref in qualified.snapshot.leaves:
                    row = rows[_key(ref.identity)]
                    identity = ref.identity
                    record_id = identity.record_id if key is None else opaque_record_id(key, grant_id=signed.grant_id,
                        table=identity.table, source_id=identity.source_id, dataset_id=identity.dataset_id,
                        record_id=identity.record_id)
                    records.append({"record_id": record_id, "source_id": identity.source_id,
                                    "canonical_table": identity.table, "content": row.get("content")})
                if key is not None:
                    # The canonical ids are gone from the ids, but the LIST was still ordered by
                    # them (the snapshot sorts leaves by their canonical identity), which ranks
                    # the records the owner's store holds. Under an opaque view the order is the
                    # opaque one, which says nothing a recipient did not already hold.
                    records.sort(key=lambda record: record["record_id"])
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
                # C6: what was released, so the owner's shadow audit can ask about it later
                # (permissions_v2/shadow_index.py). Ids and a sealed pointer, never content; off unless the node
                # is told otherwise; and inside its own try, so a release is never a casualty of being auditable.
                _file_shadow_index(ledger, request_id=request_id, grant_id=signed.grant_id, records=output.records,
                                   record_key=key, now=self.clock())
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

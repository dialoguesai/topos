"""Closed P2b fact policy and scalar schema; mirrored without engine services.

Explicit opt-in only. The existing P2a parser remains unchanged. Parsing a policy
or projection is not qualification, authenticated owner review or release.
"""
from __future__ import annotations

from typing import Annotated, Literal
import unicodedata

from pydantic import Field, StringConstraints, field_validator, model_validator

from .contract import (Binding, EvidenceUse, Generation, HardConstraints, Hash,
    Identifier, Only, Predicate, SourceUniverse, StrictModel, Validity)

CAPABILITY = "permissions-beta/p2b-v1"
EVALUATOR = "hard-rules/p2b-v1"
CAPABILITY_STATED_DAY = "permissions-beta/p2b-v2"
EVALUATOR_STATED_DAY = "hard-rules/p2b-v2"
CAPABILITY_ATTESTED = "permissions-beta/p2b-v3"
EVALUATOR_ATTESTED = "hard-rules/p2b-v3"
CAPABILITY_WORK = "permissions-beta/p2b-v4"
EVALUATOR_WORK = "hard-rules/p2b-v4"
FACT_VALIDITY_EXACT_INSTANT = "exact_instant_v1"
FACT_VALIDITY_STATED_DAY = "stated_day_v1"
FACT_CAPABILITIES = (CAPABILITY, CAPABILITY_STATED_DAY, CAPABILITY_ATTESTED, CAPABILITY_WORK)
FactCapability = Literal["permissions-beta/p2b-v1", "permissions-beta/p2b-v2", "permissions-beta/p2b-v3",
                         "permissions-beta/p2b-v4"]
VOCABULARY = "owner-review-vocabulary/v1"
PURPOSE = "owner-stated-fact-projection"
VIEW = "owner_stated_fact.scalar.v1"
PROJECTION_VERSION = "exact-owner-preference/v1"
FAMILY = "owner_stated_fact"
WORK_FAMILY = "owner_stated_work"
WORK_VIEW = "owner_stated_work.scalar.v1"
WORK_PROJECTION_VERSION = "exact-owner-work/v1"
WORK_PREDICATE = "works_at"
WORK_ASSERTION = "explicit_atomic_work_engagement"
EvidenceTable = Literal["signal_objects", "conversation_messages", "ai_chat_messages"]
Scalar = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]


def atomic_label_syntax(value):
    """One atomic label: the shared lexical floor for every scalar output family.

    This deliberately narrow lexical grammar is NOT a semantic classifier.
    Human review must attest that the exact label is one stated thing.
    Do not silently normalize bytes that were reviewed or source-bound.

    It is shared rather than per-family because the question it answers -- is
    this one label, or is it prose -- does not depend on what the label names.
    A preference and an employer are both rejected for the same reasons.
    """
    if (value != unicodedata.normalize("NFC", value) or value != " ".join(value.split())
        or value.startswith(("'", "\u2019")) or value.endswith(("'", "\u2019"))
        or not any(unicodedata.category(char)[0] in "LN" for char in value)
        or any(unicodedata.category(char)[0] not in "LMN" and char not in " -'\u2019&" for char in value)):
        raise ValueError("unsupported preference label syntax")
    return value


class FactScalarDisclosure(StrictModel):
    family: Literal["owner_stated_fact"]
    operation: Literal["read"]
    view_id: Literal["owner_stated_fact.scalar.v1"]
    subject: Literal["self"]
    predicate: Literal["prefers"]
    value: Scalar

    @field_validator("value")
    @classmethod
    def atomic_label(cls, value):
        # Same grammar, same errors, same accepted set as before it was named:
        # test_fact_work_family pins the accept/reject battery for both families.
        return atomic_label_syntax(value)


class RollingEventWindow(StrictModel):
    kind: Literal["rolling"]
    anchor: Literal["server_request_as_of"]
    max_age_seconds: Generation
    event_time_semantics: Literal["canonical_event_time_v1"]
    missing_or_ambiguous: Literal["withhold"]
    future: Literal["withhold"]


class FactVersions(StrictModel):
    vocabulary: Literal["owner-review-vocabulary/v1"]
    capability: Literal["permissions-beta/p2b-v1"]


class FactEvidenceUse(EvidenceUse):
    purpose: Literal["owner-stated-fact-projection"]
    tables: Annotated[list[EvidenceTable], Field(max_length=3)]
    event_window: RollingEventWindow

    @field_validator("tables")
    @classmethod
    def unique_tables(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("duplicate evidence table")
        return values


class FactOutputForm(StrictModel):
    family: Literal["owner_stated_fact"]
    operation: Literal["read"]
    view_id: Literal["owner_stated_fact.scalar.v1"]


class FactRelease(StrictModel):
    predicate: Predicate
    ceiling: Literal["summary", "inference", "raw"]
    forms: Annotated[list[FactOutputForm], Field(max_length=1)]


class FactRule(StrictModel):
    rule_id: Identifier
    effect: Literal["permit", "deny"]
    evidence_use: FactEvidenceUse
    release: FactRelease


class FactEvaluator(StrictModel):
    kind: Literal["hard_rules"]
    version: Literal["hard-rules/p2b-v1"]


class FactPolicyV2(StrictModel):
    version: Literal["topos-policy/v2"]
    policy_version_id: Identifier
    binding: Binding
    versions: FactVersions
    validity: Validity
    source_universe: SourceUniverse
    hard_constraints: HardConstraints
    rules: Annotated[list[FactRule], Field(max_length=64)]
    evaluator: FactEvaluator
    natural_language: None

    @model_validator(mode="after")
    def closed_rules(self):
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate rule")
        for rule in self.rules:
            selection = rule.evidence_use.sources
            universe = self.source_universe
            if isinstance(selection, Only):
                if not set(selection.values) <= set(universe.source_ids):
                    raise ValueError("source outside pinned universe")
            elif (selection.universe_id, selection.universe_revision) != (universe.universe_id, universe.revision):
                raise ValueError("universe mismatch")
        return self


class FactDecision(StrictModel):
    stage: Literal["output_release"]
    verdict: Literal["permit", "deny", "indeterminate"]
    policy_hash: Hash
    candidate_revision: Hash
    evaluator_version: Literal["hard-rules/p2b-v1"]
    matched_allow_clause_ids: list[Identifier]
    matched_deny_clause_ids: list[Identifier]
    reason_code: Literal["rule_permit", "rule_deny", "unknown_context", "unsupported_view", "stale_authority", "fact_not_current"]
    required_projection_id: Literal["owner_stated_fact.scalar.v1"] | None
    missing_context_codes: list[Literal["classification", "lineage", "time", "fact_validity"]]


class StatedDayFactValidity(StrictModel):
    """Every temporal meaning a stated-day policy relies on, selected explicitly.

    A `valid_from` of the exact lexical form YYYY-MM-DD is a stated calendar day
    whose timezone basis the producers did not record. Such a day counts as
    current only once it has ended at every Earth offset, i.e. from 12:00:00 UTC
    on the following day. Explicit UTC instants keep their exact P2b v1 meaning.
    Year, month, naive, offset or otherwise malformed values stay unknown and
    withhold; a stated day that has not fully elapsed withholds as not current.
    The payload's real-world period fields are not evaluated by this contract.
    """
    semantics: Literal["stated_day_v1"]
    precision: Literal["day"]
    timezone_basis: Literal["unrecorded_any_earth_offset"]
    current_from: Literal["next_day_12_00_utc"]
    instants: Literal["explicit_utc_exact"]
    unknown: Literal["withhold"]
    not_elapsed: Literal["withhold"]


class StatedDayFactVersions(StrictModel):
    vocabulary: Literal["owner-review-vocabulary/v1"]
    capability: Literal["permissions-beta/p2b-v2"]
    fact_validity: StatedDayFactValidity


class StatedDayFactEvaluator(StrictModel):
    kind: Literal["hard_rules"]
    version: Literal["hard-rules/p2b-v2"]


class StatedDayFactPolicy(FactPolicyV2):
    """P2b rules, reviews and scalar view unchanged; only fact validity differs.

    The v1 class stays byte-identical, so existing signed v1 policies keep their
    hashes and exact-instant behaviour. A v1 document never parses as v2 and a
    v2 document never parses as v1; the registry dispatches on the capability.
    """
    versions: StatedDayFactVersions
    evaluator: StatedDayFactEvaluator


class StatedDayFactDecision(FactDecision):
    evaluator_version: Literal["hard-rules/p2b-v2"]


class ExactInstantFactValidity(StrictModel):
    """The v1 temporal meaning, written out so a v3 policy states it explicitly.

    v1 and v2 carry their validity in the capability itself. v3 does not: it
    selects the subject rule, and the temporal rule is chosen alongside it, so
    reading a v3 document tells you both without knowing which capability
    implied which.
    """
    semantics: Literal["exact_instant_v1"]
    precision: Literal["instant"]
    instants: Literal["explicit_utc_exact"]
    unknown: Literal["withhold"]


AttestedFactValidity = Annotated[StatedDayFactValidity | ExactInstantFactValidity,
                                 Field(discriminator="semantics")]


class OwnerAttestedSubjectBinding(StrictModel):
    """Whom a release under this policy may be about, and what withholds.

    Nothing here grants anything. It records which owner-identity rule the
    signed capability selected, so the resolver cannot be asked for one rule and
    the policy evaluated under another. Every outcome that is not a live,
    current attestation is `withhold`; there is no permissive setting to pick.
    """
    contract: Literal["owner_attested_v1"]
    statement_version: Literal["owner-identity-attestation/v1"]
    subjects: Literal["owner_attested_entities"]
    unattested: Literal["withhold"]
    moved_since_attestation: Literal["withhold"]
    rekeyed_facts: Literal["withhold"]
    literal_self_when_shadowed: Literal["withhold"]


class AttestedFactVersions(StrictModel):
    vocabulary: Literal["owner-review-vocabulary/v1"]
    capability: Literal["permissions-beta/p2b-v3"]
    fact_validity: AttestedFactValidity
    subject_binding: OwnerAttestedSubjectBinding


class AttestedFactEvaluator(StrictModel):
    kind: Literal["hard_rules"]
    version: Literal["hard-rules/p2b-v3"]


class AttestedSubjectFactPolicy(FactPolicyV2):
    """Owner-attested subjects. Deliberately not a subclass of the v2 policy.

    Rules, reviews and the scalar view are unchanged. Subclassing the stated-day
    policy would make every v3 document an instance of v2 as well, and the
    dispatch that decides temporal meaning is an isinstance test. A v3 policy
    that chose exact instants would then be evaluated as a stated-day one.
    """
    versions: AttestedFactVersions
    evaluator: AttestedFactEvaluator


class AttestedSubjectFactDecision(FactDecision):
    evaluator_version: Literal["hard-rules/p2b-v3"]


class WorkScalarDisclosure(StrictModel):
    """One organisation the owner said, in the first person, that they work for.

    A second output family, not a second spelling of the first. The preference
    family answers "what does the owner like"; this one answers "who does the
    owner work for". They share the evidence rules, the scalar shape and the
    label grammar, and nothing else: a preference review can never authorize
    this view, and this view can never carry `prefers`.
    """
    family: Literal["owner_stated_work"]
    operation: Literal["read"]
    view_id: Literal["owner_stated_work.scalar.v1"]
    subject: Literal["self"]
    predicate: Literal["works_at"]
    value: Scalar

    @field_validator("value")
    @classmethod
    def atomic_label(cls, value):
        return atomic_label_syntax(value)


class WorkFactOutputForm(StrictModel):
    family: Literal["owner_stated_work"]
    operation: Literal["read"]
    view_id: Literal["owner_stated_work.scalar.v1"]


class WorkFactRelease(StrictModel):
    predicate: Predicate
    ceiling: Literal["summary", "inference", "raw"]
    forms: Annotated[list[WorkFactOutputForm], Field(max_length=1)]


class WorkFactRule(StrictModel):
    rule_id: Identifier
    effect: Literal["permit", "deny"]
    evidence_use: FactEvidenceUse
    release: WorkFactRelease


class OwnerStatedWorkFamily(StrictModel):
    """What this family is, written into the document the owner signs.

    Nothing here is enforced by this contract, and none of it grants anything.
    It is the recorded premise of the family, in the same sense that the subject
    binding records which identity rule was selected: a reader of a signed v4
    policy can see which producer semantics the owner was told they were
    releasing, without reading the engine.

    `producer` names the ONLY writer in the engine that emits a `works_at` fact
    both marked `scoped` and asserted by the owner: the first-person
    present-tense message patterns in `features.facts.extract`. Those read
    "I work at X" / "I am working for X" out of a row the owner themselves
    typed. If that ever stops being the only such writer, this literal is the
    thing that has to change, and every signed v4 policy stops parsing until it
    does. That is the point of writing it down rather than assuming it.
    """
    name: Literal["owner_stated_work"]
    view_id: Literal["owner_stated_work.scalar.v1"]
    predicate: Literal["works_at"]
    assertion: Literal["explicit_atomic_work_engagement"]
    producer: Literal["first_person_present_tense_message_statement_v1"]
    projection_version: Literal["exact-owner-work/v1"]
    other_predicates: Literal["withhold"]


class WorkFactVersions(StrictModel):
    vocabulary: Literal["owner-review-vocabulary/v1"]
    capability: Literal["permissions-beta/p2b-v4"]
    fact_validity: AttestedFactValidity
    subject_binding: OwnerAttestedSubjectBinding
    output_family: OwnerStatedWorkFamily


class WorkFactEvaluator(StrictModel):
    kind: Literal["hard_rules"]
    version: Literal["hard-rules/p2b-v4"]


class WorkFactPolicy(FactPolicyV2):
    """The second output family, on the owner-attested subject rule.

    Not a subclass of the attested policy, for the reason that class gives: the
    temporal and family dispatches are isinstance tests, and a subclass would be
    an instance of its parent too. It carries the attested subject binding by
    composition instead, so v4 never regresses to the legacy literal-self rule.

    `rules` is re-declared because a rule's release form names the family. That
    is the only structural difference from v3; evidence use, the event window,
    correlation and exclusions are the v1 rules unchanged.
    """
    versions: WorkFactVersions
    evaluator: WorkFactEvaluator
    rules: Annotated[list[WorkFactRule], Field(max_length=64)]


class WorkFactDecision(FactDecision):
    evaluator_version: Literal["hard-rules/p2b-v4"]
    required_projection_id: Literal["owner_stated_work.scalar.v1"] | None


FactPolicy = FactPolicyV2 | StatedDayFactPolicy | AttestedSubjectFactPolicy | WorkFactPolicy
AnyFactDecision = FactDecision | StatedDayFactDecision | AttestedSubjectFactDecision | WorkFactDecision
AnyScalarDisclosure = FactScalarDisclosure | WorkScalarDisclosure
# family name -> (view id, projection version, disclosure class). The projection
# layer reads this instead of hardcoding one family, and a policy names its
# family rather than being assumed to mean the first one.
OUTPUT_FAMILIES = {FAMILY: (VIEW, PROJECTION_VERSION, FactScalarDisclosure),
                   WORK_FAMILY: (WORK_VIEW, WORK_PROJECTION_VERSION, WorkScalarDisclosure)}
# Which family a signed capability releases. Closed and explicit, like the
# subject-contract map beside it: the release path must never infer a family
# from the row it is about to disclose, only from the capability that was signed.
FAMILY_BY_CAPABILITY = {CAPABILITY: FAMILY, CAPABILITY_STATED_DAY: FAMILY,
                        CAPABILITY_ATTESTED: FAMILY, CAPABILITY_WORK: WORK_FAMILY}


def fact_output_family(policy) -> str:
    """The output family a parsed policy selected; every pre-v4 policy is the first."""
    versions = policy.versions
    if isinstance(versions, WorkFactVersions):
        return versions.output_family.name
    return FAMILY


def fact_validity_semantics(policy) -> str:
    """The validity contract a parsed policy selected; v1 policies are exact instants."""
    versions = policy.versions
    if isinstance(versions, (StatedDayFactVersions, AttestedFactVersions, WorkFactVersions)):
        return versions.fact_validity.semantics
    return FACT_VALIDITY_EXACT_INSTANT



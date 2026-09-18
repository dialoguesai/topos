"""Closed beta policy grammar, with correlated rules and three-valued predicates.

This is a syntactic registry, not certification of an executable disclosure path.
NL, graphs, vectors, summaries and facts are intentionally not accepted in P2a.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator

from .canonical import MAX_INTEGER, PolicyError, canonical_bytes, parse_json

Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")]
Hash = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Number = Annotated[int, Field(strict=True, ge=0, le=MAX_INTEGER)]
Generation = Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)]
Table = Literal["conversation_messages", "ai_chat_messages"]
VIEW = "canonical.message_disclosure.v1"
CAPABILITY = "permissions-beta/p2a-v1"
# The same raw message release under the owner-attested subject rule the fact
# labels use. Its policy and decision classes live in registry.py: they reuse the
# fact contract's subject block, and fact_contract imports this module.
CAPABILITY_ATTESTED = "permissions-beta/p2a-v2"
EVALUATOR_ATTESTED = "hard-rules/p2a-v2"
# p2a-v2's grammar and subject rule with the view whose record ids are opaque:
# `imessage:<ROWID>` counts the owner's whole store, so two released ids told the
# recipient how many messages lay between them (design §6.4, channel 11). A view
# whose ids change meaning is a new view; the classes live in registry.py.
VIEW_OPAQUE = "canonical.message_disclosure.v2"
CAPABILITY_OPAQUE = "permissions-beta/p2a-v3"
EVALUATOR_OPAQUE = "hard-rules/p2a-v3"
SOURCE_CAPABILITIES = (CAPABILITY, CAPABILITY_ATTESTED, CAPABILITY_OPAQUE)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @classmethod
    def parse(cls, raw: bytes | str | dict):
        try:
            value = parse_json(raw) if isinstance(raw, (bytes, str)) else raw
            canonical_bytes(value)
            return cls.model_validate(value)
        except ValidationError:
            # Pydantic diagnostics include the rejected input; even a private
            # caller logging a traceback must not echo denied candidate data.
            raise PolicyError("schema_invalid") from None


class Only(StrictModel):
    kind: Literal["only"]
    values: list[Identifier]

    @field_validator("values")
    @classmethod
    def unique(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("duplicate selection")
        return values


class All(StrictModel):
    kind: Literal["all"]
    universe_id: Identifier
    universe_revision: Generation
    growth: Literal["require_consent"]


SourceSelection = Annotated[Union[Only, All], Field(discriminator="kind")]


class Atom(StrictModel):
    kind: Literal["atom"]
    attribute: Literal["domain", "actor_role", "subject", "sensitivity"]
    operator: Literal["intersects"]
    values: list[Identifier]


class AllOf(StrictModel):
    kind: Literal["all_of"]
    terms: list["Predicate"]


class AnyOf(StrictModel):
    kind: Literal["any_of"]
    terms: list["Predicate"]


class Not(StrictModel):
    kind: Literal["not"]
    term: "Predicate"


Predicate = Annotated[Union[Atom, AllOf, AnyOf, Not], Field(discriminator="kind")]
for _predicate in (AllOf, AnyOf, Not):
    _predicate.model_rebuild()


def evaluate_predicate(predicate: Predicate, attributes: dict[str, list[str] | None]) -> bool | None:
    """Kleene logic: absent/malformed classification stays Unknown under NOT."""
    if isinstance(predicate, Atom):
        value = attributes.get(predicate.attribute)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return None
        return bool(set(value).intersection(predicate.values))
    if isinstance(predicate, Not):
        value = evaluate_predicate(predicate.term, attributes)
        return None if value is None else not value
    values = [evaluate_predicate(term, attributes) for term in predicate.terms]
    if isinstance(predicate, AllOf):
        return False if False in values else None if None in values else True
    return True if True in values else None if None in values else False


class Binding(StrictModel):
    environment_id: Identifier
    node_id: Identifier
    resource_id: Identifier
    owner_id: Identifier
    actor_id: Identifier
    client_id: Identifier
    grant_id: Identifier
    assignment_id: Identifier


class Versions(StrictModel):
    vocabulary: Identifier
    capability: Literal["permissions-beta/p2a-v1"]


class Validity(StrictModel):
    starts_at: Number
    expires_at: Number

    @model_validator(mode="after")
    def ordered(self):
        if self.expires_at <= self.starts_at:
            raise ValueError("empty validity")
        return self


class SourceUniverse(StrictModel):
    universe_id: Identifier
    revision: Generation
    source_ids: list[Identifier]

    @field_validator("source_ids")
    @classmethod
    def unique(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("duplicate source")
        return values


class HardConstraints(StrictModel):
    owner_only: Literal["deny"]
    unknown_classification: Literal["withhold"]
    unknown_lineage: Literal["withhold"]
    cross_rule_derivation: Literal["deny"]
    capability_growth: Literal["require_consent"]


class EvidenceUse(StrictModel):
    sources: SourceSelection
    predicate: Predicate
    purpose: Identifier
    processors: Only
    new_records: Literal["include_if_predicate"]

    @model_validator(mode="after")
    def local_processors(self):
        if any(value != "owner-engine-local" for value in self.processors.values):
            raise ValueError("unsupported processor")
        return self


class OutputForm(StrictModel):
    family: Literal["canonical_record"]
    operation: Literal["read"]
    view_id: Literal["canonical.message_disclosure.v1"]
    tables: list[Table]

    @field_validator("tables")
    @classmethod
    def unique(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("duplicate table")
        return values


class Release(StrictModel):
    predicate: Predicate
    ceiling: Literal["summary", "inference", "raw"]
    forms: list[OutputForm]


class Rule(StrictModel):
    rule_id: Identifier
    effect: Literal["permit", "deny"]
    evidence_use: EvidenceUse
    release: Release


class Evaluator(StrictModel):
    kind: Literal["hard_rules"]
    version: Literal["hard-rules/p2a-v1"]


class PolicyV2(StrictModel):
    version: Literal["topos-policy/v2"]
    policy_version_id: Identifier
    binding: Binding
    versions: Versions
    validity: Validity
    source_universe: SourceUniverse
    hard_constraints: HardConstraints
    rules: list[Rule]
    evaluator: Evaluator
    natural_language: None

    @model_validator(mode="after")
    def closed_rules(self):
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate rule")
        for rule in self.rules:
            sources = rule.evidence_use.sources
            universe = self.source_universe
            if isinstance(sources, Only):
                if not set(sources.values).issubset(universe.source_ids):
                    raise ValueError("source outside pinned universe")
            elif (sources.universe_id, sources.universe_revision) != (universe.universe_id, universe.revision):
                raise ValueError("universe mismatch")
        return self


class Decision(StrictModel):
    stage: Literal["classification", "evidence_use", "output_release"]
    verdict: Literal["permit", "deny", "indeterminate"]
    policy_hash: Hash
    candidate_revision: Hash
    evaluator_version: Literal["hard-rules/p2a-v1"]
    matched_allow_clause_ids: list[Identifier]
    matched_deny_clause_ids: list[Identifier]
    reason_code: Literal["rule_permit", "rule_deny", "unknown_context", "owner_only", "unsupported_view", "stale_authority"]
    required_projection_id: Literal["canonical.message_disclosure.v1"] | None
    missing_context_codes: list[Literal["classification", "lineage", "source", "processor", "projection"]]


class MessageRecord(StrictModel):
    record_id: Identifier
    source_id: Identifier
    canonical_table: Table
    content: Annotated[str, StringConstraints(strict=True, max_length=100_000)]


class MessageDisclosure(StrictModel):
    family: Literal["canonical_record"]
    operation: Literal["read"]
    view_id: Literal["canonical.message_disclosure.v1"]
    records: Annotated[list[MessageRecord], Field(max_length=100)]


def capability_document() -> dict[str, Any]:
    return {
        "version": CAPABILITY,
        # Every capability that releases this view. `version` stays the p2a-v1
        # grammar so an existing reader of it is unchanged.
        "capabilities": list(SOURCE_CAPABILITIES),
        "registered_forms": [{"family": "canonical_record", "operation": "read", "view_id": VIEW},
                             {"family": "canonical_record", "operation": "read", "view_id": VIEW_OPAQUE}],
        "executable_forms": [],
        "natural_language": False,
        "lineage_certified": False,
    }

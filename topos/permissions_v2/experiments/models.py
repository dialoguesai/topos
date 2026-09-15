"""Separate experimental grammar. Never accepted as a signed Policy v2 policy."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from ..canonical import digest
from ..contract import Hash, Identifier, Number, Predicate, StrictModel, Validity
from ..signing import AuthorityBinding

FORM = "experiment.synthetic_fact.v1"
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=16000)]
Ids = Annotated[list[Identifier], Field(max_length=32)]
Stage = Literal["evidence_use", "output_release"]
Arm = Literal["rules_v2", "semantic_v1"]
Verdict = Literal["permit", "deny", "indeterminate"]
Zero = Annotated[int, Field(strict=True, ge=0, le=0)]


class Clause(StrictModel):
    clause_id: Identifier
    text: Text


class Example(StrictModel):
    example_id: Identifier
    clause_id: Identifier
    verdict: Literal["permit", "deny"]
    role: Literal["illustration"]
    text: Text


class Prose(StrictModel):
    original: Text
    inclusions: Annotated[list[Clause], Field(max_length=32)]
    exclusions: Annotated[list[Clause], Field(max_length=32)]
    examples: Annotated[list[Example], Field(max_length=32)]


class Rule(StrictModel):
    clause_id: Identifier
    effect: Literal["permit", "deny"]
    sources: Ids
    evidence_predicate: Predicate
    output_predicate: Predicate
    forms: Annotated[list[Literal["experiment.synthetic_fact.v1"]], Field(max_length=1)]


class ExperimentPolicy(StrictModel):
    version: Literal["topos-offline-experiment/v1"]
    experiment_id: Identifier
    authority: AuthorityBinding
    validity: Validity
    source_universe: Ids
    processor: Literal["owner-engine-local"]
    owner_approved_revision: Hash
    rules: Annotated[list[Rule], Field(max_length=64)]
    prose: Prose

    @model_validator(mode="after")
    def coherent(self):
        # The owner-side provider supplies this reviewed capsule. This digest
        # detects edits; it is not a substitute for owner authentication.
        approved = self.model_dump(exclude={"owner_approved_revision"})
        if digest(approved) != self.owner_approved_revision:
            raise ValueError("unreviewed capsule revision")
        allow = [clause.clause_id for clause in self.prose.inclusions]
        deny = [clause.clause_id for clause in self.prose.exclusions]
        ids = allow + deny
        if len(set(ids)) != len(ids) or len(set(self.source_universe)) != len(self.source_universe):
            raise ValueError("duplicate clause/source")
        if {rule.clause_id for rule in self.rules} != set(ids) or len(self.rules) != len(ids):
            raise ValueError("correlated clause map")
        for rule in self.rules:
            if (rule.effect == "permit") != (rule.clause_id in allow) or not set(rule.sources) <= set(self.source_universe) or len(set(rule.sources)) != len(rule.sources):
                raise ValueError("rule binding")
        example_ids = [example.example_id for example in self.prose.examples]
        if len(set(example_ids)) != len(example_ids):
            raise ValueError("duplicate example")
        for example in self.prose.examples:
            if example.clause_id not in (allow if example.verdict == "permit" else deny):
                raise ValueError("example cannot amend exclusion")
        return self


class OutputUnit(StrictModel):
    unit_id: Identifier
    record_revision: Hash
    attributes: dict[Literal["domain", "actor_role", "subject", "sensitivity"], list[Identifier] | None]
    text: Text
    owner_only: bool


class Unit(OutputUnit):
    source_id: Identifier | None
    table: Literal["conversation_messages", "ai_chat_messages"]


class Candidate(StrictModel):
    candidate_id: Identifier
    qualification_revision: Hash
    lineage_revision: Hash
    lineage_state: Literal["qualified", "unknown"]
    protection_revision: Hash
    form: Identifier
    evidence: Annotated[list[Unit], Field(min_length=1, max_length=16)]
    output: OutputUnit

    @model_validator(mode="after")
    def unique(self):
        ids = [unit.unit_id for unit in self.evidence]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate evidence")
        return self


class RequestContext(StrictModel):
    authority: AuthorityBinding
    request_id: Identifier
    request_hash: Hash
    as_of: Number
    processor: Literal["owner-engine-local"]


class TrustedSnapshot(StrictModel):
    """Produced/refreshed by a trusted node provider, never a recipient/model."""
    current_authority: AuthorityBinding
    candidate: Candidate


class EvaluatorConfig(StrictModel):
    arm: Arm
    evaluator_version: Literal["rules-experiment/v1", "semantic-experiment/v1"]
    session_id: Identifier
    model_id: Identifier | None
    model_revision: Hash | None
    prompt_revision: Hash | None
    timeout_ms: Annotated[int, Field(strict=True, ge=1, le=30000)]
    max_prompt_bytes: Annotated[int, Field(strict=True, ge=1024, le=65536)]
    max_response_bytes: Annotated[int, Field(strict=True, ge=128, le=8192)]
    max_output_tokens: Annotated[int, Field(strict=True, ge=32, le=1024)]
    temperature: Zero

    @model_validator(mode="after")
    def coherent(self):
        semantic = self.arm == "semantic_v1"
        if self.evaluator_version != ("semantic-experiment/v1" if semantic else "rules-experiment/v1"):
            raise ValueError("arm version")
        pins = (self.model_id, self.model_revision, self.prompt_revision)
        if (semantic and any(pin is None for pin in pins)) or (not semantic and any(pin is not None for pin in pins)):
            raise ValueError("model pins")
        return self


class Judgment(StrictModel):
    """Only model-selectable fields. No output text, authority or new clauses."""
    verdict: Verdict
    matched_allow_clause_ids: Ids
    matched_deny_clause_ids: Ids
    required_projection_id: Literal["experiment.synthetic_fact.v1"] | None
    missing_context_codes: Annotated[list[Literal["classification", "context", "projection"]], Field(max_length=3)]

    @model_validator(mode="after")
    def unique(self):
        for values in (self.matched_allow_clause_ids, self.matched_deny_clause_ids, self.missing_context_codes):
            if len(set(values)) != len(values):
                raise ValueError("duplicate decision field")
        return self


class Decision(StrictModel):
    stage: Stage
    verdict: Verdict
    reason_code: Literal["rule_permit", "rule_deny", "semantic_permit", "semantic_deny", "unknown_context", "owner_only", "unknown_lineage", "unsupported_form", "stale_authority", "source_outside_boundary", "no_structural_match", "processor_boundary", "policy_time", "unconfigured_model", "model_timeout", "model_error", "model_identity", "malformed_decision", "prompt_budget", "response_budget", "clause_binding", "projection_required", "evidence_withheld"]
    policy_hash: Hash
    candidate_revision: Hash
    evaluator_config_hash: Hash
    context_hash: Hash
    matched_allow_clause_ids: Ids
    matched_deny_clause_ids: Ids
    required_projection_id: Literal["experiment.synthetic_fact.v1"] | None
    missing_context_codes: Annotated[list[Literal["classification", "context", "projection"]], Field(max_length=3)]


class ExperimentResult(StrictModel):
    experiment_id: Identifier
    arm: Arm
    evidence: Decision
    output: Decision | None
    verdict: Verdict
    execution_enabled: Literal[False]

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
VOCABULARY = "owner-review-vocabulary/v1"
PURPOSE = "owner-stated-fact-projection"
VIEW = "owner_stated_fact.scalar.v1"
PROJECTION_VERSION = "exact-owner-preference/v1"
EvidenceTable = Literal["signal_objects", "conversation_messages", "ai_chat_messages"]
Scalar = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]


class FactScalarDisclosure(StrictModel):
    family: Literal["owner_stated_fact"]
    operation: Literal["read"]
    view_id: Literal["owner_stated_fact.scalar.v1"]
    subject: Literal["self"]
    predicate: Literal["prefers"]
    value: Scalar

    @field_validator("value")
    @classmethod
    def atomic_label_syntax(cls, value):
        # This deliberately narrow lexical grammar is NOT a semantic classifier.
        # Human review must attest that the exact label is one stated preference.
        # Do not silently normalize bytes that were reviewed or source-bound.
        if (value != unicodedata.normalize("NFC", value) or value != " ".join(value.split())
            or value.startswith(("'", "’")) or value.endswith(("'", "’"))
            or not any(unicodedata.category(char)[0] in "LN" for char in value)
            or any(unicodedata.category(char)[0] not in "LMN" and char not in " -'’&" for char in value)):
            raise ValueError("unsupported preference label syntax")
        return value


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



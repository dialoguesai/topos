"""p2c-v1: permitted-set message search. Closed grammar, request, view and decisions.

A p2c-v1 grant is the p2a-v2 raw message grant (owner-attested subject rule,
the same rules, the same decision function) plus a `search` declaration. Search
adds discovery, never access: every record a search returns is re-decided at
release by `release.source_message_decision` over the record's own fact, and
only a `permit` releases it. See MESSAGE_SEARCH.md.

Ceiling is raw only. p2a's decision skips a permit rule under any other ceiling
(release.py), so a summary-ceiling search grant would promise results that the
access decision can never permit. It is refused at parse instead.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from .contract import (Binding, Decision, EvidenceUse, Generation, HardConstraints, Hash, Identifier, Number,
    Only, Predicate, SourceUniverse, StrictModel, Table, Validity)
from .fact_contract import OwnerAttestedSubjectBinding, RollingEventWindow

CAPABILITY_SEARCH = "permissions-beta/p2c-v1"
EVALUATOR_SEARCH = "hard-rules/p2c-v1"
VIEW_SEARCH = "canonical.message_search.v1"
REQUEST_TYPE_SEARCH = "permissions.v2.search"
SEARCH_CAPABILITIES = (CAPABILITY_SEARCH,)
MAX_PERMITTED_RECORDS_CEILING = 5_000
MAX_K_CEILING = 25
MAX_QUERY_CHARS = 8_000
MAX_SEARCH_BYTES = 256_000
MAX_RECORD_CHARS = 100_000
OpaqueRecordId = Annotated[str, StringConstraints(strict=True, pattern=r"^r\.[0-9a-f]{64}$")]


class SearchVersions(StrictModel):
    vocabulary: Literal["owner-review-vocabulary/v1"]
    capability: Literal["permissions-beta/p2c-v1"]
    subject_binding: OwnerAttestedSubjectBinding


class SearchEvaluator(StrictModel):
    kind: Literal["hard_rules"]
    version: Literal["hard-rules/p2c-v1"]


class SearchOutputForm(StrictModel):
    family: Literal["canonical_record"]
    operation: Literal["search"]
    view_id: Literal["canonical.message_search.v1"]
    tables: list[Table]

    @field_validator("tables")
    @classmethod
    def unique(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("duplicate table")
        return values


class SearchRelease(StrictModel):
    predicate: Predicate
    ceiling: Literal["raw"]
    forms: list[SearchOutputForm]


class SearchRule(StrictModel):
    rule_id: Identifier
    effect: Literal["permit", "deny"]
    evidence_use: EvidenceUse
    release: SearchRelease


class SearchDeclaration(StrictModel):
    """What the owner consented to on top of the rules: search, its view, its bounds."""
    view_id: Literal["canonical.message_search.v1"]
    tables: list[Table]
    max_permitted_records: Annotated[int, Field(strict=True, ge=1, le=MAX_PERMITTED_RECORDS_CEILING)]
    max_k: Annotated[int, Field(strict=True, ge=1, le=MAX_K_CEILING)]
    window: RollingEventWindow

    @field_validator("tables")
    @classmethod
    def tables_nonempty_unique(cls, values):
        if not values or len(set(values)) != len(values):
            raise ValueError("search tables")
        return values


class SearchPolicy(StrictModel):
    version: Literal["topos-policy/v2"]
    policy_version_id: Identifier
    binding: Binding
    versions: SearchVersions
    validity: Validity
    source_universe: SourceUniverse
    hard_constraints: HardConstraints
    rules: list[SearchRule]
    search: SearchDeclaration
    evaluator: SearchEvaluator
    natural_language: None

    @model_validator(mode="after")
    def closed(self):
        # PolicyV2.closed_rules, restated: unique rule ids, sources inside the pinned universe.
        ids = [rule.rule_id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate rule")
        universe = self.source_universe
        for rule in self.rules:
            sources = rule.evidence_use.sources
            if isinstance(sources, Only):
                if not set(sources.values).issubset(universe.source_ids):
                    raise ValueError("source outside pinned universe")
            elif (sources.universe_id, sources.universe_revision) != (universe.universe_id, universe.revision):
                raise ValueError("universe mismatch")
        permitted_tables = {table for rule in self.rules if rule.effect == "permit"
                            for form in rule.release.forms for table in form.tables}
        if not set(self.search.tables) <= permitted_tables:
            raise ValueError("search table outside every permit rule")
        return self


class SearchWindow(StrictModel):
    after: Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
    before: Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]

    @model_validator(mode="after")
    def ordered(self):
        if self.before <= self.after:
            raise ValueError("empty window")
        return self


class SearchIntent(StrictModel):
    """The only recipient-controlled input to a search. No grant, view, table or ceiling."""
    query: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=MAX_QUERY_CHARS)]
    k: Annotated[int, Field(strict=True, ge=1, le=MAX_K_CEILING)]
    window: SearchWindow | None = None


def signed_payload(intent: SearchIntent) -> dict:
    """The one form of a search intent that is hashed into the envelope: absent window, absent key."""
    return SearchIntent.parse(intent.model_dump()).model_dump(exclude_none=True)


class SearchRecord(StrictModel):
    record_id: OpaqueRecordId
    source_id: Identifier
    canonical_table: Table
    event_at: Number
    content: Annotated[str, StringConstraints(strict=True, max_length=MAX_RECORD_CHARS)]


class MessageSearchResult(StrictModel):
    family: Literal["canonical_record"]
    operation: Literal["search"]
    view_id: Literal["canonical.message_search.v1"]
    records: Annotated[list[SearchRecord], Field(max_length=MAX_K_CEILING)]


class SearchMemberDecision(Decision):
    """One fact's p2a decision under a p2c grant. Private; its hash binds the set."""
    evaluator_version: Literal["hard-rules/p2c-v1"]


class SearchSetDecision(StrictModel):
    stage: Literal["output_release"]
    verdict: Literal["permit", "deny"]
    policy_hash: Hash
    candidate_revision: Hash
    evaluator_version: Literal["hard-rules/p2c-v1"]
    matched_allow_clause_ids: list[Identifier]
    matched_deny_clause_ids: Annotated[list[Identifier], Field(max_length=0)]
    reason_code: Literal["rule_permit", "set_refused"]
    required_projection_id: Literal["canonical.message_search.v1"] | None
    member_count: Annotated[int, Field(strict=True, ge=0, le=MAX_K_CEILING)]
    missing_context_codes: Annotated[list[str], Field(max_length=0)]

    @model_validator(mode="after")
    def coherent(self):
        permit = self.verdict == "permit"
        if permit != (self.reason_code == "rule_permit") or permit != (self.required_projection_id is not None):
            raise ValueError("decision incoherent")
        if not permit and (self.member_count or self.matched_allow_clause_ids):
            raise ValueError("refused set names members")
        if len(set(self.matched_allow_clause_ids)) != len(self.matched_allow_clause_ids) \
                or self.matched_allow_clause_ids != sorted(self.matched_allow_clause_ids):
            raise ValueError("clauses not canonical")
        return self


class SearchMemberBinding(StrictModel):
    """Aligned 1:1 with the output records; internal to the node, never sent."""
    table: Table
    source_id: Identifier
    record_id: Identifier
    fact_id: Identifier
    allow_clause_id: Identifier
    member_decision_hash: Hash


def search_capability_document() -> dict:
    return {
        "version": CAPABILITY_SEARCH,
        "capabilities": list(SEARCH_CAPABILITIES),
        "registered_forms": [{"family": "canonical_record", "operation": "search", "view_id": VIEW_SEARCH}],
        "request": {"max_query_chars": MAX_QUERY_CHARS, "max_k": MAX_K_CEILING},
        "max_permitted_records": MAX_PERMITTED_RECORDS_CEILING,
        "ceilings": ["raw"],
        "natural_language": False,
    }

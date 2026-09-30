"""Closed multi-family search contract. Not advertised until all release gates exist.

Result types are explicitly signed. Evidence tables describe contributing source
records, not a license to expose arbitrary columns from a canonical object.
"""
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from .contract import Hash, Identifier, Number, StrictModel
from .search_contract import (SearchPolicy, SearchDeclaration, SearchOutputForm, SearchRelease,
    SearchRule, SearchMemberDecision, SearchSetDecision, OpaqueRecordId)

CAPABILITY_KNOWLEDGE = "permissions-beta/p2c-v3"
VIEW_KNOWLEDGE = "canonical.knowledge_search.v1"
EVALUATOR_KNOWLEDGE = "hard-rules/p2c-v3"
# IF-5 (evidence families, contracts/IF-5_evidence_families.md): only this capability widens the table and
# kind vocabularies. p2a and p2c-v1/v2 keep `contract.Table`, so a message-search grant can never list a
# journal table, and a node that predates a table refuses a knowledge policy naming it at parse.
KnowledgeTable = Literal["conversation_messages", "ai_chat_messages", "journal_entries", "activity_events"]
ResultKind = Literal["message", "fact", "goal", "relationship", "journal_entry", "interest"]
# A raw family releases only from its own table (IF-5 §2).
KIND_TABLES = {"journal_entry": "journal_entries", "interest": "activity_events"}
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=8000)]


class KnowledgeBinding(StrictModel):
    contract: Literal["permissioned_knowledge_v1"]
    authorship: Literal["native_provenance_required"]
    classification: Literal["machine_review_with_owner_corrections/v1"]
    lineage: Literal["complete_permitted_support/v1"]
    exclusions: Literal["item_and_dependencies"]


class KnowledgeVersions(StrictModel):
    vocabulary: Literal["owner-review-vocabulary/v1"]
    capability: Literal["permissions-beta/p2c-v3"]
    subject_binding: KnowledgeBinding


class KnowledgeEvaluator(StrictModel):
    kind: Literal["hard_rules"]
    version: Literal["hard-rules/p2c-v3"]


class KnowledgeOutputForm(SearchOutputForm):
    view_id: Literal["canonical.knowledge_search.v1"]
    tables: list[KnowledgeTable]


class KnowledgeRelease(SearchRelease):
    forms: list[KnowledgeOutputForm]


class KnowledgeRule(SearchRule):
    release: KnowledgeRelease


class KnowledgeDeclaration(SearchDeclaration):
    view_id: Literal["canonical.knowledge_search.v1"]
    max_k: Annotated[int, Field(strict=True, ge=1, le=10)]
    tables: list[KnowledgeTable]
    result_types: list[ResultKind] = Field(min_length=1, max_length=6)
    time_semantics: Literal["underlying_evidence_time/v1"]

    @field_validator("result_types")
    @classmethod
    def unique_types(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("duplicate result type")
        return values


class KnowledgePolicy(SearchPolicy):
    versions: KnowledgeVersions
    rules: list[KnowledgeRule]
    search: KnowledgeDeclaration
    evaluator: KnowledgeEvaluator

    @model_validator(mode="after")
    def kinds_have_their_tables(self):
        # Signing a raw family without listing its table describes a grant the owner never saw: refused at
        # parse on both sides, never discovered at release.
        for kind, table in KIND_TABLES.items():
            if kind in self.search.result_types and table not in self.search.tables:
                raise ValueError("result type without its table")
        return self


class Citation(StrictModel):
    record_id: OpaqueRecordId
    source_id: Identifier
    # Only the permitted source projection, never an unrestricted lookup URL.
    content: Text


class KnowledgeRecord(StrictModel):
    record_id: OpaqueRecordId
    content: Text
    source_ids: list[Identifier] = Field(min_length=1, max_length=20)
    citations: list[Citation] = Field(min_length=1, max_length=20)
    # None carries no time. Exact dates embedded in content are still governed
    # by content classification; this field only controls metadata precision.
    event_at: Number | None = None

    @model_validator(mode="after")
    def exact_sources(self):
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("duplicate source")
        if set(self.source_ids) != {c.source_id for c in self.citations}:
            raise ValueError("citation source mismatch")
        if len({c.record_id for c in self.citations}) != len(self.citations):
            raise ValueError("duplicate citation")
        return self


class MessageResult(KnowledgeRecord):
    kind: Literal["message"]


class FactResult(KnowledgeRecord):
    kind: Literal["fact"]
    assertion: Literal["owner_stated", "inferred"]


class GoalResult(KnowledgeRecord):
    kind: Literal["goal"]
    status: Literal["stated_intention", "active", "completed", "abandoned", "unknown"]


class RelationshipResult(KnowledgeRecord):
    kind: Literal["relationship"]
    subject: Text
    relation: Identifier
    object: Text


class JournalEntryResult(KnowledgeRecord):
    """One journal entry, released only when the grant signs `journal_entry` (IF-5 §3)."""
    kind: Literal["journal_entry"]


class InterestResult(KnowledgeRecord):
    """A monthly browsing interest: an assessed topic label, never a URL, title or host (IF-5 §1.3, §3)."""
    kind: Literal["interest"]
    label: Text
    month: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
    strength: Literal["low", "medium", "high"]


KnowledgeItem = Annotated[MessageResult | FactResult | GoalResult | RelationshipResult | JournalEntryResult
                          | InterestResult, Field(discriminator="kind")]


class KnowledgeSearchResult(StrictModel):
    family: Literal["canonical_record"]
    operation: Literal["search"]
    view_id: Literal["canonical.knowledge_search.v1"]
    records: list[KnowledgeItem] = Field(max_length=10)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({r.record_id for r in self.records}) != len(self.records):
            raise ValueError("duplicate record")
        return self


class KnowledgeMemberDecision(SearchMemberDecision):
    evaluator_version: Literal["hard-rules/p2c-v3"]
    required_projection_id: Literal["canonical.knowledge_search.v1"] | None


class KnowledgeSetDecision(SearchSetDecision):
    evaluator_version: Literal["hard-rules/p2c-v3"]
    required_projection_id: Literal["canonical.knowledge_search.v1"] | None
    member_count: Annotated[int, Field(strict=True, ge=0, le=10)]


class KnowledgeMemberBinding(StrictModel):
    kind: ResultKind
    record_id: OpaqueRecordId
    source_ids: list[Identifier] = Field(min_length=1, max_length=20)
    evidence_tables: list[KnowledgeTable] = Field(min_length=1, max_length=4)
    evidence_revision: Hash
    projection_revision: Hash
    allow_clause_id: Identifier
    member_decision_hash: Hash

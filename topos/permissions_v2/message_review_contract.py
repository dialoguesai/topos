"""Closed owner-only review messages for independently eligible source text."""
from typing import Annotated, Literal
from pydantic import Field
from .contract import Hash, Identifier, Number, StrictModel
from .evidence import EvidenceBinding, EvidenceIdentity, EvidenceRevision

class MessageSnapshot(StrictModel):
    binding: EvidenceBinding
    canonical_file_revision: Hash
    message: EvidenceRevision
    protection_revision: Hash

    @property
    def leaves(self):
        return [self.message]

    @property
    def artifacts(self):
        return []


class MessageClassification(StrictModel):
    evidence: EvidenceRevision
    # Values are the grant vocabulary, not arbitrary model-created categories.
    domains: list[Identifier] = Field(min_length=1, max_length=16)
    sensitivity: Literal["none", "personal", "special", "unknown"]
    authorship: Literal["owner_authored", "other", "unknown"]
    speech: Literal["original_message", "third_party_quote", "mixed", "unknown"]
    independent_copies: Literal["none_known", "present", "unknown"]
    protected_content: Literal["none", "present", "unknown"]


class OwnerMessageReview(StrictModel):
    version: Literal["topos-owner-message-review/v1"]
    review_id: Identifier
    owner_id: Identifier
    reviewed_at: Number
    rubric: Literal["whole-message-owner-review/v1"]
    snapshot: MessageSnapshot
    classifications: list[MessageClassification] = Field(min_length=1, max_length=1)



class MessageLookup(StrictModel):
    identity: EvidenceIdentity


class RecordMessageReview(StrictModel):
    review_id: Identifier
    expected_snapshot: MessageSnapshot
    classification: MessageClassification
    expected_current_review_revision: Hash | None


class MessageReviewQueue(StrictModel):
    after: Number
    before: Number
    limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 10


class MessageReviewPreview(StrictModel):
    snapshot: MessageSnapshot
    content: str
    current_review_revision: Hash | None
    classification: MessageClassification | None = None
    classification_origin: Literal["owner", "automatic", "pending"] = "pending"
    opted_out: bool = False


class MessageReviewPage(StrictModel):
    records: list[MessageReviewPreview] = Field(max_length=20)
    scanned: Annotated[int, Field(strict=True, ge=0, le=200)]
    truncated: bool


class MessageOptOutResult(StrictModel):
    identity: EvidenceIdentity
    opted_out: bool


class MessageReviewResult(StrictModel):
    review: OwnerMessageReview
    review_revision: Hash


class AutomaticReviewRequest(StrictModel):
    after: Number
    before: Number


class AutomaticReviewLookup(StrictModel):
    pass


class AutomaticReviewStatus(StrictModel):
    state: Literal["idle", "running", "complete", "cancelled", "failed"]
    scanned: Number = 0
    assessed: Number = 0
    current: Number = 0
    withheld: Number = 0
    unresolved: Number = 0
    # Machine assessments are preparation, not a grant activation.
    sharing_activated: Literal[False] = False

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


# `queue_page`: the whole window, both message tables, walked by cursor. `queue` above
# stays as it was for clients that predate paging.
MessageTable = Literal["conversation_messages", "ai_chat_messages"]
MAX_REVIEW_ORDER = 500
MAX_PAGE_SCAN = 200


class MessageQueueCursor(StrictModel):
    """Where a page stopped, as its sort key: owner-order rank, newest first, then table and id."""
    rank: Annotated[int, Field(strict=True, ge=0, le=MAX_REVIEW_ORDER)]
    event_at_us: Number
    table: MessageTable
    record_id: Identifier


class MessageRef(StrictModel):
    """One row of an owner-supplied review order. A field left out matches any value."""
    table: MessageTable | None = None
    record_id: Identifier
    source_id: Identifier | None = None
    dataset_id: Identifier | None = None


class MessageQueuePageRequest(StrictModel):
    after: Number
    before: Number
    limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 10
    filter: Literal["all", "withheld_uncertain"] = "all"
    cursor: MessageQueueCursor | None = None
    order: list[MessageRef] = Field(default_factory=list, max_length=MAX_REVIEW_ORDER)


class MessageQueuePage(StrictModel):
    records: list[MessageReviewPreview] = Field(max_length=20)
    scanned: Annotated[int, Field(strict=True, ge=0, le=MAX_PAGE_SCAN)]
    next_cursor: MessageQueueCursor | None
    # Rows after next_cursor. Exact when every one was checked; otherwise an upper bound.
    remaining: Number
    remaining_exact: bool
    order_matched: Number


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

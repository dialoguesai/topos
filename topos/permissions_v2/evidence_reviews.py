"""Strict owner review service. Exact preview contents never enter recipient APIs."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .canonical import PolicyError, canonical_bytes, digest
from .contract import Hash, Identifier, StrictModel
from .evidence import (EvidenceResolver, EvidenceReviewStore, EvidenceRevision, EvidenceSnapshot,
    OwnerEvidenceReview, ReviewedClassification, _json, _key, _owner, _row_revision)

MAX_PREVIEW_BYTES = 524_288


class EvidenceLookup(StrictModel):
    fact_id: Identifier


class RecordEvidenceReview(StrictModel):
    review_id: Identifier
    expected_snapshot: EvidenceSnapshot
    expected_current_review_revision: Hash | None
    classifications: list[ReviewedClassification] = Field(min_length=1, max_length=128)


class RevokeEvidenceReview(StrictModel):
    fact_id: Identifier
    review_id: Identifier
    expected_review_revision: Hash


class NullCell(StrictModel):
    kind: Literal["null"]


class ValueCell(StrictModel):
    kind: Literal["integer", "float", "text", "blob"]
    value: str


Cell = Annotated[Union[NullCell, ValueCell], Field(discriminator="kind")]


class OwnerPreviewRecord(StrictModel):
    evidence: EvidenceRevision
    cells: dict[str, Cell]
    disclosure: Literal["scoped", "owner_only", "unknown"] | None
    protected_record: bool


class QualificationSummary(StrictModel):
    verdict: Literal["qualified", "withheld"]
    reason_code: str


class EvidenceReviewState(StrictModel):
    version: Literal["topos-owner-evidence-review-state/v1"]
    fact_id: Identifier
    current_review: OwnerEvidenceReview | None
    current_review_revision: Hash | None
    qualification: QualificationSummary
    execution_enabled: Literal[False]

    @model_validator(mode="after")
    def correlated_review(self):
        if (self.current_review is None) != (self.current_review_revision is None):
            raise ValueError("review revision pair required")
        if self.current_review is not None and (self.current_review.snapshot.fact_id != self.fact_id
            or digest(self.current_review.model_dump()) != self.current_review_revision):
            raise ValueError("review binding mismatch")
        return self


class OwnerEvidencePreview(EvidenceReviewState):
    version: Literal["topos-owner-evidence-preview/v1"]
    status: Literal["complete", "incomplete"]
    reason_code: str | None
    snapshot: EvidenceSnapshot | None
    records: list[OwnerPreviewRecord] = Field(min_length=1, max_length=128)
    entity_protection_present: bool

    @model_validator(mode="after")
    def complete_snapshot(self):
        if (self.status == "complete") != (self.snapshot is not None):
            raise ValueError("snapshot completeness mismatch")
        if (self.status == "complete") != (self.reason_code is None):
            raise ValueError("incomplete reason required")
        if self.snapshot is not None and self.snapshot.fact_id != self.fact_id:
            raise ValueError("snapshot fact mismatch")
        return self


class EvidenceReviewMutation(StrictModel):
    version: Literal["topos-owner-evidence-review-mutation/v1"]
    action: Literal["recorded", "revoked"]
    review_id: Identifier
    review_revision: Hash
    state: EvidenceReviewState
    execution_enabled: Literal[False]


def _cells(row):
    result = {}
    for name, value in row.items():
        # Resolver-added parent/source hashes is already represented by the evidence
        # revision; it is not a physical SQLite column.
        if name in {"_p2b_parent_revision", "_p2b_source_revision"}:
            continue
        if value is None:
            result[name] = NullCell(kind="null")
        elif type(value) is int:
            result[name] = ValueCell(kind="integer", value=str(value))
        elif type(value) is float:
            result[name] = ValueCell(kind="float", value=value.hex())
        elif type(value) is str:
            result[name] = ValueCell(kind="text", value=value)
        elif type(value) is bytes:
            result[name] = ValueCell(kind="blob", value=value.hex())
        else:
            raise PolicyError("evidence_malformed")
    return result


class EvidenceReviewService:
    """Runtime-owned service; no caller can provide its store or resolver."""
    def __init__(self, resolver: EvidenceResolver, reviews: EvidenceReviewStore):
        if resolver.binding != reviews.binding or resolver._file_revision() != reviews.canonical_file_revision:
            raise PolicyError("review_database_binding")
        self.resolver, self.reviews = resolver, reviews

    def _state(self, conn, floor, fact_id, db):
        current = self.reviews._current_in(db, fact_id)
        try:
            self.resolver._qualified_bundle(conn, floor, fact_id, self.reviews, db)
            qualification = QualificationSummary(verdict="qualified", reason_code="owner_reviewed_current_evidence")
        except PolicyError as exc:
            qualification = QualificationSummary(verdict="withheld", reason_code=exc.code)
        return EvidenceReviewState(version="topos-owner-evidence-review-state/v1", fact_id=fact_id,
            current_review=current, current_review_revision=digest(current.model_dump()) if current else None,
            qualification=qualification, execution_enabled=False)

    def read(self, request: EvidenceLookup) -> EvidenceReviewState:
        _owner(self.resolver.binding)
        request = EvidenceLookup.parse(request.model_dump())
        return self._read_state(request.fact_id, require_fact=True)

    def _read_state(self, fact_id, *, require_fact):
        with self.resolver._read() as (conn, floor):
            self.reviews._observe_clock(conn)
            if require_fact:
                self.resolver._load(conn, self.resolver._identity("signal_objects", fact_id))
            with self.reviews._db() as db:
                return self._state(conn, floor, fact_id, db)

    def preview(self, request: EvidenceLookup) -> OwnerEvidencePreview:
        _owner(self.resolver.binding)
        request = EvidenceLookup.parse(request.model_dump())
        with self.resolver._read() as (conn, floor):
            self.reviews._observe_clock(conn)
            root = self.resolver._identity("signal_objects", request.fact_id)
            root_row = self.resolver._load(conn, root)
            reason, snapshot = None, None
            try:
                snapshot, rows = self.resolver._snapshot(conn, floor, request.fact_id)
                versions = snapshot.artifacts + snapshot.leaves
            except PolicyError as exc:
                # Owner can inspect a bounded root even when its lineage cannot
                # support review. Never invent a partial qualified snapshot.
                reason = exc.code
                versions = [EvidenceRevision(identity=root, revision=_row_revision(root_row))]
                rows = {_key(root): root_row}
            records = []
            for version in versions:
                row = rows[_key(version.identity)]
                disclosure = None
                if version.identity.table == "signal_objects":
                    try:
                        value = _json(row["payload_json"], dict).get("disclosure")
                    except PolicyError:
                        value = None
                    disclosure = value if value in ("scoped", "owner_only") else "unknown"
                protected = conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=? LIMIT 1",
                    (version.identity.table, version.identity.record_id)).fetchone() is not None
                records.append(OwnerPreviewRecord(evidence=version, cells=_cells(row), disclosure=disclosure, protected_record=protected))
            with self.reviews._db() as db:
                state = self._state(conn, floor, request.fact_id, db)
                response = OwnerEvidencePreview(**(state.model_dump() | {"version":"topos-owner-evidence-preview/v1"}),
                    status="complete" if snapshot else "incomplete", reason_code=reason, snapshot=snapshot,
                    records=records, entity_protection_present=conn.execute("SELECT 1 FROM entity_blackholes LIMIT 1").fetchone() is not None)
                try:
                    size = len(canonical_bytes(response.model_dump()))
                except PolicyError as exc:
                    if exc.code == "json_size":
                        raise PolicyError("preview_too_large") from None
                    raise
                if size > MAX_PREVIEW_BYTES:
                    raise PolicyError("preview_too_large")
                return response

    def record(self, request: RecordEvidenceReview, *, now: int) -> EvidenceReviewMutation:
        _owner(self.resolver.binding)
        request = RecordEvidenceReview.parse(request.model_dump())
        review = self.reviews.record_review(resolver=self.resolver, review_id=request.review_id,
            expected_snapshot=request.expected_snapshot, classifications=request.classifications, reviewed_at=now,
            expected_current_review_revision=request.expected_current_review_revision, _server_timestamp_retry=True)
        return EvidenceReviewMutation(version="topos-owner-evidence-review-mutation/v1", action="recorded",
            review_id=review.review_id, review_revision=digest(review.model_dump()),
            state=self.read(EvidenceLookup(fact_id=review.snapshot.fact_id)), execution_enabled=False)

    def revoke(self, request: RevokeEvidenceReview) -> EvidenceReviewMutation:
        _owner(self.resolver.binding)
        request = RevokeEvidenceReview.parse(request.model_dump())
        review = self.reviews.revoke_review(request.review_id, fact_id=request.fact_id,
            expected_review_revision=request.expected_review_revision)
        return EvidenceReviewMutation(version="topos-owner-evidence-review-mutation/v1", action="revoked",
            review_id=review.review_id, review_revision=digest(review.model_dump()),
            state=self._read_state(request.fact_id, require_fact=False), execution_enabled=False)

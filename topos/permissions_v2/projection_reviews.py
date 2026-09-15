"""Authenticated owner output reviews over freshly qualified exact preferences.

Recipient callers cannot supply a candidate, qualification, review or database.
Stored output review is one requirement for future delivery, never a permit.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from .canonical import PolicyError, canonical_bytes, digest
from .contract import Hash, Identifier, StrictModel
from .evidence import EvidenceResolver, EvidenceReviewStore, Qualification, _key, _owner
from .evidence_reviews import EvidenceLookup
from .fact_projection import (FactProjectionCandidate, FactProjectionReview, OutputClassification,
    bind_output_review, prepare_fact_projection)
from topos.storage.db.write_gate import with_db_write

STORE_VERSION = "topos-private-projection-reviews/v1"


class ProjectionQualification(StrictModel):
    verdict: Literal["reviewed", "withheld"]
    reason_code: str


class ProjectionReviewState(StrictModel):
    version: Literal["topos-owner-projection-state/v1"]
    fact_id: Identifier
    current_review: FactProjectionReview | None
    current_review_revision: Hash | None
    qualification: ProjectionQualification
    authorization_status: Literal["not_evaluated"]
    execution_enabled: Literal[False]

    @model_validator(mode="after")
    def correlation(self):
        if (self.current_review is None) != (self.current_review_revision is None):
            raise ValueError("review revision required")
        if self.current_review is not None and (self.current_review.candidate.snapshot.fact_id != self.fact_id
            or digest(self.current_review.model_dump()) != self.current_review_revision
            or self.current_review.status != "approved"):
            raise ValueError("output review binding")
        if self.qualification.verdict == "reviewed" and self.current_review is None:
            raise ValueError("current review required")
        return self


class OwnerProjectionPreview(ProjectionReviewState):
    version: Literal["topos-owner-projection-preview/v1"]
    candidate: FactProjectionCandidate | None
    candidate_hash: Hash | None
    candidate_reason_code: str | None
    minimum_output_sensitivity: Literal["none", "personal", "special"] | None

    @model_validator(mode="after")
    def exact_candidate(self):
        if self.candidate is None:
            if self.candidate_hash is not None or self.minimum_output_sensitivity is not None or not self.candidate_reason_code:
                raise ValueError("withheld candidate fields")
        elif (self.candidate.snapshot.fact_id != self.fact_id or self.candidate_hash != digest(self.candidate.model_dump())
            or self.candidate_reason_code is not None or self.minimum_output_sensitivity is None):
            raise ValueError("candidate binding")
        if self.qualification.verdict == "reviewed":
            order = {"none":0, "personal":1, "special":2}
            if (self.candidate is None or self.current_review is None or self.current_review.candidate != self.candidate
                or order[self.current_review.classification.sensitivity] < order[self.minimum_output_sensitivity]):
                raise ValueError("reviewed candidate binding")
        return self


class RecordProjectionReview(StrictModel):
    review_id: Identifier
    expected_candidate: FactProjectionCandidate
    expected_candidate_hash: Hash
    expected_current_review_revision: Hash | None
    classification: OutputClassification

    @model_validator(mode="after")
    def exact_candidate(self):
        if self.expected_candidate_hash != digest(self.expected_candidate.model_dump()):
            raise ValueError("candidate hash mismatch")
        return self


class RevokeProjectionReview(StrictModel):
    fact_id: Identifier
    review_id: Identifier
    expected_review_revision: Hash


class ProjectionReviewMutation(StrictModel):
    version: Literal["topos-owner-projection-mutation/v1"]
    action: Literal["recorded", "revoked"]
    review_id: Identifier
    review_revision: Hash
    state: ProjectionReviewState
    authorization_status: Literal["not_evaluated"]
    execution_enabled: Literal[False]

    @model_validator(mode="after")
    def exact_state(self):
        if self.action == "recorded":
            if (self.state.current_review is None or self.state.current_review.review_id != self.review_id
                or self.state.current_review_revision != self.review_revision or self.state.qualification.verdict != "reviewed"):
                raise ValueError("recorded output state binding")
        elif self.state.current_review is not None or self.state.qualification.verdict != "withheld":
            raise ValueError("revoked output state binding")
        return self


class ProjectionReviewStore(EvidenceReviewStore):
    """Separate private file, reusing checked identity/clock/write transactions.

    The inherited table layout is intentionally identical; a distinct durable
    contract singleton prevents an evidence-review file becoming an output store.
    No inherited evidence mutation entry point is exposed by this subclass.
    """
    def __init__(self, path, *, resolver, _existing_only=False):
        path = Path(path)
        with with_db_write():
            existed = path.exists()
            super().__init__(path, resolver=resolver, _existing_only=_existing_only)
            with super()._db() as db:
                if not existed:
                    db.execute("CREATE TABLE projection_contract(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version TEXT NOT NULL)")
                    db.execute("INSERT INTO projection_contract VALUES(1,?)", (STORE_VERSION,))
                self._contract(db)

    @staticmethod
    def _contract(db):
        if db.execute("SELECT singleton,version FROM projection_contract").fetchall() != [(1, STORE_VERSION)]:
            raise PolicyError("projection_store_binding")

    @contextmanager
    def _db(self, *, initializing=False):
        with super()._db(initializing=initializing) as db:
            if not initializing:
                self._contract(db)
            yield db

    @staticmethod
    def _current_in(db, fact_id):
        rows = db.execute("SELECT review_json FROM fact_reviews WHERE fact_id=? AND active=1", (fact_id,)).fetchmany(2)
        if len(rows) > 1:
            raise PolicyError("output_review_ambiguous")
        review = FactProjectionReview.parse(rows[0][0]) if rows else None
        if review is not None and (review.status != "approved" or review.candidate.snapshot.fact_id != fact_id):
            raise PolicyError("output_review_binding")
        return review

    def record_review(self, **_kwargs):
        raise PolicyError("projection_service_required")

    def revoke_review(self, *_args, **_kwargs):
        raise PolicyError("projection_service_required")


class ProjectionReviewService:
    def __init__(self, resolver: EvidenceResolver, evidence_reviews: EvidenceReviewStore, outputs: ProjectionReviewStore):
        if (resolver.binding != evidence_reviews.binding or resolver.binding != outputs.binding
            or resolver._file_revision() != evidence_reviews.canonical_file_revision
            or resolver._file_revision() != outputs.canonical_file_revision
            or evidence_reviews.path == outputs.path
            or evidence_reviews._file_identity == outputs._file_identity
            or evidence_reviews.store_id == outputs.store_id):
            raise PolicyError("projection_service_binding")
        self.resolver, self.evidence_reviews, self.outputs = resolver, evidence_reviews, outputs

    @contextmanager
    def _transaction(self):
        with self.resolver._read() as (conn, floor):
            self.evidence_reviews._observe_clock(conn)
            self.outputs._observe_clock(conn)
            with self.evidence_reviews._db() as evidence_db, self.outputs._db() as output_db:
                yield conn, floor, evidence_db, output_db

    def _candidate(self, conn, floor, fact_id, evidence_db):
        evidence, rows = self.resolver._qualified_bundle(conn, floor, fact_id, self.evidence_reviews, evidence_db)
        qualification = Qualification(verdict="qualified", reason_code="owner_reviewed_current_evidence", evidence=evidence)
        root = next(ref for ref in evidence.snapshot.artifacts if ref.identity.record_id == fact_id)
        row = rows[_key(root.identity)]
        candidate = prepare_fact_projection(qualification=qualification, fact_row=row)
        return qualification, row, candidate, rows

    def _state(self, conn, floor, fact_id, evidence_db, output_db, *, now):
        current = self.outputs._current_in(output_db, fact_id)
        candidate, candidate_reason, minimum = None, None, None
        try:
            qualification, row, candidate, _rows = self._candidate(conn, floor, fact_id, evidence_db)
            order = {"none": 0, "personal": 1, "special": 2}
            minimum = max((item.sensitivity for item in qualification.evidence.classifications), key=order.__getitem__)
            bind_output_review(qualification=qualification, fact_row=row, review=current, now=now)
            status = ProjectionQualification(verdict="reviewed", reason_code="owner_reviewed_current_projection")
        except PolicyError as exc:
            if candidate is None:
                candidate_reason = exc.code
            status = ProjectionQualification(verdict="withheld", reason_code=exc.code)
        state = ProjectionReviewState(version="topos-owner-projection-state/v1", fact_id=fact_id,
            current_review=current, current_review_revision=digest(current.model_dump()) if current else None,
            qualification=status, authorization_status="not_evaluated", execution_enabled=False)
        return state, candidate, candidate_reason, minimum

    def preview(self, request: EvidenceLookup, *, now: int) -> OwnerProjectionPreview:
        _owner(self.resolver.binding)
        request = EvidenceLookup.parse(request.model_dump())
        with self._transaction() as (conn, floor, evidence_db, output_db):
            state, candidate, reason, minimum = self._state(conn, floor, request.fact_id, evidence_db, output_db, now=now)
            return OwnerProjectionPreview(**(state.model_dump() | {"version":"topos-owner-projection-preview/v1"}),
                candidate=candidate, candidate_hash=digest(candidate.model_dump()) if candidate else None,
                candidate_reason_code=reason, minimum_output_sensitivity=minimum)

    def read(self, request: EvidenceLookup, *, now: int) -> ProjectionReviewState:
        _owner(self.resolver.binding)
        request = EvidenceLookup.parse(request.model_dump())
        with self._transaction() as (conn, floor, evidence_db, output_db):
            return self._state(conn, floor, request.fact_id, evidence_db, output_db, now=now)[0]

    def record(self, request: RecordProjectionReview, *, now: int) -> ProjectionReviewMutation:
        _owner(self.resolver.binding)
        request = RecordProjectionReview.parse(request.model_dump())
        fact_id = request.expected_candidate.snapshot.fact_id
        with self._transaction() as (conn, floor, evidence_db, output_db):
            qualification, row, candidate, _rows = self._candidate(conn, floor, fact_id, evidence_db)
            if candidate != request.expected_candidate:
                raise PolicyError("output_review_stale")
            current = self.outputs._current_in(output_db, fact_id)
            existing = output_db.execute("SELECT review_json,active FROM fact_reviews WHERE review_id=?", (request.review_id,)).fetchone()
            review = FactProjectionReview.parse({"version":"topos-fact-projection-review/v1", "review_id":request.review_id,
                "owner_id":self.resolver.binding.owner_id, "reviewed_at":now, "status":"approved",
                "candidate":candidate.model_dump(), "candidate_hash":request.expected_candidate_hash,
                "output_hash":digest(candidate.output.model_dump()), "classification":request.classification.model_dump()})
            if existing:
                old = FactProjectionReview.parse(existing[0])
                if (existing[1] != 1 or current is None or current.review_id != old.review_id
                    or old.model_dump(exclude={"reviewed_at"}) != review.model_dump(exclude={"reviewed_at"})):
                    raise PolicyError("output_review_id_conflict")
                review = old
            elif (digest(current.model_dump()) if current else None) != request.expected_current_review_revision:
                raise PolicyError("output_review_conflict")
            bind_output_review(qualification=qualification, fact_row=row, review=review, now=now)
            if not existing:
                output_db.execute("UPDATE fact_reviews SET active=0 WHERE fact_id=?", (fact_id,))
                output_db.execute("INSERT INTO fact_reviews VALUES(?,?,?,1)", (review.review_id, fact_id, canonical_bytes(review.model_dump()).decode("ascii")))
            state = self._state(conn, floor, fact_id, evidence_db, output_db, now=now)[0]
            return ProjectionReviewMutation(version="topos-owner-projection-mutation/v1", action="recorded", review_id=review.review_id,
                review_revision=digest(review.model_dump()), state=state, authorization_status="not_evaluated", execution_enabled=False)

    def revoke(self, request: RevokeProjectionReview, *, now: int) -> ProjectionReviewMutation:
        _owner(self.resolver.binding)
        request = RevokeProjectionReview.parse(request.model_dump())
        with self._transaction() as (conn, floor, evidence_db, output_db):
            existing = output_db.execute("SELECT review_json,active FROM fact_reviews WHERE review_id=? AND fact_id=?", (request.review_id, request.fact_id)).fetchone()
            if existing is None:
                raise PolicyError("output_review_unknown")
            review = FactProjectionReview.parse(existing[0])
            if review.candidate.snapshot.fact_id != request.fact_id or digest(review.model_dump()) != request.expected_review_revision:
                raise PolicyError("output_review_conflict")
            current = self.outputs._current_in(output_db, request.fact_id)
            if current is not None and current.review_id != review.review_id:
                raise PolicyError("output_review_conflict")
            output_db.execute("UPDATE fact_reviews SET active=0 WHERE review_id=?", (review.review_id,))
            state = self._state(conn, floor, request.fact_id, evidence_db, output_db, now=now)[0]
            return ProjectionReviewMutation(version="topos-owner-projection-mutation/v1", action="revoked", review_id=review.review_id,
                review_revision=digest(review.model_dump()), state=state, authorization_status="not_evaluated", execution_enabled=False)

    def with_reviewed(self, fact_id: str, *, now: int, callback):
        """Only a trusted node release adapter may supply this in-process callback.

        Holds canonical, evidence-review and output-review gates through callback
        completion. The callback still must perform signed policy/final-send checks.
        """
        with self._transaction() as (conn, floor, evidence_db, output_db):
            qualification, row, candidate, rows = self._candidate(conn, floor, fact_id, evidence_db)
            current = self.outputs._current_in(output_db, fact_id)
            reviewed = bind_output_review(qualification=qualification, fact_row=row, review=current, now=now)
            return callback(qualification.evidence, reviewed, rows)

"""Strict owner review service. Exact preview contents never enter recipient APIs.

Under implicit review (EVIDENCE.md) a qualifying fact is available unless the owner
deselected it, so besides the explicit review surface this module carries the
owner's review queue -- every current fact, least confident first, with its
labels, its standing and its terminal source count -- and the opt-out / opt-in
mutations that are the owner's deselection. All of it is owner-only.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, StringConstraints, model_validator

from .canonical import PolicyError, canonical_bytes, digest
from .contract import Hash, Identifier, Number, StrictModel
from .evidence import (QUALIFIED_REASON, EvidenceResolver, EvidenceReviewStore, EvidenceRevision, EvidenceSnapshot,
    OwnerEvidenceReview, ReviewedClassification, _json, _key, _owner, _row_revision, implicit_labels)
from .identity import ATTESTED_CONTRACT

MAX_PREVIEW_BYTES = 524_288
MAX_QUEUE_PAGE = 200
MAX_DISPLAY_CHARS = 200
ReviewMode = Literal["explicit", "implicit", "opted_out"]


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
    # How the fact stands: an explicit owner review, the node's implicit one (the owner has neither
    # reviewed nor deselected it), or deselected by the owner. `qualification` says whether it
    # currently qualifies under that standing.
    review_mode: ReviewMode
    opted_out: bool

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


class ReviewQueueRequest(StrictModel):
    """A page of the owner's review queue: least confident first, so the owner reviews what the model doubted."""
    offset: Number = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=MAX_QUEUE_PAGE)] = 50
    source_id: Identifier | None = None
    include_opted_out: bool = True


class ReviewQueueItem(StrictModel):
    fact_id: Identifier
    # The extractor's confidence as an integer per mille (0.7 -> 700): canonical JSON carries no floats.
    confidence_permille: Annotated[int, Field(strict=True, ge=0, le=1000)]
    altitude: str | None
    disclosure: Literal["scoped", "owner_only", "unknown"] | None
    asserted_by: str | None
    # The fact's own claim, for the owner: never a terminal message's text.
    predicate: Annotated[str, StringConstraints(max_length=MAX_DISPLAY_CHARS)] | None
    object_value: Annotated[str, StringConstraints(max_length=MAX_DISPLAY_CHARS)] | None
    dimension: str | None
    # The labels the fact carries under implicit review (owner-review-vocabulary/v1).
    domains: list[str]
    sensitivity: Literal["none", "personal", "special"]
    source_ids: list[str]
    terminal_source_count: Number
    review_mode: ReviewMode
    opted_out: bool
    qualification: QualificationSummary


class ReviewQueuePage(StrictModel):
    version: Literal["topos-owner-review-queue/v1"]
    items: list[ReviewQueueItem]
    total: Number
    offset: Number
    limit: Number
    execution_enabled: Literal[False]


class ReviewTotalsRequest(StrictModel):
    pass


class SourceReviewTotals(StrictModel):
    source_id: Identifier | None
    facts: Number
    qualifying: Number
    opted_out: Number
    withheld: Number


class ReviewTotals(StrictModel):
    """"N of M facts shareable", overall and per source."""
    version: Literal["topos-owner-review-totals/v1"]
    facts: Number
    qualifying: Number
    opted_out: Number
    withheld: Number
    sources: list[SourceReviewTotals]
    execution_enabled: Literal[False]


class FactOptOut(StrictModel):
    fact_id: Identifier
    note: Annotated[str, StringConstraints(max_length=MAX_DISPLAY_CHARS)] | None = None


class FactOptIn(StrictModel):
    fact_id: Identifier


class OptOutMutation(StrictModel):
    version: Literal["topos-owner-evidence-opt-out-mutation/v1"]
    action: Literal["opted_out", "opted_in"]
    changed: bool
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
        opted_out = self.reviews._opted_out_in(db, fact_id)
        qualification, _evidence = self._qualification(conn, floor, fact_id, db)
        return EvidenceReviewState(version="topos-owner-evidence-review-state/v1", fact_id=fact_id,
            current_review=current, current_review_revision=digest(current.model_dump()) if current else None,
            qualification=qualification, execution_enabled=False,
            review_mode="opted_out" if opted_out else ("explicit" if current else "implicit"), opted_out=opted_out)

    def _qualification(self, conn, floor, fact_id, db, *, contract=None):
        try:
            kwargs = {"contract": contract} if contract else {}
            evidence, _rows = self.resolver._qualified_bundle(conn, floor, fact_id, self.reviews, db, **kwargs)
            return QualificationSummary(verdict="qualified", reason_code=QUALIFIED_REASON[evidence.review_mode]), evidence
        except PolicyError as exc:
            return QualificationSummary(verdict="withheld", reason_code=exc.code), None

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
                versions = [EvidenceRevision(identity=root, revision=_row_revision(root_row, table="signal_objects"))]
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

    # --- the owner's review queue under implicit review ----------------------------------------

    @staticmethod
    def _candidates(conn):
        """Every current fact, least confident first: the order the owner should review in."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(signal_objects)")}
        altitude = "altitude" if "altitude" in columns else "NULL AS altitude"
        return conn.execute(f"SELECT object_id, confidence, signal_dimension, payload_json, source_refs_json, {altitude} "
                            "FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL "
                            "ORDER BY confidence ASC, object_id ASC").fetchall()

    @staticmethod
    def _sources_of(raw_refs) -> list[str]:
        try:
            refs = _json(raw_refs, list)
        except PolicyError:
            return []
        found = []
        for ref in refs:
            source = ref.get("source_id") if isinstance(ref, dict) else None
            if isinstance(source, str) and source and source not in found:
                found.append(source)
        return sorted(found)

    def _item(self, conn, floor, row, db) -> ReviewQueueItem:
        fact_id = row["object_id"]
        try:
            payload = _json(row["payload_json"], dict)
        except PolicyError:
            payload = {}
        domains, sensitivity = implicit_labels(payload, row["signal_dimension"])
        sources = self._sources_of(row["source_refs_json"])
        qualification, evidence = self._qualification(conn, floor, fact_id, db, contract=ATTESTED_CONTRACT)
        current = self.reviews._current_in(db, fact_id)
        opted_out = self.reviews._opted_out_in(db, fact_id)
        try:
            refs = len(_json(row["source_refs_json"], list))
        except PolicyError:
            refs = 0
        disclosure = payload.get("disclosure")
        text = lambda value: (value[:MAX_DISPLAY_CHARS] if isinstance(value, str) else None)  # noqa: E731
        confidence = min(max(float(row["confidence"] or 0.0), 0.0), 1.0)
        return ReviewQueueItem(fact_id=fact_id, confidence_permille=int(round(confidence * 1000)),
            altitude=text(row["altitude"]) if row["altitude"] is not None else text(payload.get("altitude")),
            disclosure=disclosure if disclosure in ("scoped", "owner_only") else ("unknown" if disclosure is not None else None),
            asserted_by=text(payload.get("asserted_by")), predicate=text(payload.get("predicate")),
            object_value=text(payload.get("object_value")), dimension=text(row["signal_dimension"]),
            domains=list(domains), sensitivity=sensitivity, source_ids=sources,
            terminal_source_count=len(evidence.snapshot.leaves) if evidence is not None else refs,
            review_mode="opted_out" if opted_out else ("explicit" if current else "implicit"), opted_out=opted_out,
            qualification=qualification)

    def queue(self, request: ReviewQueueRequest) -> ReviewQueuePage:
        _owner(self.resolver.binding)
        request = ReviewQueueRequest.parse(request.model_dump())
        with self.resolver._read() as (conn, floor):
            self.reviews._observe_clock(conn)
            with self.reviews._db() as db:
                opted_out = self.reviews._opt_outs_in(db)
                rows = [row for row in self._candidates(conn)
                        if (request.source_id is None or request.source_id in self._sources_of(row["source_refs_json"]))
                        and (request.include_opted_out or row["object_id"] not in opted_out)]
                page = rows[request.offset:request.offset + request.limit]
                return ReviewQueuePage(version="topos-owner-review-queue/v1", items=[self._item(conn, floor, row, db) for row in page],
                    total=len(rows), offset=request.offset, limit=request.limit, execution_enabled=False)

    def totals(self, request: ReviewTotalsRequest) -> ReviewTotals:
        """Qualifying, deselected and withheld counts, overall and per source: "N of M facts shareable"."""
        _owner(self.resolver.binding)
        ReviewTotalsRequest.parse(request.model_dump())
        counts: dict = {}
        overall = {"facts": 0, "qualifying": 0, "opted_out": 0, "withheld": 0}

        def bump(bucket, key):
            bucket["facts"] += 1
            bucket[key] += 1
        with self.resolver._read() as (conn, floor):
            self.reviews._observe_clock(conn)
            with self.reviews._db() as db:
                for row in self._candidates(conn):
                    qualification, _ = self._qualification(conn, floor, row["object_id"], db, contract=ATTESTED_CONTRACT)
                    key = ("qualifying" if qualification.verdict == "qualified"
                           else "opted_out" if qualification.reason_code == "owner_opted_out" else "withheld")
                    bump(overall, key)
                    for source in self._sources_of(row["source_refs_json"]) or [None]:
                        bump(counts.setdefault(source, {"facts": 0, "qualifying": 0, "opted_out": 0, "withheld": 0}), key)
        sources = [SourceReviewTotals(source_id=source, **counts[source])
                   for source in sorted(counts, key=lambda value: (value is None, value or ""))]
        return ReviewTotals(version="topos-owner-review-totals/v1", **overall, sources=sources, execution_enabled=False)

    def opt_out(self, request: FactOptOut, *, now: int) -> OptOutMutation:
        _owner(self.resolver.binding)
        request = FactOptOut.parse(request.model_dump())
        changed = self.reviews.opt_out(request.fact_id, now=now, note=request.note)
        return OptOutMutation(version="topos-owner-evidence-opt-out-mutation/v1", action="opted_out", changed=changed,
            state=self._read_state(request.fact_id, require_fact=False), execution_enabled=False)

    def opt_in(self, request: FactOptIn) -> OptOutMutation:
        _owner(self.resolver.binding)
        request = FactOptIn.parse(request.model_dump())
        changed = self.reviews.opt_in(request.fact_id)
        return OptOutMutation(version="topos-owner-evidence-opt-out-mutation/v1", action="opted_in", changed=changed,
            state=self._read_state(request.fact_id, require_fact=False), execution_enabled=False)

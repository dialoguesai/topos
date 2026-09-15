"""Separate explicit output review enrollment, requiring existing evidence enrollment."""
from typing import Literal

from .evidence_review_runtime import ReviewEnrollment, ReviewEnrollmentRuntime
from .projection_reviews import ProjectionReviewService, ProjectionReviewStore


class ProjectionEnrollment(ReviewEnrollment):
    version: Literal["topos-owner-projection-enrollment/v1"]


class ProjectionEnrollmentRuntime(ReviewEnrollmentRuntime):
    enrollment_type = ProjectionEnrollment
    enrollment_version = "topos-owner-projection-enrollment/v1"
    store_type = ProjectionReviewStore
    not_enrolled = "projection_reviews_not_enrolled"

    def __init__(self, *, evidence_service, **kwargs):
        super().__init__(**kwargs)
        self.evidence_service = evidence_service

    def _make_service(self, store):
        return ProjectionReviewService(self.resolver, self.evidence_service.reviews, store)

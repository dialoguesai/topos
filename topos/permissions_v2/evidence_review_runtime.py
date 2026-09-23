"""Explicit private review-store enrollment; existing-only access never creates.

The marker is the store's external durable floor. It binds the resource, the
canonical database's durable clock identity, the store path, the random store
identity persisted inside the store, and a digest of every review row. Every
review mutation publishes the marker as pending before the SQLite commit and
active after it, so a crash in either order leaves enrollment closed and an
in-place restore of an older store file is refused as rollback. Device and
inode numbers are never persisted: a bind mount renumbers them across a VM
restart, which would otherwise close a healthy store forever.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
from typing import Literal

from .canonical import PolicyError, canonical_bytes
from .contract import Generation, Hash, StrictModel
from .evidence import EvidenceBinding, EvidenceResolver, EvidenceReviewStore, _checked_file, _owner
from .evidence_reviews import EvidenceReviewService
from topos.storage.db.write_gate import with_db_write


class ReviewEnrollment(StrictModel):
    version: Literal["topos-owner-evidence-enrollment/v2"]
    state: Literal["pending", "active"]
    binding: EvidenceBinding
    canonical_file_revision: Hash
    review_store_path: str
    store_id: Hash | None
    authority_digest: Hash | None
    revision: Generation


def _write_new(path, body):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_bytes(body.model_dump()))
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise PolicyError("review_enrollment_unavailable") from None


def _sync_directory(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        raise PolicyError("review_enrollment_unavailable") from None


class ReviewEnrollmentRuntime:
    enrollment_type = ReviewEnrollment
    enrollment_version = "topos-owner-evidence-enrollment/v2"
    store_type = EvidenceReviewStore
    not_enrolled = "evidence_reviews_not_enrolled"

    def __init__(self, *, canonical_database: Path, binding: EvidenceBinding, path: Path):
        self.resolver = EvidenceResolver(canonical_database, binding=binding)
        self.path = path
        self.marker = path.with_name(path.name + ".enrollment.json")
        self._service = None
        self._enrollment = None
        self._store = None

    def _make_service(self, store):
        return EvidenceReviewService(self.resolver, store)

    def _load_marker(self):
        info = _checked_file(self.marker, code="review_enrollment_unavailable")
        if info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise PolicyError("review_enrollment_unavailable")
        try:
            if info.st_size > 8192:
                raise PolicyError("review_enrollment_unavailable")
            result = self.enrollment_type.parse(self.marker.read_bytes())
        except OSError:
            raise PolicyError("review_enrollment_unavailable") from None
        if (result.binding != self.resolver.binding or result.canonical_file_revision != self.resolver._file_revision()
            or result.review_store_path != str(self.path)):
            raise PolicyError("review_enrollment_unavailable")
        return result

    def _read_marker(self):
        result = self._load_marker()
        if result.state != "active" or result.store_id is None or result.authority_digest is None:
            raise PolicyError("review_enrollment_unavailable")
        _checked_file(self.path, code="review_database_binding")
        if self._enrollment is not None and (result.revision < self._enrollment.revision
            or (result.revision == self._enrollment.revision and result != self._enrollment)):
            raise PolicyError("review_enrollment_unavailable")
        return result

    def _replace_marker(self, body):
        temporary = self.marker.with_name(self.marker.name + "." + secrets.token_hex(8))
        _write_new(temporary, body)
        try:
            os.replace(temporary, self.marker)
        except OSError:
            raise PolicyError("review_enrollment_unavailable") from None
        _sync_directory(self.marker.parent)

    # --- external rollback floor used by the enrolled store ---------------------

    def expected_authority_digest(self) -> str:
        if self._enrollment is None or self._enrollment.authority_digest is None:
            raise PolicyError("review_enrollment_unavailable")
        return self._enrollment.authority_digest

    def publish_pending(self, authority_digest: str) -> None:
        current = self._read_marker()
        self._replace_marker(current.model_copy(update={"state": "pending", "authority_digest": authority_digest,
            "revision": current.revision + 1}))

    def publish_active(self, authority_digest: str) -> None:
        pending = self._load_marker()
        stable = {"state", "authority_digest", "revision"}
        if (self._enrollment is None or pending.state != "pending" or pending.authority_digest != authority_digest
            or pending.revision != self._enrollment.revision + 1
            or pending.model_dump(exclude=stable) != self._enrollment.model_dump(exclude=stable)):
            raise PolicyError("review_enrollment_unavailable")
        active = pending.model_copy(update={"state": "active"})
        self._replace_marker(active)
        self._enrollment = active

    def get(self, *, require_existing: bool = True) -> EvidenceReviewService:
        with with_db_write():
            if not self.marker.exists():
                if self._enrollment is not None or self.path.exists() or self.marker.is_symlink():
                    raise PolicyError("review_enrollment_unavailable")
                if require_existing:
                    raise PolicyError(self.not_enrolled)
                _owner(self.resolver.binding)
                _checked_file(self.path, code="review_database_binding", may_create=True)
                _checked_file(self.marker, code="review_enrollment_unavailable", may_create=True)
                pending = self.enrollment_type(version=self.enrollment_version, state="pending",
                    binding=self.resolver.binding, canonical_file_revision=self.resolver._file_revision(),
                    review_store_path=str(self.path), store_id=None, authority_digest=None, revision=1)
                # Persist intent first. A crash at any later point must require
                # deliberate recovery, never silently create a replacement store.
                _write_new(self.marker, pending)
                _sync_directory(self.marker.parent)
                reviews = self.store_type(self.path, resolver=self.resolver)
                self._replace_marker(pending.model_copy(update={"state": "active", "store_id": reviews.store_id,
                    "authority_digest": reviews.current_authority_digest()}))
                reviews._floor = self
                self._store = reviews
                self._service = self._make_service(reviews)
            enrollment = self._read_marker()
            if self._service is None:
                reviews = self.store_type(self.path, resolver=self.resolver, _existing_only=True)
                if reviews.store_id != enrollment.store_id:
                    raise PolicyError("review_database_binding")
                reviews._floor = self
                self._store = reviews
                self._service = self._make_service(reviews)
            elif self._store.store_id != enrollment.store_id:
                raise PolicyError("review_database_binding")
            self._enrollment = enrollment
            self._store._check_file()
            return self._service

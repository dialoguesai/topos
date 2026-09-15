"""Explicit private review-store enrollment; existing-only access never creates."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
from typing import Literal

from .canonical import PolicyError, canonical_bytes
from .contract import Hash, StrictModel
from .evidence import EvidenceBinding, EvidenceResolver, EvidenceReviewStore, _checked_file, _owner
from .evidence_reviews import EvidenceReviewService
from topos.storage.db.write_gate import with_db_write


class ReviewEnrollment(StrictModel):
    version: Literal["topos-owner-evidence-enrollment/v1"]
    state: Literal["pending", "active"]
    binding: EvidenceBinding
    canonical_file_revision: Hash
    review_store_path: str
    store_device: str | None
    store_inode: str | None


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
    enrollment_version = "topos-owner-evidence-enrollment/v1"
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

    def _read_marker(self):
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
            or result.review_store_path != str(self.path) or result.state != "active"):
            raise PolicyError("review_enrollment_unavailable")
        info = _checked_file(self.path, code="review_database_binding")
        if (result.store_device, result.store_inode) != (str(info.st_dev), str(info.st_ino)):
            raise PolicyError("review_database_binding")
        if self._enrollment is not None and result != self._enrollment:
            raise PolicyError("review_enrollment_unavailable")
        return result

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
                    review_store_path=str(self.path), store_device=None, store_inode=None)
                # Persist intent first. A crash at any later point must require
                # deliberate recovery, never silently create a replacement store.
                _write_new(self.marker, pending)
                _sync_directory(self.marker.parent)
                reviews = self.store_type(self.path, resolver=self.resolver)
                active = pending.model_copy(update={"state":"active", "store_device":str(reviews._file_identity[0]),
                    "store_inode":str(reviews._file_identity[1])})
                temporary = self.marker.with_name(self.marker.name + "." + secrets.token_hex(8))
                _write_new(temporary, active)
                try:
                    os.replace(temporary, self.marker)
                except OSError:
                    raise PolicyError("review_enrollment_unavailable") from None
                _sync_directory(self.marker.parent)
                self._store = reviews
                self._service = self._make_service(reviews)
            enrollment = self._read_marker()
            if self._service is None:
                reviews = self.store_type(self.path, resolver=self.resolver, _existing_only=True)
                self._store = reviews
                self._service = self._make_service(reviews)
            self._enrollment = enrollment
            self._store._check_file()
            return self._service

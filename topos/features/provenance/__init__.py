"""Provenance: record roles and posture plumbing (PLAN_PROVENANCE_SPLIT P1)."""

from .posture import make_posture_resolver
from .roles import (
    ROLE_ADDRESSED,
    ROLE_AMBIENT,
    ROLE_AUTHORED,
    ROLE_OBSERVED,
    ROLE_PARTICIPATED,
    ROLES,
    is_engaged,
    owner_authored,
    record_role,
)

__all__ = [
    "ROLE_ADDRESSED",
    "ROLE_AMBIENT",
    "ROLE_AUTHORED",
    "ROLE_OBSERVED",
    "ROLE_PARTICIPATED",
    "ROLES",
    "is_engaged",
    "make_posture_resolver",
    "owner_authored",
    "record_role",
]

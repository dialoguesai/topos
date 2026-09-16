"""Explicit closed capability dispatch; never fall back after a parse failure."""
from __future__ import annotations

from .canonical import PolicyError, parse_json
from .contract import PolicyV2, Decision, MessageDisclosure
from .fact_contract import (AttestedSubjectFactDecision, AttestedSubjectFactPolicy, FactPolicyV2,
    FactDecision, FactScalarDisclosure, StatedDayFactPolicy, StatedDayFactDecision)

Policy = PolicyV2 | FactPolicyV2 | StatedDayFactPolicy | AttestedSubjectFactPolicy
PolicyDecision = Decision | FactDecision | StatedDayFactDecision | AttestedSubjectFactDecision
Disclosure = MessageDisclosure | FactScalarDisclosure


def value_of(raw):
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    return parse_json(raw) if isinstance(raw, (str, bytes)) else raw


def parse_policy(raw) -> Policy:
    raw = value_of(raw)
    if type(raw) is not dict or type(raw.get("versions")) is not dict:
        raise PolicyError("unsupported_capability")
    capability = raw["versions"].get("capability")
    if capability == "permissions-beta/p2a-v1":
        return PolicyV2.parse(raw)
    if capability == "permissions-beta/p2b-v1":
        return FactPolicyV2.parse(raw)
    if capability == "permissions-beta/p2b-v2":
        return StatedDayFactPolicy.parse(raw)
    if capability == "permissions-beta/p2b-v3":
        return AttestedSubjectFactPolicy.parse(raw)
    raise PolicyError("unsupported_capability")


def parse_decision(raw, *, capability: str) -> PolicyDecision:
    if capability == "permissions-beta/p2a-v1":
        return Decision.parse(value_of(raw))
    if capability == "permissions-beta/p2b-v1":
        return FactDecision.parse(value_of(raw))
    if capability == "permissions-beta/p2b-v2":
        return StatedDayFactDecision.parse(value_of(raw))
    if capability == "permissions-beta/p2b-v3":
        return AttestedSubjectFactDecision.parse(value_of(raw))
    raise PolicyError("unsupported_capability")


def parse_disclosure(raw, *, capability: str) -> Disclosure:
    if capability == "permissions-beta/p2a-v1":
        return MessageDisclosure.parse(value_of(raw))
    if capability in ("permissions-beta/p2b-v1", "permissions-beta/p2b-v2", "permissions-beta/p2b-v3"):
        return FactScalarDisclosure.parse(value_of(raw))
    raise PolicyError("unsupported_capability")

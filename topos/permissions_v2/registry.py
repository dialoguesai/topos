"""Explicit closed capability dispatch; never fall back after a parse failure."""
from __future__ import annotations

from typing import Literal

from .canonical import PolicyError, parse_json
from .contract import (CAPABILITY_OPAQUE, SOURCE_CAPABILITIES, Identifier, PolicyV2, Decision, MessageDisclosure,
    OutputForm, Release, Rule, StrictModel)
from .fact_contract import (AttestedSubjectFactDecision, AttestedSubjectFactPolicy, FactPolicyV2,
    FactDecision, FactScalarDisclosure, OwnerAttestedSubjectBinding, StatedDayFactPolicy, StatedDayFactDecision,
    WorkFactDecision, WorkFactPolicy, WorkScalarDisclosure)
from .search_contract import CAPABILITY_SEARCH, MessageSearchResult, SearchPolicy, SearchSetDecision


class AttestedSourceVersions(StrictModel):
    vocabulary: Identifier
    capability: Literal["permissions-beta/p2a-v2"]
    subject_binding: OwnerAttestedSubjectBinding


class AttestedSourceEvaluator(StrictModel):
    kind: Literal["hard_rules"]
    version: Literal["hard-rules/p2a-v2"]


class AttestedSubjectSourcePolicy(PolicyV2):
    """p2a-v1's raw message grammar, rules and view, on the owner-attested subject rule.

    p2a-v1 reads the frozen legacy rule (exactly one `is_self` row, never an
    attestation) while the fact labels read attestations, so withdrawing one
    stopped label grants but not raw grants. This capability carries the fact
    contract's own subject block by composition, the same class p2b-v3 carries,
    so both families name one rule. The v1 class is untouched and a v1 document
    never parses here, nor this one there; the registry dispatches on the
    capability literal and the release adapter picks the rule from it.

    It is defined here because it composes that block, and fact_contract imports
    contract. Subclassing the v1 class keeps rule correlation and the pinned
    source universe; no dispatch in this package is an isinstance test on it.
    """
    versions: AttestedSourceVersions
    evaluator: AttestedSourceEvaluator


class AttestedSubjectSourceDecision(Decision):
    evaluator_version: Literal["hard-rules/p2a-v2"]


class OpaqueSourceVersions(AttestedSourceVersions):
    capability: Literal["permissions-beta/p2a-v3"]


class OpaqueSourceEvaluator(StrictModel):
    kind: Literal["hard_rules"]
    version: Literal["hard-rules/p2a-v3"]


class OpaqueOutputForm(OutputForm):
    view_id: Literal["canonical.message_disclosure.v2"]


class OpaqueRelease(Release):
    forms: list[OpaqueOutputForm]


class OpaqueRule(Rule):
    release: OpaqueRelease


class OpaqueSubjectSourcePolicy(PolicyV2):
    """p2a-v3: p2a-v2's grammar, rules and subject rule, releasing the opaque-id view.

    The only difference on the wire is `record_id`: a keyed hash under a per-grant
    key (`opaque_ids`), stable within the grant and meaningless across grants, in
    place of the canonical counter. Every other field, check and limit is p2a-v2's.
    """
    versions: OpaqueSourceVersions
    evaluator: OpaqueSourceEvaluator
    rules: list[OpaqueRule]


class OpaqueSubjectSourceDecision(Decision):
    evaluator_version: Literal["hard-rules/p2a-v3"]
    required_projection_id: Literal["canonical.message_disclosure.v2"] | None


class OpaqueMessageDisclosure(MessageDisclosure):
    view_id: Literal["canonical.message_disclosure.v2"]


Policy = (PolicyV2 | FactPolicyV2 | StatedDayFactPolicy | AttestedSubjectFactPolicy | WorkFactPolicy | AttestedSubjectSourcePolicy
          | OpaqueSubjectSourcePolicy | SearchPolicy)
PolicyDecision = (Decision | FactDecision | StatedDayFactDecision | AttestedSubjectFactDecision | WorkFactDecision
                  | AttestedSubjectSourceDecision | OpaqueSubjectSourceDecision | SearchSetDecision)
Disclosure = (MessageDisclosure | OpaqueMessageDisclosure | FactScalarDisclosure | WorkScalarDisclosure
              | MessageSearchResult)


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
    if capability == "permissions-beta/p2a-v2":
        return AttestedSubjectSourcePolicy.parse(raw)
    if capability == "permissions-beta/p2a-v3":
        return OpaqueSubjectSourcePolicy.parse(raw)
    if capability == "permissions-beta/p2b-v1":
        return FactPolicyV2.parse(raw)
    if capability == "permissions-beta/p2b-v2":
        return StatedDayFactPolicy.parse(raw)
    if capability == "permissions-beta/p2b-v3":
        return AttestedSubjectFactPolicy.parse(raw)
    if capability == "permissions-beta/p2b-v4":
        return WorkFactPolicy.parse(raw)
    if capability == CAPABILITY_SEARCH:
        return SearchPolicy.parse(raw)
    raise PolicyError("unsupported_capability")


def parse_decision(raw, *, capability: str) -> PolicyDecision:
    if capability == "permissions-beta/p2a-v1":
        return Decision.parse(value_of(raw))
    if capability == "permissions-beta/p2a-v2":
        return AttestedSubjectSourceDecision.parse(value_of(raw))
    if capability == "permissions-beta/p2a-v3":
        return OpaqueSubjectSourceDecision.parse(value_of(raw))
    if capability == "permissions-beta/p2b-v1":
        return FactDecision.parse(value_of(raw))
    if capability == "permissions-beta/p2b-v2":
        return StatedDayFactDecision.parse(value_of(raw))
    if capability == "permissions-beta/p2b-v3":
        return AttestedSubjectFactDecision.parse(value_of(raw))
    if capability == "permissions-beta/p2b-v4":
        return WorkFactDecision.parse(value_of(raw))
    if capability == CAPABILITY_SEARCH:
        return SearchSetDecision.parse(value_of(raw))
    raise PolicyError("unsupported_capability")


def parse_disclosure(raw, *, capability: str) -> Disclosure:
    # p2a-v3 releases the opaque-id view; p2a-v1 and p2a-v2 the one they were signed for.
    if capability == CAPABILITY_OPAQUE:
        return OpaqueMessageDisclosure.parse(value_of(raw))
    if capability in SOURCE_CAPABILITIES:
        return MessageDisclosure.parse(value_of(raw))
    if capability in ("permissions-beta/p2b-v1", "permissions-beta/p2b-v2", "permissions-beta/p2b-v3"):
        return FactScalarDisclosure.parse(value_of(raw))
    # v4 is the one P2b capability whose disclosure is NOT the preference scalar.
    if capability == "permissions-beta/p2b-v4":
        return WorkScalarDisclosure.parse(value_of(raw))
    if capability == CAPABILITY_SEARCH:
        return MessageSearchResult.parse(value_of(raw))
    raise PolicyError("unsupported_capability")

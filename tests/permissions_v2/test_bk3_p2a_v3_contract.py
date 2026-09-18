"""p2a-v3's contract: exactly p2a-v2 plus the opaque-id view, and nothing else moves.

  V1  the p2a-v3 exports and the protocol unions it joined are pinned byte for byte
  V2  a p2a-v3 policy may name only the v2 view, and a p2a-v2 policy only the v1 view
  V3  the decision and disclosure classes follow the capability; neither view parses
      under the other capability
  V4  the frozen p2a-v1 and p2a-v2 exports are unchanged (their own tests pin the bytes)
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from tests.permissions_v2 import source_attested_schemas, source_opaque_schemas
from tests.permissions_v2.production_node import work_policy
from tests.permissions_v2.production_corpus import BINDING
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.identity import ATTESTED_CONTRACT, SUBJECT_CONTRACT_BY_CAPABILITY
from topos.permissions_v2.registry import (OpaqueMessageDisclosure, OpaqueSubjectSourcePolicy, parse_disclosure,
    parse_policy)
from topos.permissions_v2.release import RETIRED_SOURCE_CAPABILITIES, SOURCE_DECISIONS, SOURCE_VIEWS

V3 = "permissions-beta/p2a-v3"


def v3(view="canonical.message_disclosure.v2"):
    return work_policy(BINDING, capability=V3, evaluator="hard-rules/p2a-v3", view=view)


@pytest.mark.parametrize("model", source_opaque_schemas.OPAQUE_MODELS + source_attested_schemas.PROTOCOL_MODELS,
                         ids=lambda model: model.__name__)
def test_V1_exports_are_exact(model):
    directory = (source_opaque_schemas.OPAQUE_FIXTURES if model in source_opaque_schemas.OPAQUE_MODELS
                 else source_attested_schemas.FIXTURES)
    assert (directory / f"{model.__name__}.schema.json").read_bytes() == source_attested_schemas.export(model)


def test_V2_each_capability_names_only_its_own_view():
    assert isinstance(parse_policy(v3()), OpaqueSubjectSourcePolicy)
    with pytest.raises(PolicyError):
        parse_policy(v3(view="canonical.message_disclosure.v1"))
    v2 = work_policy(BINDING, view="canonical.message_disclosure.v2")
    with pytest.raises(PolicyError):
        parse_policy(v2)


def test_V3_decision_view_and_rule_follow_the_capability():
    assert SUBJECT_CONTRACT_BY_CAPABILITY[V3] == ATTESTED_CONTRACT
    assert SOURCE_VIEWS[V3][0] == "canonical.message_disclosure.v2" and SOURCE_DECISIONS[V3][1] == "hard-rules/p2a-v3"
    assert V3 not in RETIRED_SOURCE_CAPABILITIES
    body = {"family": "canonical_record", "operation": "read", "view_id": "canonical.message_disclosure.v2",
            "records": [{"record_id": "r." + "0" * 64, "source_id": "imessage", "canonical_table": "conversation_messages",
                         "content": "x"}]}
    assert isinstance(parse_disclosure(body, capability=V3), OpaqueMessageDisclosure)
    with pytest.raises(PolicyError):
        parse_disclosure(body, capability="permissions-beta/p2a-v2")
    old = deepcopy(body)
    old["view_id"] = "canonical.message_disclosure.v1"
    with pytest.raises(PolicyError):
        parse_disclosure(old, capability=V3)

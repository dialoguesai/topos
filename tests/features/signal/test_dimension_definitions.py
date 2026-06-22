"""Tests for explicit signal dimension definition registry."""

from __future__ import annotations

import copy

import pytest

from topos.features.signal.brief_schemas import llm_prompt_for_dimension
from topos.features.signal.dimension_definition_loader import (
    DimensionDefinitionError,
    get_definition,
    list_definition_ids,
    validate_definition,
)
from topos.features.signal.dimension_registry import (
    GATE_OBJECTS_BY_DIMENSION,
    VIEW_TYPES_BY_DIMENSION,
    dimension_has_explicit_definition,
)


def test_master_schema_valid() -> None:
    for dim_id in ("time", "relationships", "intentions", "profile", "work"):
        defn = get_definition(dim_id)
        assert defn["dimension_id"] == dim_id
        assert defn["status"] == "typed"


def test_mvp_definitions_load_and_validate() -> None:
    ids = list_definition_ids()
    assert {"time", "relationships", "intentions", "profile", "work"}.issubset(set(ids))
    time_def = get_definition("time")
    entity_ids = {e["id"] for e in time_def["primary_entity_types"]}
    assert {"AvailabilityWindow", "Commitment", "RoutinePattern"}.issubset(entity_ids)
    assert "interval_calendar" in time_def["view_types"]


def test_invalid_definition_fails_validation() -> None:
    bad = copy.deepcopy(get_definition("time"))
    bad.pop("core_question")
    with pytest.raises(DimensionDefinitionError):
        validate_definition(bad)


def test_loader_integrates_with_registry() -> None:
    assert "interval_calendar" in VIEW_TYPES_BY_DIMENSION["time"]
    assert "warmth_score" in GATE_OBJECTS_BY_DIMENSION["relationships"]
    assert "goal_alignment_vector" in GATE_OBJECTS_BY_DIMENSION["intentions"]
    assert dimension_has_explicit_definition("time")
    assert not dimension_has_explicit_definition("unknown")


def test_llm_prompt_uses_definition_hints() -> None:
    prompt = llm_prompt_for_dimension("time")
    assert "calendar event titles" in prompt


def test_unknown_definition_raises() -> None:
    with pytest.raises(DimensionDefinitionError):
        get_definition("not_a_dimension")

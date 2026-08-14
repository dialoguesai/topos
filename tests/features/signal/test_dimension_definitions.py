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


def test_every_registry_dimension_has_a_typed_definition() -> None:
    """All ten signal dimensions are typed — no dimension derives signal untyped."""
    from topos.features.signal.dimension_registry import SIGNAL_DIMENSION_IDS

    ids = set(list_definition_ids())
    missing = [d for d in SIGNAL_DIMENSION_IDS if d not in ids]
    assert missing == [], f"untyped dimensions: {missing}"
    for dim_id in SIGNAL_DIMENSION_IDS:
        defn = get_definition(dim_id)
        assert defn["status"] == "typed"
        assert defn["primary_entity_types"], dim_id
        assert dimension_has_explicit_definition(dim_id)


def test_gate_objects_are_declared_signal_objects() -> None:
    """A gate object the dimension never emits cannot gate anything.

    Covers every definition file, not a hand-listed subset — profile.json carried
    an undeclared ``skill_vector_overlap`` from creation precisely because it sat
    outside the enumerated list this test used to check.
    """
    from topos.features.signal.dimension_registry import SIGNAL_DIMENSION_IDS

    checked = []
    for dim_id in SIGNAL_DIMENSION_IDS:
        defn = get_definition(dim_id)
        orphans = set(defn["gate_objects"]) - set(defn["signal_objects"])
        assert orphans == set(), f"{dim_id} gates on undeclared objects: {sorted(orphans)}"
        checked.append(dim_id)
    assert set(checked) >= {"interests", "wellbeing", "resources", "memory", "places", "profile"}


def test_gate_objects_fail_closed_when_undeclared() -> None:
    """No declaration grants nothing — never the dimension's whole signal set."""
    from topos.features.signal import dimension_registry as reg

    assert reg.gate_objects_by_dimension("not_a_dimension") == []
    assert reg.gate_objects_by_dimension("") == []


def test_empty_gate_objects_grants_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dimension declaring no gates must not inherit the data-health catalog."""
    from topos.features.signal import dimension_registry as reg

    monkeypatch.setattr(reg, "gate_objects_for_dimension", lambda _dim: [])
    assert reg.gate_objects_by_dimension("profile") == []


def test_no_dimension_gates_on_a_living_brief() -> None:
    """Living briefs are full narrative PII; no gate may read one."""
    for dim_id, gates in GATE_OBJECTS_BY_DIMENSION.items():
        leaked = [g for g in gates if g.endswith("_living_brief")]
        assert leaked == [], f"{dim_id} gates on narrative brief objects: {leaked}"


def test_undeclared_gate_object_fails_validation() -> None:
    """The subset invariant is enforced at load, not only by this test module."""
    bad = copy.deepcopy(get_definition("profile"))
    bad["gate_objects"] = list(bad["gate_objects"]) + ["not_an_emitted_object"]
    with pytest.raises(DimensionDefinitionError, match="undeclared signal objects"):
        validate_definition(bad)


def test_every_signal_write_site_is_declared() -> None:
    """SignalObjectStore raises on an undeclared (dimension, object_type).

    Static sweep so an undeclared write is a build failure, not a runtime ValueError
    on whichever node first exercises that code path.
    """
    import glob
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "topos"
    pairs: set[tuple[str, str, str]] = set()
    patterns = (
        r'upsert_object\(\s*\n?\s*"([a-z_]+)",\s*\n?\s*"([a-z_]+)"',
        r'dimension="([a-z_]+)",\s*\n?[^)]*?object_type="([a-z_]+)"',
    )
    for path in glob.glob(str(root / "**" / "*.py"), recursive=True):
        if "build/lib" in path:
            continue
        src = Path(path).read_text(encoding="utf-8", errors="ignore")
        for pat in patterns:
            for m in re.finditer(pat, src):
                pairs.add((m.group(1), m.group(2), path))
        for m in re.finditer(r'object_type="([a-z_]+)",\s*\n?\s*dimension="([a-z_]+)"', src):
            pairs.add((m.group(2), m.group(1), path))

    assert pairs, "sweep found no write sites — the regex drifted from the call shape"
    undeclared = [
        (dim, obj, path)
        for dim, obj, path in sorted(pairs)
        if dim in set(list_definition_ids())
        and obj not in set(get_definition(dim)["signal_objects"])
    ]
    assert undeclared == [], f"undeclared signal writes: {undeclared}"


def test_sensitive_dimensions_carry_a_redaction_clause() -> None:
    """extraction_hints reaches the brief LLM prompt — the redaction must ride with it."""
    for dim_id in ("wellbeing", "resources", "places", "memory", "interests"):
        hints = get_definition(dim_id)["extraction_hints"]
        assert "Never" in hints or "never" in hints, dim_id
        assert hints in llm_prompt_for_dimension(dim_id), dim_id


def test_unknown_definition_raises() -> None:
    with pytest.raises(DimensionDefinitionError):
        get_definition("not_a_dimension")

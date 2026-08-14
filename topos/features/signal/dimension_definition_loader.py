"""Load and validate explicit signal dimension definition JSON files."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

_DEFINITIONS_DIR = Path(__file__).resolve().parent / "definitions"
_SCHEMA_PATH = _DEFINITIONS_DIR / "schema" / "dimension_definition.schema.json"

_TYPED_DIMENSION_IDS = frozenset({"time", "relationships", "intentions", "profile", "work"})


class DimensionDefinitionError(ValueError):
    """Raised when a dimension definition is missing or invalid."""


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise DimensionDefinitionError(f"Expected object in {path.name}")
    return data


@lru_cache(maxsize=1)
def _schema() -> Dict[str, Any]:
    return _load_json(_SCHEMA_PATH)


@lru_cache(maxsize=1)
def _validator():
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover
        raise DimensionDefinitionError("jsonschema is required for dimension definitions") from exc

    return jsonschema.Draft202012Validator(_schema())


def validate_definition(data: Dict[str, Any]) -> None:
    errors = sorted(_validator().iter_errors(data), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        path = ".".join(str(p) for p in first.path) or "root"
        raise DimensionDefinitionError(f"Invalid dimension definition at {path}: {first.message}")

    # JSON Schema cannot express a subset relation between two sibling arrays,
    # so the invariant is enforced here: a gate object the dimension never
    # emits cannot gate anything, and it silently widens the write allowlist
    # in SignalObjectStore._validate_object_type.
    orphans = sorted(set(data.get("gate_objects") or []) - set(data.get("signal_objects") or []))
    if orphans:
        raise DimensionDefinitionError(
            f"Invalid dimension definition at gate_objects: {data.get('dimension_id')!r} "
            f"gates on undeclared signal objects: {orphans}"
        )


def list_definition_ids() -> List[str]:
    ids: List[str] = []
    for path in sorted(_DEFINITIONS_DIR.glob("*.json")):
        ids.append(path.stem)
    return ids


@lru_cache(maxsize=32)
def get_definition(dimension_id: str) -> Dict[str, Any]:
    dim = str(dimension_id or "").strip().lower()
    if not dim:
        raise DimensionDefinitionError("dimension_id is required")
    path = _DEFINITIONS_DIR / f"{dim}.json"
    if not path.is_file():
        raise DimensionDefinitionError(f"Unknown signal dimension definition: {dimension_id!r}")
    data = _load_json(path)
    validate_definition(data)
    if data.get("dimension_id") != dim:
        raise DimensionDefinitionError(f"dimension_id mismatch in {path.name}")
    return data


def get_definition_or_none(dimension_id: str) -> Optional[Dict[str, Any]]:
    try:
        return get_definition(dimension_id)
    except DimensionDefinitionError:
        return None


def view_types_for_dimension(dimension_id: str) -> List[str]:
    defn = get_definition_or_none(dimension_id)
    if not defn:
        return ["narrative_brief"]
    views = defn.get("view_types")
    return list(views) if isinstance(views, list) else ["narrative_brief"]


def gate_objects_for_dimension(dimension_id: str) -> List[str]:
    defn = get_definition_or_none(dimension_id)
    if not defn:
        return []
    gates = defn.get("gate_objects")
    return list(gates) if isinstance(gates, list) else []


def extraction_hints_for_dimension(dimension_id: str) -> str:
    defn = get_definition_or_none(dimension_id)
    if not defn:
        return ""
    return str(defn.get("extraction_hints") or "")


def is_typed_dimension(dimension_id: str) -> bool:
    defn = get_definition_or_none(dimension_id)
    if defn is None:
        return False
    return str(defn.get("status") or "") == "typed"


def clear_definition_cache() -> None:
    get_definition.cache_clear()

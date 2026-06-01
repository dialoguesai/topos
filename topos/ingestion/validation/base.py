"""Validation primitives for ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    errors: list[str]
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class SchemaDefinition:
    schema_id: str
    version: str
    raw_schema: Dict[str, Any]


class SchemaValidator:
    """Validates raw records against a schema definition."""

    def validate(self, record: Dict[str, Any], schema: Optional[SchemaDefinition] = None) -> ValidationResult:
        raise NotImplementedError

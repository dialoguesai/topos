"""Default schema validator (no-op placeholder)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import SchemaDefinition, SchemaValidator, ValidationResult


class NoOpSchemaValidator(SchemaValidator):
    def validate(self, record: Dict[str, Any], schema: Optional[SchemaDefinition] = None) -> ValidationResult:
        _ = (record, schema)
        return ValidationResult(is_valid=True, errors=[], metadata={})

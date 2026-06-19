"""Wiki MVP pipeline stage identifiers (Phase 0)."""

from __future__ import annotations

from enum import Enum


class PipelineStage(str, Enum):
    SOURCE_CONNECT = "source_connect"
    RAW_WRITE = "raw_write"
    CANONICAL_MAP = "canonical_map"
    SIGNAL_DERIVE = "signal_derive"
    DIMENSION_PROFILE = "dimension_profile"
    DATA_HEALTH = "data_health"

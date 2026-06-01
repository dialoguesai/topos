"""Coarse table layer labels for list_database_tables (system / raw / enrichment / canonical)."""

from __future__ import annotations

from typing import Dict, Tuple

TableLayerKind = str  # "system" | "raw" | "enrichment" | "canonical"

_LAYER_LABELS: Dict[TableLayerKind, str] = {
    "system": "Topos system",
    "raw": "Raw",
    "enrichment": "Enrichment",
    "canonical": "Canonical",
}

# Maps engine list_database_tables category keys to coarse layer kinds.
_CATEGORY_TO_LAYER: Dict[str, TableLayerKind] = {
    "system": "system",
    "enrichment_system": "system",
    "raw_retention": "raw",
    "raw_enrichment": "enrichment",
    "browser_flat": "raw",
    "source": "raw",
    "canonical": "canonical",
    "canonical_enrichment": "enrichment",
    "other": "raw",
}


def layer_kind_for_category(category_key: str) -> TableLayerKind:
    key = (category_key or "").strip()
    return _CATEGORY_TO_LAYER.get(key, "raw")


def layer_label_for_kind(layer_kind: str) -> str:
    return _LAYER_LABELS.get((layer_kind or "").strip(), "Raw")


def layer_for_category(category_key: str) -> Tuple[TableLayerKind, str]:
    kind = layer_kind_for_category(category_key)
    return kind, layer_label_for_kind(kind)


def layer_kind_labels() -> Dict[TableLayerKind, str]:
    return dict(_LAYER_LABELS)

"""Data source registry."""

from .definitions import DataSourceDefinition
from .registry import REGISTRY, list_sources

__all__ = ["DataSourceDefinition", "REGISTRY", "list_sources"]

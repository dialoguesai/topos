"""The declared-field-map vocabulary is a contract with two consumers.

`topos/sources/declared_field_map_spec.py` is copied verbatim into the control
plane's bundled mirror (CONNECTOR_SPEC.md §4,
`topos-control-plane/scripts/regenerate_bundled_mirror.sh`) so a declaration can
be validated there without the engine's canonicalization stack. Two ways that
breaks silently: the spec grows an engine import (the mirror stops importing),
or a transform name validates but has no implementation (a declaration installs
clean and maps nothing).
"""

from __future__ import annotations

import ast
import pathlib


SPEC_PATH = pathlib.Path(__file__).resolve().parents[2] / "topos" / "sources" / "declared_field_map_spec.py"


def test_spec_module_imports_nothing_from_the_engine() -> None:
    tree = ast.parse(SPEC_PATH.read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append("." * (node.level or 0) + (node.module or ""))
    # stdlib only: a relative import or a topos.* import would not resolve in
    # the mirror, where only the sources package is copied.
    assert imported == ["__future__", "typing"], imported


def test_every_declared_transform_has_an_implementation() -> None:
    from topos.canonicalization.declared_field_map import TRANSFORMS
    from topos.sources.declared_field_map_spec import TRANSFORM_IDS

    assert set(TRANSFORMS) == set(TRANSFORM_IDS)


def test_id_columns_cover_every_upsertable_canonical_table() -> None:
    """A declaration may only target a table the canonical store can upsert, and
    the identity column must be the one the store requires."""
    import inspect

    from topos.sources.declared_field_map_spec import ID_COLUMNS
    from topos.storage.canonical import canonical_store

    source = inspect.getsource(canonical_store.SQLiteCanonicalStore)
    for table, id_column in ID_COLUMNS.items():
        assert f'table == "{table}"' in source, table
        assert f"{table} upsert requires {id_column}" in source, (table, id_column)

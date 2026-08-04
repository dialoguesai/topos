"""C4 junk NER ≤3-char filter + C6 identifier→display name rendering."""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.resolver import is_valid_entity_surface
from topos.features.stats.insights import (
    _contact_label_map,
    _group_label,
    _identifier_lookup_keys,
)
from topos.storage.db.migrations import apply_all_migrations

pytestmark = [pytest.mark.check("C-quality-entity-hygiene-c4-c6")]


def test_c4_rejects_le_3_char_junk() -> None:
    assert not is_valid_entity_surface("ab")
    assert not is_valid_entity_surface("xy")
    assert not is_valid_entity_surface("the")
    # Known live-spine crumbs from plan C4 narrative
    for crumb in ("ok", "id", "na", "pm", "am"):
        assert not is_valid_entity_surface(crumb), crumb


def test_c4_allowlist_and_codes_survive() -> None:
    assert is_valid_entity_surface("AWS")
    assert is_valid_entity_surface("C3")
    assert is_valid_entity_surface("Max")
    assert is_valid_entity_surface("Maya Chen")


def test_c6_identifier_lookup_keys_cover_phone_variants() -> None:
    keys = _identifier_lookup_keys("+17184834576")
    assert "+17184834576" in keys
    assert "17184834576" in keys
    assert "7184834576" in keys


def test_c6_group_label_resolves_phone_variants(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "c6.db"))
    apply_all_migrations(conn)
    conn.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self) "
        "VALUES (?, ?, ?, ?, 0)",
        ("c1", "default", "imessage", "Maya Chen"),
    )
    conn.execute(
        "INSERT INTO contact_identifiers "
        "(dataset_id, source_id, identifier, identifier_type, contact_id) "
        "VALUES (?, ?, ?, ?, ?)",
        ("default", "imessage", "+17184834576", "phone", "c1"),
    )
    conn.commit()
    label_map = _contact_label_map(conn)
    assert _group_label("+17184834576", label_map) == "Maya Chen (+17184834576)"
    # Stat group keys sometimes omit '+' — still resolve via digit variants.
    assert _group_label("17184834576", label_map).startswith("Maya Chen")
    assert _group_label("self", label_map) == "myself"
    assert _group_label("unknown-handle", label_map) == "unknown-handle"
    conn.close()

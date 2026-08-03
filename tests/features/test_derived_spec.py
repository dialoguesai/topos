"""derived_spec hash — PLAN_GRAPH_QUERY_AND_LATENT_EDGES §5 M4."""

from __future__ import annotations

import sqlite3

from topos.features.signal.derived_spec import (
    ENGINE_CONFIG_KEY,
    derived_spec,
    derived_spec_changed,
    derived_spec_version,
    persist_derived_spec_version,
    stored_derived_spec_version,
)


def test_hash_is_stable_across_calls() -> None:
    a = derived_spec_version()
    b = derived_spec_version()
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_hash_insensitive_to_key_ordering() -> None:
    spec = derived_spec()
    # Rebuild with shuffled top-level insertion order.
    shuffled = {k: spec[k] for k in reversed(list(spec.keys()))}
    assert derived_spec_version(spec) == derived_spec_version(shuffled)


def test_hash_moves_when_a_governing_parameter_changes(monkeypatch) -> None:
    baseline = derived_spec_version()
    monkeypatch.setattr(
        "topos.features.entities.affinity.AFFINITY_TOPN",
        99,
        raising=False,
    )
    # Re-import path goes through derived_spec(), which reads the live module
    # attribute — patch the module the collector imports from.
    import topos.features.entities.affinity as affinity_mod

    monkeypatch.setattr(affinity_mod, "AFFINITY_TOPN", 99)
    moved = derived_spec_version()
    assert moved != baseline


def test_persist_and_changed_round_trip(tmp_path) -> None:
    from topos.storage.db.migrations import apply_all_migrations

    conn = sqlite3.connect(tmp_path / "spec.db")
    apply_all_migrations(conn)
    assert stored_derived_spec_version(conn) is None
    assert derived_spec_changed(conn) is True

    written = persist_derived_spec_version(conn)
    assert stored_derived_spec_version(conn) == written
    assert derived_spec_changed(conn) is False

    row = conn.execute(
        "SELECT value FROM engine_config WHERE key = ?", (ENGINE_CONFIG_KEY,)
    ).fetchone()
    assert row is not None
    assert row[0] == written

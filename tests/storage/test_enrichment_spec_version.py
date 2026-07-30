"""PLAN M3: coverage spec_version migration + stale anti-join."""

from __future__ import annotations

import sqlite3

import pytest

from topos.api.enrichment import _get_enriched_message_ids
from topos.enrichment.catalog import catalog_spec_version, get_catalog_entry
from topos.enrichment.models.mvp_defaults import JOB_SPEC_VERSIONS, job_spec_version
from topos.storage.db.migrations import apply_all_migrations, ensure_migrations_applied
from topos.storage.db.migrations.enrichment_spec_version_v1 import (
    MIGRATION_ID,
    apply_enrichment_spec_version_v1_up,
)
from topos.upgrades.runner import (
    consent_upgrade_step,
    run_pending_upgrades,
    runner_status,
)


@pytest.mark.public
def test_catalog_exposes_spec_version():
    assert job_spec_version("entities") == JOB_SPEC_VERSIONS["entities"]
    entry = get_catalog_entry("entities")
    assert entry is not None
    assert entry.spec_version == JOB_SPEC_VERSIONS["entities"]
    assert catalog_spec_version("entities") == entry.spec_version
    assert "spec_version" in entry.to_dict()


@pytest.mark.public
def test_migration_adds_spec_version_columns(tmp_path):
    db = tmp_path / "spec.db"
    conn = sqlite3.connect(db)
    apply_all_migrations(conn)
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(message_entities)").fetchall()
    }
    assert "spec_version" in cols
    emb_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(signal_embeddings)").fetchall()
    }
    assert "spec_version" in emb_cols
    row = conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()
    assert row is not None
    conn.close()


@pytest.mark.public
def test_stale_predicate_treats_null_as_version_zero(tmp_path):
    db = tmp_path / "stale.db"
    conn = sqlite3.connect(db)
    apply_all_migrations(conn)
    conn.execute(
        """
        INSERT INTO message_entities (
            entity_id, record_id, source_id, entity_text, model, provider, payload_json
        ) VALUES ('e1', 'm1', 'src', 'Alice', 'model', 'hf', '{}')
        """
    )
    conn.commit()
    # Presence-only would count m1; min_spec=1 must exclude NULL/0.
    current = _get_enriched_message_ids(
        "message_entities", conn, min_spec_version=1
    )
    assert "m1" not in current
    conn.execute(
        "UPDATE message_entities SET spec_version=1 WHERE record_id='m1'"
    )
    conn.commit()
    current = _get_enriched_message_ids(
        "message_entities", conn, min_spec_version=1
    )
    assert "m1" in current
    conn.close()


@pytest.mark.public
def test_prompt_step_stays_pending_consent_until_api(tmp_path, monkeypatch):
    db = tmp_path / "consent.db"
    conn = sqlite3.connect(db)
    apply_all_migrations(conn)
    from topos.upgrades.runner import _ensure_engine_config, _stamp_baseline

    _ensure_engine_config(conn)
    # Drop legacy engine_config without updated_at if migrations created a thin table.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(engine_config)").fetchall()}
    if "updated_at" not in cols:
        conn.execute("DROP TABLE engine_config")
        _ensure_engine_config(conn)
    _stamp_baseline(conn, "1.3.3")
    conn.commit()

    fake_steps = [
        {
            "id": "slow-reembed",
            "kind": "none",
            "title": "Re-embed",
            "why": "test",
            "cost": "slow",
            "consent": "prompt",
        }
    ]

    monkeypatch.setattr(
        "topos.upgrades.runner.plan_upgrade",
        lambda conn, shipped=None: {
            "shipped": "1.3.4",
            "baseline": "1.3.3",
            "fresh_install": False,
            "steps": fake_steps,
        },
    )
    monkeypatch.setattr(
        "topos.upgrades.runner._enabled",
        lambda: True,
    )

    result = run_pending_upgrades(conn, shipped="1.3.4")
    assert result.get("steps_pending_consent") == 1
    status = runner_status(conn)
    assert any(s["id"] == "slow-reembed" for s in status.get("pending_consent_steps") or [])

    consent_upgrade_step(conn, "slow-reembed", shipped="1.3.4")
    row = conn.execute(
        "SELECT status FROM derivation_ledger WHERE version=? AND step_id=?",
        ("1.3.4", "slow-reembed"),
    ).fetchone()
    assert row and row[0] == "pending"

    result2 = run_pending_upgrades(conn, shipped="1.3.4")
    assert result2.get("steps_run") == 1
    row = conn.execute(
        "SELECT status FROM derivation_ledger WHERE version=? AND step_id=?",
        ("1.3.4", "slow-reembed"),
    ).fetchone()
    assert row and row[0] == "done"
    conn.close()


@pytest.mark.public
def test_ensure_migrations_applies_spec_version(tmp_path):
    db = tmp_path / "ensure.db"
    conn = sqlite3.connect(db)
    # Pre-create a coverage table without the column, then ensure.
    conn.execute(
        """
        CREATE TABLE message_topics (
            topic_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            topic TEXT,
            model TEXT,
            provider TEXT,
            payload_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    apply_enrichment_spec_version_v1_up(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(message_topics)").fetchall()}
    assert "spec_version" in cols
    # Full ensure on empty DB also lands the migration via registry.
    conn2 = sqlite3.connect(tmp_path / "ensure2.db")
    ensure_migrations_applied(conn2)
    led = conn2.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()
    assert led is not None
    conn.close()
    conn2.close()

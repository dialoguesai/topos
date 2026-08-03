"""C4 residual / Wave B4: one-shot scrub of already-minted junk spine entities."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from topos.features.entities.resolver import EntityResolver
from topos.features.lifecycle.derived_scrub import purge_junk_minted_entities
from topos.storage.db.migrations import apply_all_migrations

pytestmark = [pytest.mark.check("C-quality-entity-hygiene-c4-c6")]


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "junk.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _mention(conn, entity_id: str, surface: str) -> None:
    mid = f"men_{uuid.uuid4().hex[:12]}"
    conn.execute(
        """
        INSERT INTO entity_mentions
            (mention_id, entity_id, record_id, source_id, canonical_table,
             surface_text, confidence, event_at, created_at)
        VALUES (?, ?, ?, 'imessage', 'conversation_messages', ?, 0.9,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (mid, entity_id, f"rec_{uuid.uuid4().hex[:8]}", surface),
    )
    conn.execute(
        "UPDATE entities SET mention_count = mention_count + 1 WHERE entity_id=?",
        (entity_id,),
    )
    conn.commit()


def test_dry_run_finds_junk_without_deleting(conn) -> None:
    r = EntityResolver(conn)
    junk_id = r._create_entity("dy", "person")
    keep_id = r._create_entity("AWS", "org")
    _mention(conn, junk_id, "dy")
    _mention(conn, keep_id, "AWS")

    report = purge_junk_minted_entities(conn, dry_run=True)
    assert report["dry_run"] is True
    assert report["junk_entities_found"] >= 1
    assert any(s["entity_id"] == junk_id for s in report["samples"])
    assert conn.execute(
        "SELECT 1 FROM entities WHERE entity_id=?", (junk_id,)
    ).fetchone()


def test_apply_removes_junk_keeps_allowlisted(conn) -> None:
    r = EntityResolver(conn)
    junk_id = r._create_entity("ok", "person")
    keep_short = r._create_entity("Max", "person")
    keep_long = r._create_entity("Maya Chen", "person")
    _mention(conn, junk_id, "ok")
    _mention(conn, keep_short, "Max")
    _mention(conn, keep_long, "Maya Chen")

    # Edge touching junk — must cascade-delete then rebuild.
    conn.execute(
        """
        INSERT INTO entity_edges
            (edge_id, src_entity_id, dst_entity_id, edge_type, weight,
             evidence_count, created_at, updated_at)
        VALUES (?, ?, ?, 'co_occurrence', 1.0, 1,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (f"e_{uuid.uuid4().hex[:8]}", junk_id, keep_long),
    )
    conn.commit()

    report = purge_junk_minted_entities(conn, dry_run=False)
    assert report["junk_entities_removed"] >= 1
    assert (
        conn.execute(
            "SELECT 1 FROM entities WHERE entity_id=?", (junk_id,)
        ).fetchone()
        is None
    )
    assert conn.execute(
        "SELECT 1 FROM entities WHERE entity_id=?", (keep_short,)
    ).fetchone()
    assert conn.execute(
        "SELECT 1 FROM entities WHERE entity_id=?", (keep_long,)
    ).fetchone()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM entity_mentions WHERE entity_id=?", (junk_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM entity_edges WHERE src_entity_id=? OR dst_entity_id=?",
            (junk_id, junk_id),
        ).fetchone()[0]
        == 0
    )


def test_self_and_synthetic_hubs_never_scrubbed(conn) -> None:
    r = EntityResolver(conn)
    self_id = r._create_entity("Me", "person", is_self=True)
    # Force a short self name that would otherwise look like junk.
    conn.execute(
        "UPDATE entities SET canonical_name='me', normalized_name='me' WHERE entity_id=?",
        (self_id,),
    )
    conn.execute(
        """
        INSERT INTO entities
            (entity_id, entity_type, canonical_name, normalized_name,
             aliases_json, identifiers_json, is_self, mention_count)
        VALUES ('goal_x', 'goal', 'xx', 'xx', '[]', '[]', 0, 0)
        """
    )
    conn.commit()

    purge_junk_minted_entities(conn, dry_run=False)
    assert conn.execute(
        "SELECT 1 FROM entities WHERE entity_id=?", (self_id,)
    ).fetchone()
    assert conn.execute(
        "SELECT 1 FROM entities WHERE entity_id='goal_x'"
    ).fetchone()

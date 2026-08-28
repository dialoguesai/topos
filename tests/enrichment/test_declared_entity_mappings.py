"""Declarative entity mappings (§5a capability 4): structured sources mint
their entities from declared record fields; NER is suppressed for them.

github_activity is the first consumer — commit rows carry their repo/org in
metadata_json, and NER over commit prose misclassified repos as person nodes.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.declared_mappings import (
    EDGE_WORKED_ON,
    extract_declared_entities,
    ner_suppressed_source_ids,
    reclassify_misdeclared_entities,
)

GITHUB_ROW = {
    "event_id": "github:push:dialoguesai/topos-react-app:abc123",
    "source_id": "github_activity",
    "occurred_at": "2026-07-10T12:00:00Z",
    "title": "push: fix routines save",
    "metadata_json": json.dumps(
        {"event_type": "PushEvent", "repo": "dialoguesai/topos-react-app", "actor": "robin"}
    ),
    "_table": "activity_events",
}


def test_github_row_mints_project_and_org() -> None:
    rows = extract_declared_entities(GITHUB_ROW)
    by_type = {r["entity_type"]: r for r in rows}
    assert set(by_type) == {"project", "org"}

    project = by_type["project"]
    assert project["entity_text"] == "dialoguesai/topos-react-app"
    assert project["record_id"] == GITHUB_ROW["event_id"]
    assert project["event_at"] == "2026-07-10T12:00:00Z"
    assert project["canonical_table"] == "activity_events"
    assert project["provider"] == "declared"
    assert project["confidence"] == 1.0
    # The declared owner edge rides on the project row, not the org row.
    assert project["self_edge"] == EDGE_WORKED_ON

    org = by_type["org"]
    assert org["entity_text"] == "dialoguesai"
    assert "self_edge" not in org


def test_metadata_json_accepts_dict_form() -> None:
    row = dict(GITHUB_ROW)
    row["metadata_json"] = {"repo": "acme/widgets"}
    rows = extract_declared_entities(row)
    assert {r["entity_text"] for r in rows} == {"acme/widgets", "acme"}


def test_explicit_id_time_contract_wins() -> None:
    rows = extract_declared_entities(
        GITHUB_ROW, record_id="override-id", event_at="2020-01-01T00:00:00Z"
    )
    assert all(r["record_id"] == "override-id" for r in rows)
    assert all(r["event_at"] == "2020-01-01T00:00:00Z" for r in rows)


def test_unmapped_source_and_missing_fields_yield_nothing() -> None:
    assert extract_declared_entities({"source_id": "imessage", "message_id": "m1"}) == []
    # Mapped source, but no repo field → nothing minted.
    assert (
        extract_declared_entities(
            {
                "event_id": "e1",
                "source_id": "github_activity",
                "metadata_json": "{}",
            }
        )
        == []
    )
    # Malformed metadata_json string → nothing, no crash.
    assert (
        extract_declared_entities(
            {
                "event_id": "e2",
                "source_id": "github_activity",
                "metadata_json": "not json",
            }
        )
        == []
    )
    # No record id at all → nothing (spine mentions need a record anchor).
    assert (
        extract_declared_entities(
            {"source_id": "github_activity", "metadata_json": '{"repo": "a/b"}'}
        )
        == []
    )


def test_repo_without_org_prefix_skips_org_entity() -> None:
    row = dict(GITHUB_ROW)
    row["metadata_json"] = json.dumps({"repo": "standalone-repo"})
    rows = extract_declared_entities(row)
    assert [r["entity_type"] for r in rows] == ["project"]


def test_ner_suppressed_for_declared_sources() -> None:
    assert "github_activity" in ner_suppressed_source_ids()


@pytest.mark.asyncio
async def test_enrich_suppresses_ner_and_emits_declared(monkeypatch) -> None:
    """github rows never reach the NER engine; declared rows come back instead."""
    from topos.enrichment.jobs.canonical.entities_job import EntitiesJob

    monkeypatch.setenv("TOPOS_ENTITY_SPINE", "off")

    class ExplodingEngine:
        """enrich() must not fall through to NER for suppressed sources."""

    job = EntitiesJob(engine=ExplodingEngine())
    results = await job.enrich([GITHUB_ROW])
    assert {r["entity_type"] for r in results} == {"project", "org"}
    assert all(r["provider"] == "declared" for r in results)
    assert all(r["record_id"] == GITHUB_ROW["event_id"] for r in results)


@pytest.mark.asyncio
async def test_enrich_resolves_declared_into_spine(monkeypatch, tmp_path) -> None:
    """Declared types survive resolution (no map_ner_type coercion) and the
    self → worked_on edge lands at the record's event time."""
    from topos import core
    from topos.enrichment.jobs.canonical.entities_job import EntitiesJob
    from topos.storage.db.migrations import apply_all_migrations

    # Spine resolution runs on a worker thread (asyncio.to_thread), so the
    # injected connection must allow cross-thread use, matching how core.state
    # opens every real connection.
    conn = sqlite3.connect(str(tmp_path / "spine.db"), check_same_thread=False)
    apply_all_migrations(conn)
    conn.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, known_usernames_json, is_self)"
        " VALUES ('c-self', 'ds', 'src', 'Sierra Yankee', '[]', 1)"
    )
    conn.commit()
    monkeypatch.setattr(core.state, "get_db_connection", lambda: conn)
    monkeypatch.setenv("TOPOS_ENTITY_SPINE", "on")

    class ExplodingEngine:
        """NER suppressed for github_activity — engine must never run."""

    job = EntitiesJob(engine=ExplodingEngine())
    await job.enrich([GITHUB_ROW])

    types = dict(
        conn.execute(
            "SELECT canonical_name, entity_type FROM entities WHERE contact_id IS NULL"
        ).fetchall()
    )
    assert types.get("dialoguesai/topos-react-app") == "project"
    assert types.get("dialoguesai") == "org"

    mentions = conn.execute(
        "SELECT COUNT(*) FROM entity_mentions WHERE record_id=?",
        (GITHUB_ROW["event_id"],),
    ).fetchone()[0]
    assert mentions == 2

    edge = conn.execute(
        """
        SELECT s.is_self, d.canonical_name, e.last_event_at
        FROM entity_edges e
        JOIN entities s ON s.entity_id = e.src_entity_id
        JOIN entities d ON d.entity_id = e.dst_entity_id
        WHERE e.edge_type = ?
        """,
        (EDGE_WORKED_ON,),
    ).fetchone()
    assert edge is not None
    assert bool(edge[0]) is True
    assert edge[1] == "dialoguesai/topos-react-app"
    assert edge[2] == GITHUB_ROW["occurred_at"]
    conn.close()


def test_reclassify_migration_fixes_repo_persons_only() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE entities (entity_id TEXT PRIMARY KEY, canonical_name TEXT, entity_type TEXT)"
    )
    conn.execute("CREATE TABLE entity_mentions (entity_id TEXT, record_id TEXT)")
    conn.executemany(
        "INSERT INTO entities VALUES (?, ?, ?)",
        [
            ("e1", "dialoguesai/topos-react-app", "person"),  # NER damage → fix
            ("e2", "Maya Chen", "person"),  # real person → untouched
            ("e3", "acme/widgets", "person"),  # repo-shaped but no github mention → untouched
        ],
    )
    conn.executemany(
        "INSERT INTO entity_mentions VALUES (?, ?)",
        [("e1", "github:push:x:1"), ("e2", "github:push:x:1"), ("e3", "imessage:9")],
    )
    changed = reclassify_misdeclared_entities(conn)
    assert changed == 1
    types = dict(conn.execute("SELECT entity_id, entity_type FROM entities").fetchall())
    assert types == {"e1": "project", "e2": "person", "e3": "person"}
    # Idempotent.
    assert reclassify_misdeclared_entities(conn) == 0

"""C1 / A2.1 residual — cohort → entity-id resolvers.

BEFORE (stub): contacts/calendar_attendees cohorts never widened allow-list →
named contact persons stayed unauthorized under cohort-only grants (utility miss).

AFTER: membership tokens resolve against the live/seeded DB; blackholes stripped;
stats_aggregate stays aggregate-only; A8 denial≡absence for off-cohort / fab preserved.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from topos.query.cohort_resolvers import (
    apply_cohort_membership,
    resolve_accessible_entity_cohorts,
)
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.pipeline import QueryPipelineOrchestrator, _selector_unauthorized
from topos.storage.adapters.factory import AdapterFactory

pytestmark = [pytest.mark.check("C-quality-selector-entity-grant")]

FABRICATED = "Zephyrine Quaddlebock"


def _seed_cohort_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE entities (
            entity_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            identifiers_json TEXT NOT NULL DEFAULT '[]',
            embedding_blob BLOB,
            is_self INTEGER NOT NULL DEFAULT 0,
            contact_id TEXT,
            first_seen TEXT,
            last_seen TEXT,
            mention_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE entity_edges (
            edge_id TEXT PRIMARY KEY,
            src_entity_id TEXT NOT NULL,
            dst_entity_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            valid_from TEXT,
            valid_to TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE calendar_events (
            event_id TEXT PRIMARY KEY,
            title TEXT,
            starts_at TEXT,
            ends_at TEXT,
            metadata_json TEXT
        );
        CREATE TABLE entity_blackholes (
            blackhole_id TEXT PRIMARY KEY,
            entity_id TEXT,
            normalized_name TEXT,
            canonical_name TEXT,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            processing_tier TEXT,
            rebuild_state TEXT,
            note TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    people = [
        ("ent_maya", "Maya Chen", "maya chen", "c-maya", 0, "[]"),
        ("ent_alex", "Alex Rivera", "alex rivera", "c-alex", 0, "[]"),
        ("ent_jordan", "Jordan Lee", "jordan lee", None, 0, '["jordan@example.com"]'),
        ("ent_self", "Owner", "owner", "c-self", 1, "[]"),
        ("ent_gossip", "Odile Mention", "odile mention", None, 0, "[]"),
    ]
    for eid, name, norm, contact_id, is_self, idents in people:
        conn.execute(
            """
            INSERT INTO entities (
                entity_id, entity_type, canonical_name, normalized_name,
                contact_id, is_self, identifiers_json, mention_count
            ) VALUES (?, 'person', ?, ?, ?, ?, ?, 1)
            """,
            (eid, name, norm, contact_id, is_self, idents),
        )
    conn.execute(
        """
        INSERT INTO entity_edges (
            edge_id, src_entity_id, dst_entity_id, edge_type, weight, valid_to
        ) VALUES ('e1', 'ent_self', 'ent_jordan', 'communicates_with', 1.0, NULL)
        """
    )
    conn.execute(
        """
        INSERT INTO calendar_events (event_id, title, starts_at, ends_at, metadata_json)
        VALUES (
            'ev1', '1:1', '2026-08-01T10:00:00Z', '2026-08-01T10:30:00Z',
            ?
        )
        """,
        (json.dumps({"attendees": [{"displayName": "Alex Rivera"}, {"email": "jordan@example.com"}]}),),
    )
    conn.commit()
    return conn


def test_resolve_without_db_fails_closed() -> None:
    assert resolve_accessible_entity_cohorts(["contacts", "message_peers"], None) == []


def test_contacts_cohort_resolves_contact_linked_persons(tmp_path: Path) -> None:
    db = tmp_path / "c1.db"
    conn = _seed_cohort_db(db)
    ids = resolve_accessible_entity_cohorts(["contacts"], conn)
    conn.close()
    assert set(ids) == {"ent_maya", "ent_alex"}
    assert "ent_self" not in ids
    assert "ent_gossip" not in ids


def test_message_peers_and_calendar_attendees(tmp_path: Path) -> None:
    db = tmp_path / "c1b.db"
    conn = _seed_cohort_db(db)
    peers = resolve_accessible_entity_cohorts(["message_peers"], conn)
    cal = resolve_accessible_entity_cohorts(["calendar_attendees"], conn)
    conn.close()
    assert peers == ["ent_jordan"]
    assert set(cal) == {"ent_alex", "ent_jordan"}


def test_stats_aggregate_and_unknown_do_not_widen(tmp_path: Path) -> None:
    db = tmp_path / "c1c.db"
    conn = _seed_cohort_db(db)
    assert resolve_accessible_entity_cohorts(["stats_aggregate"], conn) == []
    assert resolve_accessible_entity_cohorts(["not_a_real_cohort"], conn) == []
    assert resolve_accessible_entity_cohorts(["none"], conn) == []
    conn.close()


def test_blackholed_entity_stripped_from_cohort(tmp_path: Path) -> None:
    db = tmp_path / "c1bh.db"
    conn = _seed_cohort_db(db)
    conn.execute(
        """
        INSERT INTO entity_blackholes (
            blackhole_id, entity_id, normalized_name, canonical_name, created_at, updated_at
        ) VALUES ('bh1', 'ent_maya', 'maya chen', 'Maya Chen', datetime('now'), datetime('now'))
        """
    )
    conn.commit()
    ids = resolve_accessible_entity_cohorts(["contacts"], conn)
    conn.close()
    assert "ent_maya" not in ids
    assert ids == ["ent_alex"]


def test_resolve_scope_manifest_widens_with_db_conn(tmp_path: Path) -> None:
    db = tmp_path / "c1m.db"
    conn = _seed_cohort_db(db)
    # Without db: fail closed (BEFORE residual).
    bare = resolve_scope_manifest(
        "messages:read",
        filter_manifest={"accessible_entity_cohorts": ["contacts"]},
    )
    assert bare.accessible_entity_ids == []
    assert bare.entity_selector_policy_active is True

    widened = resolve_scope_manifest(
        "messages:read",
        filter_manifest={
            "accessible_entity_ids": ["ent_extra"],
            "accessible_entity_cohorts": ["contacts"],
        },
        db_conn=conn,
    )
    conn.close()
    assert "ent_maya" in widened.accessible_entity_ids
    assert "ent_alex" in widened.accessible_entity_ids
    assert "ent_extra" in widened.accessible_entity_ids


def test_apply_cohort_membership_unions_enums(tmp_path: Path) -> None:
    db = tmp_path / "c1a.db"
    conn = _seed_cohort_db(db)
    manifest = resolve_scope_manifest(
        "messages:read",
        filter_manifest={
            "accessible_entity_ids": ["ent_gossip"],
            "accessible_entity_cohorts": ["message_peers"],
        },
    )
    assert manifest.accessible_entity_ids == ["ent_gossip"]
    expanded = apply_cohort_membership(manifest, conn)
    conn.close()
    assert "ent_gossip" in expanded.accessible_entity_ids
    assert "ent_jordan" in expanded.accessible_entity_ids


@pytest.mark.asyncio
async def test_seeded_contacts_cohort_permits_member_denies_outsider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AFTER: cohort membership → named allow; off-cohort / fab still denial≡absence."""
    monkeypatch.setenv("TOPOS_SELECTOR_ENFORCEMENT", "1")
    db = tmp_path / "c1pipe.db"
    conn = _seed_cohort_db(db)
    conn.close()
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(db))

    adapters = AdapterFactory.create("local_database", db_path=db)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    grant = {
        "filter_manifest": {"access_mode_ceiling": "summary"},
        "accessible_entity_ids": [],
        "accessible_entity_cohorts": ["contacts"],
    }
    # Resolve without db (caller may); pipeline must widen before selector.
    manifest = resolve_scope_manifest("messages:read", filter_manifest=grant)
    assert manifest.accessible_entity_ids == []

    # Use the seeded DB directly (process get_db_connection may be cached elsewhere).
    live_conn = sqlite3.connect(db)
    live = apply_cohort_membership(manifest, live_conn)
    assert "ent_maya" in live.accessible_entity_ids
    assert "ent_jordan" not in live.accessible_entity_ids
    with patch(
        "topos.features.entities.linking.link_query_entities",
        return_value=[{"entity_id": "ent_maya", "entity_type": "person"}],
    ):
        assert (
            _selector_unauthorized(live_conn, "Tell me everything about Maya Chen", live)
            is False
        )
    with patch(
        "topos.features.entities.linking.link_query_entities",
        return_value=[{"entity_id": "ent_jordan", "entity_type": "person"}],
    ):
        assert (
            _selector_unauthorized(live_conn, "Tell me everything about Jordan Lee", live)
            is True
        )
    live_conn.close()

    async def _run(q: str) -> dict:
        return await orch.execute(
            query_text=q,
            scope_id="messages:read",
            access_mode="summary",
            manifest=manifest,
            filter_manifest=grant,
            query_session_id=f"c1-{uuid.uuid4().hex[:8]}",
            requester_id="grantee-x",
            owner_id="owner",
            is_grantee_request=True,
        )

    outsider = await _run("Tell me everything about Jordan Lee")
    fab = await _run(f"Tell me everything about {FABRICATED}")
    agg = await _run("How many people do I message each week?")

    def _n(resp: dict) -> int:
        pr = resp.get("public_result") or {}
        for k in ("summaries", "rows", "items", "scores"):
            v = pr.get(k)
            if isinstance(v, list):
                return len(v)
        return 0

    # Off-cohort named + fabricated → empty twin (A7/A8 must not regress).
    assert _n(outsider) == 0, outsider
    assert _n(fab) == 0, fab
    # Aggregate still available under cohort grant (nameless).
    assert _n(agg) >= 1, agg
    blob = str((agg.get("public_result") or {})).lower()
    assert "maya" not in blob and "alex" not in blob

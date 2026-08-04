"""A2.E3 / C5 — access-advantage ablation: suppress ≡ physical row ablation.

Eval-first: prove unauthorized entity data does not participate in retrieval
(PermLLM access-advantage 0) and that the suppress answer is invariant when
unauthorized rows are physically removed from a seeded DB.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory

pytestmark = [pytest.mark.check("C-quality-selector-entity-grant")]

QQ_ENGINE = Path(__file__).resolve().parents[1] / "gap" / "qq" / "engine"
sys.path.insert(0, str(QQ_ENGINE))
from selector_eval_cases import (  # type: ignore[import-not-found]  # noqa: E402
    access_advantage_metrics,
    score_access_advantage_ablation,
    shapes_invariant,
)

FABRICATED = "Zephyrine Quaddlebock"


def _seed_entities_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
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
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    rows = [
        ("ent_maya_aa", "person", "Maya Chen", "maya chen", 12),
        ("ent_alex_aa", "person", "Alex Rivera", "alex rivera", 10),
        ("ent_jordan_aa", "person", "Jordan Lee", "jordan lee", 8),
    ]
    for eid, etype, name, norm, mentions in rows:
        conn.execute(
            """
            INSERT INTO entities (
                entity_id, entity_type, canonical_name, normalized_name,
                aliases_json, mention_count, is_self
            ) VALUES (?, ?, ?, ?, '[]', ?, 0)
            """,
            (eid, etype, name, norm, mentions),
        )
    conn.commit()
    conn.close()


def _ablate_entity(src: Path, dst: Path, entity_id: str) -> None:
    """Physical ablation: copy DB and delete the unauthorized entity row."""
    shutil.copy2(src, dst)
    conn = sqlite3.connect(dst)
    conn.execute("DELETE FROM entities WHERE entity_id = ?", (entity_id,))
    conn.commit()
    conn.close()


def test_retrieve_suppress_never_touches_stores(tmp_path: Path) -> None:
    """Mechanism: suppress_selectors → selector_suppressed, stores_touched=[]."""
    db = tmp_path / "aa_suppress.db"
    _seed_entities_db(db)
    adapters = AdapterFactory.create("local_database", db_path=db)
    retrieval = DefaultSignalRetrievalAdapter(adapters)
    grant = {
        "filter_manifest": {"access_mode_ceiling": "summary"},
        "accessible_entity_ids": ["ent_maya_aa"],
    }
    manifest = resolve_scope_manifest("relationship_context:read", filter_manifest=grant)
    bundle = retrieval.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text="Tell me everything about Alex Rivera",
            filter_manifest=grant,
            suppress_selectors=True,
            requester_id="grantee-x",
        )
    )
    assert bundle.retrieval_metadata.get("retrieval_strategy") == "selector_suppressed"
    assert bundle.stores_touched == []
    assert (bundle.context_packet or {}).get("summaries") == []
    scored = score_access_advantage_ablation(
        case_id="AA-MECH",
        suppress_response={
            "public_result": bundle.context_packet,
            "turn_outcome": "ok",
        },
        ablated_response={
            "public_result": {"answer_type": "summary", "summaries": []},
            "turn_outcome": "ok",
        },
        stores_touched=bundle.stores_touched,
        retrieval_strategy=bundle.retrieval_metadata.get("retrieval_strategy"),
    )
    assert scored["access_advantage"] == 0.0


@pytest.mark.asyncio
async def test_seeded_answer_invariant_under_physical_ablation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suppress-with-Alex-present ≡ query-after-Alex-rows-deleted (enforcement off)."""
    monkeypatch.setenv("TOPOS_SELECTOR_ENFORCEMENT", "1")
    full_db = tmp_path / "aa_full.db"
    ablated_db = tmp_path / "aa_ablated.db"
    _seed_entities_db(full_db)
    _ablate_entity(full_db, ablated_db, "ent_alex_aa")

    grant = {
        "filter_manifest": {"access_mode_ceiling": "summary"},
        "accessible_entity_ids": ["ent_maya_aa"],
    }
    q = "Tell me everything about Alex Rivera"

    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(full_db))
    orch_full = QueryPipelineOrchestrator(
        adapters=AdapterFactory.create("local_database", db_path=full_db)
    )
    manifest = resolve_scope_manifest("relationship_context:read", filter_manifest=grant)
    suppress_resp = await orch_full.execute(
        query_text=q,
        scope_id="relationship_context:read",
        access_mode="summary",
        manifest=manifest,
        filter_manifest=grant,
        query_session_id=f"aa-sup-{uuid.uuid4().hex[:8]}",
        requester_id="grantee-x",
        owner_id="owner",
        is_grantee_request=True,
    )
    audit = suppress_resp.get("audit") or {}
    # build_query_audit_event flattens retrieval_strategy onto the audit event.
    strategy = audit.get("retrieval_strategy")
    stores = audit.get("stores_touched") or []

    # Physical ablation twin: Alex row gone; enforcement off so suppress is not
    # the reason for emptiness — absence of unauthorized rows is.
    monkeypatch.setenv("TOPOS_SELECTOR_ENFORCEMENT", "0")
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(ablated_db))
    orch_ablated = QueryPipelineOrchestrator(
        adapters=AdapterFactory.create("local_database", db_path=ablated_db)
    )
    # Legacy unrestricted grant (no entity keys) — ablation, not policy, empties.
    ablated_grant = {"filter_manifest": {"access_mode_ceiling": "summary"}}
    ablated_manifest = resolve_scope_manifest(
        "relationship_context:read", filter_manifest=ablated_grant
    )
    ablated_resp = await orch_ablated.execute(
        query_text=q,
        scope_id="relationship_context:read",
        access_mode="summary",
        manifest=ablated_manifest,
        filter_manifest=ablated_grant,
        query_session_id=f"aa-abl-{uuid.uuid4().hex[:8]}",
        requester_id="grantee-x",
        owner_id="owner",
        is_grantee_request=True,
    )

    # Fabricated control on the full DB under enforcement (denial≡absence twin).
    monkeypatch.setenv("TOPOS_SELECTOR_ENFORCEMENT", "1")
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(full_db))
    fab_resp = await orch_full.execute(
        query_text=f"Tell me everything about {FABRICATED}",
        scope_id="relationship_context:read",
        access_mode="summary",
        manifest=manifest,
        filter_manifest=grant,
        query_session_id=f"aa-fab-{uuid.uuid4().hex[:8]}",
        requester_id="grantee-x",
        owner_id="owner",
        is_grantee_request=True,
    )

    assert strategy == "selector_suppressed", audit
    assert stores == [], stores
    assert shapes_invariant(suppress_resp, ablated_resp)
    assert shapes_invariant(suppress_resp, fab_resp)

    scored = score_access_advantage_ablation(
        case_id="AA-SEED",
        suppress_response=suppress_resp,
        ablated_response=ablated_resp,
        stores_touched=stores,
        retrieval_strategy=strategy,
        leak_control_response=None,
    )
    assert scored["access_advantage"] == 0.0, scored
    m = access_advantage_metrics(
        [{"case_id": "AA-SEED", "should_refuse": True, "access_advantage_ablation": scored}]
    )
    assert m["access_advantage_mean"] == 0.0

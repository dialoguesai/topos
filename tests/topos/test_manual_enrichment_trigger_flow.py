from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _reload_modules(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setenv("TOPOS_KEY", "test-key")
    monkeypatch.setenv("CONTROL_PLANE_URL", "")
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("TOPOS_KEY", "test-key")
    monkeypatch.setenv("TOPOS_CONTROL_PLANE_URL", "")
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("TOPOS_DATABASE_MODE", "local")
    for mod in (
        "topos.config.settings",
        "topos.core.state",
        "topos.ingestion.manager",
        "topos.ingestion.ingest_helpers",
        "topos.api.enrichment",
        "topos.sources.runtime_install",
    ):
        sys.modules.pop(mod, None)


@pytest.mark.asyncio
async def test_manual_enrichment_not_auto_then_manual_trigger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "manual_enrichment_flow.db"
    _reload_modules(monkeypatch, db_path)

    from topos.api.enrichment import _get_enrichment_status_core, _process_enrichment_core
    from topos.enrichment.orchestrator import EnrichmentOrchestrator
    from topos.ingestion.ingest_helpers import ingest_file_payload
    from topos.sources.runtime_install import install_source_from_version_row

    source_id = "manual_enrichment_test_source"
    parser_id = "managed.file.manual_enrichment.v1"
    mapper_id = "managed.mapper.manual_enrichment.v1"
    dataset_id = "manual-user:manual-enrichment"

    # Version-row style payload to match registry install flow.
    version_row = {
        "version_id": "test-version-1",
        "organization_id": "test-org",
        "app_id": "test-app",
        "source_id": source_id,
        "schema_id": parser_id,
        "source_definition_json": {
            "source_id": source_id,
            "display_name": "Manual Enrichment Test Source",
            "source_type": "file",
            "schema_id": parser_id,
            "parser_id": parser_id,
            "canonical_mapper_id": mapper_id,
            "canonical_group_id": "ai_messages",
            "enrichment_trigger": "manual",
            "ingestion_trigger": "automatic",
            "canonical_enrichment_jobs": ["emo_27"],
            "raw_enrichment_jobs": [],
            "file_ingest_shape": {
                "parser_extract_map": {
                    "message_id": "id",
                    "conversation_id": "metadata.parent_id",
                    "sender_type": "author.role",
                    "event_at": "create_time",
                    "content": "content.parts[*]",
                    "metadata_json": "metadata",
                }
            },
        },
        "compatibility_json": {
            "parser_id": parser_id,
            "canonical_mapper_id": mapper_id,
            "source_type": "file",
            "canonical_group_id": "ai_messages",
        },
    }

    sample_file = tmp_path / "manual_enrichment_sample.jsonl"
    sample_record = {
        "id": "msg-1",
        "author": {"role": "user", "name": "alice"},
        "create_time": "2026-04-15T12:00:00Z",
        "content": {"parts": ["hello manual enrichment"]},
        "metadata": {"parent_id": "conv-1"},
    }
    sample_file.write_text(json.dumps(sample_record) + "\n", encoding="utf-8")

    calls: list[dict] = []
    original_run_canonical = EnrichmentOrchestrator.run_canonical

    async def fake_run_canonical(self, canonical_messages, job_names=None, progress_callback=None):
        calls.append({"count": len(canonical_messages), "job_names": list(job_names or [])})
        # Simulate enrichment persistence so status transitions from unprocessed -> processed.
        conn = self.tables_manager.conn if self.tables_manager else None
        if conn is not None:
            created_at = datetime.now(timezone.utc).isoformat()
            for msg in canonical_messages:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO message_emotions (
                        message_id, source_id, emotion_label, confidence, model_name, all_emotions_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        msg.get("message_id"),
                        msg.get("source_id"),
                        "neutral",
                        1.0,
                        "test-model",
                        "[]",
                        created_at,
                    ),
                )
            conn.commit()
        return {
            "jobs_run": len(job_names or []),
            "records_created": {"message_emotions": len(canonical_messages)},
            "errors": [],
        }

    monkeypatch.setattr(EnrichmentOrchestrator, "run_canonical", fake_run_canonical)

    handle, _payload = install_source_from_version_row(version_row)
    try:
        ingest_result = await ingest_file_payload(
            dataset_id=dataset_id,
            schema_id=parser_id,
            file_path=str(sample_file),
            source_id=source_id,
        )
        assert ingest_result["status"] == "ok"
        assert ingest_result["records_processed"] == 1

        # Manual trigger means enrichment should NOT run during ingestion.
        assert calls == []

        before = await _get_enrichment_status_core(source_id=source_id, dataset_id=dataset_id)
        assert before["status"] == "ok"
        assert before["total_messages"] >= 1
        assert before["unprocessed_messages"] >= 1
        assert before["enrichment_trigger"] == "manual"

        process_result = await _process_enrichment_core(source_id=source_id, dataset_id=dataset_id)
        assert process_result["status"] == "ok"
        assert process_result["messages_processed"] >= 1
        assert len(calls) == 1
        assert calls[0]["job_names"] == ["emo_27"]

        after = await _get_enrichment_status_core(source_id=source_id, dataset_id=dataset_id)
        assert after["status"] == "ok"
        assert after["processed_messages"] >= 1
        assert after["unprocessed_messages"] == 0
    finally:
        handle.uninstall()
        monkeypatch.setattr(EnrichmentOrchestrator, "run_canonical", original_run_canonical)
        # Ensure test sqlite file can be cleaned up on all platforms.
        try:
            conn = sqlite3.connect(str(db_path))
            conn.close()
        except Exception:
            pass

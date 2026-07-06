"""WS-B: Enrichment Lab — bundles, dry-run worker, model overrides, apply-preferred."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.enrichment_lab import bundles as lab_bundles
from topos.enrichment_lab import store as lab_store
from topos.enrichment_lab import worker as lab_worker


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------


def test_bundle_metadata_lists_enrichment_fit():
    meta = lab_bundles.list_bundle_metadata()
    assert meta
    by_id = {m["id"]: m for m in meta}
    personal = by_id["enrich.messages.personal"]
    assert personal["enrichment_fit"]["emo_27"] == "recommended"
    assert personal["record_count"] > 0

    urls = by_id["enrich.browser.urls"]
    assert "url_classification" in urls["enrichment_fit"]


def test_bundle_compatibility():
    bundle = lab_bundles.get_bundle("enrich.messages.personal")
    assert lab_bundles.is_bundle_compatible_with_job(bundle, "emo_27")
    assert not lab_bundles.is_bundle_compatible_with_job(bundle, "url_classification")


# ---------------------------------------------------------------------------
# Model tag parsing
# ---------------------------------------------------------------------------


def test_parse_model_tag():
    assert lab_worker.parse_model_tag("default", default_provider="huggingface") == (None, None)
    assert lab_worker.parse_model_tag("hf:org/model", default_provider=None) == (
        "huggingface",
        "org/model",
    )
    assert lab_worker.parse_model_tag("ollama:llama3.2", default_provider=None) == (
        "ollama",
        "llama3.2",
    )
    assert lab_worker.parse_model_tag("org/some-model", default_provider="ollama") == (
        "huggingface",
        "org/some-model",
    )
    assert lab_worker.parse_model_tag("mistral", default_provider="ollama") == (
        "ollama",
        "mistral",
    )


# ---------------------------------------------------------------------------
# Store + worker dry-run (fake engine — no model downloads, no node writes)
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row

    import topos.core.state as state_mod
    import topos.enrichment_lab.service as service_mod

    monkeypatch.setattr(state_mod, "get_db_connection", lambda: c)
    monkeypatch.setattr(service_mod, "get_db_connection", lambda: c)
    yield c
    c.close()


class _FakeResult:
    def __init__(self, output):
        self.status = "completed"
        self.output = output
        self.error = None


class _FakeEngine:
    """Sentiment-shaped fake: records which model each task requested."""

    def __init__(self):
        self.tasks = []

    def run(self, task):
        self.tasks.append(task)
        return _FakeResult(
            {
                "label": "positive",
                "score": 0.9,
                "provider": task.model_request.provider,
                "model": task.model_request.model or "registry-default",
            }
        )


@pytest.mark.asyncio
async def test_lab_dry_run_writes_only_lab_tables(conn, monkeypatch):
    from topos.enrichment_lab import service as lab_service

    fake = _FakeEngine()
    import topos.enrichment_lab.worker as worker_mod

    monkeypatch.setattr(worker_mod, "_ModelOverrideEngine", lab_worker._ModelOverrideEngine)

    import topos.engine as engine_mod

    monkeypatch.setattr(engine_mod, "Engine", lambda: fake)
    # worker imports Engine inside the function from ..engine
    gid = lab_service.create_job_group(
        job_id="sentiment",
        models=["default", "hf:other/model"],
        dataset_kind="bundle",
        bundle_id="enrich.messages.personal",
    )
    # The service schedules the worker in background; run it synchronously here.
    await worker_mod._process_group(gid)

    data = lab_service.serialize_job_group(conn, gid)
    group = data["group"]
    runs = data["runs"]
    assert group["status"] in ("completed", "completed_with_errors")
    assert len(runs) == 16  # 8 records x 2 models
    succeeded = [r for r in runs if r["status"] == "succeeded"]
    assert succeeded, [r.get("error_code") for r in runs]
    sample = succeeded[0]
    assert sample["output"], "run output should contain enrichment rows"
    assert sample["latency_ms"] is not None

    # Model override was applied for the hf: tagged runs.
    override_models = {
        t.model_request.model for t in fake.tasks if t.model_request.model is not None
    }
    assert "other/model" in override_models

    # Dry-run: no enrichment output tables were created in the node DB.
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "message_sentiment" not in tables
    assert tables >= {"enrichment_lab_job_group", "enrichment_lab_run"}


@pytest.mark.asyncio
async def test_lab_apply_preferred_sets_device_override(conn, monkeypatch):
    from topos.enrichment_lab import service as lab_service

    # Engine config storage used by model_overrides.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)"
    )

    import topos.core.state as state_mod

    def fake_get(cfg_conn, key):
        row = cfg_conn.execute("SELECT value FROM engine_config WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def fake_set(cfg_conn, key, value):
        cfg_conn.execute(
            "INSERT OR REPLACE INTO engine_config (key, value) VALUES (?, ?)", (key, value)
        )
        cfg_conn.commit()

    monkeypatch.setattr(state_mod, "get_engine_config_value", fake_get)
    monkeypatch.setattr(state_mod, "set_engine_config_value", fake_set)

    fake = _FakeEngine()
    import topos.engine as engine_mod

    monkeypatch.setattr(engine_mod, "Engine", lambda: fake)

    gid = lab_service.create_job_group(
        job_id="sentiment",
        models=["hf:winner/model"],
        dataset_kind="bundle",
        bundle_id="enrich.messages.personal",
    )
    lab_store.patch_group(conn, gid, preferred_model_tag="hf:winner/model")
    result = lab_service.apply_preferred_model(gid)
    assert result["status"] == "ok"
    assert result["overrides"]["sentiment"]["model"] == "winner/model"

    from topos.enrichment.model_overrides import get_model_override

    override = get_model_override("sentiment", conn=conn)
    assert override == {"provider": "huggingface", "model": "winner/model"}


def test_create_job_group_validations(conn):
    from topos.enrichment_lab import service as lab_service

    with pytest.raises(ValueError, match="not runnable"):
        lab_service.create_job_group(
            job_id="topic_clusters", models=["default"], bundle_id="enrich.messages.personal"
        )
    with pytest.raises(ValueError, match="Unknown enrichment"):
        lab_service.create_job_group(
            job_id="nope", models=["default"], bundle_id="enrich.messages.personal"
        )
    with pytest.raises(ValueError, match="not compatible"):
        lab_service.create_job_group(
            job_id="url_classification",
            models=["default"],
            bundle_id="enrich.messages.personal",
        )
    with pytest.raises(ValueError, match="Invalid HuggingFace"):
        lab_service.create_job_group(
            job_id="sentiment",
            models=["hf:not a valid id!!"],
            bundle_id="enrich.messages.personal",
        )

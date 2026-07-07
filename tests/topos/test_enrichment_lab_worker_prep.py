"""Worker HF pre-flight: task-mismatch guard, download phase, clear errors."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

import pytest

from topos.enrichment_lab import model_resolve
from topos.enrichment_lab import store as lab_store
from topos.enrichment_lab import worker as lab_worker


@pytest.fixture(autouse=True)
def _clear_resolver_cache():
    model_resolve.clear_cache()
    yield
    model_resolve.clear_cache()


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


def _make_group(conn, model_tag: str = "hf:org/model") -> tuple[str, list]:
    gid = lab_store.insert_group(
        conn,
        job_id="sentiment",
        dataset_kind="bundle",
        models=[model_tag],
        record_inputs={"r1": {"body": "hello"}, "r2": {"body": "world"}},
        bundle_id="enrich.messages.personal",
        bundle_version="v1",
    )
    runs = [dict(r) for r in lab_store.list_runs(conn, gid)]
    return gid, runs


def _resolved(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "status": "ok",
        "model_id": "org/model",
        "pipeline_tag": "text-classification",
        "size_bytes": 1000,
        "compatible_jobs": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def test_prepare_fails_on_task_mismatch(conn, monkeypatch):
    _gid, runs = _make_group(conn)
    monkeypatch.setattr(
        model_resolve,
        "resolve_model",
        lambda m, **kw: _resolved(pipeline_tag="token-classification"),
    )
    error = lab_worker._prepare_hf_model(conn, "sentiment", "org/model", runs)
    assert error is not None and error.startswith("task_mismatch")
    assert "token-classification" in error
    assert "text-classification" in error


def test_prepare_fails_on_not_found(conn, monkeypatch):
    _gid, runs = _make_group(conn)
    monkeypatch.setattr(
        model_resolve, "resolve_model", lambda m, **kw: _resolved(status="not_found")
    )
    error = lab_worker._prepare_hf_model(conn, "sentiment", "org/model", runs)
    assert error is not None and "not found" in error


def test_prepare_fails_on_gated(conn, monkeypatch):
    _gid, runs = _make_group(conn)
    monkeypatch.setattr(
        model_resolve, "resolve_model", lambda m, **kw: _resolved(status="unauthorized")
    )
    error = lab_worker._prepare_hf_model(conn, "sentiment", "org/model", runs)
    assert error is not None and "gated or private" in error


def test_prepare_skips_download_when_cached(conn, monkeypatch):
    _gid, runs = _make_group(conn)
    monkeypatch.setattr(model_resolve, "resolve_model", lambda m, **kw: _resolved())
    monkeypatch.setattr(lab_worker, "_hf_model_cached", lambda m: True)

    def fail_download(model: str) -> Optional[str]:  # pragma: no cover
        raise AssertionError("cached model must not be re-downloaded")

    monkeypatch.setattr(lab_worker, "_ensure_hf_model", fail_download)
    assert lab_worker._prepare_hf_model(conn, "sentiment", "org/model", runs) is None
    # No run entered the downloading state.
    statuses = {dict(r)["status"] for r in lab_store.list_runs(conn, _gid)}
    assert statuses == {"queued"}


def test_prepare_marks_runs_downloading_and_downloads(conn, monkeypatch):
    gid, runs = _make_group(conn)
    monkeypatch.setattr(model_resolve, "resolve_model", lambda m, **kw: _resolved())
    monkeypatch.setattr(lab_worker, "_hf_model_cached", lambda m: False)
    monkeypatch.setattr(lab_worker, "_disk_space_error", lambda size: None)
    downloaded = []

    def fake_download(model: str) -> Optional[str]:
        # Runs must already be flagged before the (slow) download starts.
        statuses = {dict(r)["status"] for r in lab_store.list_runs(conn, gid)}
        assert statuses == {"downloading_model"}
        downloaded.append(model)
        return None

    monkeypatch.setattr(lab_worker, "_ensure_hf_model", fake_download)
    assert lab_worker._prepare_hf_model(conn, "sentiment", "org/model", runs) is None
    assert downloaded == ["org/model"]


def test_prepare_reports_download_failure(conn, monkeypatch):
    _gid, runs = _make_group(conn)
    monkeypatch.setattr(model_resolve, "resolve_model", lambda m, **kw: _resolved())
    monkeypatch.setattr(lab_worker, "_hf_model_cached", lambda m: False)
    monkeypatch.setattr(
        lab_worker, "_ensure_hf_model", lambda m: "download_failed: no space left on device"
    )
    error = lab_worker._prepare_hf_model(conn, "sentiment", "org/model", runs)
    assert error is not None and error.startswith("download_failed")


def test_prepare_fails_on_insufficient_disk(conn, monkeypatch):
    _gid, runs = _make_group(conn)
    monkeypatch.setattr(
        model_resolve, "resolve_model", lambda m, **kw: _resolved(size_bytes=10**13)
    )
    monkeypatch.setattr(lab_worker, "_hf_model_cached", lambda m: False)
    error = lab_worker._prepare_hf_model(conn, "sentiment", "org/model", runs)
    assert error is not None and error.startswith("insufficient_disk_space")


def test_prepare_hub_unreachable_does_not_block(conn, monkeypatch):
    """Offline nodes must still run already-downloaded models."""
    _gid, runs = _make_group(conn)
    monkeypatch.setattr(
        model_resolve,
        "resolve_model",
        lambda m, **kw: _resolved(status="unreachable", pipeline_tag=None, size_bytes=None),
    )
    monkeypatch.setattr(lab_worker, "_hf_model_cached", lambda m: True)
    assert lab_worker._prepare_hf_model(conn, "sentiment", "org/model", runs) is None


@pytest.mark.asyncio
async def test_process_group_fails_runs_on_prep_error(conn, monkeypatch):
    """End-to-end: a failing pre-flight marks every run of that model failed."""
    from topos.enrichment_lab import service as lab_service

    gid, _ = _make_group(conn, model_tag="hf:org/wrong-task-model")
    monkeypatch.setattr(
        lab_worker, "_prepare_hf_model", lambda c, j, m, r: "task_mismatch: nope"
    )
    await lab_worker._process_group(gid)

    data = lab_service.serialize_job_group(conn, gid)
    assert data["group"]["status"] == "completed_with_errors"
    assert all(r["status"] == "failed" for r in data["runs"])
    assert all(r["error_code"] == "task_mismatch: nope" for r in data["runs"])
    assert all(r["finished_at"] for r in data["runs"])


def test_disk_space_error_none_for_unknown_size():
    assert lab_worker._disk_space_error(None) is None
    assert lab_worker._disk_space_error(0) is None

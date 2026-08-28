"""Credit-paused derivation debt resumes when the wallet fills or ingest goes local."""

from __future__ import annotations

import sqlite3
from typing import Iterator

import pytest

from topos.enrichment import job_readiness
from topos.enrichment.derivation_recovery import (
    pending_derivation_summary,
    record_failed_derivation,
    revive_capability_blocked_debts,
)
from topos.pipeline.job_store import fail_job
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up


@pytest.fixture(autouse=True)
def _clean_probe_cache() -> Iterator[None]:
    job_readiness.reset_probe_cache()
    yield
    job_readiness.reset_probe_cache()


@pytest.fixture
def conn(tmp_path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(str(tmp_path / "debt.db"))
    apply_pipeline_jobs_v1_up(db)
    yield db
    db.close()


def _set_providers(monkeypatch: pytest.MonkeyPatch, **status: str) -> None:
    monkeypatch.setattr(
        "topos.features.signal.data_health.check_provider_status",
        lambda: dict(status),
    )
    job_readiness.reset_probe_cache()


def _park_credits(conn: sqlite3.Connection, *, batch: str) -> str:
    job_id = record_failed_derivation(
        conn,
        source_id="github_activity",
        sync_batch_id=batch,
        job_name="topics",
        error="insufficient_credits",
        record_ids=["m1"],
        record_count=1,
    )
    assert job_id
    fail_job(conn, job_id, error="insufficient_credits")
    return job_id


def _status_of(conn: sqlite3.Connection, job_id: str) -> str:
    row = conn.execute(
        "SELECT status FROM pipeline_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    return str(row[0])


def test_summary_counts_insufficient_credits(conn: sqlite3.Connection) -> None:
    _park_credits(conn, batch="b-sum")
    summary = pending_derivation_summary(conn)
    assert summary["insufficient_credits"] == 1
    assert summary["known_gaps"] is True


def test_wallet_fill_revives_credit_paused_debts(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    wallet = {"positive": False}
    monkeypatch.setattr(
        "topos.engine.hosted_llm_wallet.hosted_llm_wallet_allows",
        lambda force=False: wallet["positive"],
    )
    monkeypatch.setattr(
        "topos.engine.hosted_llm_wallet.ingest_uses_hosted_llm",
        lambda: True,
    )
    _set_providers(monkeypatch, ollama="up", huggingface="up")
    revive_capability_blocked_debts(conn)  # persist "already ready" + empty wallet

    job_id = _park_credits(conn, batch="b-fill")
    assert revive_capability_blocked_debts(conn)["credits_revived"] == 0
    assert _status_of(conn, job_id) == "failed"

    wallet["positive"] = True
    out = revive_capability_blocked_debts(conn)
    assert out["credits_revived"] == 1
    assert _status_of(conn, job_id) == "queued"

    fail_job(conn, job_id, error="insufficient_credits")
    assert revive_capability_blocked_debts(conn)["credits_revived"] == 0


def test_switching_ingest_off_hosted_revives_credit_paused_debts(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    hosted = {"on": True}
    monkeypatch.setattr(
        "topos.engine.hosted_llm_wallet.hosted_llm_wallet_allows",
        lambda force=False: False,
    )
    monkeypatch.setattr(
        "topos.engine.hosted_llm_wallet.ingest_uses_hosted_llm",
        lambda: hosted["on"],
    )
    _set_providers(monkeypatch, ollama="up", huggingface="up")
    revive_capability_blocked_debts(conn)

    job_id = _park_credits(conn, batch="b-local")
    hosted["on"] = False
    out = revive_capability_blocked_debts(conn)
    assert out["credits_revived"] == 1
    assert _status_of(conn, job_id) == "queued"

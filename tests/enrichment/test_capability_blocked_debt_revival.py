"""Parked derivation debt waits for its model, then resumes on its own.

A debt whose job needs a provider the machine does not have could only ever
spend its one attempt re-running into the same wall and parking 'failed' —
after which nothing in the queue moved it, because ``requeue_job`` only
releases a live claim and ``recover_stale_jobs`` only touches 'running'. The
work resumed when a human hit ``POST /signal/derivation-debt/retry``, or never.

So: the executor declines to burn the attempt while the provider is absent, and
a sweep re-queues the parked rows on the not-ready → ready edge.
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

import pytest

from topos.enrichment import job_readiness
from topos.enrichment.derivation_recovery import (
    record_failed_derivation,
    revive_capability_blocked_debts,
    run_derivation_retry_job,
)
from topos.pipeline.job_store import fail_job, requeue_failed_jobs
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up


@pytest.fixture(autouse=True)
def _clean_probe_cache() -> Iterator[None]:
    """The probe caches for 30s; the readiness EDGE lives per-database."""
    job_readiness.reset_probe_cache()
    yield
    job_readiness.reset_probe_cache()


@pytest.fixture
def conn(tmp_path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(str(tmp_path / "debt.db"))
    apply_pipeline_jobs_v1_up(db)
    yield db
    db.close()


def _set_providers(monkeypatch, **status: str) -> None:
    """Pin the reachability probe; readiness caches, so reset around it."""
    monkeypatch.setattr(
        "topos.features.signal.data_health.check_provider_status",
        lambda: dict(status),
    )
    job_readiness.reset_probe_cache()


def _park_debt(conn: sqlite3.Connection, *, job_name: str, batch: str) -> str:
    job_id = record_failed_derivation(
        conn,
        source_id="grow_journal",
        sync_batch_id=batch,
        job_name=job_name,
        error="ollama_unreachable",
        record_ids=["m1"],
        record_count=1,
    )
    assert job_id
    fail_job(conn, job_id, error="waiting for provider: ollama not reachable")
    return job_id


def _status_of(conn: sqlite3.Connection, job_id: str) -> str:
    row = conn.execute(
        "SELECT status FROM pipeline_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    return str(row[0])


# --- readiness classification -------------------------------------------------


def test_rules_jobs_never_hold(monkeypatch) -> None:
    _set_providers(monkeypatch, ollama="down", huggingface="up")
    assert job_readiness.should_hold_job("relationship_edges")[0] is False


def test_llm_jobs_hold_without_ollama(monkeypatch) -> None:
    _set_providers(monkeypatch, ollama="down", huggingface="up")
    hold, reason = job_readiness.should_hold_job("topics")
    assert hold is True
    assert "ollama" in reason
    assert job_readiness.should_hold_job("goal_extraction")[0] is True


def test_llm_jobs_release_once_ollama_answers(monkeypatch) -> None:
    _set_providers(monkeypatch, ollama="up", huggingface="up")
    assert job_readiness.should_hold_job("topics")[0] is False


def test_uncatalogued_job_never_holds(monkeypatch) -> None:
    """Holding what we cannot classify would stall debts we don't understand."""
    _set_providers(monkeypatch, ollama="down", huggingface="up")
    assert job_readiness.should_hold_job("facts")[0] is False
    assert job_readiness.should_hold_job("not_a_real_job")[0] is False


def test_uncached_hf_job_is_not_ready_but_never_holds(monkeypatch) -> None:
    """The two questions come apart here, which is the whole reason for both.

    Weights that are not on disk mean the job cannot run *now*, so a person
    should be told. But the first run downloads them, so holding its debt
    would strand work a networked node finishes unaided.
    """
    _set_providers(monkeypatch, ollama="up", huggingface="up")
    monkeypatch.setattr(job_readiness, "hf_model_cached", lambda *_a, **_k: False)

    state = job_readiness.readiness_of("entities")
    assert state.ready is False
    assert state.blocking is False
    assert "not downloaded" in state.reason
    assert job_readiness.should_hold_job("entities")[0] is False


def test_llm_job_set_is_derived_from_the_catalog() -> None:
    """The set data_health gates on must come from the catalog, not a literal."""
    assert job_readiness.jobs_for_provider("ollama") == frozenset(
        {"topics", "dimension_summary", "goal_extraction"}
    )
    assert "entities" in job_readiness.jobs_for_provider("huggingface")
    assert job_readiness.jobs_for_provider("nonexistent") == frozenset()


def test_cached_hf_job_is_ready(monkeypatch) -> None:
    _set_providers(monkeypatch, ollama="down", huggingface="up")
    monkeypatch.setattr(job_readiness, "hf_model_cached", lambda *_a, **_k: True)

    state = job_readiness.readiness_of("embeddings")
    assert state.ready is True
    assert state.model == "sentence-transformers/all-MiniLM-L6-v2"


# --- the executor holds instead of burning the attempt ------------------------


@pytest.mark.asyncio
async def test_retry_executor_holds_while_provider_absent(monkeypatch) -> None:
    _set_providers(monkeypatch, ollama="down", huggingface="up")

    called = False

    async def _should_not_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"outcome": "recovered"}

    monkeypatch.setattr(
        "topos.enrichment.derivation_recovery.retry_single_derivation",
        _should_not_run,
    )

    result = await run_derivation_retry_job(
        {"source_id": "grow_journal", "sync_batch_id": "b1", "job_name": "topics"}
    )

    assert result["status"] == "error"
    assert "waiting for provider" in result["error"]
    assert not called, "must not reload records just to defer again"


# --- the sweep is edge-triggered ----------------------------------------------


def test_no_revival_while_provider_stays_down(monkeypatch, conn) -> None:
    _set_providers(monkeypatch, ollama="down", huggingface="up")
    job_id = _park_debt(conn, job_name="topics", batch="b-down")

    assert revive_capability_blocked_debts(conn)["revived"] == 0
    assert _status_of(conn, job_id) == "failed"


def test_revival_on_the_ready_edge(monkeypatch, conn) -> None:
    _set_providers(monkeypatch, ollama="down", huggingface="up")
    job_id = _park_debt(conn, job_name="topics", batch="b-edge")
    revive_capability_blocked_debts(conn)  # observe "down" first

    _set_providers(monkeypatch, ollama="up", huggingface="up")
    out = revive_capability_blocked_debts(conn)

    assert out["newly_ready"] == ["ollama"]
    assert out["revived"] == 1
    assert _status_of(conn, job_id) == "queued"


def test_second_sweep_without_a_flip_revives_nothing(monkeypatch, conn) -> None:
    """Level-triggered would re-queue genuinely broken debts on every sweep."""
    _set_providers(monkeypatch, ollama="down", huggingface="up")
    job_id = _park_debt(conn, job_name="topics", batch="b-once")
    revive_capability_blocked_debts(conn)

    _set_providers(monkeypatch, ollama="up", huggingface="up")
    assert revive_capability_blocked_debts(conn)["revived"] == 1

    # It ran and failed again for some non-capability reason.
    fail_job(conn, job_id, error="a real bug")
    out = revive_capability_blocked_debts(conn)

    assert out["newly_ready"] == []
    assert out["revived"] == 0
    assert _status_of(conn, job_id) == "failed"


def test_revival_leaves_non_llm_debts_alone(monkeypatch, conn) -> None:
    _set_providers(monkeypatch, ollama="down", huggingface="up")
    llm_debt = _park_debt(conn, job_name="topics", batch="b-mixed-llm")
    rules_debt = _park_debt(conn, job_name="relationship_edges", batch="b-mixed-rules")
    revive_capability_blocked_debts(conn)

    _set_providers(monkeypatch, ollama="up", huggingface="up")
    out = revive_capability_blocked_debts(conn)

    assert out["revived"] == 1
    assert _status_of(conn, llm_debt) == "queued"
    assert _status_of(conn, rules_debt) == "failed", "ollama says nothing about a rules job"


def test_readiness_edge_survives_a_restart(monkeypatch, conn) -> None:
    """Install the model, relaunch the app — the common real-world sequence.

    The edge lives in engine_config, so the relaunch does not erase the
    observation that ollama was down; coming up reachable is still a flip.
    """
    _set_providers(monkeypatch, ollama="down", huggingface="up")
    job_id = _park_debt(conn, job_name="goal_extraction", batch="b-restart")
    revive_capability_blocked_debts(conn)  # this process observes "down"

    # Next process: nothing in memory carries over, the database does.
    job_readiness.reset_probe_cache()
    _set_providers(monkeypatch, ollama="up", huggingface="up")

    assert revive_capability_blocked_debts(conn)["revived"] == 1
    assert _status_of(conn, job_id) == "queued"


def test_relaunch_with_provider_always_up_revives_nothing(monkeypatch, conn) -> None:
    """A node that never lost its provider must not retry broken debts on boot."""
    _set_providers(monkeypatch, ollama="up", huggingface="up")
    revive_capability_blocked_debts(conn)  # observe "up" and persist it

    job_id = _park_debt(conn, job_name="topics", batch="b-broken")
    fail_job(conn, job_id, error="a real bug")

    job_readiness.reset_probe_cache()  # simulate the relaunch
    out = revive_capability_blocked_debts(conn)

    assert out["newly_ready"] == []
    assert out["revived"] == 0
    assert _status_of(conn, job_id) == "failed"


def test_never_observed_node_with_provider_up_heals_once(monkeypatch, conn) -> None:
    """An upgraded node carrying parked debts gets one sweep, then settles."""
    _set_providers(monkeypatch, ollama="up", huggingface="up")
    job_id = _park_debt(conn, job_name="topics", batch="b-upgrade")

    assert revive_capability_blocked_debts(conn)["revived"] == 1
    assert _status_of(conn, job_id) == "queued"

    fail_job(conn, job_id, error="failed again")
    assert revive_capability_blocked_debts(conn)["revived"] == 0


# --- the transition itself ----------------------------------------------------


def test_requeue_failed_jobs_only_moves_failed_rows(conn) -> None:
    parked = _park_debt(conn, job_name="topics", batch="b-parked")
    queued = record_failed_derivation(
        conn,
        source_id="grow_journal",
        sync_batch_id="b-queued",
        job_name="topics",
        error="x",
        record_ids=["m1"],
        record_count=1,
    )

    assert requeue_failed_jobs(conn, [parked, str(queued)]) == 1
    assert _status_of(conn, parked) == "queued"
    assert requeue_failed_jobs(conn, []) == 0

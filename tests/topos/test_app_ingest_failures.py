from __future__ import annotations

import sqlite3

import pytest

from topos.core.handlers import handle_control_plane_request
from topos.storage.db.migrations import apply_all_migrations


@pytest.mark.asyncio
async def test_app_ingest_logs_and_returns_error_when_all_records_fail(
    monkeypatch,
    caplog,
) -> None:
    async def _fail_ingest(**kwargs):  # noqa: ANN003
        raise TypeError("unhashable type: 'slice'")

    monkeypatch.setattr(
        "topos.ingestion.ingest_helpers.ingest_ui_payload",
        _fail_ingest,
    )

    with caplog.at_level("WARNING"):
        result = await handle_control_plane_request(
            {
                "id": "req-app-ingest-fail",
                "type": "app_ingest",
                "payload": {
                    "user_id": "user-1",
                    "dataset_id": "user-1:default:device1",
                    "source_id": "browser_events",
                    "schema_id": "browser.events.v1",
                    "records": [{"event_type": "click", "url": "https://example.com"}],
                },
            }
        )

    assert result["status"] == "error"
    assert "failed ingestion" in result.get("error", "")
    assert result["payload"]["records_processed"] == 0
    assert len(result["payload"]["errors"]) == 1
    assert any("[PIPELINE:APP_INGEST] Record failed" in rec.message for rec in caplog.records)
    assert any("[PIPELINE:APP_INGEST] Ingest completed with failures" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_app_ingest_returns_ok_with_errors_on_partial_success(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "partial.db"
    # The direct-ingest DB stretch runs on a worker thread (asyncio.to_thread),
    # so the injected connection must allow cross-thread use.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)

    from topos.ingestion import ingest_helpers as ingest_helpers_module

    real_ingest = ingest_helpers_module.ingest_ui_payload
    call_count = {"n": 0}

    async def _mixed_ingest(**kwargs):  # noqa: ANN003
        call_count["n"] += 1
        if call_count["n"] == 1:
            return await real_ingest(**kwargs)
        raise RuntimeError("second record failed")

    monkeypatch.setattr(ingest_helpers_module, "ingest_ui_payload", _mixed_ingest)

    result = await handle_control_plane_request(
        {
            "id": "req-app-ingest-partial",
            "type": "app_ingest",
            "payload": {
                "user_id": "user-1",
                "dataset_id": "user-1:default:device1",
                "source_id": "browser_events",
                "schema_id": "browser.events.v1",
                "records": [
                    {
                        "event_type": "click",
                        "url": "https://example.com/one",
                        "visited_at": "2026-05-29T16:40:00.000Z",
                        "content": {"x": 1},
                    },
                    {
                        "event_type": "click",
                        "url": "https://example.com/two",
                        "visited_at": "2026-05-29T16:41:00.000Z",
                        "content": {"x": 2},
                    },
                ],
            },
        }
    )

    assert result["status"] == "ok"
    assert result["payload"]["records_processed"] == 1
    assert result["payload"]["records_total"] == 2
    assert len(result["payload"]["errors"]) == 1
    conn.close()


@pytest.mark.asyncio
async def test_app_ingest_timeline_survives_cancelled_background_enrichment(
    monkeypatch,
    tmp_path,
) -> None:
    db_path = tmp_path / "restart-safe.db"
    # The direct-ingest DB stretch runs on a worker thread (asyncio.to_thread),
    # so the injected connection must allow cross-thread use.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)

    class _CancelledTask:
        def cancelled(self) -> bool:
            return True

        def add_done_callback(self, callback) -> None:  # noqa: ANN001
            callback(self)

    def _cancel_immediately(coro):  # noqa: ANN001
        coro.close()
        return _CancelledTask()

    monkeypatch.setattr("topos.core.handlers.ingest.asyncio.create_task", _cancel_immediately)

    result = await handle_control_plane_request(
        {
            "id": "req-app-ingest-restart",
            "type": "app_ingest",
            "payload": {
                "user_id": "user-1",
                "dataset_id": "user-1:default:device1",
                "source_id": "browser_visits",
                "schema_id": "browser.visits.v1",
                "records": [
                    {
                        "url": "https://example.com/restart",
                        "visited_at": "2026-07-13T23:20:00.000Z",
                        "title": "Restart",
                    }
                ],
            },
        }
    )

    assert result["status"] == "ok"
    canonical = conn.execute(
        "SELECT event_id FROM activity_events WHERE source_id='browser_visits'"
    ).fetchone()
    assert canonical is not None
    assert (
        conn.execute("SELECT COUNT(*) FROM timeline WHERE record_id=?", (canonical["event_id"],)).fetchone()[0]
        == 1
    )
    conn.close()


@pytest.mark.asyncio
async def test_app_ingest_takes_no_write_gate_on_event_loop(monkeypatch, tmp_path, caplog) -> None:
    """The app_ingest chain (raw→flat→canonical→timeline→UMA) must never take
    the write gate on the event-loop thread: each hold stalls every coroutine,
    including the control-plane keepalive (observed live 2026-08-08 20:17)."""
    import logging

    from topos.storage.db.write_gate import reset_loop_warning_state

    db_path = tmp_path / "loop_gate.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    # Keep the background pipeline worker out of this test: it exercises the
    # deferred lane, which has its own coverage.
    monkeypatch.setattr("topos.pipeline.job_runner.start_pipeline_worker", lambda *_: None)

    reset_loop_warning_state()
    caplog.set_level(logging.WARNING, logger="topos.storage.db.write_gate")

    result = await handle_control_plane_request(
        {
            "id": "req-app-ingest-loop-gate",
            "type": "app_ingest",
            "payload": {
                "user_id": "user-1",
                "dataset_id": "user-1:default:device1",
                "source_id": "browser_visits",
                "schema_id": "browser.visits.v1",
                "records": [
                    {
                        "url": "https://example.com/loop-gate",
                        "visited_at": "2026-08-08T20:17:00.000Z",
                        "title": "Loop gate",
                    }
                ],
            },
        }
    )

    assert result["status"] == "ok"
    loop_warnings = [
        rec.getMessage()
        for rec in caplog.records
        if "acquired on the event-loop thread" in rec.getMessage()
    ]
    assert not loop_warnings, loop_warnings
    conn.close()


@pytest.mark.asyncio
async def test_app_ingest_fails_when_source_not_installed() -> None:
    result = await handle_control_plane_request(
        {
            "id": "req-app-ingest-missing-source",
            "type": "app_ingest",
            "payload": {
                "user_id": "user-1",
                "dataset_id": "user-1:default:device1",
                "source_id": "my_uninstalled_stream",
                "schema_id": "journal.time_log.v1",
                "records": [{"startDate": "2026-06-23", "goal": "Should fail"}],
            },
        }
    )

    assert result["status"] == "error"
    assert "not installed" in result.get("error", "").lower()

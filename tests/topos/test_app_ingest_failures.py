from __future__ import annotations

import sqlite3

import pytest

from topos.core.handlers import handle_control_plane_request


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
    conn = sqlite3.connect(str(db_path))
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

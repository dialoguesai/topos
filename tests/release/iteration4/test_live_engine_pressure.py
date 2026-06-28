"""Iteration 4 — Live engine smoke against running localhost:9000."""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.release_pressure, pytest.mark.p0, pytest.mark.live]

LOCAL_ENGINE = os.environ.get("TOPOS_ENGINE_URL", "http://localhost:9000")
TOPOS_KEY = os.environ.get("TOPOS_KEY", "")


def _auth_headers() -> dict[str, str]:
    if not TOPOS_KEY:
        pytest.skip("TOPOS_KEY not set")
    return {"Authorization": f"Bearer {TOPOS_KEY}"}


@pytest.fixture(scope="module")
def engine_up() -> None:
    import httpx

    try:
        r = httpx.get(f"{LOCAL_ENGINE}/healthcheck", timeout=5.0)
    except Exception as exc:
        pytest.skip(f"Engine not reachable at {LOCAL_ENGINE}: {exc}")
    if r.status_code != 200:
        pytest.skip(f"Engine healthcheck returned {r.status_code}")


class TestLiveScrubSafetyPressure:
    """Top-level dry_run must never mutate live DB."""

    def test_top_level_dry_run_does_not_delete_rows(self, engine_up) -> None:
        import httpx

        headers = _auth_headers()
        _VOLATILE_TABLES = frozenset({"mcp_request_log", "sync_event_log", "request_audit_log"})

        def _table_row_counts(payload: dict) -> dict[str, int | None]:
            tables = (payload or {}).get("tables") or {}
            counts: dict[str, int | None] = {}
            if isinstance(tables, dict):
                for rows in tables.values():
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        if isinstance(row, dict) and row.get("name"):
                            counts[str(row["name"])] = row.get("row_count", row.get("count"))
            elif isinstance(tables, list):
                for row in tables:
                    if isinstance(row, dict) and row.get("name"):
                        counts[str(row["name"])] = row.get("row_count", row.get("count"))
            return counts

        with httpx.Client(base_url=LOCAL_ENGINE, timeout=60.0) as client:
            tables_before = client.post("/api/local/list_database_tables", headers=headers)
            assert tables_before.status_code == 200, tables_before.text[:200]
            before_counts = _table_row_counts(tables_before.json())
            scrub = client.post(
                "/v1/source-scrub",
                headers=headers,
                json={
                    "source_id": "__nonexistent_pressure_test_source__",
                    "dry_run": True,
                    "preset": "scrub",
                },
            )
            assert scrub.status_code in (200, 400, 404, 503), scrub.text[:300]
            if scrub.status_code == 200:
                body = scrub.json()
                assert body.get("scrub_status") in ("dry_run", "completed", "error"), body
            tables_after = client.post("/api/local/list_database_tables", headers=headers)
            assert tables_after.status_code == 200
            after_counts = _table_row_counts(tables_after.json())
            for name, count in before_counts.items():
                if name in _VOLATILE_TABLES:
                    continue
                if name in after_counts and count is not None and after_counts[name] is not None:
                    assert after_counts[name] == count, f"table {name} row count changed after dry_run scrub"


class TestLiveSignalPressure:
    def test_unknown_cluster_returns_404(self, engine_up) -> None:
        import httpx

        with httpx.Client(base_url=LOCAL_ENGINE, timeout=30.0) as client:
            r = client.get(
                "/v1/signal/topic-clusters/00000000-0000-0000-0000-000000000000/members",
                headers=_auth_headers(),
            )
        assert r.status_code == 404

    def test_vector_search_nonsense_low_confidence(self, engine_up) -> None:
        import httpx

        with httpx.Client(base_url=LOCAL_ENGINE, timeout=60.0) as client:
            r = client.get(
                "/v1/signal/vectors/search",
                headers=_auth_headers(),
                params={"q": "xyzzy qwerty nonsense zzz", "limit": 5},
            )
        if r.status_code == 503:
            pytest.skip("vector tier unavailable")
        assert r.status_code == 200, r.text[:200]
        items = (r.json() or {}).get("items") or []
        for item in items:
            sim = float(item.get("similarity") or item.get("hybrid_score") or 0.0)
            assert sim >= 0.30, f"hit below min similarity threshold: {item}"


class TestLiveQueryPressure:
    def test_empty_query_via_pipeline_unit(self) -> None:
        """Live engine optional; deny path validated in-process."""
        import asyncio

        from topos.query.manifest_validation import resolve_scope_manifest
        from topos.query.pipeline import QueryPipelineOrchestrator
        from topos.storage.adapters.factory import AdapterFactory

        async def _run() -> None:
            orch = QueryPipelineOrchestrator(adapters=AdapterFactory.create("memory"))
            out = await orch.execute(
                query_text="   ",
                scope_id="messages:read",
                access_mode="raw",
                manifest=resolve_scope_manifest("messages:read"),
                query_session_id="live-pressure-empty",
            )
            assert out.get("deny_reason") == "empty_query"

        asyncio.run(_run())

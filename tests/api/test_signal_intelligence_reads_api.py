"""Facts / Insights / Timeline read APIs (data-screen tab backends)."""

from __future__ import annotations

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from topos.features.facts.extract import extract_facts_from_batch
from topos.features.stats.engine import StatsEngine
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def populated_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "reads_api.db"))
    apply_all_migrations(conn)
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
        " VALUES ('ent_self', 'person', 'Jordan Lee', 'jordan lee', 1)"
    )
    conn.commit()

    # facts: one current job, one past job (belief history), one owner-only
    extract_facts_from_batch(conn, [
        {"_table": "profile_records", "record_id": "p1", "source_id": "src",
         "record_type": "experience", "title": "Staff Engineer",
         "organization": "Topos", "description": "Lead ingestion pipelines."},
        {"_table": "profile_records", "record_id": "p2", "source_id": "src",
         "record_type": "experience", "title": "Engineer",
         "organization": "Dialogues", "description": "Built messaging. 2021–2024."},
        {"_table": "journal_entries", "record_id": "j1", "source_id": "src",
         "entry_at": "2026-05-11T07:05:00Z", "category": "exercise",
         "content": "long run for the half marathon block"},
    ])

    # stats: fold journal rows and promote insights
    engine = StatsEngine(conn)
    engine.fold_batch([
        {"record_id": f"jr{i}", "_table": "journal_entries",
         "entry_at": f"2026-06-{i + 1:02d}T08:00:00Z", "category": "exercise",
         "duration_minutes": 30 + i}
        for i in range(4)
    ])
    engine.promote_insights(AdapterFactory.create("local_database", conn=conn))

    # timeline rows
    for i in range(5):
        conn.execute(
            "INSERT OR REPLACE INTO timeline (event_at, record_id, source_id, canonical_table, record_type)"
            " VALUES (?, ?, 'src', ?, NULL)",
            (f"2026-06-{i + 1:02d}T08:00:00Z", f"jr{i}",
             "journal_entries" if i < 3 else "calendar_events"),
        )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture()
def app_ctx(populated_conn, monkeypatch):
    import topos.core.state as state_mod

    monkeypatch.setattr(state_mod, "get_db_connection", lambda: populated_conn)
    from topos.app import app
    from topos.auth import require_api_key

    async def _fake_key():
        return "test-key"

    app.dependency_overrides[require_api_key] = _fake_key
    yield app
    app.dependency_overrides.pop(require_api_key, None)


async def _get(app, path: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers={"Authorization": "Bearer test-key"})


@pytest.mark.asyncio
async def test_facts_list_active_with_subject_names(app_ctx) -> None:
    resp = await _get(app_ctx, "/v1/signal/facts")
    assert resp.status_code == 200
    body = resp.json()
    rendered = {i["rendered"] for i in body["items"]}
    assert any("you works at Topos" in r for r in rendered)
    assert any("worked at Dialogues (2021–2024)" in r for r in rendered)
    assert all(i["is_active"] for i in body["items"])
    assert body["predicate_counts"]["works_at"] == 1
    # journal-derived habit fact is owner_only
    training = [i for i in body["items"] if i["predicate"] == "training_for"]
    assert training and training[0]["disclosure"] == "owner_only"


@pytest.mark.asyncio
async def test_facts_predicate_filter_and_history(app_ctx, populated_conn) -> None:
    resp = await _get(app_ctx, "/v1/signal/facts?predicate=works_at")
    assert [i["object_value"] for i in resp.json()["items"]] == ["Topos"]

    # supersede works_at, then history should include the closed row
    extract_facts_from_batch(populated_conn, [
        {"_table": "profile_records", "record_id": "p3", "source_id": "src",
         "record_type": "experience", "title": "Principal Engineer",
         "organization": "NewCo", "description": "Lead platform."},
    ])
    resp = await _get(app_ctx, "/v1/signal/facts?predicate=works_at&include_closed=true")
    values = {(i["object_value"], i["is_active"]) for i in resp.json()["items"]}
    assert ("NewCo", True) in values and ("Topos", False) in values


@pytest.mark.asyncio
async def test_insights_list_with_dimension_counts(app_ctx) -> None:
    resp = await _get(app_ctx, "/v1/signal/insights")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] > 0
    assert any("exercise" in i["text"] for i in body["items"])
    assert all(i["disclosure"] == "owner_only" for i in body["items"])
    assert body["dimension_counts"].get("wellbeing", 0) >= 1
    # each insight carries the exclusion handle (stat_id + group_key)
    assert all("stat_id" in i and "group_key" in i for i in body["items"])


@pytest.mark.asyncio
async def test_timeline_paged_filtered(app_ctx) -> None:
    resp = await _get(app_ctx, "/v1/signal/timeline?limit=2")
    body = resp.json()
    assert body["total"] == 5 and len(body["items"]) == 2
    assert body["items"][0]["event_at"] > body["items"][1]["event_at"]  # newest first
    assert body["table_counts"] == {"journal_entries": 3, "calendar_events": 2}

    resp = await _get(app_ctx, "/v1/signal/timeline?canonical_table=calendar_events")
    assert resp.json()["total"] == 2

    resp = await _get(app_ctx, "/v1/signal/timeline?date_from=2026-06-04&date_to=2026-06-05")
    assert resp.json()["total"] == 2


@pytest.mark.asyncio
async def test_reads_require_auth(populated_conn, monkeypatch) -> None:
    import topos.core.state as state_mod

    monkeypatch.setattr(state_mod, "get_db_connection", lambda: populated_conn)
    from topos.app import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/v1/signal/facts", "/v1/signal/insights", "/v1/signal/timeline"):
            resp = await client.get(path)
            assert resp.status_code == 401, path

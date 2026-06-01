"""Filter Lab schema, bundles, and HTTP surface (in-memory DB)."""

import sqlite3
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from topos.config.settings import settings
from topos.config.sanitization_ollama import SANITIZATION_OLLAMA_TRANSFORM_IDS
from topos.filter_lab import bundles as bundles_mod
from topos.filter_lab import store
from topos.filter_lab.schema import ensure_filter_lab_schema


@pytest.fixture()
def memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE engine_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    ensure_filter_lab_schema(conn)
    return conn


def test_ensure_filter_lab_schema_creates_tables(memory_conn: sqlite3.Connection) -> None:
    cur = memory_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'filter_lab%'"
    )
    names = {r[0] for r in cur.fetchall()}
    assert "filter_lab_job_group" in names
    assert "filter_lab_run" in names
    assert "filter_lab_model_event" in names


def test_list_bundle_metadata() -> None:
    meta = bundles_mod.list_bundle_metadata()
    assert len(meta) >= 4
    ids = {m["id"] for m in meta}
    assert "lab.messages.casual" in ids
    assert "lab.nsfw.synthetic" in ids


def test_bundle_filter_fit_covers_all_ollama_transforms() -> None:
    expected = set(SANITIZATION_OLLAMA_TRANSFORM_IDS)
    for m in bundles_mod.list_bundle_metadata():
        ff = m.get("filter_fit") or {}
        assert set(ff.keys()) == expected, m["id"]
        for v in ff.values():
            assert v in (
                bundles_mod.FIT_RECOMMENDED,
                bundles_mod.FIT_SUPPORTED,
                bundles_mod.FIT_STRESS,
            )


def test_insert_group_and_list_runs(memory_conn: sqlite3.Connection) -> None:
    gid = store.insert_group(
        memory_conn,
        filter_id="pii_redaction",
        bundle_id="lab.messages.casual",
        bundle_version="2",
        baseline_models=["llama3.2"],
        models=["llama3.2"],
        record_ids=[f"m{i}" for i in range(1, 11)],
        options={},
    )
    runs = store.list_runs(memory_conn, gid)
    assert len(runs) == 10  # 1 model × 10 records


def test_get_bundle_preview() -> None:
    d = bundles_mod.get_bundle_preview("lab.messages.casual")
    assert d is not None
    assert d["id"] == "lab.messages.casual"
    assert len(d["records"]) >= 1
    assert all("id" in r and "body" in r for r in d["records"])
    assert bundles_mod.get_bundle_preview("no.such.bundle") is None


def test_filter_lab_bundles_http(memory_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from topos.app import app
    import topos.core.state as st
    from topos.filter_lab import service as lab_service

    monkeypatch.setattr(st, "db_conn", memory_conn, raising=False)
    monkeypatch.setattr(st, "_db_conn_path", None, raising=False)
    monkeypatch.setattr(st, "get_db_connection", lambda: memory_conn, raising=False)
    monkeypatch.setattr(lab_service, "get_db_connection", lambda: memory_conn, raising=False)

    client = TestClient(app)
    res = client.get("/v1/filter-lab/bundles", headers={"Authorization": f"Bearer {settings.topos_key}"})
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert any(b.get("id") == "lab.messages.casual" for b in data)


def test_list_all_job_groups(memory_conn: sqlite3.Connection) -> None:
    gid_pii = store.insert_group(
        memory_conn,
        filter_id="pii_redaction",
        bundle_id="lab.messages.casual",
        bundle_version="2",
        baseline_models=["llama3.2"],
        models=["llama3.2"],
        record_ids=["m1"],
        options={},
    )
    gid_nsfw = store.insert_group(
        memory_conn,
        filter_id="nsfw_sanitization",
        bundle_id="lab.messages.casual",
        bundle_version="2",
        baseline_models=["llama3.2"],
        models=["llama3.2"],
        record_ids=["m1"],
        options={},
    )
    all_rows = store.list_all_job_groups(memory_conn, limit=20, offset=0)
    ids = {dict(r)["id"] for r in all_rows}
    assert gid_pii in ids and gid_nsfw in ids
    pii_only = store.list_groups_for_filter(memory_conn, "pii_redaction", limit=20, offset=0)
    assert len(pii_only) >= 1
    assert all(dict(r)["filter_id"] == "pii_redaction" for r in pii_only)


def test_filter_lab_list_job_groups_all_http(memory_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from topos.app import app
    import topos.core.state as st
    from topos.filter_lab import service as lab_service

    monkeypatch.setattr(st, "db_conn", memory_conn, raising=False)
    monkeypatch.setattr(st, "_db_conn_path", None, raising=False)
    monkeypatch.setattr(st, "get_db_connection", lambda: memory_conn, raising=False)
    monkeypatch.setattr(lab_service, "get_db_connection", lambda: memory_conn, raising=False)
    store.insert_group(
        memory_conn,
        filter_id="pii_redaction",
        bundle_id="lab.messages.casual",
        bundle_version="2",
        baseline_models=["llama3.2"],
        models=["llama3.2"],
        record_ids=["m1"],
        options={},
    )
    store.insert_group(
        memory_conn,
        filter_id="nsfw_sanitization",
        bundle_id="lab.messages.casual",
        bundle_version="2",
        baseline_models=["llama3.2"],
        models=["llama3.2"],
        record_ids=["m1"],
        options={},
    )
    client = TestClient(app)
    res = client.get(
        "/v1/filter-lab/job-groups?limit=50&offset=0",
        headers={"Authorization": f"Bearer {settings.topos_key}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert "groups" in body
    fids = {g.get("filter_id") for g in body["groups"]}
    assert "pii_redaction" in fids
    assert "nsfw_sanitization" in fids


def test_filter_lab_get_bundle_http(memory_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from topos.app import app
    import topos.core.state as st
    from topos.filter_lab import service as lab_service

    monkeypatch.setattr(st, "db_conn", memory_conn, raising=False)
    monkeypatch.setattr(st, "_db_conn_path", None, raising=False)
    monkeypatch.setattr(st, "get_db_connection", lambda: memory_conn, raising=False)
    monkeypatch.setattr(lab_service, "get_db_connection", lambda: memory_conn, raising=False)

    client = TestClient(app)
    res = client.get(
        "/v1/filter-lab/bundles/lab.messages.casual",
        headers={"Authorization": f"Bearer {settings.topos_key}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body.get("id") == "lab.messages.casual"
    assert isinstance(body.get("records"), list)
    assert len(body["records"]) >= 1
    res404 = client.get(
        "/v1/filter-lab/bundles/does.not.exist",
        headers={"Authorization": f"Bearer {settings.topos_key}"},
    )
    assert res404.status_code == 404


def test_filter_lab_create_job_skips_worker(memory_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from topos.app import app
    import topos.core.state as st
    from topos.filter_lab import service as lab_service

    monkeypatch.setattr(st, "db_conn", memory_conn, raising=False)
    monkeypatch.setattr(st, "_db_conn_path", None, raising=False)
    monkeypatch.setattr(st, "get_db_connection", lambda: memory_conn, raising=False)
    monkeypatch.setattr(lab_service, "get_db_connection", lambda: memory_conn, raising=False)

    class _FakeOllamaAdapter:
        def __init__(self, *a, **k):
            pass

        def list_models(self):
            return ["llama3.2"]

    with patch("topos.filter_lab.service.schedule_process_job_group", lambda _gid: None):
        with patch("topos.filter_lab.service.OllamaAdapter", _FakeOllamaAdapter):
            client = TestClient(app)
            res = client.post(
                "/v1/filter-lab/job-groups",
                headers={"Authorization": f"Bearer {settings.topos_key}"},
                json={
                    "filter_id": "pii_redaction",
                    "bundle_id": "lab.messages.casual",
                    "models": ["llama3.2"],
                },
            )
    assert res.status_code == 200
    body = res.json()
    assert "group" in body and "runs" in body
    assert body["group"]["filter_id"] == "pii_redaction"
    assert len(body["runs"]) == 10


def test_filter_lab_delete_job_group_removes_row(memory_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from topos.app import app
    import topos.core.state as st
    from topos.filter_lab import service as lab_service

    monkeypatch.setattr(st, "db_conn", memory_conn, raising=False)
    monkeypatch.setattr(st, "_db_conn_path", None, raising=False)
    monkeypatch.setattr(st, "get_db_connection", lambda: memory_conn, raising=False)
    monkeypatch.setattr(lab_service, "get_db_connection", lambda: memory_conn, raising=False)

    class _FakeOllamaAdapter:
        def __init__(self, *a, **k):
            pass

        def list_models(self):
            return ["llama3.2"]

    with patch("topos.filter_lab.service.schedule_process_job_group", lambda _gid: None):
        with patch("topos.filter_lab.service.OllamaAdapter", _FakeOllamaAdapter):
            client = TestClient(app)
            res = client.post(
                "/v1/filter-lab/job-groups",
                headers={"Authorization": f"Bearer {settings.topos_key}"},
                json={
                    "filter_id": "pii_redaction",
                    "bundle_id": "lab.messages.casual",
                    "models": ["llama3.2"],
                },
            )
    assert res.status_code == 200
    gid = str(res.json()["group"]["id"])
    res_del = client.delete(
        f"/v1/filter-lab/job-groups/{gid}",
        headers={"Authorization": f"Bearer {settings.topos_key}"},
    )
    assert res_del.status_code == 200
    assert res_del.json().get("deleted") is True
    res_get = client.get(
        f"/v1/filter-lab/job-groups/{gid}",
        headers={"Authorization": f"Bearer {settings.topos_key}"},
    )
    assert res_get.status_code == 404


def test_history_summaries_for_group_ids(memory_conn: sqlite3.Connection) -> None:
    gid = store.insert_group(
        memory_conn,
        filter_id="pii_redaction",
        bundle_id="lab.messages.casual",
        bundle_version="2",
        baseline_models=["llama3.2"],
        models=["llama3.2", "mistral"],
        record_ids=["m1", "m2"],
        options={},
    )
    runs = store.list_runs(memory_conn, gid)
    by_model: dict[str, str] = {}
    for r in runs:
        d = dict(r)
        by_model.setdefault(d["model_tag"], d["id"])
    store.update_run(memory_conn, by_model["llama3.2"], status="succeeded", latency_ms=100)
    store.patch_run(memory_conn, by_model["llama3.2"], user_liked=True, user_quality_score_0_10=8)
    store.update_run(memory_conn, by_model["mistral"], status="succeeded", latency_ms=300)
    store.patch_run(memory_conn, by_model["mistral"], user_quality_score_0_10=6)
    for r in runs:
        rid = dict(r)["id"]
        if rid in (by_model["llama3.2"], by_model["mistral"]):
            continue
        store.update_run(memory_conn, rid, status="succeeded", latency_ms=200)
    summ = store.history_summaries_for_group_ids(memory_conn, [gid])[gid]
    assert summ["any_liked"] is True
    assert summ["avg_latency_ms"] == 200  # mean of 4 latencies: 100,300,200,200
    assert "llama3.2" in summ["models"] and "mistral" in summ["models"]
    assert summ["rating_text"] is not None
    assert "8/10" in summ["rating_text"] and "6/10" in summ["rating_text"]


def test_enrich_job_groups_list_with_run_summaries(memory_conn: sqlite3.Connection) -> None:
    from topos.filter_lab import service as lab_service

    gid = store.insert_group(
        memory_conn,
        filter_id="pii_redaction",
        bundle_id="lab.messages.casual",
        bundle_version="2",
        baseline_models=["llama3.2"],
        models=["tinyllama"],
        record_ids=["m1"],
        options={},
    )
    rid = dict(store.list_runs(memory_conn, gid)[0])["id"]
    store.update_run(memory_conn, rid, status="succeeded", latency_ms=500)
    store.patch_run(memory_conn, rid, user_liked=True, user_quality_score_0_10=9)
    row = store.get_group(memory_conn, gid)
    assert row is not None
    g = dict(row)
    g["baseline_models"] = []
    g["pulled_models"] = []
    g["options"] = {}
    lab_service.enrich_job_groups_list_with_run_summaries(memory_conn, [g])
    assert g["history_summary"]["models"] == "tinyllama"
    assert g["history_summary"]["avg_latency_ms"] == 500
    assert g["history_summary"]["any_liked"] is True
    assert g["history_summary"]["rating_text"] == "9/10"


def test_filter_lab_auth_required() -> None:
    from topos.app import app

    client = TestClient(app)
    res = client.get("/v1/filter-lab/bundles")
    assert res.status_code == 401

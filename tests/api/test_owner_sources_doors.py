"""The owner-socket doors for the AI-coding-tool sources (CONTRACTS.md §2.8).

``GET /api/local/owner/scope`` answers the bound identity; ``preview`` is counts only and writes
nothing; ``enable`` creates the canonical install row, the settings row, an hourly schedule and
the backfill job; ``pause`` makes the sync door and the scheduler refuse; ``resume`` lifts it;
``sync`` refuses unless enabled and never takes a dataset from the caller. Every door is socket
only. Synthetic databases and invented transcripts; nothing under ``~`` is read.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.ingestion.ai_tool_fixtures import DATASET, OWNER, TOPOS, FakeReader, arm, build_node, record
from topos.uds import UDSChannelApp

SOURCE = "claude_code_sessions"
BASE = "/api/local/owner"


@pytest.fixture
def node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn = build_node(tmp_path / "doors.db")
    reader = arm(monkeypatch, conn, reader=FakeReader([
        record("claude_code", "p1", "sess-a", "user", "make the lantern glow", project_key="lantern"),
        record("claude_code", "p2", "sess-a", "assistant", "wiring it", when="2026-10-14T10:00:01.000Z", project_key="lantern"),
        record("claude_code", "p3", "sess-b", "user", "second session", when="2026-10-13T09:00:00.000Z", project_key="beacon"),
    ]))
    monkeypatch.setattr("topos.pipeline.job_runner.start_pipeline_worker", lambda conn_factory: None)
    from topos.api.owner_sources import router

    app = FastAPI()
    app.include_router(router)
    yield app, conn, reader
    conn.close()


def _client(app):
    return TestClient(UDSChannelApp(app))


# --- socket only ---------------------------------------------------------------


_DOORS = (
    ("/scope", "get"),
    (f"/sources/{SOURCE}", "get"),
    (f"/sources/{SOURCE}/preview", "post"),
    (f"/sources/{SOURCE}/enable", "post"),
    (f"/sources/{SOURCE}/pause", "post"),
    (f"/sources/{SOURCE}/resume", "post"),
    (f"/sources/{SOURCE}/sync", "post"),
    (f"/sources/{SOURCE}/reclassify-touches", "post"),
)


def test_every_door_refuses_every_principal_but_the_owner_on_the_socket(node) -> None:
    """A bare TCP request has no owner; a TCP bearer, a relay stamp and a third party on the socket all get 403."""
    from topos.auth import resolve_request_principal
    from topos.principal import OWNER_APP, THIRD_PARTY, Principal

    app, conn, _reader = node
    with TestClient(app) as client:
        for path, method in _DOORS:
            response = getattr(client, method)(BASE + path, headers={"X-Transport": "uds"})
            assert response.status_code == 401, (path, response.status_code)
    for principal in (None, Principal(THIRD_PARTY, "uds"), Principal(OWNER_APP, "cp_relay", acting_user="o"),
                      Principal(OWNER_APP, "local_http", acting_user="o")):
        app.dependency_overrides[resolve_request_principal] = lambda p=principal: p
        try:
            with TestClient(app) as client:
                for path, method in _DOORS:
                    response = client.post(BASE + path, json={}) if method == "post" else client.get(BASE + path)
                    assert response.status_code == 403 and response.json()["detail"] == "owner_socket_required", path
        finally:
            app.dependency_overrides.pop(resolve_request_principal, None)
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM user_ingestion_sources").fetchone()[0] == 0


# --- scope ---------------------------------------------------------------------


def test_scope_answers_the_bound_identity(node) -> None:
    app, _conn, _reader = node
    with _client(app) as client:
        response = client.get(BASE + "/scope")
    assert response.status_code == 200
    assert response.json() == {"user_id": OWNER, "topos_id": TOPOS, "dataset_id": DATASET, "identity_source": "stored"}


def test_scope_is_409_not_paired_without_an_identity(tmp_path, monkeypatch) -> None:
    conn = build_node(tmp_path / "unpaired.db", topos_id=None)
    arm(monkeypatch, conn)
    from topos.api.owner_sources import router

    app = FastAPI()
    app.include_router(router)
    with _client(app) as client:
        response = client.get(BASE + "/scope")
    assert response.status_code == 409 and response.json()["error"] == "not_paired"


# --- preview -------------------------------------------------------------------


def test_preview_is_counts_only_and_writes_nothing(node) -> None:
    app, conn, reader = node
    before = conn.execute("SELECT COUNT(*) FROM ai_chat_messages").fetchone()[0]
    with _client(app) as client:
        response = client.post(BASE + f"/sources/{SOURCE}/preview", json={"skip": ["scratch"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sessions"] == 2 and body["turns"] == 3
    assert body["bytes_text"] == len("make the lantern glow") + len("wiring it") + len("second session")
    assert body["folders"] == ["beacon", "lantern"]
    assert body["oldest"] == "2026-10-13T09:00:00.000Z" and body["newest"] == "2026-10-14T10:00:01.000Z"
    assert reader.calls[0]["skip_folders"] == ("scratch",)
    assert "make the lantern" not in response.text and "wiring" not in response.text
    assert conn.execute("SELECT COUNT(*) FROM ai_chat_messages").fetchone()[0] == before
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='ingestion_checkpoints'").fetchone():
        assert conn.execute("SELECT COUNT(*) FROM ingestion_checkpoints").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'raw_chat_messages_claude%'").fetchall() == []


def test_preview_and_the_doors_refuse_an_unknown_or_non_ai_source(node) -> None:
    app, _conn, _reader = node
    with _client(app) as client:
        assert client.post(BASE + "/sources/imessage/preview", json={}).status_code == 404
        assert client.post(BASE + "/sources/nonsense/enable", json={}).status_code == 404


# --- enable --------------------------------------------------------------------


def test_enable_creates_the_install_row_the_settings_the_hourly_schedule_and_the_backfill(node) -> None:
    from topos.ingestion.local_sync_schedule import get_schedule
    from topos.pipeline.job_store import get_job
    from topos.sources.install_service import list_installs

    app, conn, _reader = node
    with _client(app) as client:
        response = client.post(BASE + f"/sources/{SOURCE}/enable", json={"skip": ["secret-client"], "backfill": "all"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dataset_id"] == DATASET and body["enabled"] is True and body["paused"] is False

    installs = list_installs(scope={"user_id": OWNER, "device_id": "*", "topos_id": TOPOS, "dataset_id": DATASET}, source_id=SOURCE)
    assert len(installs) == 1 and installs[0].is_active
    definition = installs[0].source_definition_json
    definition = json.loads(definition) if isinstance(definition, str) else definition
    assert definition["canonical_group_id"] == "ai_messages"

    row = conn.execute("SELECT enabled FROM user_ingestion_sources WHERE dataset_id=? AND source_id=?", (DATASET, SOURCE)).fetchone()
    assert row and row[0] == 1

    schedule = get_schedule(conn, DATASET, SOURCE)
    assert schedule["enabled"] is True and schedule["interval_hours"] == 1

    job = get_job(conn, body["backfill_job_id"])
    assert job["payload"]["source_id"] == SOURCE and job["payload"]["dataset_id"] == DATASET
    assert job["payload"]["sync_options"]["mode"] == "full_history"
    assert job["payload"]["sync_options"]["trigger"] == "backfill"


def test_enable_with_a_30d_backfill_asks_for_the_last_thirty_days(node) -> None:
    from topos.pipeline.job_store import get_job

    app, conn, _reader = node
    with _client(app) as client:
        response = client.post(BASE + f"/sources/{SOURCE}/enable", json={"backfill": "30d"})
    assert response.status_code == 200, response.text
    options = get_job(conn, response.json()["backfill_job_id"])["payload"]["sync_options"]
    assert options["mode"] == "custom"
    start = datetime.fromisoformat(options["start_date"]).replace(tzinfo=timezone.utc)
    assert 29 <= (datetime.now(timezone.utc) - start).days <= 31


def test_enable_refuses_below_the_disk_floor_with_the_plain_sentence(node, monkeypatch) -> None:
    import topos.engine.disk_space as disk

    app, conn, _reader = node
    monkeypatch.setattr(disk, "free_bytes", lambda path=None: 2_000_000_000)
    monkeypatch.setattr(disk, "min_free_bytes", lambda conn=None: 10_000_000_000)
    with _client(app) as client:
        response = client.post(BASE + f"/sources/{SOURCE}/enable", json={"backfill": "all"})
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "disk_floor"
    assert response.json()["message"] == (
        "There isn't enough free disk space to bring in your coding sessions: 2.0 GB is free and Topos keeps "
        "10.0 GB free. Free up space, then try again."
    )
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM user_ingestion_sources").fetchone()[0] == 0


def test_enable_is_idempotent(node) -> None:
    from topos.sources.install_service import list_installs

    app, conn, _reader = node
    with _client(app) as client:
        first = client.post(BASE + f"/sources/{SOURCE}/enable", json={"backfill": "all"})
        second = client.post(BASE + f"/sources/{SOURCE}/enable", json={"backfill": "all"})
    assert first.status_code == 200 and second.status_code == 200
    active = [i for i in list_installs(source_id=SOURCE) if i.is_active]
    assert len(active) == 1


# --- pause / resume / sync -----------------------------------------------------


def test_sync_refuses_unless_the_source_is_enabled(node) -> None:
    app, conn, _reader = node
    with _client(app) as client:
        response = client.post(BASE + f"/sources/{SOURCE}/sync", json={})
    assert response.status_code == 409 and response.json()["error"] == "source_not_enabled"
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 0


def test_pause_refuses_the_sync_door_and_the_scheduler_and_resume_lifts_it(node) -> None:
    from topos.ingestion.local_sync_schedule import get_schedule, run_schedule_tick
    from topos.pipeline.job_store import get_job

    app, conn, _reader = node
    with _client(app) as client:
        assert client.post(BASE + f"/sources/{SOURCE}/enable", json={"backfill": "all"}).status_code == 200
        # The backfill job is the only job so far; drain it so the sync door would otherwise enqueue.
        conn.execute("UPDATE pipeline_jobs SET status='done'")
        conn.commit()
        paused = client.post(BASE + f"/sources/{SOURCE}/pause")
        assert paused.status_code == 200 and paused.json()["paused"] is True
        refused = client.post(BASE + f"/sources/{SOURCE}/sync", json={"session_path": "/invented/s.jsonl"})
        assert refused.status_code == 409 and refused.json()["error"] == "source_paused", refused.text
    jobs_before = conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0]

    # The scheduler: force the slot due and tick.
    conn.execute("UPDATE local_sync_schedules SET last_slot_at = NULL, last_status = NULL, last_job_id = NULL WHERE source_id=?", (SOURCE,))
    conn.commit()
    summary = run_schedule_tick(conn, now=datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc))
    assert summary["failed"] == 1 and summary["enqueued"] == 0
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == jobs_before
    stored = get_schedule(conn, DATASET, SOURCE)
    assert stored["last_status"] == "failed" and "paused" in (stored["last_error"] or "")

    with _client(app) as client:
        resumed = client.post(BASE + f"/sources/{SOURCE}/resume")
        assert resumed.status_code == 200 and resumed.json()["paused"] is False
        ok = client.post(BASE + f"/sources/{SOURCE}/sync", json={"session_path": "/invented/s.jsonl"})
    assert ok.status_code == 200, ok.text
    job = get_job(conn, ok.json()["job_id"])
    assert job["payload"]["dataset_id"] == DATASET
    assert job["payload"]["sync_options"]["session_path"] == "/invented/s.jsonl"
    assert job["payload"]["sync_options"]["mode"] == "since_last"


def test_sync_ignores_a_dataset_the_caller_sends(node) -> None:
    from topos.pipeline.job_store import get_job

    app, conn, _reader = node
    with _client(app) as client:
        assert client.post(BASE + f"/sources/{SOURCE}/enable", json={"backfill": "all"}).status_code == 200
        conn.execute("UPDATE pipeline_jobs SET status='done'")
        conn.commit()
        response = client.post(BASE + f"/sources/{SOURCE}/sync", json={"dataset_id": "attacker:topos:elsewhere"})
    assert response.status_code == 200, response.text
    job = get_job(conn, response.json()["job_id"])
    assert job["payload"]["dataset_id"] == DATASET
    assert "attacker" not in json.dumps(job["payload"])


def test_the_doors_answer_status_the_cli_can_show(node) -> None:
    app, _conn, _reader = node
    with _client(app) as client:
        assert client.post(BASE + f"/sources/{SOURCE}/enable", json={"backfill": "all"}).status_code == 200
        status = client.get(BASE + f"/sources/{SOURCE}")
    assert status.status_code == 200
    body = status.json()
    assert body["source_id"] == SOURCE and body["enabled"] is True and body["paused"] is False
    assert body["schedule"]["interval_hours"] == 1
    assert "last_sync_at" in body and "last_error" in body
    assert body["secret_sightings"] == 0 and body["last_sync_ledger"] == {}


# --- hardening before the first sync (L7) -------------------------------------------


def test_enable_hardens_the_topos_dir_before_the_backfill_is_queued(node, monkeypatch) -> None:
    import topos.ingestion.ai_tool_sync as lane

    app, conn, _reader = node
    seen: list = []

    def fake_harden():
        # Nothing may be queued yet when the clamp runs: it comes before the first sync.
        seen.append(conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0])
        return {"dir": "~/.topos", "changed": 2, "already_clamped": 5, "changed_paths": ["database.db", "logs"],
                "skipped": [], "owner_key_persisted": True}

    monkeypatch.setattr(lane, "_harden_topos_dir", fake_harden)
    monkeypatch.setattr(lane, "_owner_key_persisted", lambda: True)
    with _client(app) as client:
        response = client.post(BASE + f"/sources/{SOURCE}/enable", json={"backfill": "all"})
    assert response.status_code == 200, response.text
    assert seen == [0]
    assert response.json()["hardening"] == {"changed": 2, "already_clamped": 5, "skipped": [], "owner_key_persisted": True}
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 1


def test_enable_refuses_409_when_the_owner_key_is_not_persisted(node, monkeypatch) -> None:
    """A node whose ~/.topos was unwritable at boot is in legacy mode (DIGEST J6): no shadowing until fixed."""
    import topos.ingestion.ai_tool_sync as lane

    app, conn, _reader = node
    monkeypatch.setattr(lane, "_harden_topos_dir", lambda: {"changed": 0, "owner_key_persisted": False})
    monkeypatch.setattr(lane, "_owner_key_persisted", lambda: False)
    with _client(app) as client:
        response = client.post(BASE + f"/sources/{SOURCE}/enable", json={"backfill": "all"})
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "owner_key_not_persisted"
    assert "owner key" in response.json()["message"]
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM user_ingestion_sources").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='source_runtime_installs'").fetchone() is None


def test_enable_clamps_the_modes_of_a_real_topos_dir(node, monkeypatch, tmp_path) -> None:
    """With L7's module present: a tmp HOME's ~/.topos ends 0700 and its database 0600 after enable."""
    pytest.importorskip("topos.upgrades.hardening")
    import stat

    app, conn, _reader = node
    home = tmp_path / "home"
    topos_dir = home / ".topos"
    topos_dir.mkdir(parents=True)
    (topos_dir / "database.db").write_bytes(b"")
    (topos_dir / ".env").write_text("TOPOS_OWNER_KEY=invented-owner-key-for-this-test\n")
    topos_dir.chmod(0o755)
    (topos_dir / "database.db").chmod(0o644)
    monkeypatch.setenv("HOME", str(home))
    with _client(app) as client:
        response = client.post(BASE + f"/sources/{SOURCE}/enable", json={"backfill": "all"})
    assert response.status_code == 200, response.text
    assert stat.S_IMODE(topos_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((topos_dir / "database.db").stat().st_mode) == 0o600
    assert response.json()["hardening"]["owner_key_persisted"] is True


def test_a_build_without_the_hardening_module_still_enables_and_says_so(node, monkeypatch, caplog) -> None:
    import topos.ingestion.ai_tool_sync as lane

    app, conn, _reader = node
    monkeypatch.setattr(lane, "_harden_topos_dir", lambda: None)
    monkeypatch.setattr(lane, "_owner_key_persisted", lambda: None)
    with _client(app) as client:
        response = client.post(BASE + f"/sources/{SOURCE}/enable", json={"backfill": "all"})
    assert response.status_code == 200, response.text
    assert response.json()["hardening"] is None


# --- the owner's coding agent ----------------------------------------------------


def test_an_enrolled_agent_on_the_socket_reads_the_scope_and_the_status_but_changes_nothing(node) -> None:
    """CONTRACTS §1.2 / owner_door_routes.OWNER_AGENT_ROUTES let an enrolled agent GET /scope (the MCP server's
    session context and sources_status call it with the agent's label); the mutations stay the owner's."""
    from topos.auth import resolve_request_principal
    from topos.principal import OWNER_AGENT, Principal

    app, conn, _reader = node
    app.dependency_overrides[resolve_request_principal] = lambda: Principal(OWNER_AGENT, "uds", client_id="agent-claude-code")
    try:
        with TestClient(app) as client:
            scope = client.get(BASE + "/scope")
            assert scope.status_code == 200, scope.text
            assert scope.json()["topos_id"] == TOPOS
            status = client.get(BASE + f"/sources/{SOURCE}")
            assert status.status_code == 200, status.text
            for path in (f"/sources/{SOURCE}/preview", f"/sources/{SOURCE}/enable", f"/sources/{SOURCE}/pause",
                         f"/sources/{SOURCE}/resume", f"/sources/{SOURCE}/sync",
                         f"/sources/{SOURCE}/reclassify-touches"):
                response = client.post(BASE + path, json={})
                assert response.status_code == 403 and response.json()["detail"] == "owner_socket_required", path
        # the class is minted on the socket only: the same word on another channel is nobody
        app.dependency_overrides[resolve_request_principal] = lambda: Principal(OWNER_AGENT, "local_http", client_id="agent-claude-code")
        with TestClient(app) as client:
            assert client.get(BASE + "/scope").status_code == 403
    finally:
        app.dependency_overrides.pop(resolve_request_principal, None)
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 0


def test_reclassify_touches_is_a_counts_only_dry_run_unless_the_owner_applies(node, monkeypatch, tmp_path) -> None:
    """Owner ruling 20 Oct 2026: re-judging stored replies under the read / system-touch rule. Nothing under ``~``
    is read: the transcript root points at an empty scratch folder."""
    app, conn, _reader = node
    monkeypatch.setattr("topos.ingestion.sources.claude_code_reader.DEFAULT_ROOT", tmp_path / "no-transcripts")
    with _client(app) as client:
        dry = client.post(BASE + f"/sources/{SOURCE}/reclassify-touches", json={})
        assert dry.status_code == 200, dry.text
        assert dry.json()["applied"] is False and dry.json()["would_clear"] == 0
        assert dry.headers["cache-control"] == "no-store"
        # Applying widens what can be shared: refused at the socket without the app or a human confirmation,
        # and so is any body that is not plainly a dry run.
        for body in ({"apply": True}, {"apply": "yes"}, {"root": "/etc"}):
            refused = client.post(BASE + f"/sources/{SOURCE}/reclassify-touches", json=body)
            assert refused.status_code == 403 and refused.json()["error"] == "owner_confirmation_required", body
        from topos.uds import write_owner_confirmation

        bad = client.post(BASE + f"/sources/{SOURCE}/reclassify-touches", json={"apply": "yes"},
                          headers={"X-Topos-Owner-Confirm": write_owner_confirmation()})
        assert bad.status_code == 400
        applied = client.post(BASE + f"/sources/{SOURCE}/reclassify-touches", json={"apply": True},
                              headers={"X-Topos-Owner-Confirm": write_owner_confirmation()})
        assert applied.status_code == 200, applied.text
        assert applied.json()["applied"] is True and applied.json()["cleared"] == 0

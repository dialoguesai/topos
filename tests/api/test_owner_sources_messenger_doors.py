"""The owner-socket doors for iMessage, Signal Desktop and the extensions' sources, by source id.

protects: a node with no account had no way to turn on Messages or Signal (the web app does it through
the control plane), and ``topos-node sources enable imessage`` answered 404. The doors now serve them,
and what these guards hold is the part that must not drift:

* counting a messenger source, turning it on or back on, or syncing it needs the Topos app or a
  confirmation from a person on a terminal — a coding agent with a shell gets 403, the store is not
  opened and nothing is written;
* a source this process cannot read (no Full Disk Access, no SQLCipher binding, a Keychain request not
  allowed) is refused BEFORE an install row, a settings row, a schedule or a job exists, with a code;
* every answer is counts, flags, codes and stamps: no path and no message text.

Synthetic databases and invented counts. The readers are stood in for at their counting functions, and
the two store paths point into ``tmp_path``; nothing under ``~`` is opened.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.ingestion.ai_tool_fixtures import DATASET, OWNER, TOPOS, arm, build_node
from topos.uds import UDSChannelApp, write_owner_confirmation

pytestmark = pytest.mark.public

BASE = "/api/local/owner"
NOW = 1_794_000_000.0  # an invented clock, well after the invented messages
DAY = 86_400.0


def _confirmed() -> dict:
    return {"X-Topos-Owner-Confirm": write_owner_confirmation()}


def _client(app):
    return TestClient(UDSChannelApp(app))


class Stores:
    """The two message stores on this invented computer, and what a count of each would say."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.chat_db = tmp_path / "Messages" / "chat.db"
        self.chat_db.parent.mkdir()
        self.chat_db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 16)
        self.signal_db = tmp_path / "Signal" / "sql" / "db.sqlite"
        self.signal_db.parent.mkdir(parents=True)
        self.signal_db.write_bytes(b"not a real database")
        self.binding = True
        self.signal_error: Exception | None = None
        self.imessage_calls: list = []
        self.signal_calls: list = []
        monkeypatch.setenv("IMESSAGE_CHAT_DB", str(self.chat_db))
        monkeypatch.setenv("SIGNAL_DB_PATH", str(self.signal_db))
        monkeypatch.setenv("SIGNAL_CONFIG_PATH", str(tmp_path / "Signal" / "config.json"))

        import topos.ingestion.messenger_setup as setup
        import topos.ingestion.sources.imessage_reader as imessage_reader
        import topos.ingestion.sources.signal_reader as signal_reader

        monkeypatch.setattr(setup, "_is_macos", lambda: True)
        monkeypatch.setattr(signal_reader, "sqlcipher_available", lambda: self.binding)

        def backlog(after_rowid, chat_db_path=None, exclude_spam=True):
            self.imessage_calls.append({"after_rowid": after_rowid, "exclude_spam": exclude_spam})
            # Three kept messages (one of them recent) and two the spam policy passes over.
            dated = ((1, NOW - 400 * DAY), (2, NOW - 90 * DAY), (3, NOW - 2 * DAY))
            return imessage_reader.ImessageBacklog(
                after_rowid=after_rowid, chat_db_max_rowid=5, messages=5, spam=2,
                first_at=dated[0][1], last_at=dated[-1][1], dated=dated,
            )

        def count(start_unix=None, config_path=None, db_path=None, signal_key_hex=None):
            self.signal_calls.append({"start_unix": start_unix})
            if self.signal_error is not None:
                raise self.signal_error
            return {"messages": 40, "conversations": 6, "in_window": 40 if start_unix is None else 7,
                    "oldest": NOW - 700 * DAY, "newest": NOW - DAY}

        monkeypatch.setattr(imessage_reader, "inspect_imessage_backlog", backlog)
        monkeypatch.setattr(signal_reader, "count_signal_messages", count)
        import topos.ingestion.messenger_setup as messenger_setup

        class _Clock(messenger_setup.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(NOW, tz=tz)

        monkeypatch.setattr(messenger_setup, "datetime", _Clock)


@pytest.fixture
def node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn = build_node(tmp_path / "doors.db")
    arm(monkeypatch, conn)
    stores = Stores(tmp_path, monkeypatch)
    monkeypatch.setattr("topos.pipeline.job_runner.start_pipeline_worker", lambda conn_factory: None)
    from topos.api.owner_sources import router

    app = FastAPI()
    app.include_router(router)
    yield app, conn, stores
    conn.close()


def _nothing_written(conn: sqlite3.Connection) -> None:
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM user_ingestion_sources").fetchone()[0] == 0
    from topos.sources.install_service import INSTALL_TABLE

    found = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (INSTALL_TABLE,)).fetchone()
    if found:
        assert conn.execute(f"SELECT COUNT(*) FROM {INSTALL_TABLE}").fetchone()[0] == 0


def _installs(source_id: str):
    from topos.sources.install_service import list_installs

    return list_installs(scope={"user_id": OWNER, "device_id": "*", "topos_id": TOPOS, "dataset_id": DATASET},
                         source_id=source_id)


# --- the list ------------------------------------------------------------------


def test_the_list_names_every_catalog_source_with_its_state(node) -> None:
    from topos.sources.setup_catalog import CATALOG

    app, _conn, stores = node
    stores.binding = False
    with _client(app) as client:
        response = client.get(BASE + "/sources")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    rows = {row["source_id"]: row for row in response.json()["sources"]}
    assert list(rows) == [entry.source_id for entry in CATALOG]
    assert rows["imessage"]["kind"] == "messages" and rows["imessage"]["enabled"] is False
    assert rows["imessage"]["ready"] is True and rows["imessage"]["needs"] is None
    assert rows["signal"]["ready"] is False and rows["signal"]["needs"] == "signal_reader_missing"
    assert rows["browser_visits"]["connected"] is False and rows["browser_visits"]["app"] == "browser-history-plugin"
    assert rows["claude_code_sessions"]["enabled"] is False
    # Listing never counts a store and never asks the Keychain for a key.
    assert stores.imessage_calls == [] and stores.signal_calls == []
    assert str(stores.chat_db.parent.parent) not in response.text


def test_the_list_and_the_messenger_status_are_the_owners_not_the_enrolled_agents(node) -> None:
    """The enrolled coding agent may read the status of its own transcript source (CONTRACTS §1.2). Which
    message stores exist on this computer, and whether an extension is connected, is not its business."""
    from topos.auth import resolve_request_principal
    from topos.principal import OWNER_AGENT, Principal

    app, _conn, _stores = node
    app.dependency_overrides[resolve_request_principal] = lambda: Principal(OWNER_AGENT, "uds", client_id="agent-codex")
    try:
        with TestClient(app) as client:
            assert client.get(BASE + "/sources/codex_sessions").status_code == 200
            for path in ("/sources", "/sources/imessage", "/sources/signal", "/sources/browser_visits"):
                response = client.get(BASE + path)
                assert response.status_code == 403 and response.json()["detail"] == "owner_socket_required", path
            for verb in ("preview", "enable", "pause", "resume", "sync"):
                response = client.post(BASE + f"/sources/imessage/{verb}", json={})
                assert response.status_code == 403, verb
    finally:
        app.dependency_overrides.pop(resolve_request_principal, None)


# --- preview -------------------------------------------------------------------


def test_preview_counts_messages_and_writes_nothing(node) -> None:
    app, conn, stores = node
    with _client(app) as client:
        for source_id in ("imessage", "signal"):
            unasked = client.post(BASE + f"/sources/{source_id}/preview", json={})
            assert unasked.status_code == 403 and unasked.json()["error"] == "owner_confirmation_required"
        assert stores.imessage_calls == [] and stores.signal_calls == [], "a refused count opens no store"
        imessage = client.post(BASE + "/sources/imessage/preview", json={}, headers=_confirmed())
        signal = client.post(BASE + "/sources/signal/preview", json={"backfill": "all"}, headers=_confirmed())
    assert imessage.status_code == 200, imessage.text
    assert imessage.json() == {
        "source_id": "imessage", "backfill": "30d", "messages": 3, "in_window": 1, "conversations": None,
        "skipped_spam": 2, "oldest": imessage.json()["oldest"], "newest": imessage.json()["newest"],
    }
    assert imessage.json()["oldest"] < imessage.json()["newest"]
    assert signal.status_code == 200, signal.text
    assert signal.json()["messages"] == 40 and signal.json()["in_window"] == 40 and signal.json()["conversations"] == 6
    assert stores.signal_calls == [{"start_unix": None}]
    _nothing_written(conn)


def test_preview_refuses_a_window_it_does_not_know(node) -> None:
    app, _conn, _stores = node
    with _client(app) as client:
        for body in ({"backfill": "90d"}, {"skip": ["x"]}):
            response = client.post(BASE + "/sources/imessage/preview", json=body, headers=_confirmed())
            assert response.status_code == 400, body


# --- enable --------------------------------------------------------------------


@pytest.mark.parametrize("source_id", ["imessage", "signal"])
def test_enable_without_the_app_or_a_confirmation_is_refused_and_writes_nothing(node, source_id: str) -> None:
    app, conn, stores = node
    with _client(app) as client:
        response = client.post(BASE + f"/sources/{source_id}/enable", json={"backfill": "all"})
    assert response.status_code == 403 and response.json()["error"] == "owner_confirmation_required"
    assert stores.signal_calls == [], "the Keychain is not asked for a key on a refused request"
    _nothing_written(conn)


def test_the_route_asks_for_the_confirmation_itself_when_the_socket_gate_is_switched_off(node, monkeypatch) -> None:
    """``TOPOS_OWNER_DOOR_SPLIT=0`` turns the socket layer's gate off. A messenger source must not open with
    it: the route checks the confirmed peer too, as the fact-pack backfill's start does."""
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "0")
    from topos import uds

    assert uds.owner_door_split_enabled() is False
    app, conn, stores = node
    with _client(app) as client:
        for verb in ("preview", "enable", "resume", "sync"):
            response = client.post(BASE + "/sources/imessage/" + verb, json={})
            assert response.status_code == 403 and response.json()["error"] == "owner_confirmation_required", verb
        assert stores.imessage_calls == []
        _nothing_written(conn)
        assert client.post(BASE + "/sources/imessage/enable", json={}, headers=_confirmed()).status_code == 200


def test_enable_imessage_installs_schedules_hourly_and_queues_the_first_sync(node) -> None:
    from topos.ingestion.local_sync_schedule import get_schedule
    from topos.pipeline.job_store import get_job

    app, conn, _stores = node
    with _client(app) as client:
        response = client.post(BASE + "/sources/imessage/enable", json={}, headers=_confirmed())
        status = client.get(BASE + "/sources/imessage")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True and body["dataset_id"] == DATASET and body["scheduled"] is True
    assert body["backfill"] == "30d"

    installs = _installs("imessage")
    assert len(installs) == 1 and installs[0].is_active
    with pytest.raises(AssertionError):
        _nothing_written(conn)  # the check the refusal tests rely on does see an enable's rows
    row = conn.execute("SELECT enabled FROM user_ingestion_sources WHERE dataset_id=? AND source_id='imessage'",
                       (DATASET,)).fetchone()
    assert row and row[0] == 1
    schedule = get_schedule(conn, DATASET, "imessage")
    assert schedule["enabled"] is True and schedule["interval_hours"] == 1
    job = get_job(conn, body["backfill_job_id"])
    assert job["payload"]["source_id"] == "imessage" and job["payload"]["dataset_id"] == DATASET
    assert job["payload"]["sync_options"] == {"mode": "1m"}

    assert status.status_code == 200
    assert status.json()["enabled"] is True and status.json()["readiness"]["ready"] is True
    assert status.json()["schedule"]["enabled"] is True


def test_enable_signal_reads_everything_once_and_is_not_scheduled(node) -> None:
    from topos.ingestion.local_sync_schedule import get_schedule
    from topos.pipeline.job_store import get_job

    app, conn, stores = node
    with _client(app) as client:
        response = client.post(BASE + "/sources/signal/enable", json={"backfill": "all"}, headers=_confirmed())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True and body["scheduled"] is False and body["schedule"] is None
    assert get_schedule(conn, DATASET, "signal") is None
    assert get_job(conn, body["backfill_job_id"])["payload"]["sync_options"] == {"mode": "all"}
    assert len(_installs("signal")) == 1
    # The database was opened once before anything was written: that is when macOS asks for the key.
    assert stores.signal_calls == [{"start_unix": None}]


def test_a_second_enable_keeps_the_install_and_joins_the_sync_already_queued(node) -> None:
    app, conn, _stores = node
    with _client(app) as client:
        first = client.post(BASE + "/sources/imessage/enable", json={}, headers=_confirmed()).json()
        second = client.post(BASE + "/sources/imessage/enable", json={}, headers=_confirmed()).json()
    assert second["install_id"] == first["install_id"]
    assert second["backfill_job_id"] == first["backfill_job_id"] and second["backfill_already_running"] is True
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 1


def test_a_node_with_no_account_turns_a_messenger_source_on_under_its_local_identity(tmp_path, monkeypatch) -> None:
    """Local first (5 Nov 2026): the identity is minted on this computer; the source works under it and the
    rows move with the account later (local_identity.rescope_to_account)."""
    local_owner = "local:0123456789abcdef0123456789abcdef"
    conn = build_node(tmp_path / "local.db", user_id=local_owner)
    arm(monkeypatch, conn)
    Stores(tmp_path, monkeypatch)
    monkeypatch.setattr("topos.pipeline.job_runner.start_pipeline_worker", lambda conn_factory: None)
    from topos.api.owner_sources import router

    app = FastAPI()
    app.include_router(router)
    with _client(app) as client:
        response = client.post(BASE + "/sources/imessage/enable", json={}, headers=_confirmed())
    assert response.status_code == 200, response.text
    assert response.json()["dataset_id"] == f"{local_owner}:topos:{TOPOS}"
    conn.close()


# --- refusals that need the person --------------------------------------------------


def test_enable_refuses_before_any_write_when_macos_withholds_messages(node) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads every file; the permission cannot be withheld")
    app, conn, stores = node
    stores.chat_db.chmod(0)
    try:
        with _client(app) as client:
            response = client.post(BASE + "/sources/imessage/enable", json={}, headers=_confirmed())
            listed = client.get(BASE + "/sources").json()["sources"]
    finally:
        stores.chat_db.chmod(0o600)
    assert response.status_code == 409 and response.json()["error"] == "full_disk_access_needed"
    assert "Full Disk Access" in response.json()["message"]
    assert str(stores.chat_db) not in response.text
    assert next(r for r in listed if r["source_id"] == "imessage")["needs"] == "full_disk_access_needed"
    _nothing_written(conn)


def test_enable_says_when_there_is_no_messages_history_at_all(node) -> None:
    app, conn, stores = node
    stores.chat_db.unlink()
    with _client(app) as client:
        response = client.post(BASE + "/sources/imessage/enable", json={}, headers=_confirmed())
    assert response.status_code == 409 and response.json()["error"] == "messages_not_found"
    _nothing_written(conn)


def test_imessage_is_refused_off_macos(node, monkeypatch) -> None:
    import topos.ingestion.messenger_setup as setup

    app, conn, _stores = node
    monkeypatch.setattr(setup, "_is_macos", lambda: False)
    with _client(app) as client:
        response = client.post(BASE + "/sources/imessage/preview", json={}, headers=_confirmed())
    assert response.status_code == 409 and response.json()["error"] == "macos_only"
    _nothing_written(conn)


def test_signal_is_refused_with_its_reason_before_any_write(node) -> None:
    app, conn, stores = node
    stores.binding = False
    with _client(app) as client:
        no_binding = client.post(BASE + "/sources/signal/enable", json={}, headers=_confirmed())
    assert no_binding.status_code == 409 and no_binding.json()["error"] == "signal_reader_missing"
    assert "topos-node[signal]" in no_binding.json()["message"]
    assert stores.signal_calls == []

    stores.binding = True
    stores.signal_error = ValueError(f"Unable to open Signal DB at {stores.signal_db} with available SQLCipher settings")
    with _client(app) as client:
        no_key = client.post(BASE + "/sources/signal/enable", json={}, headers=_confirmed())
    assert no_key.status_code == 409 and no_key.json()["error"] == "signal_key_unavailable"
    assert str(stores.signal_db) not in no_key.text, "the reader's own text names a path; it must not leave"

    stores.signal_error = None
    stores.signal_db.unlink()
    with _client(app) as client:
        gone = client.post(BASE + "/sources/signal/enable", json={}, headers=_confirmed())
    assert gone.status_code == 409 and gone.json()["error"] == "signal_not_found"
    _nothing_written(conn)


def test_enable_refuses_below_the_disk_floor(node, monkeypatch) -> None:
    import topos.ingestion.ai_tool_sync as lane

    app, conn, _stores = node
    monkeypatch.setattr(lane, "disk_floor_refusal", lambda conn, needed_bytes=0: "Too little free disk space.")
    with _client(app) as client:
        response = client.post(BASE + "/sources/imessage/enable", json={}, headers=_confirmed())
    assert response.status_code == 409 and response.json()["error"] == "disk_floor"
    _nothing_written(conn)


# --- pause, resume, sync -----------------------------------------------------------


def test_pause_is_open_and_stops_the_schedule_and_resume_needs_a_confirmation(node) -> None:
    from topos.ingestion.local_sync_schedule import get_schedule

    app, conn, _stores = node
    with _client(app) as client:
        assert client.post(BASE + "/sources/imessage/pause").status_code == 409, "nothing to pause before an enable"
        client.post(BASE + "/sources/imessage/enable", json={}, headers=_confirmed())
        paused = client.post(BASE + "/sources/imessage/pause")
        assert paused.status_code == 200 and paused.json() == {
            "status": "ok", "source_id": "imessage", "enabled": False, "paused": True}
        assert get_schedule(conn, DATASET, "imessage")["enabled"] is False
        while_paused = client.post(BASE + "/sources/imessage/sync", headers=_confirmed())
        assert while_paused.status_code == 409 and while_paused.json()["error"] == "source_not_enabled"
        refused = client.post(BASE + "/sources/imessage/resume")
        assert refused.status_code == 403 and refused.json()["error"] == "owner_confirmation_required"
        assert get_schedule(conn, DATASET, "imessage")["enabled"] is False
        resumed = client.post(BASE + "/sources/imessage/resume", headers=_confirmed())
        assert resumed.status_code == 200 and resumed.json()["enabled"] is True
        assert get_schedule(conn, DATASET, "imessage")["enabled"] is True


def test_sync_needs_a_confirmation_and_an_enabled_source(node) -> None:
    from topos.pipeline.job_store import get_job

    app, conn, _stores = node
    with _client(app) as client:
        assert client.post(BASE + "/sources/signal/sync").status_code == 403
        not_enabled = client.post(BASE + "/sources/signal/sync", headers=_confirmed())
        assert not_enabled.status_code == 409 and not_enabled.json()["error"] == "source_not_enabled"
        first = client.post(BASE + "/sources/signal/enable", json={}, headers=_confirmed()).json()
        conn.execute("UPDATE pipeline_jobs SET status='done' WHERE job_id=?", (first["backfill_job_id"],))
        conn.commit()
        queued = client.post(BASE + "/sources/signal/sync", headers=_confirmed())
    assert queued.status_code == 200, queued.text
    assert queued.json()["already_running"] is False
    assert get_job(conn, queued.json()["job_id"])["payload"]["sync_options"] == {"mode": "since_last"}


# --- the extensions' sources -------------------------------------------------------


def test_an_extension_source_has_a_status_and_nothing_to_enable_here(node) -> None:
    from topos import local_pushers

    app, conn, _stores = node
    with _client(app) as client:
        before = client.get(BASE + "/sources/browser_visits").json()
        assert before["connected"] is False and before["app"] == "browser-history-plugin"
        for verb in ("preview", "enable", "pause", "resume", "sync"):
            response = client.post(BASE + f"/sources/browser_visits/{verb}", json={})
            assert response.status_code == 409 and response.json()["error"] == "extension_source", verb
            assert "pushers add browser-history-plugin --sources browser_visits,browser_events" in response.json()["message"]
        row = local_pushers.enrol_pusher(
            conn, app="browser-history-plugin",
            grant=local_pushers.PusherGrant(("browser_visits", "browser_events"),
                                            ("chrome-extension://abcdefghijklmnopabcdefghijklmnop",)),
            approved=True,
        )
        after = client.get(BASE + "/sources/browser_visits")
        events = client.get(BASE + "/sources/browser_events").json()
        shadow = client.get(BASE + "/sources/chatgpt_ui_conversation").json()
        assert after.json()["connected"] is True and events["connected"] is True
        assert shadow["connected"] is False, "one app's key says nothing about another app's source"
        assert row["key"] not in after.text and "token_hash" not in after.text
        local_pushers.revoke_pusher(conn, app="browser-history-plugin")
        assert client.get(BASE + "/sources/browser_visits").json()["connected"] is False
    assert json.dumps(before)  # plain JSON all the way down

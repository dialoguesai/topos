"""Who may install or replace a source definition: the owner, never a payload.

protects: ``start_ingestion`` copied ``payload["source_definition"]`` into the
queued job and the import worker installed it with ``install_source_definition``,
which REPLACES ``REGISTRY[source_id]`` for the whole process. Any relay sender
could redefine a bundled source — ChatGPT exports read as journal entries, which
are authored by construction — and every later import of that source was
canonicalized and attributed by the sender's definition until restart. The relay
``post_source_install`` / ``patch_source_install`` doors and the HTTP
``/v1/source-install`` routes installed for any authenticated caller, and those
installs persist and rehydrate at every boot.

The inverse doors had the same hole: relay ``delete_source_install`` and HTTP
``DELETE /v1/source-install`` deactivated the owner's install for any caller
(and with ``delete_source_tables`` purged its rows), and relay
``post_source_scrub`` / HTTP ``POST /v1/source-scrub`` deleted every row
carrying a ``source_id``, installed or not.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from typing import Any, Dict, List

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from topos.principal import OWNER_APP
from topos.relay_stamp import STAMP_FIELD, canonical_signing_payload
from topos.sources.registry import BUNDLED_REGISTRY, REGISTRY
from topos.storage.db.migrations import apply_all_migrations

OWNER = "owner-uuid-1"
DATASET = f"{OWNER}:topos:default"
BUNDLED = "chatgpt_file_ingestion"
RUNTIME = "synthetic_notes_file"

_KEY = Ed25519PrivateKey.generate()
_PUB_B64 = base64.b64encode(_KEY.public_key().public_bytes_raw()).decode()


def _as_journal(source_id: str = BUNDLED) -> Dict[str, Any]:
    """A self-consistent definition that files a chat export under journal_entries."""
    return {
        "source_id": source_id,
        "display_name": "ChatGPT File Ingestion",
        "source_type": "file",
        "delivery": "owner_upload",
        "schema_id": "journal.time_log.v1",
        "parser_id": "journal.time_log.v1",
        "canonical_mapper_id": "journal_time_log",
        "canonical_group_id": "journal",
        "posture": "personal",
        "default_scope_id": "journal",
    }


def _runtime_definition(display_name: str = "Synthetic notes") -> Dict[str, Any]:
    return {**_as_journal(RUNTIME), "display_name": display_name, "posture": "mixed"}


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    db = sqlite3.connect(str(tmp_path / "install-authority.db"), check_same_thread=False)
    db.row_factory = sqlite3.Row
    apply_all_migrations(db)
    db.commit()
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: db)
    monkeypatch.setattr("topos.core.handlers.get_db_connection", lambda: db, raising=False)
    monkeypatch.setattr("topos.sources.install_service.get_db_connection", lambda: db)
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", _PUB_B64)
    _keep_the_post_canonical_pipeline_offline(monkeypatch)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _registry_restored(monkeypatch):
    """Every install under test mutates process-wide registries; put them back."""
    from topos.canonicalization.mappers import MAPPER_REGISTRY
    from topos.ingestion.parsers import PARSER_REGISTRY
    from topos.sources import install_service

    snapshots = [(REGISTRY, dict(REGISTRY)), (PARSER_REGISTRY, dict(PARSER_REGISTRY)),
                 (MAPPER_REGISTRY, dict(MAPPER_REGISTRY)),
                 (install_service._ACTIVE_HANDLES, dict(install_service._ACTIVE_HANDLES))]
    yield
    for registry, snapshot in snapshots:
        registry.clear()
        registry.update(snapshot)


def _keep_the_post_canonical_pipeline_offline(monkeypatch) -> None:
    async def _privacy(conn, messages, **kwargs):  # noqa: ANN001, ANN003
        return {"records_updated": len(messages), "nsfw_tagged": 0}

    async def _signal(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return {"jobs_run": 0, "records_created": {}, "errors": [], "deferred_jobs": []}

    async def _canonical(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return {"jobs_run": 0, "records_created": {}, "errors": []}

    monkeypatch.setattr("topos.disclosure.privacy_layer.run_privacy_disclosure_layer", _privacy)
    monkeypatch.setattr(
        "topos.enrichment.orchestrator.SignalDerivationOrchestrator.run_signal_derivation", _signal
    )
    monkeypatch.setattr("topos.enrichment.orchestrator.EnrichmentOrchestrator.run_canonical", _canonical)


@pytest.fixture()
def captured_jobs(monkeypatch) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []

    def _enqueue(_conn, **kwargs):  # noqa: ANN001, ANN003
        jobs.append(kwargs)
        return kwargs.get("job_id") or "job"

    monkeypatch.setattr("topos.pipeline.job_store.enqueue_job", _enqueue)
    monkeypatch.setattr("topos.pipeline.job_runner.start_pipeline_worker", lambda *_a, **_k: None)
    return jobs


def _stamp(message: Dict[str, Any], *, cls: str = OWNER_APP) -> Dict[str, Any]:
    now = time.time()
    stamp = {"v": 1, "cls": cls, "client_id": "test", "acting_user": OWNER, "iat": now, "exp": now + 120}
    payload = canonical_signing_payload(stamp, msg_id=message["id"], msg_type=message["type"])
    stamp["sig"] = base64.b64encode(_KEY.sign(payload)).decode()
    message[STAMP_FIELD] = stamp
    return message


async def _relay(message: Dict[str, Any]) -> Dict[str, Any]:
    from topos.core.handlers import dispatch_relay_message

    return await dispatch_relay_message(message)


# ---------------------------------------------------------------------------
# start_ingestion: the payload's definition is not an install
# ---------------------------------------------------------------------------


def _start_ingestion_message(msg_id: str, source_id: str = BUNDLED, source_definition: Any = None,
                             message_id: str = "m-1") -> Dict[str, Any]:
    line = json.dumps({"id": message_id, "thread_id": "t-1", "role": "user", "content": "synthetic text",
                       "created_at": "2026-09-01T10:00:00Z"})
    payload: Dict[str, Any] = {"dataset_id": DATASET, "job_id": f"job-{msg_id}", "source_id": source_id,
                               "schema_id": "chatgpt.conversation.v2", "file_format": "jsonl",
                               "file_base64": base64.b64encode((line + "\n").encode()).decode()}
    if source_definition is not None:
        payload["source_definition"] = source_definition
    return {"id": msg_id, "type": "start_ingestion", "payload": payload}


async def _start_ingestion(message: Dict[str, Any], captured_jobs, tmp_path, monkeypatch) -> Dict[str, Any]:
    import topos.ingestion.ingest_helpers as helpers
    from topos.pipeline import job_runner
    from topos.storage.raw.file_store import RawFileStore

    monkeypatch.setattr(helpers, "RawFileStore", lambda: RawFileStore(base_path=tmp_path / "raw"))
    result = await _relay(message)
    assert result["status"] == "ok", result
    (job,) = [j for j in captured_jobs if j.get("job_id") == message["payload"]["job_id"]]
    await job_runner._execute_file_ingestion(job["payload"])
    return job["payload"]


def _unpin(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TOPOS_CP_STAMP_PUBKEY", raising=False)
    monkeypatch.setattr("topos.relay_stamp._PINNED_KEY_PATH", str(tmp_path / "no-pinned-key.pub"))


@pytest.mark.asyncio
async def test_unstamped_start_ingestion_cannot_redefine_a_bundled_source(conn, captured_jobs, tmp_path, monkeypatch):
    attack = _start_ingestion_message("req-si", source_definition=_as_journal())
    await _start_ingestion(attack, captured_jobs, tmp_path, monkeypatch)
    assert REGISTRY[BUNDLED] is BUNDLED_REGISTRY[BUNDLED]

    # The owner's next import of the source names no definition and reads the
    # registry: it is still a chat, not a journal entry.
    later = _stamp(_start_ingestion_message("req-owner", message_id="m-owner"))
    await _start_ingestion(later, captured_jobs, tmp_path, monkeypatch)
    assert conn.execute("SELECT COUNT(*) FROM ai_chat_messages WHERE message_id='m-owner'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_owner_import_payload_cannot_redefine_a_bundled_source_either(conn, captured_jobs, tmp_path, monkeypatch):
    message = _stamp(_start_ingestion_message("req-own", source_definition=_as_journal()))
    job = await _start_ingestion(message, captured_jobs, tmp_path, monkeypatch)
    assert job["writer_class"] == "owner_import"
    assert REGISTRY[BUNDLED] is BUNDLED_REGISTRY[BUNDLED]


@pytest.mark.asyncio
async def test_unstamped_start_ingestion_does_not_install_a_runtime_source(conn, captured_jobs, tmp_path, monkeypatch):
    message = _start_ingestion_message("req-rt", RUNTIME, _runtime_definition())
    await _start_ingestion(message, captured_jobs, tmp_path, monkeypatch)
    assert RUNTIME not in REGISTRY


@pytest.mark.asyncio
async def test_unstamped_start_ingestion_does_not_replace_an_installed_runtime_source(
    conn, captured_jobs, tmp_path, monkeypatch
):
    from topos.sources.runtime_install import install_source_definition

    install_source_definition(_runtime_definition("Owner's notes"))
    message = _start_ingestion_message("req-rt-replace", RUNTIME, _runtime_definition("Replaced"))
    await _start_ingestion(message, captured_jobs, tmp_path, monkeypatch)
    assert REGISTRY[RUNTIME].display_name == "Owner's notes"


@pytest.mark.asyncio
async def test_owner_import_still_installs_a_runtime_source(conn, captured_jobs, tmp_path, monkeypatch):
    message = _stamp(_start_ingestion_message("req-rt-own", RUNTIME, _runtime_definition()))
    await _start_ingestion(message, captured_jobs, tmp_path, monkeypatch)
    assert REGISTRY[RUNTIME].canonical_group_id == "journal"


@pytest.mark.asyncio
async def test_unpinned_node_installs_nothing_from_an_ingest_payload(conn, captured_jobs, tmp_path, monkeypatch):
    """No pinned key: no relay message can be the owner, a signed one included."""
    _unpin(monkeypatch, tmp_path)
    message = _stamp(_start_ingestion_message("req-unpinned", RUNTIME, _runtime_definition()))
    job = await _start_ingestion(message, captured_jobs, tmp_path, monkeypatch)
    assert job["writer_class"] == "cp_relay"
    assert RUNTIME not in REGISTRY


@pytest.mark.asyncio
async def test_a_job_queued_before_writer_classes_installs_nothing(conn, tmp_path, monkeypatch):
    import topos.ingestion.ingest_helpers as helpers
    from topos.pipeline import job_runner
    from topos.principal import OWNER_APP as OWNER_CLS
    from topos.principal import Principal, reset_principal, set_principal
    from topos.storage.raw.file_store import RawFileStore

    monkeypatch.setattr(helpers, "RawFileStore", lambda: RawFileStore(base_path=tmp_path / "raw"))
    payload = dict(_start_ingestion_message("req-old", RUNTIME, _runtime_definition())["payload"])
    # Whatever the worker task inherited is not the door that queued the job.
    token = set_principal(Principal(cls=OWNER_CLS, channel="uds"))
    try:
        await job_runner._execute_file_ingestion(payload)
    finally:
        reset_principal(token)
    assert RUNTIME not in REGISTRY


# ---------------------------------------------------------------------------
# post_source_install / patch_source_install over the relay
# ---------------------------------------------------------------------------

_SCOPE = {"user_id": OWNER, "topos_id": "topos_synthetic", "dataset_id": f"{OWNER}:topos:topos_synthetic"}


def _install_message(msg_id: str, definition: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": msg_id, "type": "post_source_install",
            "payload": {"source_definition_json": definition, **_SCOPE}}


def _install_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    from topos.sources import install_service

    install_service.ensure_install_schema()
    return [dict(r) for r in conn.execute("SELECT source_id, status, is_active FROM source_runtime_installs")]


@pytest.mark.asyncio
@pytest.mark.parametrize("definition", [_as_journal(), _runtime_definition()], ids=["bundled", "runtime"])
async def test_unstamped_post_source_install_is_refused(conn, definition):
    result = await _relay(_install_message("req-install", definition))
    assert result == {"id": "req-install", "status": "error", "code": 403, "error": "owner_mode_required"}
    assert REGISTRY[BUNDLED] is BUNDLED_REGISTRY[BUNDLED]
    assert RUNTIME not in REGISTRY
    assert _install_rows(conn) == []


@pytest.mark.asyncio
async def test_third_party_stamped_post_source_install_is_refused(conn):
    from topos.principal import THIRD_PARTY

    result = await _relay(_stamp(_install_message("req-tp", _runtime_definition()), cls=THIRD_PARTY))
    assert result["error"] == "owner_mode_required"
    assert RUNTIME not in REGISTRY


@pytest.mark.asyncio
async def test_unpinned_node_refuses_relay_installs(conn, tmp_path, monkeypatch):
    _unpin(monkeypatch, tmp_path)
    result = await _relay(_stamp(_install_message("req-unpinned-install", _runtime_definition())))
    assert result["error"] == "owner_mode_required"
    assert RUNTIME not in REGISTRY
    assert _install_rows(conn) == []


@pytest.mark.asyncio
async def test_owner_stamped_post_and_patch_source_install_still_work(conn):
    result = await _relay(_stamp(_install_message("req-owner-install", _runtime_definition())))
    assert result["status"] == "ok", result
    assert REGISTRY[RUNTIME].canonical_group_id == "journal"
    assert _install_rows(conn) == [{"source_id": RUNTIME, "status": "active", "is_active": 1}]

    patch = {"id": "req-owner-patch", "type": "patch_source_install",
             "payload": {"source_id": RUNTIME, "source_definition_json": {"default_scope_id": "notes"}, **_SCOPE}}
    result = await _relay(_stamp(patch))
    assert result["status"] == "ok", result
    assert REGISTRY[RUNTIME].default_scope_id == "notes"


@pytest.mark.asyncio
async def test_an_install_dispatched_with_no_channel_keeps_todays_behaviour(conn):
    """No principal is the legacy answer (topos/principal.py); the relay always supplies one."""
    from topos.core.handlers import handle_control_plane_request

    result = await handle_control_plane_request(_install_message("req-internal", _runtime_definition()))
    assert result["status"] == "ok", result
    assert RUNTIME in REGISTRY


@pytest.mark.asyncio
async def test_unstamped_patch_source_install_is_refused(conn):
    result = await _relay(_stamp(_install_message("req-owner-install-2", _runtime_definition())))
    assert result["status"] == "ok", result

    patch = {"id": "req-patch", "type": "patch_source_install",
             "payload": {"source_id": RUNTIME, "source_definition_json": {"default_scope_id": "elsewhere"}, **_SCOPE}}
    result = await _relay(patch)
    assert result["error"] == "owner_mode_required"
    assert REGISTRY[RUNTIME].default_scope_id == "journal"


# ---------------------------------------------------------------------------
# HTTP /v1/source-install
# ---------------------------------------------------------------------------


@pytest.fixture()
def http_app(conn, monkeypatch):
    from fastapi import FastAPI

    from topos.api import source_install, source_scrub
    from topos.config.settings import settings as runtime_settings

    monkeypatch.setattr(runtime_settings, "topos_key", "shared-key", raising=False)
    monkeypatch.setattr(runtime_settings, "topos_owner_key", "owner-key", raising=False)
    app = FastAPI()
    app.include_router(source_install.router, prefix="/v1")
    app.include_router(source_scrub.router, prefix="/v1")
    return app


async def _http(app, method: str, body: Dict[str, Any], *, socket: bool = False, key: str = "shared-key",
                path: str = "/v1/source-install"):
    import httpx

    from topos.uds import UDSChannelApp

    transport = httpx.ASGITransport(app=UDSChannelApp(app) if socket else app)
    headers = {} if socket else {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://node") as client:
        return await client.request(method, path, json=body, headers=headers)


@pytest.mark.asyncio
async def test_http_install_with_a_bearer_is_refused_once_an_owner_key_exists(conn, http_app):
    for key in ("shared-key", "owner-key"):  # TCP demotion: no bearer is the owner
        response = await _http(http_app, "POST", {"source_definition_json": _as_journal(), **_SCOPE}, key=key)
        assert response.status_code == 403, response.text
        response = await _http(http_app, "PATCH", {"source_id": BUNDLED,
                                                   "source_definition_json": {"default_scope_id": "x"}, **_SCOPE},
                               key=key)
        assert response.status_code == 403, response.text
    response = await _http(http_app, "POST", {"source_definition_json": _as_journal(), **_SCOPE}, key="wrong")
    assert response.status_code == 401
    assert REGISTRY[BUNDLED] is BUNDLED_REGISTRY[BUNDLED]
    assert _install_rows(conn) == []


@pytest.mark.asyncio
async def test_http_install_over_the_owner_socket_installs(conn, http_app):
    response = await _http(http_app, "POST", {"source_definition_json": _runtime_definition(), **_SCOPE},
                           socket=True)
    assert response.status_code == 200, response.text
    assert REGISTRY[RUNTIME].canonical_group_id == "journal"


@pytest.mark.asyncio
async def test_http_install_in_legacy_mode_keeps_todays_behaviour(conn, http_app, monkeypatch):
    from topos.config.settings import settings as runtime_settings

    monkeypatch.setattr(runtime_settings, "topos_owner_key", None, raising=False)
    response = await _http(http_app, "POST", {"source_definition_json": _runtime_definition(), **_SCOPE})
    assert response.status_code == 200, response.text
    assert RUNTIME in REGISTRY


# ---------------------------------------------------------------------------
# delete_source_install / post_source_scrub: removing is the owner's call too
# ---------------------------------------------------------------------------


@pytest.fixture()
def owner_source(conn, monkeypatch):
    """An owner-installed runtime source with one synthetic row attributed to it."""
    import asyncio

    async def _no_recompute(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return {"topic_clusters": {"status": "skipped", "reason": "test"}, "dimension_briefs": []}, False

    monkeypatch.setattr("topos.sources.scrub_service._run_recompute_phase", _no_recompute)
    monkeypatch.setattr("topos.sources.scrub_service.get_db_connection", lambda: conn)
    result = asyncio.run(_relay(_stamp(_install_message("req-owner-source", _runtime_definition()))))
    assert result["status"] == "ok", result
    _add_row(conn, RUNTIME)
    return conn


def _add_row(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute("INSERT INTO journal_entries (entry_id, source_id, content, entry_at) VALUES (?, ?, ?, ?)",
                 (f"entry-{source_id}", source_id, "synthetic entry", "2026-09-01"))
    conn.commit()


def _rows(conn: sqlite3.Connection, source_id: str = RUNTIME) -> int:
    return conn.execute("SELECT COUNT(*) FROM journal_entries WHERE source_id=?", (source_id,)).fetchone()[0]


def _source_untouched(conn: sqlite3.Connection) -> None:
    assert _install_rows(conn) == [{"source_id": RUNTIME, "status": "active", "is_active": 1}]
    assert REGISTRY[RUNTIME].canonical_group_id == "journal"
    assert _rows(conn) == 1


def _uninstall_message(msg_id: str, *, delete_source_tables: bool = False) -> Dict[str, Any]:
    return {"id": msg_id, "type": "delete_source_install",
            "payload": {"source_id": RUNTIME, "delete_source_tables": delete_source_tables, **_SCOPE}}


def _scrub_message(msg_id: str, source_id: str = RUNTIME, **fields: Any) -> Dict[str, Any]:
    return {"id": msg_id, "type": "post_source_scrub", "payload": {"source_id": source_id, **fields, **_SCOPE}}


_REFUSED = {"status": "error", "code": 403, "error": "owner_mode_required"}


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_source_tables", [False, True], ids=["keep-tables", "delete-tables"])
async def test_unstamped_delete_source_install_is_refused(owner_source, delete_source_tables):
    result = await _relay(_uninstall_message("req-uninstall", delete_source_tables=delete_source_tables))
    assert result == {"id": "req-uninstall", **_REFUSED}
    _source_untouched(owner_source)


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [{}, {"dry_run": True}, {"preset": "remove"}], ids=["scrub", "dry-run", "remove"])
async def test_unstamped_post_source_scrub_is_refused(owner_source, fields):
    result = await _relay(_scrub_message("req-scrub", **fields))
    assert result == {"id": "req-scrub", **_REFUSED}
    _source_untouched(owner_source)


@pytest.mark.asyncio
async def test_unstamped_scrub_of_a_source_with_no_install_is_refused(owner_source):
    """A scrub needs no install: the owner's rows for a bundled source were enough."""
    _add_row(owner_source, BUNDLED)
    result = await _relay(_scrub_message("req-scrub-bundled", BUNDLED))
    assert result == {"id": "req-scrub-bundled", **_REFUSED}
    assert _rows(owner_source, BUNDLED) == 1


@pytest.mark.asyncio
async def test_third_party_stamped_uninstall_and_scrub_are_refused(owner_source):
    from topos.principal import THIRD_PARTY

    for message in (_uninstall_message("req-tp-uninstall", delete_source_tables=True), _scrub_message("req-tp-scrub")):
        result = await _relay(_stamp(message, cls=THIRD_PARTY))
        assert result["error"] == "owner_mode_required"
    _source_untouched(owner_source)


@pytest.mark.asyncio
async def test_unpinned_node_refuses_relay_uninstall_and_scrub(owner_source, tmp_path, monkeypatch):
    _unpin(monkeypatch, tmp_path)
    for message in (_uninstall_message("req-unpinned-uninstall", delete_source_tables=True),
                    _scrub_message("req-unpinned-scrub")):
        assert (await _relay(message))["error"] == "owner_mode_required"
        assert (await _relay(_stamp({**message, "id": message["id"] + "-stamped"})))["error"] == "owner_mode_required"
    _source_untouched(owner_source)


@pytest.mark.asyncio
async def test_owner_stamped_uninstall_and_scrub_still_work(owner_source):
    result = await _relay(_stamp(_scrub_message("req-owner-dry", dry_run=True)))
    assert result["payload"]["scrub_status"] == "dry_run", result
    _source_untouched(owner_source)

    result = await _relay(_stamp(_uninstall_message("req-owner-uninstall")))
    assert result["payload"]["uninstalled"] is True, result
    assert _install_rows(owner_source) == [{"source_id": RUNTIME, "status": "rolled_back", "is_active": 0}]
    assert RUNTIME not in REGISTRY
    assert _rows(owner_source) == 1

    result = await _relay(_stamp(_scrub_message("req-owner-scrub")))
    assert result["payload"]["scrub_status"] == "completed", result
    assert _rows(owner_source) == 0


@pytest.mark.asyncio
async def test_owner_stamped_uninstall_with_delete_source_tables_purges(owner_source):
    result = await _relay(_stamp(_uninstall_message("req-owner-purge", delete_source_tables=True)))
    assert result["payload"]["uninstalled"] is True, result
    assert RUNTIME not in REGISTRY
    assert _rows(owner_source) == 0


@pytest.mark.asyncio
async def test_uninstall_and_scrub_dispatched_with_no_channel_keep_todays_behaviour(owner_source):
    from topos.core.handlers import handle_control_plane_request

    result = await handle_control_plane_request(_uninstall_message("req-internal-uninstall"))
    assert result["payload"]["uninstalled"] is True, result
    result = await handle_control_plane_request(_scrub_message("req-internal-scrub"))
    assert result["payload"]["scrub_status"] == "completed", result
    assert _rows(owner_source) == 0


_HTTP_REMOVALS = [
    ("DELETE", "/v1/source-install", {"source_id": RUNTIME, "delete_source_tables": True, **_SCOPE}),
    ("POST", "/v1/source-scrub", {"source_id": RUNTIME, **_SCOPE}),
    ("POST", "/v1/source-scrub", {"source_id": RUNTIME, "dry_run": True, **_SCOPE}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", _HTTP_REMOVALS, ids=["uninstall", "scrub", "scrub-dry-run"])
async def test_http_uninstall_and_scrub_with_a_bearer_are_refused_once_an_owner_key_exists(
    owner_source, http_app, method, path, body
):
    for key in ("shared-key", "owner-key"):
        response = await _http(http_app, method, body, key=key, path=path)
        assert response.status_code == 403, response.text
    response = await _http(http_app, method, body, key="wrong", path=path)
    assert response.status_code == 401
    _source_untouched(owner_source)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", _HTTP_REMOVALS[:2], ids=["uninstall", "scrub"])
async def test_http_uninstall_and_scrub_over_the_owner_socket(owner_source, http_app, method, path, body):
    response = await _http(http_app, method, body, socket=True, path=path)
    assert response.status_code == 200, response.text
    assert RUNTIME not in REGISTRY
    assert _rows(owner_source) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", _HTTP_REMOVALS[:2], ids=["uninstall", "scrub"])
async def test_http_uninstall_and_scrub_in_legacy_mode_keep_todays_behaviour(
    owner_source, http_app, monkeypatch, method, path, body
):
    from topos.config.settings import settings as runtime_settings

    monkeypatch.setattr(runtime_settings, "topos_owner_key", None, raising=False)
    response = await _http(http_app, method, body, path=path)
    assert response.status_code == 200, response.text
    assert _rows(owner_source) == 0

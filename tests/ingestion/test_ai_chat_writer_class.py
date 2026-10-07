"""Writer class on ai_chat rows: a grantee or app write is never the owner's speech.

protects: the relay doors that write ``ai_chat_messages`` (``app_ingest``,
``store_message``, ``start_ingestion``) resolved every unstamped caller to the
``cp_relay`` principal and then wrote rows that ``roles.record_role`` read as
authored — ``sender_type`` 'human' (the default when a record names no role)
was the whole test. Fact extraction then asserted the text as the owner's own
words. The writer class is now recorded per row at write time from the
channel-verified principal, never from the payload, and the role gate caps any
row whose writer is not the owner at ``observed``.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import sqlite3
import time
from typing import Any, Dict, List

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from topos.features.provenance.roles import ROLE_AUTHORED, ROLE_OBSERVED, owner_authored, record_role
from topos.principal import OWNER_APP, THIRD_PARTY
from topos.relay_stamp import STAMP_FIELD, canonical_signing_payload
from topos.storage.db.migrations import apply_all_migrations

OWNER = "owner-uuid-1"
GRANTEE = "grantee-uuid-2"
DATASET = f"{OWNER}:topos:default"
SOURCE = "chatgpt_ui_conversation"
INJECTED = "I live in Lisbon these days"

_KEY = Ed25519PrivateKey.generate()
_PUB_B64 = base64.b64encode(_KEY.public_key().public_bytes_raw()).decode()


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    db = sqlite3.connect(str(tmp_path / "writer-class.db"), check_same_thread=False)
    db.row_factory = sqlite3.Row
    apply_all_migrations(db)
    db.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
        " VALUES ('ent-owner', 'person', 'Owner', 'owner', 1)"
    )
    db.commit()
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: db)
    monkeypatch.setattr("topos.core.handlers.get_db_connection", lambda: db, raising=False)
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", _PUB_B64)
    _keep_the_post_canonical_pipeline_offline(monkeypatch)
    yield db
    db.close()


def _keep_the_post_canonical_pipeline_offline(monkeypatch) -> None:
    """Doors that do not defer enrichment run it inline; none of it is under test
    here, and the privacy layer would load a classifier model over the network.

    Doors that DO defer it (``app_ingest``, ``start_ingestion``) enqueue a job and
    start the pipeline worker, whose loops fetch their connection through
    ``get_db_connection()`` on their own threads. The fixtures built on this helper
    patch that accessor to ONE ``check_same_thread=False`` handle, so a live worker
    would claim and run the job on the same handle the test thread is writing
    through: two threads, one connection, and the next test-thread write fails with
    ``cannot start a transaction within a transaction`` or ``not an error`` depending
    on where the worker's ``BEGIN IMMEDIATE`` landed (the 2026-07-30 corruption
    shape; ``tests/ingestion/test_usage_inbox_write_id.py`` records the same hazard).
    The worker is never under test here, so it stays off; ``captured_jobs`` keeps the
    enqueued job itself in memory for the tests that read it."""

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
    monkeypatch.setattr("topos.pipeline.job_runner.start_pipeline_worker", lambda *_a, **_k: None)


@pytest.fixture()
def captured_jobs(monkeypatch) -> List[Dict[str, Any]]:
    """The deferred-enrichment job the handler enqueues, instead of a worker."""
    jobs: List[Dict[str, Any]] = []

    def _enqueue(_conn, **kwargs):  # noqa: ANN001, ANN003
        jobs.append(kwargs)
        return kwargs.get("job_id") or "job"

    monkeypatch.setattr("topos.pipeline.job_store.enqueue_job", _enqueue)
    monkeypatch.setattr("topos.pipeline.job_runner.start_pipeline_worker", lambda *_a, **_k: None)
    return jobs


def _stamp(message: Dict[str, Any], *, cls: str, client_id: str = "", acting_user: str = "") -> Dict[str, Any]:
    now = time.time()
    stamp = {"v": 1, "cls": cls, "client_id": client_id, "acting_user": acting_user,
             "iat": now, "exp": now + 120}
    payload = canonical_signing_payload(stamp, msg_id=message["id"], msg_type=message["type"])
    stamp["sig"] = base64.b64encode(_KEY.sign(payload)).decode()
    message[STAMP_FIELD] = stamp
    return message


async def _relay(message: Dict[str, Any]) -> Dict[str, Any]:
    from topos.core.handlers import dispatch_relay_message

    return await dispatch_relay_message(message)


def _app_ingest(msg_id: str, records: List[Dict[str, Any]], *, requester: str = GRANTEE,
                app_id: str = "grantee-app") -> Dict[str, Any]:
    return {
        "id": msg_id,
        "type": "app_ingest",
        "payload": {
            "user_id": OWNER,
            "dataset_id": DATASET,
            "source_id": SOURCE,
            "records": records,
            "resource_id": f"dataset:{OWNER}:{DATASET}",
            "app_id": app_id,
            "requesting_user_id": requester,
        },
    }


def _chat_record(message_id: str, content: str, **extra: Any) -> Dict[str, Any]:
    # No role: chat_ui_raw_payload used to default this to the owner ('human').
    return {"id": message_id, "thread_id": "thread-1", "content": content,
            "created_at": "2026-09-01T10:00:00Z", **extra}


def _row(conn: sqlite3.Connection, message_id: str) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM ai_chat_messages WHERE message_id=?", (message_id,)).fetchone()
    assert row is not None, f"no ai_chat_messages row for {message_id}"
    return dict(row)


def _owner_facts(conn: sqlite3.Connection, predicate: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"
    ).fetchall()
    facts = [json.loads(r[0]) for r in rows]
    return [f for f in facts if f.get("predicate") == predicate and f.get("asserted_by") == "owner"]


async def _run_facts(conn, jobs, monkeypatch) -> None:
    """Run the fact job over what ingest handed enrichment, rules AND LLM pass."""
    monkeypatch.setenv("TOPOS_FACTS_LLM", "on")
    from topos.config import settings as settings_mod
    import topos.features.facts.llm_extract as llm

    monkeypatch.setattr(settings_mod, "settings", settings_mod.Settings())

    def _extractor(prompt, row):  # noqa: ANN001
        if "Lisbon" in str(row.get("content") or ""):
            return [{"predicate": "prefers", "object": "Lisbon"}]
        return []

    monkeypatch.setattr(llm, "_make_ollama_extractor", lambda model, conn=None, **_: _extractor)
    monkeypatch.setattr(llm, "_resolved_extraction_request", lambda *_a, **_k: ("ollama", "stub-model"))
    monkeypatch.setattr("topos.enrichment.jobs.canonical.fact_extraction_job.get_db_connection", lambda: conn)
    from topos.enrichment.jobs.canonical.fact_extraction_job import FactExtractionJob

    records: List[Dict[str, Any]] = []
    for job in jobs:
        records.extend((job.get("payload") or {}).get("canonical_records") or [])
    assert records, "ingest handed enrichment nothing"
    await FactExtractionJob().enrich(records)


# ---------------------------------------------------------------------------
# The reproduction: a grantee's app_ingest over the relay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grantee_app_ingest_is_not_owner_speech_and_mints_no_owner_fact(
    conn, captured_jobs, monkeypatch
):
    result = await _relay(_app_ingest("req-grantee", [_chat_record("m-injected", INJECTED)]))
    assert result["status"] == "ok", result
    assert result["payload"]["records_processed"] == 1

    row = _row(conn, "m-injected")
    assert row["sender_type"] == "human"
    assert row["writer_class"] == "cp_relay"
    assert record_role(row, table="ai_chat_messages") == ROLE_OBSERVED
    assert not owner_authored(row, table="ai_chat_messages")

    await _run_facts(conn, captured_jobs, monkeypatch)
    assert _owner_facts(conn, "lives_in") == [], "rules floor asserted a grantee's sentence as the owner's"
    assert _owner_facts(conn, "prefers") == [], "LLM pass asserted a grantee's sentence as the owner's"


# ---------------------------------------------------------------------------
# The extension path: the CP's owner stamp keeps the owner's own chats authored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_stamped_extension_write_stays_the_owners_speech(conn, captured_jobs, monkeypatch):
    message = _stamp(
        _app_ingest("req-extension", [_chat_record("m-own", INJECTED, role="user")],
                    requester=OWNER, app_id="chatgpt-shadow-extension"),
        cls=OWNER_APP, client_id="chatgpt-shadow-extension", acting_user=OWNER,
    )
    result = await _relay(message)
    assert result["status"] == "ok", result

    row = _row(conn, "m-own")
    assert row["writer_class"] == "owner_app"
    assert record_role(row, table="ai_chat_messages") == ROLE_AUTHORED

    await _run_facts(conn, captured_jobs, monkeypatch)
    assert [f["object_value"] for f in _owner_facts(conn, "lives_in")] == ["Lisbon"]
    assert [f["object_value"] for f in _owner_facts(conn, "prefers")] == ["Lisbon"]


def _node_owner(conn: sqlite3.Connection, user_id: str = OWNER) -> None:
    """The owner this node knows itself by (``engine_config.user_id``), stored as the connection handshake stores it."""
    from topos.core.state import store_user_id

    store_user_id(conn, user_id)


def _refused(msg_id: str) -> Dict[str, Any]:
    return {"id": msg_id, "status": "error", "code": 403, "error": "owner_mode_required"}


def _written(conn: sqlite3.Connection, message_id: str) -> int:
    """Rows of that message; the table itself is made by the first write, so a refused frame may leave none."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_chat_messages'").fetchone() is None:
        return 0
    return conn.execute("SELECT COUNT(*) FROM ai_chat_messages WHERE message_id=?", (message_id,)).fetchone()[0]


@pytest.mark.asyncio
async def test_a_stamped_third_party_is_recorded_as_one(conn, captured_jobs):
    """The owner's own outside client: the one third-party stamp a node serves outside the share doors is one that
    names its owner (review S4 H1). Its write lands, and is never the owner's speech."""
    _node_owner(conn)
    message = _stamp(
        _app_ingest("req-3p", [_chat_record("m-3p", INJECTED, role="user")], requester=OWNER, app_id="outside-app"),
        cls=THIRD_PARTY, client_id="outside-app", acting_user=OWNER,
    )
    assert (await _relay(message))["status"] == "ok"
    row = _row(conn, "m-3p")
    assert row["writer_class"] == "third_party"
    assert not owner_authored(row, table="ai_chat_messages")


@pytest.mark.asyncio
async def test_a_stamped_third_party_for_someone_else_writes_nothing(conn, captured_jobs):
    """Until 1.5.0 this write landed as ``third_party``. The control plane never sends it (a grantee's write goes
    unstamped, ``cp_relay``), and the node now refuses it: a third party who is not the node's owner reaches only
    the share doors (review S4 H1)."""
    _node_owner(conn)
    message = _stamp(
        _app_ingest("req-3p-other", [_chat_record("m-3p-other", INJECTED, role="user")]),
        cls=THIRD_PARTY, client_id="grantee-app", acting_user=GRANTEE,
    )
    assert await _relay(message) == _refused("req-3p-other")
    assert _written(conn, "m-3p-other") == 0
    assert captured_jobs == []


@pytest.mark.asyncio
async def test_an_owner_stamp_for_another_message_does_not_carry_over(conn, captured_jobs):
    """A stamp is bound to one message id and type. Lifted onto another it does not verify, and a frame whose stamp
    does not verify is refused (review S4 M1). Until 1.5.0 it was read as no stamp and written as ``cp_relay``."""
    _node_owner(conn)
    donor = _stamp(_app_ingest("req-donor", []), cls=OWNER_APP, acting_user=OWNER)
    message = _app_ingest("req-lifted", [_chat_record("m-lifted", INJECTED, role="user")])
    message[STAMP_FIELD] = donor[STAMP_FIELD]
    assert await _relay(message) == _refused("req-lifted")
    assert _written(conn, "m-lifted") == 0
    assert captured_jobs == []


@pytest.mark.asyncio
async def test_the_payload_cannot_choose_its_writer_class(conn, captured_jobs):
    message = _app_ingest(
        "req-spoof",
        [_chat_record("m-spoof", INJECTED, role="user", writer_class="owner_app")],
    )
    message["payload"]["writer_class"] = "owner_app"
    message["writer_class"] = "owner_app"
    assert (await _relay(message))["status"] == "ok"
    row = _row(conn, "m-spoof")
    assert row["writer_class"] == "cp_relay"
    assert not owner_authored(row, table="ai_chat_messages")


# ---------------------------------------------------------------------------
# store_message and post_source_test_ingestion: same helper, same principal
# ---------------------------------------------------------------------------


def _store_message(msg_id: str, message_id: str, content: str) -> Dict[str, Any]:
    return {
        "id": msg_id,
        "type": "store_message",
        "payload": {"dataset_id": DATASET, "message_id": message_id,
                    "conversation_id": "thread-sm", "content": content},
    }


@pytest.mark.asyncio
async def test_unstamped_store_message_is_not_owner_speech(conn):
    result = await _relay(_store_message("req-sm", "m-sm", INJECTED))
    assert result["status"] == "ok", result
    row = _row(conn, "m-sm")
    assert (row["sender_type"], row["writer_class"]) == ("human", "cp_relay")
    assert not owner_authored(row, table="ai_chat_messages")


@pytest.mark.asyncio
async def test_owner_stamped_store_message_is_owner_speech(conn):
    message = _stamp(_store_message("req-sm-own", "m-sm-own", INJECTED), cls=OWNER_APP, acting_user=OWNER)
    assert (await _relay(message))["status"] == "ok"
    row = _row(conn, "m-sm-own")
    assert row["writer_class"] == "owner_app"
    assert owner_authored(row, table="ai_chat_messages")


@pytest.mark.asyncio
async def test_unstamped_source_test_ingestion_is_not_owner_speech(conn, monkeypatch):
    monkeypatch.setattr(
        "topos.api.source_install._resolve_active_source_definition",
        lambda **_: {"source_type": "ui_stream", "delivery": "client_push",
                     "schema_id": "chatgpt.conversation.v1"},
    )
    result = await _relay({
        "id": "req-sti",
        "type": "post_source_test_ingestion",
        "payload": {"source_id": SOURCE, "dataset_id": DATASET,
                    "sample_payload": _chat_record("m-sti", INJECTED)},
    })
    assert result["status"] == "ok", result
    assert _row(conn, "m-sti")["writer_class"] == "cp_relay"


# ---------------------------------------------------------------------------
# The legacy path: no streamed chat source to hand the record to
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", [None, SOURCE], ids=["source_id_omitted", "chat_source_not_streamed"])
async def test_the_legacy_path_writes_the_record(conn, tmp_path, monkeypatch, caplog, source_id):
    """Its log line previewed a local the v1-vocabulary helper replaced, so every
    request that reached this path raised NameError before the raw write."""
    import topos.ingestion.ingest_helpers as helpers
    from topos.sources.registry import REGISTRY
    from topos.storage.raw.file_store import RawFileStore

    if source_id:  # a runtime install re-registered the chat source for file upload
        monkeypatch.setitem(REGISTRY, SOURCE, dataclasses.replace(REGISTRY[SOURCE], delivery="owner_upload"))
    raw = RawFileStore(base_path=tmp_path / "raw")
    monkeypatch.setattr(helpers, "RawFileStore", lambda: raw)
    caplog.set_level(logging.INFO, logger="topos.ingestion.ingest_helpers")

    result = await helpers.ingest_ui_payload(
        dataset_id=DATASET, schema_id="chatgpt.conversation.v1", payload=_chat_record("m-legacy-path", INJECTED),
        source_id=source_id, writer_class="cp_relay",
    )
    assert result["status"] == "ok", result
    assert result["records_processed"] == 1
    assert raw.get_file_path(DATASET, "chatgpt.conversation.v1").exists()  # the direct path writes no JSONL
    assert f"content_preview={INJECTED}" in caplog.text
    row = _row(conn, "m-legacy-path")
    assert (row["content"], row["writer_class"]) == (INJECTED, "cp_relay")


# ---------------------------------------------------------------------------
# start_ingestion: the class is decided at the door and travels in the job
# ---------------------------------------------------------------------------


def _export_line(message_id: str, content: str, role: str = "user") -> str:
    return json.dumps({"id": message_id, "thread_id": "thread-file", "role": role,
                       "content": content, "created_at": "2026-09-01T10:00:00Z"})


async def _start_ingestion(message: Dict[str, Any], captured_jobs, conn, tmp_path, monkeypatch) -> None:
    import topos.ingestion.ingest_helpers as helpers
    from topos.pipeline import job_runner
    from topos.storage.raw.file_store import RawFileStore

    monkeypatch.setattr(helpers, "RawFileStore", lambda: RawFileStore(base_path=tmp_path / "raw"))
    result = await _relay(message)
    assert result["status"] == "ok", result
    (job,) = [j for j in captured_jobs if j.get("kind") == "file_ingestion"]
    await job_runner._execute_file_ingestion(job["payload"])


def _start_ingestion_message(msg_id: str, content: str, **payload_extra: Any) -> Dict[str, Any]:
    body = (_export_line("m-file", content) + "\n").encode()
    return {
        "id": msg_id,
        "type": "start_ingestion",
        "payload": {"dataset_id": DATASET, "job_id": f"job-{msg_id}", "source_id": "chatgpt_file_ingestion",
                    "schema_id": "chatgpt.conversation.v2", "file_format": "jsonl",
                    "file_base64": base64.b64encode(body).decode(), **payload_extra},
    }


@pytest.mark.asyncio
async def test_unstamped_start_ingestion_imports_as_cp_relay(conn, captured_jobs, tmp_path, monkeypatch):
    message = _start_ingestion_message("req-si", INJECTED, writer_class="owner_import")
    await _start_ingestion(message, captured_jobs, conn, tmp_path, monkeypatch)
    (job,) = [j for j in captured_jobs if j.get("kind") == "file_ingestion"]
    assert job["payload"]["writer_class"] == "cp_relay"  # the payload's claim is not copied
    row = _row(conn, "m-file")
    assert row["writer_class"] == "cp_relay"
    assert not owner_authored(row, table="ai_chat_messages")


@pytest.mark.asyncio
async def test_owner_stamped_start_ingestion_is_an_owner_import(conn, captured_jobs, tmp_path, monkeypatch):
    message = _stamp(_start_ingestion_message("req-si-own", INJECTED), cls=OWNER_APP, acting_user=OWNER)
    await _start_ingestion(message, captured_jobs, conn, tmp_path, monkeypatch)
    row = _row(conn, "m-file")
    assert row["writer_class"] == "owner_import"
    assert row["writer_dataset_id"] == DATASET  # RD5: the dataset the job was started for, recorded with the class
    assert owner_authored(row, table="ai_chat_messages")


@pytest.mark.asyncio
async def test_a_job_queued_before_writer_classes_records_none(conn, tmp_path, monkeypatch):
    """No class in the job means NULL — never the worker's inherited principal."""
    import topos.ingestion.ingest_helpers as helpers
    from topos.pipeline import job_runner
    from topos.principal import RELAY_PRINCIPAL, reset_principal, set_principal
    from topos.storage.raw.file_store import RawFileStore

    monkeypatch.setattr(helpers, "RawFileStore", lambda: RawFileStore(base_path=tmp_path / "raw"))
    payload = dict(_start_ingestion_message("req-old-job", INJECTED)["payload"])
    token = set_principal(RELAY_PRINCIPAL)  # what a worker started by a relay request inherits
    try:
        await job_runner._execute_file_ingestion(payload)
    finally:
        reset_principal(token)
    assert _row(conn, "m-file")["writer_class"] is None
    assert _row(conn, "m-file")["writer_dataset_id"] is None  # no door, so no dataset either


# ---------------------------------------------------------------------------
# The shared connection and the pipeline worker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_deferring_door_leaves_no_worker_on_the_shared_connection(conn):
    """The isolation `_keep_the_post_canonical_pipeline_offline` promises, pinned as structure.

    protects: ``conn`` is one ``check_same_thread=False`` handle that every thread gets
    from ``get_db_connection``. ``app_ingest`` defers enrichment: it queues a job and
    starts the pipeline worker, whose loops would claim and run that job on this same
    handle while the test thread writes through it. That is two threads on one sqlite3
    connection, and it surfaced as ``sqlite3.OperationalError`` at the test thread's
    next statement (``cannot start a transaction within a transaction``, ``not an
    error``, ``another row available``) in
    ``tests/permissions_v2/test_ai_chat_capture_provenance.py``, whose fixture builds on
    this helper: both full-lane runs, and 3 of 26 runs of that file alone, wherever the
    worker's claim or its table-manager build happened to land. A race pins nothing, so
    this pins what removes it: the door still queues its job, and nothing is left
    running on the loop to claim it. Without the helper's patch it fails every run,
    naming the two pending ``_worker_loop`` tasks.
    """
    result = await _relay(_app_ingest("req-worker-pin", [_chat_record("m-worker-pin", INJECTED)]))
    assert result["status"] == "ok", result
    assert _row(conn, "m-worker-pin")["writer_class"] == "cp_relay"
    queued = conn.execute(
        "SELECT COUNT(*) FROM pipeline_jobs WHERE kind='inbox_deferred_enrichment' AND status='queued'"
    ).fetchone()[0]
    assert queued == 1, "the door's deferred job is still queued; only the worker stays off"
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert others == [], f"a background task outlived the door on the test's loop: {others!r}"


# ---------------------------------------------------------------------------
# Content rewrites under a message id the owner wrote
# ---------------------------------------------------------------------------


async def _owner_write(message_id: str, content: str, *, role: str = "user") -> Dict[str, Any]:
    message = _stamp(
        _app_ingest(f"req-own-{message_id}-{role}", [_chat_record(message_id, content, role=role)],
                    requester=OWNER, app_id="chatgpt-shadow-extension"),
        cls=OWNER_APP, acting_user=OWNER,
    )
    return await _relay(message)


@pytest.mark.asyncio
async def test_a_grantee_cannot_rewrite_an_owner_row(conn, captured_jobs):
    assert (await _owner_write("m-shared", "I am training for a marathon"))["status"] == "ok"
    captured_jobs.clear()

    result = await _relay(_app_ingest("req-rewrite", [_chat_record("m-shared", INJECTED)]))
    assert result["status"] == "error", result
    assert "owner_row_rewrite_refused" in json.dumps(result["payload"]["errors"])

    row = _row(conn, "m-shared")
    assert (row["content"], row["writer_class"]) == ("I am training for a marathon", "owner_app")
    assert owner_authored(row, table="ai_chat_messages")
    assert not any((j.get("payload") or {}).get("canonical_records") for j in captured_jobs)


@pytest.mark.asyncio
async def test_a_grantee_replaying_an_owner_row_changes_and_derives_nothing(conn, captured_jobs):
    assert (await _owner_write("m-dup", INJECTED))["status"] == "ok"
    before = _row(conn, "m-dup")
    captured_jobs.clear()

    result = await _relay(_app_ingest("req-dup", [_chat_record("m-dup", INJECTED)]))
    assert result["status"] == "ok", result
    assert _row(conn, "m-dup") == before
    assert not any((j.get("payload") or {}).get("canonical_records") for j in captured_jobs)


@pytest.mark.asyncio
async def test_a_legacy_row_is_protected_like_an_owner_row(conn, captured_jobs):
    """NULL writer (written before this column, or by an internal path) keeps its content."""
    from topos.storage.canonical.ai_chat import CanonicalTablesManager, Canonicalizer

    Canonicalizer(CanonicalTablesManager(conn)).canonicalize_staging_batch(
        [{"message_id": "m-legacy", "dataset_id": DATASET, "thread_id": "t", "ts": "2026-09-01T10:00:00Z",
          "sender_type": "human", "content": "I work at the bakery", "source_id": SOURCE}],
        source="chatgpt",
    )
    assert _row(conn, "m-legacy")["writer_class"] is None

    result = await _relay(_app_ingest("req-legacy", [_chat_record("m-legacy", INJECTED)]))
    assert result["status"] == "error"
    assert _row(conn, "m-legacy")["content"] == "I work at the bakery"


@pytest.mark.asyncio
async def test_an_internal_rewrite_with_no_writer_keeps_the_rows_class(conn, captured_jobs):
    from topos.storage.canonical.ai_chat import CanonicalTablesManager, Canonicalizer

    assert (await _owner_write("m-internal", "first draft"))["status"] == "ok"
    Canonicalizer(CanonicalTablesManager(conn)).canonicalize_staging_batch(
        [{"message_id": "m-internal", "dataset_id": DATASET, "thread_id": "thread-1",
          "ts": "2026-09-01T10:00:00Z", "sender_type": "human", "content": "second draft",
          "source_id": SOURCE}],
        source="chatgpt",
    )
    row = _row(conn, "m-internal")
    assert (row["content"], row["writer_class"]) == ("second draft", "owner_app")


@pytest.mark.asyncio
async def test_a_grantee_may_update_its_own_row(conn, captured_jobs):
    assert (await _relay(_app_ingest("req-g1", [_chat_record("m-g", "partial")])))["status"] == "ok"
    assert (await _relay(_app_ingest("req-g2", [_chat_record("m-g", "partial, then whole")])))["status"] == "ok"
    row = _row(conn, "m-g")
    assert (row["content"], row["writer_class"]) == ("partial, then whole", "cp_relay")


@pytest.mark.asyncio
async def test_an_owner_write_replaces_a_pre_seeded_row_sender_included(conn, captured_jobs):
    """A grantee seeds a message id as 'human'; the owner's import says it is the
    assistant's reply. Keeping the seeded sender would make the reply the owner's."""
    assert (await _relay(_app_ingest("req-seed", [_chat_record("m-seed", "placeholder")])))["status"] == "ok"
    assert (await _owner_write("m-seed", "Lisbon is lovely in spring", role="assistant"))["status"] == "ok"
    row = _row(conn, "m-seed")
    assert (row["sender_type"], row["writer_class"], row["content"]) == (
        "assistant", "owner_app", "Lisbon is lovely in spring")
    assert not owner_authored(row, table="ai_chat_messages")


# ---------------------------------------------------------------------------
# Readers that re-derive or answer from stored rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reprocess_loader_carries_the_writer_class(conn, captured_jobs):
    from topos.ingestion.canonical_pipeline import load_canonical_records_for_signal
    from topos.sources.registry import REGISTRY

    assert (await _relay(_app_ingest("req-reload", [_chat_record("m-reload", INJECTED)])))["status"] == "ok"
    (record,) = [r for r in load_canonical_records_for_signal(conn, REGISTRY[SOURCE])
                 if r["message_id"] == "m-reload"]
    assert record["writer_class"] == "cp_relay"
    assert record_role({**record, "_table": "ai_chat_messages"}, table="ai_chat_messages") == ROLE_OBSERVED


@pytest.mark.asyncio
async def test_query_time_ownership_reads_the_writer_class(conn, captured_jobs):
    from topos.query.retrieval import _message_row_owner

    assert (await _relay(_app_ingest("req-q", [_chat_record("m-q", INJECTED)])))["status"] == "ok"
    assert (await _owner_write("m-q-own", INJECTED))["status"] == "ok"
    listed = {"record_id": "m-q", "sender_type": "human", "content": INJECTED}  # list specs omit the column
    # This node has no conversation_messages table (no messenger data): the
    # lookup used to fail there and fall back to sender_type alone.
    assert _message_row_owner("ai_chat_messages", listed, conn, {}) is False
    assert _message_row_owner("ai_chat_messages", {**listed, "record_id": "m-q-own"}, conn, {}) is True


@pytest.mark.asyncio
async def test_brief_input_labels_a_grantee_row_as_someone_elses(conn, captured_jobs):
    from topos.enrichment.jobs.canonical.brief_fallback import brief_input_text
    from topos.features.signal.brief_canonical_loader import load_canonical_messages_for_dimension

    assert (await _relay(_app_ingest("req-brief", [_chat_record("m-brief", INJECTED)])))["status"] == "ok"
    (record,) = [r for r in load_canonical_messages_for_dimension(conn, "memory")
                 if r["message_id"] == "m-brief"]
    assert record["writer_class"] == "cp_relay"
    assert brief_input_text(record).startswith("[contact] ")


# ---------------------------------------------------------------------------
# The role gate itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "table", "expected"),
    [
        ({"sender_type": "human"}, "ai_chat_messages", ROLE_AUTHORED),  # legacy: unchanged
        ({"sender_type": "human", "writer_class": None}, "ai_chat_messages", ROLE_AUTHORED),
        ({"sender_type": "human", "writer_class": "owner_app"}, "ai_chat_messages", ROLE_AUTHORED),
        ({"sender_type": "human", "writer_class": "owner_import"}, "ai_chat_messages", ROLE_AUTHORED),
        ({"sender_type": "human", "writer_class": "local_legacy"}, "ai_chat_messages", ROLE_AUTHORED),
        ({"sender_type": "human", "writer_class": "cp_relay"}, "ai_chat_messages", ROLE_OBSERVED),
        ({"sender_type": "user", "writer_class": "third_party"}, "ai_chat_messages", ROLE_OBSERVED),
        ({"sender_type": "human", "writer_class": "owner_automation"}, "ai_chat_messages", ROLE_OBSERVED),
        ({"sender_type": "assistant", "writer_class": "cp_relay"}, "ai_chat_messages", ROLE_OBSERVED),
        ({"sender_type": "assistant", "writer_class": "owner_app"}, "ai_chat_messages", "addressed"),
        ({"sender_type": "system", "writer_class": "cp_relay"}, "ai_chat_messages", "ambient"),
        ({"entry_id": "j", "writer_class": "third_party"}, "journal_entries", ROLE_OBSERVED),
        ({"is_from_self": 1, "writer_class": "cp_relay"}, "conversation_messages", ROLE_OBSERVED),
        ({"sender_type": "human", "writer_class": "some_future_class"}, "ai_chat_messages", ROLE_OBSERVED),
    ],
)
def test_role_gate_caps_rows_a_non_owner_door_wrote(row, table, expected):
    assert record_role(row, table=table) == expected


def test_writer_class_for_principal():
    from topos.features.provenance.writer_class import writer_class_for_principal
    from topos.principal import CP_RELAY, Principal

    assert writer_class_for_principal(None) == "local_legacy"
    assert writer_class_for_principal(Principal(OWNER_APP, "uds")) == "owner_app"
    assert writer_class_for_principal(Principal(OWNER_APP, "cp_relay"), owner_class="owner_import") == "owner_import"
    assert writer_class_for_principal(Principal(CP_RELAY, "cp_relay")) == "cp_relay"
    assert writer_class_for_principal(Principal(THIRD_PARTY, "local_http")) == "third_party"
    assert writer_class_for_principal(Principal("owner_automation", "cp_relay")) == "owner_automation"


# ---------------------------------------------------------------------------
# Local HTTP doors: the class comes from the credential and the transport
# ---------------------------------------------------------------------------


@pytest.fixture()
def http_app(conn, monkeypatch):
    from fastapi import FastAPI

    from topos.api import ingestion_compat, ingestion_sources, source_install
    from topos.config.settings import settings as runtime_settings

    monkeypatch.setattr(runtime_settings, "topos_key", "shared-key", raising=False)
    monkeypatch.setattr(runtime_settings, "topos_owner_key", "owner-key", raising=False)
    app = FastAPI()
    app.include_router(ingestion_sources.router)
    app.include_router(ingestion_compat.router)
    app.include_router(source_install.router, prefix="/v1")
    return app


async def _post(app, path: str, body: Dict[str, Any], *, socket: bool = False, key: str = "shared-key"):
    import httpx

    from topos.uds import UDSChannelApp

    transport = httpx.ASGITransport(app=UDSChannelApp(app) if socket else app)
    headers = {} if socket else {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://node") as client:
        return await client.post(path, json=body, headers=headers)


@pytest.mark.asyncio
async def test_http_ingest_with_a_bearer_is_a_third_party(conn, http_app):
    for key in ("shared-key", "owner-key"):  # TCP demotion: no bearer mints the owner
        message_id = f"m-http-{key}"
        response = await _post(http_app, f"/sources/{SOURCE}/ingest?dataset_id={DATASET}",
                               _chat_record(message_id, INJECTED), key=key)
        assert response.status_code == 200 and response.json()["status"] == "ok", response.text
        assert _row(conn, message_id)["writer_class"] == "third_party"


@pytest.mark.asyncio
async def test_http_ingest_over_the_owner_socket_is_the_owner(conn, http_app):
    response = await _post(http_app, f"/sources/{SOURCE}/ingest?dataset_id={DATASET}",
                           _chat_record("m-uds", INJECTED), socket=True)
    assert response.json()["status"] == "ok", response.text
    row = _row(conn, "m-uds")
    assert row["writer_class"] == "owner_app"
    assert owner_authored(row, table="ai_chat_messages")


@pytest.mark.asyncio
async def test_http_store_message_and_source_test_record_the_bearer(conn, http_app, monkeypatch):
    monkeypatch.setattr(
        "topos.api.source_install._resolve_active_source_definition",
        lambda **_: {"source_type": "ui_stream", "delivery": "client_push",
                     "schema_id": "chatgpt.conversation.v1"},
    )
    response = await _post(http_app, "/store_message",
                           {"dataset_id": DATASET, "id": "m-http-sm", "content": INJECTED})
    assert response.json()["status"] == "ok", response.text
    assert _row(conn, "m-http-sm")["writer_class"] == "third_party"

    response = await _post(http_app, "/v1/source-test-ingestion",
                           {"source_id": SOURCE, "dataset_id": DATASET,
                            "sample_payload": _chat_record("m-http-sti", INJECTED)})
    assert response.status_code == 200, response.text
    assert _row(conn, "m-http-sti")["writer_class"] == "third_party"


@pytest.mark.asyncio
async def test_http_ingest_in_legacy_mode_keeps_todays_behaviour(conn, http_app, monkeypatch):
    from topos.config.settings import settings as runtime_settings

    monkeypatch.setattr(runtime_settings, "topos_owner_key", None, raising=False)
    response = await _post(http_app, f"/sources/{SOURCE}/ingest?dataset_id={DATASET}",
                           _chat_record("m-legacy-http", INJECTED))
    assert response.json()["status"] == "ok", response.text
    row = _row(conn, "m-legacy-http")
    assert row["writer_class"] == "local_legacy"
    assert owner_authored(row, table="ai_chat_messages")

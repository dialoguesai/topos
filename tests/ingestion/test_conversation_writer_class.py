"""Writer class on conversation_messages: a grantee's write is never the owner's speech.

protects: ``conversation_messages`` decides the owner from the row itself —
``is_from_self`` or ``sender_id == 'self'`` — and both are whatever the writer
sent. An unstamped relay ``app_ingest`` for ``voxterm_transcripts`` carrying
``sender_id: "self"`` landed a row ``record_role`` read as authored, and every
lane that re-reads stored rows (reprocess, deferred-enrichment recovery, the
enrichment re-run) asserted its sentences as the owner's facts. A grantee's
``signal_upload`` did the same through ``is_from_self``. The door that wrote
each row is now recorded from the channel-verified principal, never the
payload, and the role gate caps a non-owner writer at ``observed``
(``features/provenance/writer_class.py``, as for ``ai_chat_messages``).
"""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from typing import Any, Dict, List

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from topos.features.provenance.roles import ROLE_AUTHORED, ROLE_OBSERVED, owner_authored, record_role
from topos.principal import OWNER_APP
from topos.relay_stamp import STAMP_FIELD, canonical_signing_payload
from topos.storage.db.migrations import apply_all_migrations

OWNER = "owner-uuid-1"
GRANTEE = "grantee-uuid-2"
DATASET = f"{OWNER}:topos:default"
SOURCE = "voxterm_transcripts"
FILE_SOURCE = "demo_messenger_file"
INJECTED = "I live in Lisbon these days"
TABLE = "conversation_messages"

_KEY = Ed25519PrivateKey.generate()
_PUB_B64 = base64.b64encode(_KEY.public_key().public_bytes_raw()).decode()


@pytest.fixture()
def conn(tmp_path, monkeypatch, pin_db_path):
    db_file = tmp_path / "conversation-writer-class.db"
    pin_db_path(db_file)
    db = sqlite3.connect(str(db_file), check_same_thread=False)
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
    """A file import runs enrichment inline; none of it is under test here, and
    the privacy layer would load a classifier model over the network."""

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
    """The deferred-enrichment job app_ingest enqueues, instead of a worker."""
    jobs: List[Dict[str, Any]] = []

    def _enqueue(_conn, **kwargs):  # noqa: ANN001, ANN003
        jobs.append(kwargs)
        return kwargs.get("job_id") or "job"

    monkeypatch.setattr("topos.pipeline.job_store.enqueue_job", _enqueue)
    monkeypatch.setattr("topos.pipeline.job_runner.start_pipeline_worker", lambda *_a, **_k: None)
    return jobs


def _stamp(message: Dict[str, Any], *, cls: str, acting_user: str = "") -> Dict[str, Any]:
    now = time.time()
    stamp = {"v": 1, "cls": cls, "client_id": "", "acting_user": acting_user, "iat": now, "exp": now + 120}
    payload = canonical_signing_payload(stamp, msg_id=message["id"], msg_type=message["type"])
    stamp["sig"] = base64.b64encode(_KEY.sign(payload)).decode()
    message[STAMP_FIELD] = stamp
    return message


async def _relay(message: Dict[str, Any]) -> Dict[str, Any]:
    from topos.core.handlers import dispatch_relay_message

    return await dispatch_relay_message(message)


def _app_ingest(msg_id: str, records: List[Dict[str, Any]], *, requester: str = GRANTEE) -> Dict[str, Any]:
    return {
        "id": msg_id,
        "type": "app_ingest",
        "payload": {
            "user_id": OWNER,
            "dataset_id": DATASET,
            "source_id": SOURCE,
            "records": records,
            "resource_id": f"dataset:{OWNER}:{DATASET}",
            "app_id": "voxterm",
            "requesting_user_id": requester,
        },
    }


def _segment(message_id: str, content: str, *, sender_id: str = "self", **extra: Any) -> Dict[str, Any]:
    return {"message_id": message_id, "conversation_id": "vox-1", "sender_id": sender_id,
            "content": content, "event_at": "2026-09-01T10:00:00Z", **extra}


async def _owner_write(message_id: str, content: str, *, sender_id: str = "self") -> Dict[str, Any]:
    message = _stamp(
        _app_ingest(f"req-own-{message_id}-{sender_id}", [_segment(message_id, content, sender_id=sender_id)],
                    requester=OWNER),
        cls=OWNER_APP, acting_user=OWNER,
    )
    return await _relay(message)


def _row(conn: sqlite3.Connection, message_id: str) -> Dict[str, Any]:
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE message_id=?", (message_id,)).fetchone()
    assert row is not None, f"no {TABLE} row for {message_id}"
    return dict(row)


def _job_records(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for job in jobs:
        records.extend((job.get("payload") or {}).get("canonical_records") or [])
    return records


def _reloaded(conn: sqlite3.Connection, source_id: str = SOURCE) -> List[Dict[str, Any]]:
    """What reprocess, deferred-enrichment recovery and the enrichment re-run derive from."""
    from topos.ingestion.canonical_pipeline import load_canonical_records_for_signal
    from topos.sources.registry import REGISTRY

    return load_canonical_records_for_signal(conn, REGISTRY[source_id])


def _owner_facts(conn: sqlite3.Connection, predicate: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"
    ).fetchall()
    facts = [json.loads(r[0]) for r in rows]
    return [f for f in facts if f.get("predicate") == predicate and f.get("asserted_by") == "owner"]


async def _run_facts(conn, records: List[Dict[str, Any]], monkeypatch) -> None:
    """The fact job over ``records``, rules floor AND the LLM pass (stubbed)."""
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

    assert records, "nothing to derive from"
    await FactExtractionJob().enrich(records)


# ---------------------------------------------------------------------------
# The reproduction: a grantee's voxterm app_ingest claiming sender_id 'self'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grantee_voxterm_ingest_as_self_mints_no_owner_fact(conn, captured_jobs, monkeypatch):
    result = await _relay(_app_ingest("req-grantee", [_segment("vox-injected", INJECTED)]))
    assert result["status"] == "ok", result
    assert result["payload"]["records_processed"] == 1

    # The ingest-time records AND the stored row every re-derivation lane reads.
    await _run_facts(conn, _job_records(captured_jobs) + _reloaded(conn), monkeypatch)
    assert _owner_facts(conn, "lives_in") == [], "rules floor asserted a grantee's sentence as the owner's"
    assert _owner_facts(conn, "prefers") == [], "LLM pass asserted a grantee's sentence as the owner's"

    row = _row(conn, "vox-injected")
    assert (row["sender_id"], row["is_from_self"], row["writer_class"]) == ("self", 0, "cp_relay")
    assert record_role(row, table=TABLE) == ROLE_OBSERVED
    assert not owner_authored(row, table=TABLE)


@pytest.mark.asyncio
async def test_owner_stamped_voxterm_write_stays_the_owners_speech(conn, captured_jobs, monkeypatch):
    assert (await _owner_write("vox-own", INJECTED))["status"] == "ok"

    row = _row(conn, "vox-own")
    assert row["writer_class"] == "owner_app"
    assert record_role(row, table=TABLE) == ROLE_AUTHORED

    await _run_facts(conn, _reloaded(conn), monkeypatch)
    assert [f["object_value"] for f in _owner_facts(conn, "lives_in")] == ["Lisbon"]
    assert [f["object_value"] for f in _owner_facts(conn, "prefers")] == ["Lisbon"]


@pytest.mark.asyncio
async def test_the_payload_cannot_choose_its_writer_class(conn, captured_jobs):
    message = _app_ingest("req-spoof", [_segment("vox-spoof", INJECTED, writer_class="owner_app")])
    message["payload"]["writer_class"] = "owner_app"
    message["writer_class"] = "owner_app"
    assert (await _relay(message))["status"] == "ok"
    row = _row(conn, "vox-spoof")
    assert row["writer_class"] == "cp_relay"
    assert not owner_authored(row, table=TABLE)


@pytest.mark.asyncio
async def test_ingest_time_records_carry_the_writer_class(conn, captured_jobs):
    assert (await _relay(_app_ingest("req-inline", [_segment("vox-inline", INJECTED)])))["status"] == "ok"
    (record,) = [r for r in _job_records(captured_jobs) if r["message_id"] == "vox-inline"]
    assert (record["_table"], record["writer_class"]) == (TABLE, "cp_relay")


# ---------------------------------------------------------------------------
# Content heals under a message id the owner wrote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_grantee_cannot_heal_an_owner_row(conn, captured_jobs):
    assert (await _owner_write("vox-shared", "I am training for a marathon"))["status"] == "ok"
    before = _row(conn, "vox-shared")
    captured_jobs.clear()

    result = await _relay(_app_ingest("req-heal", [_segment("vox-shared", INJECTED)]))
    assert result["status"] == "error", result
    assert "owner_row_rewrite_refused" in json.dumps(result["payload"]["errors"])

    assert _row(conn, "vox-shared") == before  # content, sync batch and ingest time untouched
    assert not _job_records(captured_jobs)


@pytest.mark.asyncio
async def test_a_grantee_replaying_an_owner_row_changes_and_derives_nothing(conn, captured_jobs):
    assert (await _owner_write("vox-dup", INJECTED))["status"] == "ok"
    before = _row(conn, "vox-dup")
    captured_jobs.clear()

    result = await _relay(_app_ingest("req-dup", [_segment("vox-dup", INJECTED)]))
    assert result["status"] == "ok", result
    assert result["payload"]["errors"] == []
    assert _row(conn, "vox-dup") == before
    assert not _job_records(captured_jobs)


def _internal_write(conn, message_id: str, content: str, *, sender_id: str = "self") -> None:
    """A write with no door: the node's own messenger sync, a reprocess replay."""
    from topos.storage.canonical import ConversationsTablesManager

    ConversationsTablesManager(conn).upsert_message_batch(
        [{"message_id": message_id, "conversation_id": "vox-1", "ts": "2026-09-01T10:00:00Z",
          "sender_type": "human", "sender_id": sender_id, "content": content}],
        DATASET, SOURCE,
    )


@pytest.mark.asyncio
async def test_a_legacy_row_is_protected_like_an_owner_row(conn, captured_jobs):
    """NULL writer (written before the column, or by the node's own sync) keeps its content."""
    _internal_write(conn, "vox-legacy", "I work at the bakery")
    assert _row(conn, "vox-legacy")["writer_class"] is None

    result = await _relay(_app_ingest("req-legacy", [_segment("vox-legacy", INJECTED)]))
    assert "owner_row_rewrite_refused" in json.dumps(result["payload"]["errors"])
    assert _row(conn, "vox-legacy")["content"] == "I work at the bakery"


@pytest.mark.asyncio
async def test_an_internal_heal_keeps_the_rows_writer_class(conn, captured_jobs):
    assert (await _owner_write("vox-internal", "first draft"))["status"] == "ok"
    _internal_write(conn, "vox-internal", "second draft")
    row = _row(conn, "vox-internal")
    assert (row["content"], row["writer_class"]) == ("second draft", "owner_app")


@pytest.mark.asyncio
async def test_an_internal_replay_never_promotes_a_grantee_row(conn, captured_jobs):
    """A reprocess replays a grantee's raw payload with no door: the row stays theirs."""
    assert (await _relay(_app_ingest("req-replay", [_segment("vox-replay", "partial")])))["status"] == "ok"
    _internal_write(conn, "vox-replay", INJECTED)
    row = _row(conn, "vox-replay")
    assert (row["content"], row["writer_class"]) == (INJECTED, "cp_relay")
    assert not owner_authored(row, table=TABLE)


@pytest.mark.asyncio
async def test_a_grantee_may_heal_its_own_row(conn, captured_jobs):
    assert (await _relay(_app_ingest("req-g1", [_segment("vox-g", "partial")])))["status"] == "ok"
    assert (await _relay(_app_ingest("req-g2", [_segment("vox-g", "partial, then whole")])))["status"] == "ok"
    row = _row(conn, "vox-g")
    assert (row["content"], row["writer_class"]) == ("partial, then whole", "cp_relay")
    assert _raw_payloads(conn)["vox-g"]["content"] == "partial, then whole"  # an accepted write keeps its raw row


@pytest.mark.asyncio
async def test_an_owner_write_takes_over_a_pre_seeded_row_sender_included(conn, captured_jobs):
    """A grantee seeds a message id as 'self'; the owner's own capture says someone
    else said it. Keeping the seeded sender would make that the owner's speech; keeping
    the seeded writer would demote the owner's real rows it collides with."""
    assert (await _relay(_app_ingest("req-seed", [_segment("vox-seed", "placeholder")])))["status"] == "ok"
    assert (await _owner_write("vox-seed", "Lisbon is lovely in spring", sender_id="Speaker 2"))["status"] == "ok"
    row = _row(conn, "vox-seed")
    assert (row["sender_id"], row["writer_class"], row["content"]) == (
        "Speaker 2", "owner_app", "Lisbon is lovely in spring")
    assert record_role(row, table=TABLE) == ROLE_OBSERVED

    assert (await _relay(_app_ingest("req-seed-2", [_segment("vox-seed-2", "placeholder")])))["status"] == "ok"
    assert (await _owner_write("vox-seed-2", INJECTED))["status"] == "ok"
    row = _row(conn, "vox-seed-2")
    assert (row["sender_id"], row["writer_class"], row["content"]) == ("self", "owner_app", INJECTED)
    assert record_role(row, table=TABLE) == ROLE_AUTHORED


# ---------------------------------------------------------------------------
# signal_upload: the export decides is_from_self, the door decides the writer
# ---------------------------------------------------------------------------


def _signal_upload(msg_id: str) -> Dict[str, Any]:
    export = [{"conversationId": "sig-1", "type": "outgoing", "body": INJECTED, "sent_at": 1788000000000}]
    return {
        "id": msg_id,
        "type": "signal_upload",
        "payload": {"dataset_id": DATASET, "file_base64": base64.b64encode(json.dumps(export).encode()).decode()},
    }


@pytest.fixture()
def signal_enrichment(monkeypatch) -> List[Dict[str, Any]]:
    handed: List[Dict[str, Any]] = []

    def _capture(*, db_conn, source_id, canonical_messages):  # noqa: ANN001
        handed.extend(canonical_messages)

    monkeypatch.setattr("topos.ingestion.local_sync._run_local_sync_enrichment_if_enabled", _capture)
    return handed


def _signal_rows(conn) -> list:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone():
        return []
    return list(conn.execute(f"SELECT message_id FROM {TABLE} WHERE source_id='signal'"))


@pytest.mark.asyncio
async def test_unstamped_signal_upload_is_not_owner_speech(conn, signal_enrichment, monkeypatch):
    # Main now refuses a non-owner Signal upload at the door (owner gate), which
    # is stronger than recording it as cp_relay: nothing lands, nothing derives.
    result = await _relay(_signal_upload("req-signal"))
    assert (result["status"], result.get("error")) == ("error", "owner_mode_required"), result

    assert _signal_rows(conn) == []
    assert signal_enrichment == []


@pytest.mark.asyncio
async def test_owner_stamped_signal_upload_is_an_owner_import(conn, signal_enrichment):
    assert (await _relay(_stamp(_signal_upload("req-signal-own"), cls=OWNER_APP, acting_user=OWNER)))["status"] == "ok"
    (row,) = [dict(r) for r in conn.execute(f"SELECT * FROM {TABLE} WHERE source_id='signal'")]
    assert row["writer_class"] == "owner_import"
    assert owner_authored(row, table=TABLE)


# ---------------------------------------------------------------------------
# start_ingestion: a conversations file import records the door that queued it
# ---------------------------------------------------------------------------


async def _file_import(message_ids: List[str], captured_jobs, tmp_path, monkeypatch) -> None:
    """A grantee's unstamped start_ingestion of a CSV, one line per id, run by the job worker."""
    import topos.ingestion.ingest_helpers as helpers
    from topos.pipeline import job_runner
    from topos.storage.raw.file_store import RawFileStore

    monkeypatch.setattr(helpers, "RawFileStore", lambda: RawFileStore(base_path=tmp_path / "raw"))
    body = "message_id,conversation_id,sender_id,content,event_at\n" + "".join(
        f"{message_id},thread-file,self,{INJECTED},2026-09-01T10:00:00Z\n" for message_id in message_ids
    )
    job_id = f"job-{message_ids[0]}"
    result = await _relay({
        "id": f"req-{job_id}",
        "type": "start_ingestion",
        "payload": {"dataset_id": DATASET, "job_id": job_id, "source_id": FILE_SOURCE,
                    "schema_id": "demo.messenger.v1", "file_format": "csv",
                    "file_base64": base64.b64encode(body.encode()).decode()},
    })
    assert result["status"] == "ok", result
    (job,) = [j for j in captured_jobs if j.get("kind") == "file_ingestion" and j["payload"].get("job_id") == job_id]
    await job_runner._execute_file_ingestion(job["payload"])


@pytest.mark.asyncio
async def test_unstamped_messenger_file_import_is_not_owner_speech(conn, captured_jobs, tmp_path, monkeypatch):
    await _file_import(["msg-file"], captured_jobs, tmp_path, monkeypatch)
    row = _row(conn, "msg-file")
    assert (row["sender_id"], row["writer_class"]) == ("self", "cp_relay")
    assert not owner_authored(row, table=TABLE)


# ---------------------------------------------------------------------------
# Raw retention: a refused write leaves nothing for a reprocess to replay
# ---------------------------------------------------------------------------


def _raw_payloads(conn: sqlite3.Connection, source_id: str = SOURCE) -> Dict[str, Dict[str, Any]]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw_%'")]
    payloads: Dict[str, Dict[str, Any]] = {}
    for table in tables:
        for record_id, payload_json in conn.execute(
            f"SELECT source_record_id, payload_json FROM {table} WHERE source_system=?", (source_id,)
        ):
            payloads[record_id] = json.loads(payload_json)
    return payloads


async def _reprocess_from_raw(conn: sqlite3.Connection, monkeypatch, source_id: str = SOURCE) -> None:
    """The raw replay: it passes no writer class, so the store's gate does not run."""
    from topos.ingestion.reprocess import reprocess_source

    monkeypatch.setattr("topos.ingestion.reprocess.get_db_connection", lambda: conn)
    await reprocess_source(source_id=source_id, dataset_id=DATASET, from_stage="raw", run_enrichment=False)


@pytest.mark.asyncio
async def test_a_reprocess_cannot_replay_a_refused_write_under_the_owners_row(conn, captured_jobs, monkeypatch):
    """Raw retention replaces by record id before the store decides: left there, the
    refused payload came back through a reprocess as the owner's authored row."""
    assert (await _owner_write("vox-raw", "I am training for a marathon"))["status"] == "ok"
    raw_before = _raw_payloads(conn)
    assert (await _relay(_app_ingest("req-raw", [_segment("vox-raw", INJECTED)])))["status"] == "error"
    assert _raw_payloads(conn) == raw_before

    await _reprocess_from_raw(conn, monkeypatch)
    row = _row(conn, "vox-raw")
    assert (row["content"], row["writer_class"]) == ("I am training for a marathon", "owner_app")
    await _run_facts(conn, _reloaded(conn), monkeypatch)
    assert _owner_facts(conn, "lives_in") == [] and _owner_facts(conn, "prefers") == []


@pytest.mark.asyncio
async def test_a_refused_write_over_a_row_with_no_raw_leaves_none(conn, captured_jobs, monkeypatch):
    _internal_write(conn, "vox-raw-legacy", "I work at the bakery")  # the node's own sync keeps no raw row here
    assert (await _relay(_app_ingest("req-raw-legacy", [_segment("vox-raw-legacy", INJECTED)])))["status"] == "error"
    assert "vox-raw-legacy" not in _raw_payloads(conn)

    await _reprocess_from_raw(conn, monkeypatch)
    assert _row(conn, "vox-raw-legacy")["content"] == "I work at the bakery"


@pytest.mark.asyncio
async def test_a_refused_file_import_row_leaves_no_raw_row(conn, captured_jobs, tmp_path, monkeypatch):
    assert (await _owner_write("msg-raw-file", "I am training for a marathon"))["status"] == "ok"
    await _file_import(["msg-raw-new", "msg-raw-file"], captured_jobs, tmp_path, monkeypatch)
    assert _row(conn, "msg-raw-file")["content"] == "I am training for a marathon"
    assert set(_raw_payloads(conn, FILE_SOURCE)) == {"msg-raw-new"}  # the accepted line keeps its raw row

    await _reprocess_from_raw(conn, monkeypatch, FILE_SOURCE)
    row = _row(conn, "msg-raw-file")
    assert (row["content"], row["writer_class"]) == ("I am training for a marathon", "owner_app")


@pytest.mark.asyncio
async def test_a_reprocess_cannot_replay_a_refused_ai_chat_write(conn, captured_jobs, monkeypatch):
    """The raw undo sits below the canonical groups: ai_chat's refusal had the same replay."""
    chat_source = "chatgpt_ui_conversation"

    def _chat(msg_id: str, content: str, requester: str) -> Dict[str, Any]:
        message = _app_ingest(msg_id, [{"id": "m-raw", "thread_id": "thread-1", "role": "user",
                                        "content": content, "created_at": "2026-09-01T10:00:00Z"}],
                              requester=requester)
        message["payload"]["source_id"] = chat_source
        return message

    owner = _stamp(_chat("req-chat-own", "I am training for a marathon", OWNER), cls=OWNER_APP, acting_user=OWNER)
    assert (await _relay(owner))["status"] == "ok"
    assert (await _relay(_chat("req-chat-grantee", INJECTED, GRANTEE)))["status"] == "error"

    await _reprocess_from_raw(conn, monkeypatch, chat_source)
    row = conn.execute("SELECT content, writer_class FROM ai_chat_messages WHERE message_id='m-raw'").fetchone()
    assert tuple(row) == ("I am training for a marathon", "owner_app")


def test_a_batch_restore_matches_the_canonical_id_not_the_raw_key(tmp_path):
    """A refusal names the canonical row (a doc_id, an entry_id); a pass-through raw
    row can be keyed on something else entirely, such as the job id."""
    from types import SimpleNamespace

    from topos.ingestion.manager import _persist_raw_retention, _restore_refused_raw
    from topos.ingestion.parsers.base import NormalizedRecord

    db = sqlite3.connect(str(tmp_path / "raw.db"))
    apply_all_migrations(db)
    source_def = SimpleNamespace(source_id="notion_pages", canonical_group_id="documents")
    records = [NormalizedRecord(record_id=f"job-{n}", payload={"doc_id": f"doc-{n}", "content": INJECTED})
               for n in (1, 2)]
    snapshots: List[Any] = []
    _persist_raw_retention(db, source_def, records, sync_batch_id="b1", records_in=2, raw_snapshots=snapshots)
    _restore_refused_raw(db, snapshots, {"doc-2": "owner_row_rewrite_refused"})
    assert set(_raw_payloads(db, "notion_pages")) == {"job-1"}


# ---------------------------------------------------------------------------
# Readers that re-derive or answer from stored rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reprocess_loader_carries_the_writer_class(conn, captured_jobs):
    assert (await _relay(_app_ingest("req-reload", [_segment("vox-reload", INJECTED)])))["status"] == "ok"
    (record,) = [r for r in _reloaded(conn) if r["message_id"] == "vox-reload"]
    assert record["writer_class"] == "cp_relay"
    assert record_role(record, table=TABLE) == ROLE_OBSERVED


def test_reprocess_loader_reads_a_schema_without_the_column(tmp_path):
    from topos.storage.canonical.conversations_tables import ensure_all_tables

    db = sqlite3.connect(str(tmp_path / "old.db"))
    ensure_all_tables(db)  # the messenger lane creates the table lazily
    apply_all_migrations(db)
    db.execute(f"ALTER TABLE {TABLE} DROP COLUMN writer_class")  # a database the previous release wrote
    db.execute(f"INSERT INTO {TABLE} (message_id, conversation_id, dataset_id, sender_id, content, event_at,"
               " source_id) VALUES ('old', 'c', 'd', 'self', 'hello there', '2026-09-01', ?)", (SOURCE,))
    (record,) = _reloaded(db)
    assert record["writer_class"] is None
    assert record_role(record, table=TABLE) == ROLE_AUTHORED  # a legacy row keeps its behaviour


@pytest.mark.asyncio
async def test_query_time_ownership_reads_the_writer_class(conn, captured_jobs):
    from topos.query.retrieval import _message_row_owner, _record_owner_authored

    assert (await _relay(_app_ingest("req-q", [_segment("vox-q", INJECTED)])))["status"] == "ok"
    assert (await _owner_write("vox-q-own", INJECTED))["status"] == "ok"
    listed = {"record_id": "vox-q", "sender_id": "self", "content": INJECTED}  # list specs omit the column
    assert _message_row_owner(TABLE, listed, conn, {}) is False
    assert _message_row_owner(TABLE, {**listed, "is_from_self": 1}, conn, {}) is False
    assert _message_row_owner(TABLE, {**listed, "record_id": "vox-q-own"}, conn, {}) is True
    assert _record_owner_authored(conn, "vox-q", {}) is False
    assert _record_owner_authored(conn, "vox-q-own", {}) is True


@pytest.mark.asyncio
async def test_a_thread_does_not_label_a_grantee_row_as_the_owner(conn, captured_jobs):
    from topos.query.retrieval import _thread_speaker

    assert (await _relay(_app_ingest("req-thread", [_segment("vox-thread", INJECTED)])))["status"] == "ok"
    assert (await _owner_write("vox-thread-own", INJECTED))["status"] == "ok"
    caches = {"role_cache": {}, "display_cache": {}, "entity_cache": {}}
    grantee = _thread_speaker(conn, TABLE, {"record_id": "vox-thread", "sender_id": "self"}, **caches)
    owner = _thread_speaker(conn, TABLE, {"record_id": "vox-thread-own", "sender_id": "self"}, **caches)
    assert grantee["kind"] != "owner"
    assert owner["kind"] == "owner"


@pytest.mark.asyncio
async def test_brief_input_labels_a_grantee_row_as_someone_elses(conn, captured_jobs):
    from topos.enrichment.jobs.canonical.brief_fallback import brief_input_text
    from topos.features.signal.brief_canonical_loader import load_canonical_messages_for_dimension

    assert (await _relay(_app_ingest("req-brief", [_segment("vox-brief", INJECTED)])))["status"] == "ok"
    (record,) = [r for r in load_canonical_messages_for_dimension(conn, "memory")
                 if r["message_id"] == "vox-brief"]
    assert record["writer_class"] == "cp_relay"
    assert brief_input_text(record).startswith("[contact] ")


# ---------------------------------------------------------------------------
# The column rides the always-run migration step
# ---------------------------------------------------------------------------


def test_the_column_is_added_to_a_table_created_without_it(tmp_path):
    from topos.storage.canonical.conversations_tables import ensure_all_tables
    from topos.storage.db.migrations import ensure_migrations_applied, read_user_version

    db = sqlite3.connect(str(tmp_path / "late.db"))
    apply_all_migrations(db)
    version = read_user_version(db)
    ensure_all_tables(db)  # the messenger lane creates its tables lazily, after migrations
    assert "writer_class" not in {r[1] for r in db.execute(f"PRAGMA table_info({TABLE})")}

    ensure_migrations_applied(db)
    assert "writer_class" in {r[1] for r in db.execute(f"PRAGMA table_info({TABLE})")}
    assert read_user_version(db) == version  # no registry order moved


# ---------------------------------------------------------------------------
# Local HTTP doors: the class comes from the credential and the transport
# ---------------------------------------------------------------------------


@pytest.fixture()
def http_app(conn, monkeypatch):
    from fastapi import FastAPI

    from topos.api import ingestion_sources
    from topos.config.settings import settings as runtime_settings

    monkeypatch.setattr(runtime_settings, "topos_key", "shared-key", raising=False)
    monkeypatch.setattr(runtime_settings, "topos_owner_key", "owner-key", raising=False)
    monkeypatch.setattr("topos.ingestion.local_sync._run_local_sync_enrichment_if_enabled", lambda **_: None)
    app = FastAPI()
    app.include_router(ingestion_sources.router)
    return app


async def _post(app, path: str, *, socket: bool = False, **kwargs: Any):
    import httpx

    from topos.uds import UDSChannelApp

    transport = httpx.ASGITransport(app=UDSChannelApp(app) if socket else app)
    headers = {} if socket else {"Authorization": "Bearer owner-key"}
    async with httpx.AsyncClient(transport=transport, base_url="http://node") as client:
        return await client.post(path, headers=headers, **kwargs)


@pytest.mark.asyncio
async def test_http_voxterm_ingest_with_a_bearer_is_a_third_party(conn, http_app):
    response = await _post(http_app, f"/sources/{SOURCE}/ingest?dataset_id={DATASET}",
                           json=_segment("vox-http", INJECTED))
    assert response.json()["status"] == "ok", response.text
    row = _row(conn, "vox-http")
    assert row["writer_class"] == "third_party"  # TCP demotion: even the owner key
    assert not owner_authored(row, table=TABLE)


@pytest.mark.asyncio
@pytest.mark.parametrize(("socket", "expected"), [(False, None), (True, "owner_import")])
async def test_http_signal_upload_records_the_door(conn, http_app, socket, expected):
    export = [{"conversationId": "sig-http", "type": "outgoing", "body": INJECTED, "sent_at": 1788000000000}]
    upload = _post(http_app, f"/sources/signal/upload?dataset_id={DATASET}", socket=socket,
                   files={"file": ("signal.json", json.dumps(export), "application/json")})
    if expected is None:
        # The owner key over TCP is demoted, and main's owner gate refuses the
        # upload before anything is stored.
        response = await upload
        assert response.status_code == 403, response.text
        assert _signal_rows(conn) == []
        return
    response = await upload
    assert response.json()["status"] == "ok", response.text
    (row,) = [dict(r) for r in conn.execute(f"SELECT * FROM {TABLE} WHERE source_id='signal'")]
    assert (row["is_from_self"], row["writer_class"]) == (1, expected)
    assert owner_authored(row, table=TABLE)

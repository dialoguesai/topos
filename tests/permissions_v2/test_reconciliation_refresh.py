"""RD8: an owner refresh re-proves one recovery enrollment against a fresh capture, all or nothing.

  F1  re-proven rows keep their link at the next revision, new rows gain one, unproven rows lose theirs
  F2  canonical rows, the source row and every opaque-id input stay byte for byte; the clock moves once
  F3  a stale (never a revoked) enrollment is brought current; a disabled source refuses
  F4  a whole-message ceiling is carried only while its row revision is unchanged
  F5  any refusal or mismatch leaves the ledger, the marker and the previous proof exactly as they were
  F6  only the authenticated owner refreshes; an unchanged or unenrolled capture refuses
  F7  a capture no enrollment names is discarded; a named one never is
  F8  the owner door: local owner socket only, and nothing without an enrollment
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.ingestion.test_owner_snapshot import NOW
from tests.permissions_v2.test_imessage_reconciliation import snapshot
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
from topos.permissions_v2.imessage_reconciliation import ATTRIBUTED_CONTRACT, parse_reconciliation_snapshot
from topos.permissions_v2.ingest_provenance import OWNER_ATTESTATION
from topos.permissions_v2.reconciliation_facts import classification
from topos.permissions_v2.reconciliation_provenance import (
    discard_capture, publish_existing, refresh_existing, validate_existing)

DATASET = "native-dataset"
LEDGER = ("ingest_provenance_enrollments", "ingest_provenance_jobs", "ingest_provenance_records",
          "ingest_provenance_state", "permissions_v2_protection_state")


def capture(service, name, ids):
    """A private native capture holding exactly these owner-sent ROWIDs, as the recovery writes it."""
    keep = ",".join(str(i) for i in ids)

    def mutate(db):
        db.execute("UPDATE message SET is_from_me=1")
        db.execute(f"DELETE FROM message WHERE ROWID NOT IN ({keep})")
        db.execute(f"DELETE FROM chat_message_join WHERE message_id NOT IN ({keep})")
    data = snapshot(count=max(ids), mutate=mutate)
    path = service.root / (name + ".db")
    path.write_bytes(data)
    path.chmod(0o400)
    return name, data


def add_canonical(conn, data):
    columns = [r[1] for r in conn.execute("PRAGMA table_info(conversation_messages)")]
    for native in parse_reconciliation_snapshot(data, now=NOW):
        if conn.execute("SELECT 1 FROM conversation_messages WHERE message_id=?", (native.message_id,)).fetchone():
            continue
        row = {"message_id": native.message_id, "source_record_id": native.message_id, "source_id": "imessage",
               "dataset_id": DATASET, "owner_user_id": None, "conversation_id": native.conversation_id,
               "content": native.content, "event_at": native.event_at, "is_from_self": 1, "sender_id": "self",
               "sender_type": "human", "actor_role": None,
               "metadata_json": json.dumps({"message_guid": native.message_guid, "chat_guid": native.chat_guid,
                                            "chat_identifier": native.chat_identifier, "associated_message_type": 0})}
        conn.execute("INSERT INTO conversation_messages VALUES(" + ",".join("?" for _ in columns) + ")",
                     [row.get(column) for column in columns])
    conn.commit()


def describe(service, conn, name):
    with owner():
        return service.describe_snapshot(conn, snapshot_id=name, reader_contract=ATTRIBUTED_CONTRACT)


@pytest.fixture
def store(ingest_fixture):
    service, conn, _ = ingest_fixture
    conn.execute("CREATE TABLE ai_chat_messages(message_id TEXT,content TEXT)")
    name, data = capture(service, "capture-a", [1, 2])
    add_canonical(conn, data)
    desc = describe(service, conn, name)
    with owner():
        enrollment = service.enroll(conn, snapshot_id=name, dataset_id=DATASET, snapshot_sha256=desc["snapshot_sha256"],
                                    owner_attestation=OWNER_ATTESTATION, reader_contract=ATTRIBUTED_CONTRACT)
    return service, conn, enrollment["enrollment_id"]


def publish(store, **kwargs):
    service, conn, enrollment = store
    with owner():
        return publish_existing(service, conn, enrollment_id=enrollment, **kwargs)


def refresh(store, name, **kwargs):
    service, conn, _ = store
    desc = describe(service, conn, name)
    with owner(**kwargs.pop("principal", {})):
        return refresh_existing(service, conn, dataset_id=kwargs.pop("dataset_id", DATASET), snapshot_id=name,
                                snapshot_sha256=desc["snapshot_sha256"], owner_attestation=OWNER_ATTESTATION, **kwargs)


def proven(store, message_id):
    service, conn, _ = store
    try:
        validate_existing(service, conn, message_id=message_id, dataset_id=DATASET)
        return True
    except PolicyError:
        return False


def ledger(conn):
    return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in LEDGER}


def test_F1_reproves_links_new_rows_and_drops_unproven(store):
    service, conn, enrollment = store
    publish(store)
    name, data = capture(service, "capture-b", [2, 3])
    add_canonical(conn, data)
    assert [proven(store, f"imessage:{i}") for i in (1, 2, 3)] == [True, True, False]
    counts = refresh(store, name)
    assert counts == {"dropped_unproven": 1, "linked_new": 1, "previous_capture_removed": 1, "reproven": 1}
    assert [proven(store, f"imessage:{i}") for i in (1, 2, 3)] == [False, True, True]
    row = conn.execute("SELECT enrollment_id, revision, state, snapshot_json FROM ingest_provenance_enrollments").fetchall()
    assert [(r[0], r[1], r[2], json.loads(r[3])["snapshot_id"]) for r in row] == [(enrollment, 2, "active", "capture-b")]
    jobs = conn.execute("SELECT enrollment_revision, status FROM ingest_provenance_jobs").fetchall()
    assert [tuple(job) for job in jobs] == [(2, "done")]
    links = conn.execute("SELECT message_id, enrollment_revision FROM ingest_provenance_records ORDER BY 1").fetchall()
    assert [tuple(link) for link in links] == [("imessage:2", 2), ("imessage:3", 2)]
    assert not (service.root / "capture-a.db").exists() and (service.root / "capture-b.db").exists()


def test_F2_rows_source_and_opaque_inputs_unchanged_and_clock_moves_once(store):
    service, conn, _ = store
    publish(store)
    name, data = capture(service, "capture-b", [1, 2])
    rows = conn.execute("SELECT * FROM conversation_messages ORDER BY 1").fetchall()
    sources = conn.execute("SELECT * FROM user_ingestion_sources").fetchall()
    clock = conn.execute("SELECT generation FROM permissions_v2_protection_state").fetchone()[0]
    assert refresh(store, name)["reproven"] == 2
    # Every input of an opaque record id -- table, source, dataset, record id -- is read from these rows.
    assert conn.execute("SELECT * FROM conversation_messages ORDER BY 1").fetchall() == rows
    assert conn.execute("SELECT * FROM user_ingestion_sources").fetchall() == sources
    assert conn.execute("SELECT generation FROM permissions_v2_protection_state").fetchone()[0] == clock + 1


def test_F3_a_stale_enrollment_is_brought_current(store):
    service, conn, _ = store
    publish(store)
    conn.execute("UPDATE user_ingestion_sources SET posture='personal'")
    conn.commit()
    assert not proven(store, "imessage:1")
    name, _ = capture(service, "capture-b", [1, 2])
    assert refresh(store, name)["reproven"] == 2
    assert proven(store, "imessage:1") and proven(store, "imessage:2")


def test_F3_a_revoked_enrollment_is_never_refreshed(store):
    service, conn, enrollment = store
    publish(store)
    with owner():
        service.revoke(conn, enrollment_id=enrollment)
    name, _ = capture(service, "capture-b", [1, 2])
    before = ledger(conn)
    with pytest.raises(PolicyError, match="reconciliation_enrollment_revoked"):
        refresh(store, name)
    assert ledger(conn) == before and not proven(store, "imessage:1")


def test_F3_a_disabled_source_refuses(store):
    service, conn, _ = store
    publish(store)
    conn.execute("UPDATE user_ingestion_sources SET enabled=0")
    conn.commit()
    name, _ = capture(service, "capture-b", [1, 2])
    before = ledger(conn)
    with pytest.raises(PolicyError, match="ingest_source_disabled"):
        refresh(store, name)
    assert ledger(conn) == before


def test_F4_ceiling_carried_only_for_unchanged_rows(store):
    service, conn, _ = store
    ceiling = classification({"domains": ["work", "health"], "sensitivity": "special"})
    publish(store, classifications={"imessage:1": ceiling})
    name, _ = capture(service, "capture-b", [1, 2])
    counts = refresh(store, name)
    assert counts["reproven"] == 2 and counts["ceiling_carried"] == 1
    identity = json.loads(conn.execute("SELECT row_identity FROM ingest_provenance_records WHERE message_id='imessage:1'").fetchone()[0])
    assert identity["classification"] == ceiling
    # Still an exact native match, but a changed reviewed surface: the ceiling saw other text.
    conn.execute("UPDATE conversation_messages SET actor_role='authored' WHERE message_id='imessage:1'")
    conn.commit()
    name, _ = capture(service, "capture-c", [1, 2])
    counts = refresh(store, name)
    assert counts["reproven_row_changed"] == 1 and "ceiling_carried" not in counts
    identity = json.loads(conn.execute("SELECT row_identity FROM ingest_provenance_records WHERE message_id='imessage:1'").fetchone()[0])
    assert identity["classification"] is None and proven(store, "imessage:1")


def test_F5_a_mismatch_leaves_everything_as_it_was(store):
    service, conn, _ = store
    publish(store)
    name, _ = capture(service, "capture-b", [1, 2])
    conn.execute("UPDATE conversation_messages SET content='changed' WHERE message_id='imessage:2'")
    conn.commit()
    before, marker = ledger(conn), service.marker.read_bytes()
    with pytest.raises(PolicyError, match="reconciliation_content_mismatch"):
        refresh(store, name)
    assert ledger(conn) == before and service.marker.read_bytes() == marker
    assert proven(store, "imessage:1") and (service.root / "capture-a.db").exists()


def test_F6_only_the_owner_and_only_a_new_capture_of_an_enrolled_dataset(store):
    service, conn, _ = store
    publish(store)
    name, _ = capture(service, "capture-b", [1, 2])
    before = ledger(conn)
    with pytest.raises(PolicyError):
        refresh(store, name, principal={"actor": "another"})
    with pytest.raises(PolicyError, match="reconciliation_refresh_unchanged"):
        refresh(store, "capture-a")
    with pytest.raises(PolicyError, match="reconciliation_refresh_unenrolled"):
        refresh(store, name, dataset_id="another-dataset")
    assert ledger(conn) == before


def test_F6_window_start_splits_aged_out_links_from_unproven_ones(store):
    service, conn, _ = store
    publish(store)
    name, _ = capture(service, "capture-b", [2])
    event = conn.execute("SELECT event_at FROM conversation_messages WHERE message_id='imessage:1'").fetchone()[0]
    counts = refresh(store, name, window_start_us=canonical_utc_microseconds(event) + 1)
    assert counts["dropped_before_window"] == 1 and "dropped_unproven" not in counts


def test_F7_only_an_unnamed_capture_is_discarded(store):
    service, conn, _ = store
    publish(store)
    named = describe(service, conn, "capture-a")
    assert discard_capture(service, conn, named) is False and (service.root / "capture-a.db").exists()
    name, _ = capture(service, "capture-b", [1, 2])
    unnamed = describe(service, conn, name)
    assert discard_capture(service, conn, unnamed) is True and not (service.root / "capture-b.db").exists()


@pytest.fixture
def door(monkeypatch):
    from fastapi import FastAPI
    from topos.api.permissions_native_probe import router
    from topos.permissions_v2 import runtime
    calls = []
    monkeypatch.setattr(runtime, "get_runtime", lambda: calls.append(True) or (_ for _ in ()).throw(AssertionError))
    app = FastAPI()
    app.include_router(router)
    return app, calls


BODY = {"dataset_id": DATASET, "starts_at": "2023-03-01T00:00:00.000000+00:00",
        "ends_at": "2023-03-09T00:00:00.000000+00:00", "owner_attestation": OWNER_ATTESTATION}


@pytest.mark.parametrize("channel", ["local_http", "cp_relay"])
def test_F8_refresh_is_not_a_remote_owner_or_recipient_operation(door, channel):
    from fastapi.testclient import TestClient
    from topos.auth import resolve_request_principal
    from topos.principal import OWNER_APP, Principal
    app, calls = door
    app.dependency_overrides[resolve_request_principal] = lambda: Principal(OWNER_APP, channel, acting_user="owner-1")
    with TestClient(app) as client:
        response = client.post("/v1/permissions-beta/v2/imessage/refresh", json=BODY)
    assert response.status_code == 403 and calls == []


def test_F8_a_wrong_attestation_is_refused_before_anything_runs(door):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    app, calls = door
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post("/v1/permissions-beta/v2/imessage/refresh", json=BODY | {"owner_attestation": "yes"})
    assert response.status_code == 422 and calls == []


def test_F8_resync_reports_counts_and_never_raises(monkeypatch):
    from contextlib import contextmanager
    from topos.api import permissions_native_probe as door_module
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED", "true")
    synced = []

    @contextmanager
    def transaction():
        yield "ledger-connection"
    index = SimpleNamespace(sweep=lambda: 0, rebuild_all=lambda: {"g1": "ready", "g2": "stale"})
    node = SimpleNamespace(protocol=SimpleNamespace(ledger=SimpleNamespace(_transaction=transaction),
                                                    _sync_protection=synced.append),
                           message_search_index=lambda: index)
    assert door_module._resync_search(node) == {"protection_synced": True, "grants": 2, "ready": 1}
    assert synced == ["ledger-connection"]
    node.protocol._sync_protection = lambda _conn: (_ for _ in ()).throw(RuntimeError("closed"))
    assert door_module._resync_search(node) == {"protection_synced": False, "grants": 0, "ready": 0}


def test_F6_the_attestation_sentence_is_required(store):
    service, conn, _ = store
    publish(store)
    name, _ = capture(service, "capture-b", [1, 2])
    desc = describe(service, conn, name)
    before = ledger(conn)
    with owner(), pytest.raises(PolicyError, match="ingest_owner_attestation_required"):
        refresh_existing(service, conn, dataset_id=DATASET, snapshot_id=name, snapshot_sha256=desc["snapshot_sha256"],
                         owner_attestation="I agree")
    assert ledger(conn) == before


def test_F7_a_capture_leaves_out_rows_another_enrollment_proves(store, tmp_path, monkeypatch):
    from topos.permissions_v2 import native_imessage_probe as probe
    service, conn, _ = store
    native = tmp_path / "native-chat.db"
    _, data = capture(service, "native-source", [1, 2])
    native.write_bytes(data)
    (service.root / "native-source.db").unlink()
    actual = probe.probe_native_messages
    monkeypatch.setattr(probe, "probe_native_messages", lambda canonical, **kw: actual(canonical, **kw, _native_path=native))
    reader = sqlite3.connect(service.resolver.path.as_uri() + "?mode=ro", uri=True)
    reader.row_factory = sqlite3.Row
    reader.execute("PRAGMA query_only=ON")
    reader.execute("BEGIN")
    try:
        name, measured = probe.capture_matching_snapshot(reader, snapshot_root=service.root, dataset_id=DATASET,
            owner_id="owner-1", starts_at="2023-03-01T00:00:00.000000+00:00", ends_at="2023-03-09T00:00:00.000000+00:00",
            now=datetime(2023, 3, 9, tzinfo=timezone.utc),
            skip=lambda message_id: "row_owned_elsewhere" if message_id == "imessage:1" else None)
    finally:
        reader.close()
    parsed = parse_reconciliation_snapshot((service.root / (name + ".db")).read_bytes(), now=NOW, reader_contract=ATTRIBUTED_CONTRACT)
    assert [record.message_id for record in parsed] == ["imessage:2"]
    assert measured["counts"]["excluded_row_owned_elsewhere"] == 1 and measured["counts"]["canonical_exact_match"] == 2


def test_F6_a_window_that_would_drop_a_young_link_is_refused(store):
    service, conn, _ = store
    publish(store)
    name, _ = capture(service, "capture-b", [2])
    event = conn.execute("SELECT event_at FROM conversation_messages WHERE message_id='imessage:1'").fetchone()[0]
    event_us = canonical_utc_microseconds(event)
    before = ledger(conn)
    # Two days after the message: a 30-day grant could still release it, so the late window is refused.
    with pytest.raises(PolicyError, match="reconciliation_refresh_window_too_short"):
        refresh(store, name, window_start_us=event_us + 1, now_seconds=event_us // 1_000_000 + 2 * 86400)
    assert ledger(conn) == before and proven(store, "imessage:1")
    # Forty days after it, the same window only drops a link no 30-day grant can release.
    counts = refresh(store, name, window_start_us=event_us + 1, now_seconds=event_us // 1_000_000 + 40 * 86400)
    assert counts["dropped_before_window"] == 1


@pytest.fixture
def owner_door(store, tmp_path, monkeypatch):
    """The real route over the real service and a synthetic native database; only the runtime is a stand-in."""
    from contextlib import contextmanager
    from fastapi import FastAPI
    from topos.api.permissions_native_probe import router
    from topos.permissions_v2 import native_imessage_probe as probe, runtime
    service, conn, _ = store
    publish(store)
    native = tmp_path / "native-chat.db"
    _, data = capture(service, "native-source", [1, 2, 3])
    native.write_bytes(data)
    (service.root / "native-source.db").unlink()
    add_canonical(conn, data)
    actual = probe.probe_native_messages
    monkeypatch.setattr(probe, "probe_native_messages", lambda canonical, **kw: actual(canonical, **kw, _native_path=native))
    synced = []

    @contextmanager
    def ledger_transaction():
        yield "ledger"

    def connect():
        opened = sqlite3.connect(service.resolver.path.as_uri() + "?mode=rw", uri=True, timeout=30)
        opened.row_factory = sqlite3.Row
        return opened
    node = SimpleNamespace(ingestion=lambda: service, ingestion_connection=connect,
                           protocol=SimpleNamespace(ledger=SimpleNamespace(identity=SimpleNamespace(owner_id="owner-1"),
                                                                           _transaction=ledger_transaction),
                                                    _sync_protection=synced.append))
    monkeypatch.setattr(runtime, "get_runtime", lambda: node)
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(router)
    return app, synced


def test_F9_the_owner_door_refreshes_end_to_end(owner_door, store):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    app, synced = owner_door
    service, conn, _ = store
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post("/v1/permissions-beta/v2/imessage/refresh", json=BODY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authority_created"] is True and body["counts"]["canonical_exact_match"] == 3
    assert body["refresh"] == {"linked_new": 1, "previous_capture_removed": 1, "reproven": 2}
    assert body["search"] == {"protection_synced": True, "grants": 0, "ready": 0} and synced == ["ledger"]
    assert all(proven(store, f"imessage:{i}") for i in (1, 2, 3))
    assert response.headers["cache-control"] == "no-store"


def test_F9_a_failed_refresh_changes_nothing_and_leaves_no_capture(owner_door, store, monkeypatch):
    from fastapi.testclient import TestClient
    from topos.permissions_v2 import reconciliation_provenance
    from topos.uds import UDSChannelApp
    app, synced = owner_door
    service, conn, _ = store
    real, calls = reconciliation_provenance.compare_existing_message, []

    def flaky(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise PolicyError("reconciliation_content_mismatch")
        return real(*args, **kwargs)
    monkeypatch.setattr(reconciliation_provenance, "compare_existing_message", flaky)
    before, files = ledger(conn), sorted(path.name for path in service.root.iterdir())
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post("/v1/permissions-beta/v2/imessage/refresh", json=BODY)
    assert response.status_code == 503 and response.json()["detail"] == "reconciliation_content_mismatch"
    assert ledger(conn) == before and synced == []
    assert sorted(path.name for path in service.root.iterdir()) == files
    assert proven(store, "imessage:1") and not proven(store, "imessage:3")

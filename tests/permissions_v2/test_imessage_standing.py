"""The owner's standing attestation for their own iMessages (owner decision 1, 1 Oct 2026).

  S1  the record is private, exact and atomic; anything else refuses
  S2  the native accounts: the account columns of sent rows in the window, counts only out of the process
  S3  preview, statement, disarm: the owner's own, exactly what they saw, through the owner's channels only
  S4  a second Apple ID on the same Mac never reads as the owner: a sent row from an account the owner did not
      attest refuses the whole run and writes nothing; a row with no account is never proven by this path
  S5  every iMessage dataset that holds the owner's rows is enrolled, each capture checked exactly first
  S6  every enrollment is refreshed: a dry run, then the same capture; no refresh that would change nothing; no
      loss is ever acknowledged
  S7  the standing principal passes the owner check only where the caller allows it, and no request carries it
  S8  one run at a time with the owner's own recovery and refresh; a record of another owner refuses
  S9  when a run is due without a sync; the scheduler's hook after a settled sync never raises

Every fixture is synthetic: accounts are invented strings, never phone-number shaped.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_imessage_reconciliation import snapshot
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401
from topos.permissions_v2 import imessage_standing as standing
from topos.permissions_v2 import native_imessage_probe as probe
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.imessage_reconciliation import FORMS_CONTRACT, parse_reconciliation_snapshot
from topos.permissions_v2.reconciliation_provenance import validate_existing
from topos.principal import OWNER_APP, THIRD_PARTY, Principal, current_principal

DAY = 86_400
OWNER_ACCOUNT = ("e:owner@example.invalid", "synthetic-account-guid-owner")
SECOND_ACCOUNT = ("e:second@example.invalid", "synthetic-account-guid-second")
DATASET, OTHER = "native-dataset", "other-dataset"


# -- fixtures ------------------------------------------------------------------------------------------------

def native_ns(days_ago, now):
    return (int(now) - 978307200 - int(days_ago * DAY)) * 1_000_000_000 + 123_456_000


def native_db(path, rows, *, now=None, columns=("account", "account_guid")):
    """chat.db with owner-sent ROWIDs 1..n: rows maps ROWID -> (days ago, (account, account_guid) or None)."""
    now = int(time.time()) if now is None else now

    def mutate(db):
        for column in columns:
            db.execute(f'ALTER TABLE message ADD COLUMN "{column}" TEXT')
        db.execute("UPDATE message SET is_from_me=1")
        for rowid, (days_ago, account) in rows.items():
            db.execute("UPDATE message SET date=? WHERE ROWID=?", (native_ns(days_ago, now), rowid))
            if account is not None:
                for column, value in zip(("account", "account_guid"), account):
                    if column in columns:
                        db.execute(f'UPDATE message SET "{column}"=? WHERE ROWID=?', (value, rowid))
    data = snapshot(count=max(rows), mutate=mutate)
    path.write_bytes(data)
    return data


def add_canonical(conn, data, datasets):
    """Canonical rows for the native rows, each in the dataset `datasets` names for its ROWID (default DATASET)."""
    columns = [r[1] for r in conn.execute("PRAGMA table_info(conversation_messages)")]
    for native in parse_reconciliation_snapshot(data, now=datetime.now(timezone.utc)):
        rowid = int(native.message_id.split(":")[1])
        row = {"message_id": native.message_id, "source_record_id": native.message_id, "source_id": "imessage",
               "dataset_id": datasets.get(rowid, DATASET), "owner_user_id": None,
               "conversation_id": native.conversation_id, "content": native.content, "event_at": native.event_at,
               "is_from_self": 1, "sender_id": "self", "sender_type": "human", "actor_role": None,
               "metadata_json": json.dumps({"message_guid": native.message_guid, "chat_guid": native.chat_guid,
                                            "chat_identifier": native.chat_identifier, "associated_message_type": 0})}
        conn.execute("INSERT OR REPLACE INTO conversation_messages VALUES(" + ",".join("?" for _ in columns) + ")",
                     [row.get(column) for column in columns])
    conn.commit()


class Node:
    """The runtime the standing module reads: the real ingest service and canonical database, a stand-in ledger."""

    def __init__(self, service, *, owner_id="owner-1"):
        self.service = service
        self.synced = []

        @contextmanager
        def transaction():
            db = sqlite3.connect(":memory:")
            try:
                db.execute("CREATE TABLE p2a_grants(grant_id TEXT PRIMARY KEY)")
                yield db
            finally:
                db.close()
        ledger = SimpleNamespace(identity=SimpleNamespace(owner_id=owner_id), _transaction=transaction,
                                 _authority=lambda *_: (_ for _ in ()).throw(PolicyError("grant_inactive")))
        self.protocol = SimpleNamespace(canonical_database=service.resolver.path, ledger=ledger,
                                        _sync_protection=lambda _db: self.synced.append(True))

    def ingestion(self):
        return self.service

    def ingestion_connection(self):
        conn = sqlite3.connect(self.service.resolver.path.as_uri() + "?mode=rw", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn


@pytest.fixture
def node(ingest_fixture, tmp_path, monkeypatch):
    service, conn, _ = ingest_fixture
    conn.execute("CREATE TABLE IF NOT EXISTS ai_chat_messages(message_id TEXT,content TEXT)")
    conn.commit()
    native = tmp_path / "native-chat.db"
    actual = probe.probe_native_messages
    monkeypatch.setattr(probe, "probe_native_messages", lambda canonical, **kw: actual(canonical, **kw, _native_path=native))
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED", raising=False)
    return SimpleNamespace(runtime=Node(service), service=service, conn=conn, native=native)


def setup_rows(node, rows, datasets=None):
    data = native_db(node.native, rows)
    add_canonical(node.conn, data, datasets or {})
    return data


def as_owner(channel="uds", actor="owner-1"):
    return owner(actor=actor, channel=channel)


def armed(node, **kwargs):
    with as_owner():
        token = standing.preview(node.runtime, native_path=node.native)["accounts_token"]
        return standing.arm(node.runtime, statement=standing.STANDING_STATEMENT, accounts_token=token,
                            native_path=node.native, **kwargs)


def run(node, **kwargs):
    return standing.maintain(node.runtime, reason="test", native_path=node.native, **kwargs)


def links(conn):
    return sorted(row[0] for row in conn.execute("SELECT message_id FROM ingest_provenance_records"))


def ledger_state(conn):
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND "
                                             "(name GLOB 'ingest_provenance_*' OR name='permissions_v2_protection_state') "
                                             "ORDER BY 1")]
    return {table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables}


def proven(node, message_id, dataset=DATASET):
    try:
        validate_existing(node.service, node.conn, message_id=message_id, dataset_id=dataset)
        return True
    except PolicyError:
        return False


def record(node):
    return standing.read_record(standing.record_path(node.runtime))


# -- S1: the record ------------------------------------------------------------------------------------------

def test_S1_the_record_round_trips_privately_and_atomically(tmp_path):
    directory = tmp_path / "permissions-v2"
    directory.mkdir(mode=0o700)
    path = directory / standing.RECORD_NAME
    assert standing.read_record(path) is None
    value = {"version": standing.STANDING_VERSION, "state": "previewed", "owner_id": "owner-1",
             "key": "ab" * 32, "accounts": []}
    standing.write_record(path, value)
    assert standing.read_record(path) == value
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and sorted(p.name for p in directory.iterdir()) == [path.name]


def valid_record():
    return {"version": standing.STANDING_VERSION, "state": "armed", "owner_id": "owner-1", "key": "ab" * 32,
            "accounts": ["cd" * 32]}


@pytest.mark.parametrize("change", [
    {"version": "imessage-standing-attestation/v0"}, {"state": "on"}, {"owner_id": ""}, {"key": "zz" * 32},
    {"key": "ab" * 31}, {"accounts": ["CD" * 32]}, {"accounts": ["cd" * 32, "cd" * 32]},
    {"accounts": ["ef" * 32, "cd" * 32]}, {"accounts": []}, {"accounts": "cd" * 32},
])
def test_S1_a_record_that_is_not_exactly_valid_refuses(tmp_path, change):
    directory = tmp_path / "permissions-v2"
    directory.mkdir(mode=0o700)
    path = directory / standing.RECORD_NAME
    path.write_text(json.dumps({**valid_record(), **change}))
    path.chmod(0o600)
    with pytest.raises(PolicyError, match="standing_record_invalid"):
        standing.read_record(path)


@pytest.mark.parametrize("tamper", ["group_readable", "symlink", "hard_link", "oversized", "not_json",
                                    "open_directory"])
def test_S1_a_record_that_is_not_private_or_not_a_record_refuses(tmp_path, tamper):
    directory = tmp_path / "permissions-v2"
    directory.mkdir(mode=0o700)
    path = directory / standing.RECORD_NAME
    standing.write_record(path, valid_record())
    if tamper == "group_readable":
        path.chmod(0o640)
    elif tamper == "symlink":
        target = directory / "elsewhere.json"
        os.replace(path, target)
        path.symlink_to(target)
    elif tamper == "hard_link":
        os.link(path, directory / "second-name.json")
    elif tamper == "oversized":
        path.write_text(json.dumps({**valid_record(), "padding": "x" * 70_000}))
    elif tamper == "not_json":
        path.write_text("{")
    else:
        directory.chmod(0o755)
    with pytest.raises(PolicyError, match="standing_record_invalid"):
        standing.read_record(path)


# -- S2: the native accounts ---------------------------------------------------------------------------------

def window_us(days=30):
    now = int(time.time())
    return {"starts_us": (now - days * DAY) * 1_000_000, "ends_us": (now + 1) * 1_000_000}


def test_S2_the_account_columns_of_sent_rows_in_the_window(tmp_path):
    native = tmp_path / "chat.db"
    native_db(native, {1: (3, OWNER_ACCOUNT), 2: (5, (OWNER_ACCOUNT[0], "")), 3: (7, None), 4: (45, SECOND_ACCOUNT)})
    found = standing.native_accounts(native_path=native, **window_us())
    assert found == {1: frozenset({("account", OWNER_ACCOUNT[0]), ("account_guid", OWNER_ACCOUNT[1])}),
                     2: frozenset({("account", OWNER_ACCOUNT[0])}), 3: frozenset()}


def test_S2_one_account_column_is_enough_and_none_cannot_be_attested(tmp_path):
    native = tmp_path / "chat.db"
    native_db(native, {1: (3, OWNER_ACCOUNT)}, columns=("account_guid",))
    assert standing.native_accounts(native_path=native, **window_us()) == {
        1: frozenset({("account_guid", OWNER_ACCOUNT[1])})}
    native_db(native, {1: (3, None)}, columns=())
    with pytest.raises(PolicyError, match="standing_account_unavailable"):
        standing.native_accounts(native_path=native, **window_us())


def test_S2_an_unreadable_or_unbounded_native_database_refuses(tmp_path, monkeypatch):
    with pytest.raises(PolicyError, match="native_probe_unavailable"):
        standing.native_accounts(native_path=tmp_path / "missing.db", **window_us())
    native = tmp_path / "chat.db"
    native_db(native, {1: (3, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT)})
    monkeypatch.setattr(standing, "_MAX_SENT_ROWS", 1)
    with pytest.raises(PolicyError, match="native_probe_message_limit"):
        standing.native_accounts(native_path=native, **window_us())
    with pytest.raises(PolicyError, match="native_probe_window_invalid"):
        standing.native_accounts(native_path=native, starts_us=5, ends_us=5)


# -- S3: preview, statement, disarm --------------------------------------------------------------------------

def test_S3_the_preview_shows_counts_and_never_an_identifier(node):
    setup_rows(node, {1: (3, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT), 3: (5, SECOND_ACCOUNT), 4: (6, None)})
    with as_owner():
        shown = standing.preview(node.runtime, native_path=node.native)
    assert (shown["accounts"], shown["sent_messages"], shown["sent_without_account"]) == (2, [2, 1], 1)
    assert shown["state"] == "off" and shown["statement"] == standing.STANDING_STATEMENT
    text = json.dumps(shown)
    assert all(value not in text for value in OWNER_ACCOUNT + SECOND_ACCOUNT)
    assert all(value not in standing.record_path(node.runtime).read_text() for value in OWNER_ACCOUNT + SECOND_ACCOUNT)
    assert record(node)["state"] == "previewed" and not links(node.conn) if node.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='ingest_provenance_records'").fetchone() else True


def test_S3_the_statement_arms_exactly_the_accounts_the_owner_saw(node):
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    with as_owner():
        token = standing.preview(node.runtime, native_path=node.native)["accounts_token"]
        with pytest.raises(PolicyError, match="standing_statement_required"):
            standing.arm(node.runtime, statement="yes", accounts_token=token, native_path=node.native)
        with pytest.raises(PolicyError, match="standing_accounts_changed"):
            standing.arm(node.runtime, statement=standing.STANDING_STATEMENT, accounts_token="0" * 64,
                         native_path=node.native)
        # A second account signs in between the preview and the statement: the owner did not see it.
        native_db(node.native, {1: (3, OWNER_ACCOUNT), 2: (2, SECOND_ACCOUNT)})
        with pytest.raises(PolicyError, match="standing_accounts_changed"):
            standing.arm(node.runtime, statement=standing.STANDING_STATEMENT, accounts_token=token,
                         native_path=node.native)
        assert record(node)["state"] == "previewed"
        native_db(node.native, {1: (3, OWNER_ACCOUNT)})
        result = standing.arm(node.runtime, statement=standing.STANDING_STATEMENT, accounts_token=token,
                              native_path=node.native)
    assert result["state"] == "armed" and result["accounts"] == 1 and result["last_run"] is None
    kept = record(node)
    assert kept["state"] == "armed" and len(kept["accounts"]) == 2 and "preview" not in kept
    assert kept["channel"] == "uds" and type(kept["attested_at"]) is int


def test_S3_a_statement_needs_a_preview_first(node):
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    with as_owner(), pytest.raises(PolicyError, match="standing_preview_required"):
        standing.arm(node.runtime, statement=standing.STANDING_STATEMENT, accounts_token="0" * 64,
                     native_path=node.native)


def test_S3_disarm_stops_the_runs_and_revokes_nothing(node):
    setup_rows(node, {1: (3, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT)})
    armed(node)
    assert run(node)["outcome"] == "ok" and links(node.conn) == ["imessage:1", "imessage:2"]
    with as_owner():
        assert standing.disarm(node.runtime)["state"] == "off"
    assert run(node) == {"ran": False, "reason": "not_armed"}
    assert proven(node, "imessage:1") and proven(node, "imessage:2")


@pytest.mark.parametrize("principal", [
    None, Principal(THIRD_PARTY, "uds"), Principal(OWNER_APP, "local_http", acting_user="owner-1"),
    Principal(OWNER_APP, "cp_relay", acting_user="another-owner"), Principal(OWNER_APP, "cp_relay"),
    Principal(OWNER_APP, standing.STANDING_CHANNEL, acting_user="owner-1"),
])
def test_S3_only_the_owner_on_the_owners_channels_previews_states_or_disarms(node, principal):
    from topos.principal import reset_principal, set_principal
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    token = set_principal(principal)
    try:
        for call in (lambda: standing.preview(node.runtime, native_path=node.native),
                     lambda: standing.arm(node.runtime, statement=standing.STANDING_STATEMENT, accounts_token="0" * 64,
                                          native_path=node.native),
                     lambda: standing.disarm(node.runtime)):
            with pytest.raises(PolicyError, match="owner_authority_required"):
                call()
    finally:
        reset_principal(token)
    assert record(node) is None


def test_S3_the_owner_through_the_relay_can_state(node):
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    with as_owner(channel="cp_relay"):
        token = standing.preview(node.runtime, native_path=node.native)["accounts_token"]
        assert standing.arm(node.runtime, statement=standing.STANDING_STATEMENT, accounts_token=token,
                            native_path=node.native)["state"] == "armed"
    assert record(node)["channel"] == "cp_relay"


# -- S4: a second Apple ID never reads as the owner ----------------------------------------------------------

@pytest.mark.parametrize("foreign", [
    SECOND_ACCOUNT,                                  # another Apple ID signed in to Messages on this Mac
    (OWNER_ACCOUNT[0], SECOND_ACCOUNT[1]),           # the owner's address under another account
    (SECOND_ACCOUNT[0], OWNER_ACCOUNT[1]),           # another address under the owner's account object
])
def test_S4_a_sent_row_from_an_unattested_account_refuses_the_whole_run(node, foreign):
    setup_rows(node, {1: (3, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT)})
    armed(node)
    data = native_db(node.native, {1: (3, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT), 3: (2, foreign)})
    add_canonical(node.conn, data, {})
    before = ledger_state(node.conn)
    result = run(node)
    assert result["outcome"] == "refused" and result["refusal"] == "standing_account_unattested"
    assert result["unattested_rows"] == 1
    assert ledger_state(node.conn) == before and node.runtime.synced == []
    assert record(node)["last_run"]["refusal"] == "standing_account_unattested"
    assert not any(value in json.dumps(record(node)) for value in OWNER_ACCOUNT + SECOND_ACCOUNT)


def test_S4_after_the_owner_states_again_over_both_accounts_both_are_proven(node):
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    armed(node)
    setup_rows(node, {1: (3, OWNER_ACCOUNT), 2: (2, SECOND_ACCOUNT)})
    assert run(node)["refusal"] == "standing_account_unattested"
    armed(node)
    assert run(node)["outcome"] == "ok" and links(node.conn) == ["imessage:1", "imessage:2"]


def test_S4_a_sent_row_with_no_account_is_never_proven_by_this_path(node):
    setup_rows(node, {1: (3, OWNER_ACCOUNT), 2: (4, None), 3: (5, OWNER_ACCOUNT)})
    armed(node)
    result = run(node)
    assert result["outcome"] == "ok" and result["rows_without_account"] == 1
    assert result["datasets"][DATASET]["counts"]["excluded_account_unknown"] == 1
    assert links(node.conn) == ["imessage:1", "imessage:3"] and not proven(node, "imessage:2")


def test_S4_a_row_that_appears_after_the_account_scan_is_unknown_and_left_out(node, monkeypatch):
    setup_rows(node, {1: (3, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT)})
    armed(node)
    scan = standing.native_accounts
    monkeypatch.setattr(standing, "native_accounts", lambda **kw: {k: v for k, v in scan(**kw).items() if k != 2})
    run(node)
    assert links(node.conn) == ["imessage:1"]


# -- S5: every owner dataset is enrolled ---------------------------------------------------------------------

def test_S5_every_dataset_holding_the_owners_rows_is_enrolled_and_published(node):
    setup_rows(node, {1: (3, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT), 3: (5, OWNER_ACCOUNT)}, datasets={3: OTHER})
    armed(node)
    result = run(node)
    assert result["outcome"] == "ok", result
    assert result["datasets"][DATASET]["enrolled"] == 1 and result["datasets"][DATASET]["linked_new"] == 2
    assert result["datasets"][OTHER]["enrolled"] == 1 and result["datasets"][OTHER]["linked_new"] == 1
    assert proven(node, "imessage:1") and proven(node, "imessage:2") and proven(node, "imessage:3", OTHER)
    rows = node.conn.execute("SELECT dataset_id, channel, json_extract(snapshot_json,'$.reader_contract') "
                             "FROM ingest_provenance_enrollments ORDER BY 1").fetchall()
    assert [tuple(row) for row in rows] == [(DATASET, standing.STANDING_CHANNEL, FORMS_CONTRACT),
                                            (OTHER, standing.STANDING_CHANNEL, FORMS_CONTRACT)]
    assert node.runtime.synced == [True] and result["search"]["protection_synced"] is True
    assert node.conn.execute("SELECT json_extract(row_identity,'$.classification') FROM ingest_provenance_records "
                             "WHERE message_id='imessage:1'").fetchone()[0] is None


def test_S5_a_capture_that_does_not_compare_exactly_is_never_enrolled(node, monkeypatch):
    from topos.permissions_v2 import imessage_reconciliation
    setup_rows(node, {1: (3, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT)})
    armed(node)
    monkeypatch.setattr(imessage_reconciliation, "compare_existing_message",
                        lambda *a, **k: (_ for _ in ()).throw(PolicyError("reconciliation_content_mismatch")))
    files = sorted(p.name for p in node.service.root.iterdir())
    result = run(node)
    assert result["datasets"][DATASET] == {"refused": "reconciliation_content_mismatch"}
    assert node.conn.execute("SELECT 1 FROM sqlite_master WHERE name='ingest_provenance_enrollments'").fetchone() is None \
        or node.conn.execute("SELECT count(*) FROM ingest_provenance_enrollments").fetchone()[0] == 0
    assert sorted(p.name for p in node.service.root.iterdir()) == files


def test_S5_a_disabled_dataset_is_skipped_and_a_revoked_one_is_never_enrolled_again(node):
    setup_rows(node, {1: (3, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT)}, datasets={2: OTHER})
    node.conn.execute("INSERT INTO user_ingestion_sources VALUES(?,?,0,NULL)", (OTHER, "imessage"))
    node.conn.commit()
    armed(node)
    result = run(node)
    assert result["datasets"][OTHER] == {"skipped": "source_disabled"} and result["datasets"][DATASET]["enrolled"] == 1
    enrollment = node.conn.execute("SELECT enrollment_id FROM ingest_provenance_enrollments").fetchone()[0]
    with owner():
        node.service.revoke(node.conn, enrollment_id=enrollment)
    assert run(node)["datasets"][DATASET] == {"skipped": "revoked"}


def test_S5_an_enrollment_of_the_snapshot_lane_is_left_alone(node, monkeypatch):
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    node.conn.execute("CREATE TABLE IF NOT EXISTS ingest_provenance_enrollments (enrollment_id TEXT PRIMARY KEY, "
                      "snapshot_json TEXT NOT NULL, dataset_id TEXT NOT NULL UNIQUE, revision INTEGER, state TEXT, "
                      "source_generation INTEGER, attestation TEXT, authorized_at INTEGER, channel TEXT)")
    monkeypatch.setattr(standing, "owner_datasets", lambda conn, owner_id: [DATASET])
    armed(node)
    from topos.permissions_v2 import ingest_provenance
    monkeypatch.setattr(ingest_provenance, "_read_json", lambda value: {"reader_contract": "imessage-owner-snapshot/v1"})
    node.conn.execute("INSERT INTO ingest_provenance_enrollments VALUES('e1','{}',?,1,'active',0,'a',0,'uds')", (DATASET,))
    node.conn.commit()
    assert run(node)["datasets"][DATASET] == {"skipped": "another_lane"}


# -- S6: every enrollment is refreshed -----------------------------------------------------------------------

def test_S6_new_rows_are_linked_by_a_dry_run_then_the_same_capture(node, monkeypatch):
    from topos.permissions_v2 import reconciliation_provenance
    setup_rows(node, {1: (5, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT)})
    armed(node)
    run(node)
    setup_rows(node, {1: (5, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT), 3: (1, OWNER_ACCOUNT)})
    calls = []
    real = reconciliation_provenance.refresh_existing

    def watched(*args, **kwargs):
        calls.append((kwargs["dry_run"] if "dry_run" in kwargs else False, kwargs["snapshot_id"],
                      kwargs.get("accept_uncovered", False), kwargs.get("accept_unproven", False)))
        return real(*args, **kwargs)
    monkeypatch.setattr(reconciliation_provenance, "refresh_existing", watched)
    result = run(node)
    assert result["outcome"] == "ok", result
    assert [call[0] for call in calls] == [True, False] and calls[0][1] == calls[1][1]
    assert not any(call[2] or call[3] for call in calls)
    assert result["datasets"][DATASET]["refresh"]["linked_new"] == 1
    assert links(node.conn) == ["imessage:1", "imessage:2", "imessage:3"] and proven(node, "imessage:3")
    assert node.conn.execute("SELECT channel FROM ingest_provenance_enrollments").fetchone()[0] == standing.STANDING_CHANNEL


def test_S6_a_refresh_that_would_change_nothing_is_not_made(node):
    setup_rows(node, {1: (5, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT)})
    armed(node)
    run(node)
    before, files = ledger_state(node.conn), sorted(p.name for p in node.service.root.iterdir())
    result = run(node)
    assert result["datasets"][DATASET]["unchanged"] == 1 and "search" not in result
    assert ledger_state(node.conn) == before and sorted(p.name for p in node.service.root.iterdir()) == files


def test_S6_a_loss_is_never_acknowledged(node):
    """Two of three proven rows change natively: the dry run refuses as mass-unproven, nothing is retired, and the
    refusal is the owner's to read."""
    setup_rows(node, {1: (5, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT), 3: (3, OWNER_ACCOUNT)})
    armed(node)
    run(node)
    with sqlite3.connect(node.native) as db:
        db.execute("UPDATE message SET text='changed natively' WHERE ROWID IN (2,3)")
    before = ledger_state(node.conn)
    result = run(node)
    assert result["outcome"] == "refused"
    assert result["datasets"][DATASET] == {"refused": "reconciliation_refresh_mass_unproven"}
    assert ledger_state(node.conn) == before and all(proven(node, f"imessage:{i}") for i in (1, 2, 3))
    assert record(node)["last_run"]["datasets"][DATASET]["refused"] == "reconciliation_refresh_mass_unproven"


def test_S6_a_stale_enrollment_is_brought_current_even_when_nothing_else_changed(node):
    setup_rows(node, {1: (5, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT)})
    armed(node)
    run(node)
    node.conn.execute("UPDATE user_ingestion_sources SET posture='personal' WHERE dataset_id=?", (DATASET,))
    node.conn.commit()
    assert not proven(node, "imessage:1")
    result = run(node)
    assert "refresh" in result["datasets"][DATASET] and proven(node, "imessage:1")


# -- S7: the standing principal ------------------------------------------------------------------------------

def test_S7_the_two_names_of_the_standing_channel_agree():
    from topos.permissions_v2 import evidence
    assert evidence.STANDING_CHANNEL == standing.STANDING_CHANNEL


@pytest.mark.parametrize("kwargs", [{"accept_uncovered": True}, {"accept_unproven": True}])
def test_S7_the_standing_principal_never_acknowledges_a_loss(node, kwargs):
    from topos.permissions_v2.reconciliation_provenance import refresh_existing
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    armed(node)
    run(node)
    with standing.standing_principal("owner-1"), pytest.raises(PolicyError, match="owner_authority_required"):
        refresh_existing(node.service, node.conn, dataset_id=DATASET, snapshot_id="canary", snapshot_sha256="0" * 64,
                         owner_attestation="x", window_start_us=0, window_end_us=1, coverage_seconds=30 * DAY,
                         **kwargs)


def test_S7_the_standing_principal_never_derives_or_sets_a_ceiling(node):
    from topos.permissions_v2.reconciliation_provenance import publish_existing
    with standing.standing_principal("owner-1"):
        for kwargs in ({"derive": lambda conn, rows: None}, {"classifications": {"imessage:1": {"x": 1}}}):
            with pytest.raises(PolicyError, match="owner_authority_required"):
                publish_existing(node.service, node.conn, enrollment_id="ingest-enrollment-0", **kwargs)


def test_S7_the_standing_principal_enrolls_only_the_existing_row_lane(node):
    with standing.standing_principal("owner-1"), pytest.raises(PolicyError, match="owner_authority_required"):
        node.service.enroll(node.conn, snapshot_id="canary", dataset_id=DATASET, snapshot_sha256="0" * 64,
                            owner_attestation="x")


def test_S7_the_standing_principal_passes_only_where_the_caller_allows_it():
    from topos.permissions_v2.evidence import EvidenceBinding, _owner
    binding = EvidenceBinding(environment_id="permissions-beta-test", node_id="node-1", resource_id="resource-1",
                              owner_id="owner-1")
    with standing.standing_principal("owner-1"):
        with pytest.raises(PolicyError, match="owner_authority_required"):
            _owner(binding)
        _owner(binding, standing=True)
    with standing.standing_principal("another-owner"), pytest.raises(PolicyError, match="owner_authority_required"):
        _owner(binding, standing=True)
    assert current_principal() is None


def test_S7_under_the_standing_principal_revocation_and_the_snapshot_lane_refuse(node):
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    armed(node)
    run(node)
    enrollment = node.conn.execute("SELECT enrollment_id FROM ingest_provenance_enrollments").fetchone()[0]
    with standing.standing_principal("owner-1"):
        for call in (lambda: node.service.revoke(node.conn, enrollment_id=enrollment),
                     lambda: node.service.enqueue(node.conn, enrollment_id=enrollment),
                     lambda: node.service.describe_snapshot(node.conn, snapshot_id="canary")):
            with pytest.raises(PolicyError, match="owner_authority_required"):
                call()


def test_S7_no_request_resolves_to_the_standing_principal():
    from fastapi import FastAPI, Depends
    from fastapi.testclient import TestClient
    from topos.auth import resolve_request_principal
    from topos.uds import UDSChannelApp
    app = FastAPI()

    @app.get("/who")
    def who(principal=Depends(resolve_request_principal)):
        return {"channel": principal.channel if principal else None}
    with TestClient(UDSChannelApp(app)) as client:
        response = client.get("/who", headers={"X-Channel": standing.STANDING_CHANNEL})
    assert response.json() == {"channel": "uds"}


def test_S7_a_run_leaves_no_principal_behind(node):
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    armed(node)
    run(node)
    assert current_principal() is None


# -- S8: one run at a time; the owner's own record -----------------------------------------------------------

def test_S8_a_run_waits_for_the_owners_own_recovery_or_refresh(node):
    from topos.api.permissions_native_probe import _RECOVERY_LOCK
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    armed(node)
    assert _RECOVERY_LOCK.acquire(blocking=False)
    try:
        assert run(node) == {"ran": False, "reason": "busy"}
    finally:
        _RECOVERY_LOCK.release()
    assert record(node)["last_run"] is None
    assert run(node)["outcome"] == "ok" and _RECOVERY_LOCK.acquire(blocking=False)
    _RECOVERY_LOCK.release()


def test_S8_a_record_of_another_owner_refuses(node):
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    armed(node)
    node.runtime.protocol.ledger.identity.owner_id = "another-owner"
    result = run(node)
    assert result["refusal"] == "standing_owner_changed" and not node.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='ingest_provenance_records'").fetchone()
    assert standing.status(node.runtime)["state"] == "off"


def test_S8_a_refused_native_read_is_recorded_and_retried_within_the_hour(node, monkeypatch):
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    armed(node)
    node.native.unlink()
    result = run(node)
    assert result["refusal"] == "native_probe_unavailable"
    kept = record(node)
    assert not standing.due(kept, kept["last_run"]["at"] + standing.RETRY_SECONDS - 1)
    assert standing.due(kept, kept["last_run"]["at"] + standing.RETRY_SECONDS)


# -- S9: when a run is due; the scheduler's hooks ------------------------------------------------------------

def test_S9_due_after_the_statement_weekly_and_never_when_not_armed():
    record_value = {**valid_record(), "attested_at": 1_000, "last_run": None}
    assert standing.due(record_value, 1_000)
    ran = {**record_value, "last_run": {"at": 2_000, "outcome": "ok"}}
    assert not standing.due(ran, 2_000 + standing.CADENCE_SECONDS - 1) and standing.due(ran, 2_000 + standing.CADENCE_SECONDS)
    assert standing.due({**ran, "attested_at": 3_000}, 3_000)
    assert not standing.due({**ran, "state": "disarmed"}, 10**9) and not standing.due(None, 10**9)


def test_S9_the_hooks_never_raise_when_permissions_are_off(monkeypatch):
    from topos.permissions_v2 import runtime
    monkeypatch.setattr(runtime, "get_runtime", lambda: (_ for _ in ()).throw(PolicyError("permissions_v2_disabled")))
    assert standing.after_scheduled_sync("dataset-synthetic", "imported") == {"ran": False,
                                                                              "reason": "permissions_v2_disabled"}
    assert standing.run_if_due() == {"ran": False, "reason": "permissions_v2_disabled"}
    assert standing.after_scheduled_sync("dataset-synthetic", "up_to_date") == {"ran": False,
                                                                                "reason": "nothing_imported"}


def test_S9_the_hooks_run_against_the_live_runtime(node, monkeypatch):
    from topos.permissions_v2 import runtime
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    armed(node)
    monkeypatch.setattr(runtime, "get_runtime", lambda: node.runtime)
    monkeypatch.setattr(standing, "maintain", lambda rt, **kw: {"ran": True, "reason": kw["reason"], "rt": rt})
    assert standing.after_scheduled_sync(DATASET, "imported")["reason"] == "scheduled_sync"
    assert standing.run_if_due()["reason"] == "due"


def test_S9_a_settled_imessage_sync_hands_over_and_never_raises(tmp_path, monkeypatch):
    from topos.ingestion import local_sync_schedule as schedule
    calls = []
    monkeypatch.setattr(standing, "after_scheduled_sync", lambda dataset, outcome: calls.append((dataset, outcome))
                        or {"ran": True, "outcome": "ok"})
    schedule._prove_after_sync("dataset-synthetic", "imported")
    monkeypatch.setattr(standing, "after_scheduled_sync",
                        lambda dataset, outcome: (_ for _ in ()).throw(RuntimeError("synthetic")))
    schedule._prove_after_sync("dataset-synthetic", "imported")  # logged, not raised
    monkeypatch.setattr(standing, "run_if_due", lambda: (_ for _ in ()).throw(RuntimeError("synthetic")))
    assert schedule._prove_when_due() == {"ran": False, "reason": "RuntimeError"}
    assert calls == [("dataset-synthetic", "imported")]


def test_S9_the_tick_settles_an_imessage_run_then_hands_it_over(tmp_path, monkeypatch):
    from topos.ingestion import local_sync_schedule as schedule
    from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up
    conn = sqlite3.connect(tmp_path / "node.db")
    apply_pipeline_jobs_v1_up(conn)
    calls = []
    monkeypatch.setattr(standing, "after_scheduled_sync", lambda dataset, outcome: calls.append((dataset, outcome)) or {})
    stored = {"dataset_id": "dataset-synthetic", "source_id": "imessage", "last_status": "running",
              "last_job_id": "job-1"}
    monkeypatch.setattr(schedule, "_record_run", lambda *a, **k: None)
    conn.execute("INSERT INTO pipeline_jobs (job_id, kind, status, payload_json, detail_json, created_at, updated_at) "
                 "VALUES ('job-1', 'local_sync', 'done', '{}', ?, datetime('now'), datetime('now'))",
                 (json.dumps({"status": "ok", "sync": {"outcome": "imported", "records_processed": 3}}),))
    conn.commit()
    schedule._settle_running(conn, stored, datetime.now(timezone.utc))
    schedule._settle_running(conn, {**stored, "source_id": "signal"}, datetime.now(timezone.utc))
    assert calls == [("dataset-synthetic", "imported")]


# -- S10: the product surface ---------------------------------------------------------------------------------

def handler_call(handler, payload, principal):
    import asyncio
    from topos.principal import reset_principal, set_principal
    token = set_principal(principal)
    try:
        return asyncio.run(handler({"id": "r", "payload": {"source_id": "imessage", "dataset_id": DATASET, **payload}}))
    finally:
        reset_principal(token)


@pytest.fixture
def surface(node, monkeypatch):
    import topos.core.handlers as hub
    from topos.permissions_v2 import runtime
    monkeypatch.setattr(hub, "get_db_connection", lambda: node.conn)
    monkeypatch.setattr(runtime, "get_runtime", lambda: node.runtime)
    scan = standing.native_accounts
    # Never the owner's Messages database: every read here is of the synthetic one.
    monkeypatch.setattr(standing, "native_accounts", lambda **kw: scan(**{**kw, "native_path": node.native}))
    setup_rows(node, {1: (3, OWNER_ACCOUNT)})
    return node


def test_S10_the_owner_states_through_the_settings_surface(surface):
    from topos.core.handlers.sources import handle_get_source_settings, handle_put_source_settings
    owner_principal = Principal(OWNER_APP, "cp_relay", acting_user="owner-1")
    shown = handler_call(handle_put_source_settings, {"proof_standing": {"action": "preview"}}, owner_principal)
    assert shown["status"] == "ok", shown
    preview = shown["payload"]["proof_standing"]
    assert preview["accounts"] == 1 and all(value not in json.dumps(shown) for value in OWNER_ACCOUNT)
    stated = handler_call(handle_put_source_settings, {"proof_standing": {
        "action": "arm", "statement": preview["statement"], "accounts_token": preview["accounts_token"]}}, owner_principal)
    assert stated["payload"]["proof_standing"]["state"] == "armed"
    read = handler_call(handle_get_source_settings, {}, owner_principal)
    assert read["payload"]["proof_standing"]["state"] == "armed"
    off = handler_call(handle_put_source_settings, {"proof_standing": {"action": "disarm"}}, owner_principal)
    assert off["payload"]["proof_standing"]["state"] == "off"


def test_S10_the_surface_is_the_owners_alone(surface):
    from topos.core.handlers.sources import handle_get_source_settings, handle_put_source_settings
    stranger = Principal(THIRD_PARTY, "local_http")
    refused = handler_call(handle_put_source_settings, {"proof_standing": {"action": "preview"}}, stranger)
    assert refused["status"] == "error" and refused.get("code") == 403
    assert "proof_standing" not in handler_call(handle_get_source_settings, {}, stranger)["payload"]
    assert record(surface) is None


@pytest.mark.parametrize("payload,error", [
    ({"proof_standing": {"action": "everything"}}, "proof_standing.action"),
    ({"proof_standing": "arm"}, "proof_standing.action"),
])
def test_S10_a_malformed_request_is_refused(surface, payload, error):
    from topos.core.handlers.sources import handle_put_source_settings
    result = handler_call(handle_put_source_settings, payload, Principal(OWNER_APP, "uds"))
    assert result["status"] == "error" and error in result["error"]


def test_S10_a_refusal_comes_back_as_a_code(surface):
    from topos.core.handlers.sources import handle_put_source_settings
    result = handler_call(handle_put_source_settings, {"proof_standing": {"action": "arm", "statement": "yes"}},
                          Principal(OWNER_APP, "uds"))
    assert result["payload"]["proof_standing"] == {"error": "standing_statement_required"}


def test_S3_a_preview_with_no_account_on_any_sent_row_refuses(node):
    setup_rows(node, {1: (3, None), 2: (4, None)})
    with as_owner(), pytest.raises(PolicyError, match="standing_account_unavailable"):
        standing.preview(node.runtime, native_path=node.native)
    assert record(node) is None


def test_S6_a_v2_enrollment_is_moved_to_v3_even_when_it_would_only_reprove(node):
    """The owner's recovery made a v2 enrollment (revision 1); the first standing run refreshes it into v3."""
    from topos.permissions_v2.imessage_reconciliation import ATTRIBUTED_CONTRACT
    from topos.permissions_v2.ingest_provenance import OWNER_ATTESTATION
    from topos.permissions_v2.reconciliation_provenance import publish_existing
    data = setup_rows(node, {1: (5, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT)})
    name = "capture-v2"
    path = node.service.root / (name + ".db")
    path.write_bytes(data)
    path.chmod(0o400)
    with owner():
        desc = node.service.describe_snapshot(node.conn, snapshot_id=name, reader_contract=ATTRIBUTED_CONTRACT)
        enrollment = node.service.enroll(node.conn, snapshot_id=name, dataset_id=DATASET,
                                         snapshot_sha256=desc["snapshot_sha256"], owner_attestation=OWNER_ATTESTATION,
                                         reader_contract=ATTRIBUTED_CONTRACT)
        publish_existing(node.service, node.conn, enrollment_id=enrollment["enrollment_id"])
    armed(node)
    result = run(node)
    assert result["datasets"][DATASET]["refresh"]["reproven"] == 2, result
    assert node.conn.execute("SELECT json_extract(snapshot_json,'$.reader_contract') FROM ingest_provenance_enrollments"
                             ).fetchone()[0] == FORMS_CONTRACT


@pytest.mark.asyncio
async def test_S9_the_schedulers_tick_asks_whether_a_run_is_due(tmp_path, monkeypatch):
    import asyncio
    from topos.ingestion import local_sync_schedule as schedule
    asked = []
    monkeypatch.setattr(schedule, "_prove_when_due", lambda: asked.append(True) or {"ran": False})
    monkeypatch.setattr(schedule, "run_schedule_tick", lambda conn: {"enqueued": 0})
    task = asyncio.create_task(schedule.run_scheduler_loop(lambda: None, tick_seconds=3600, startup_delay_seconds=0))
    try:
        for _ in range(200):
            if asked:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert asked == [True]


def test_S5_a_dataset_with_nothing_provable_in_the_window_is_skipped_not_refused(node):
    setup_rows(node, {1: (3, OWNER_ACCOUNT), 2: (45, OWNER_ACCOUNT)}, datasets={2: OTHER})
    armed(node)
    result = run(node)
    assert result["outcome"] == "ok" and result["datasets"][OTHER] == {"skipped": "nothing_to_prove"}
    assert result["datasets"][DATASET]["enrolled"] == 1


def test_S5_a_row_another_enrollment_proves_stays_its_own(node):
    """A row linked by one dataset's enrollment that is now filed in another dataset is left out of that dataset's
    capture (`excluded_row_owned_elsewhere`): one message, one link."""
    setup_rows(node, {1: (5, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT), 3: (3, OWNER_ACCOUNT)})
    armed(node)
    assert run(node)["datasets"][DATASET]["linked_new"] == 3
    node.conn.execute("UPDATE conversation_messages SET dataset_id=? WHERE message_id='imessage:3'", (OTHER,))
    node.conn.commit()
    data = native_db(node.native, {1: (5, OWNER_ACCOUNT), 2: (4, OWNER_ACCOUNT), 3: (3, OWNER_ACCOUNT),
                                   4: (2, OWNER_ACCOUNT)})
    add_canonical(node.conn, data, {3: OTHER, 4: OTHER})
    result = run(node)
    other = result["datasets"][OTHER]
    assert other["enrolled"] == 1 and other["linked_new"] == 1, result
    assert other["counts"]["excluded_row_owned_elsewhere"] == 1
    owners = dict(node.conn.execute("SELECT r.message_id, e.dataset_id FROM ingest_provenance_records r "
                                    "JOIN ingest_provenance_enrollments e USING(enrollment_id)").fetchall())
    assert owners["imessage:3"] == DATASET and owners["imessage:4"] == OTHER

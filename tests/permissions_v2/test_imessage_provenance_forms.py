"""RD12: the existing-row comparison reads two more forms of the owner's own sent text (reader contract v3).

- G1  Messages' chain from a message to the one before it (`reply_to_guid`) is not a form of the message: v3 reads
      the row as an ordinary message, counts it, and never captures the chain; v1 and v2 still refuse it
- G2  an inline reply is the owner's own text: v3 reads it with the thread it answers, and the comparison requires
      the stored row to name the same originator and part; a reply whose two fields do not fit together rejects
- G3  nothing else widened: reactions, forwards and quotes, subjects, attachments, system, deleted and spam rows
      reject under v3 as under v2; the exact body and the exact native time are still required
- G4  a v3 capture names its reader: the ledger reads v2 and v3 enrollments, a first recovery enrolls v3, a refresh
      moves an enrollment to v3 in its one transaction, and the same capture bytes are no refresh whichever label
      the enrollment carries
- G5  a capture holding a reply cannot be enrolled as v2, and an older reader refuses it whole (fail closed)
- G6  the sync's enrolled-dataset guard counts a recovery enrollment (v2 or v3), not only the snapshot lane's
- G7  count-only: the forms the reader used to refuse, and a body the sync stored without its surrounding whitespace
- G8  an attachment's caption is the owner's words: v3 reads a sent attachment that has one and matches it to the
      stored caption exactly as the sync stores it (no placeholder, so nothing about the attachment can be
      released); a stored body that kept the placeholder, a different body, or an attachment with no caption
      never matches; v1 and v2 still refuse every attachment

Every fixture is synthetic. Captures are dated from the clock as each test runs.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from tests.ingestion.test_owner_snapshot import NOW
from tests.permissions_v2.test_imessage_reconciliation import sample, snapshot
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401
from tests.permissions_v2.test_native_imessage_probe import (  # noqa: F401
    ARGS, canonical_as_ingested, files, form_buckets, full_native, run, set_native)
from tests.permissions_v2.test_reconciliation_refresh import (
    DATASET, DAY, describe, links, proven, publish, recent_window)
from topos.ingestion import local_sync
from topos.ingestion.imessage_attributed_text import caption_text
from topos.ingestion.owner_snapshot import SnapshotRejected, thread_reply
from topos.permissions_v2 import imessage_reconciliation, ingest_provenance
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.imessage_reconciliation import (
    ATTRIBUTED_CONTRACT, CONTRACT, FORMS_CONTRACT, RECONCILIATION_CONTRACTS, NativeMessage, compare_existing_message,
    parse_reconciliation_snapshot)
from topos.permissions_v2.ingest_protocol import IMESSAGE_READER_CONTRACT
from topos.permissions_v2.ingest_provenance import OWNER_ATTESTATION
from topos.permissions_v2.reconciliation_provenance import (
    ExistingProvenancePass, publish_existing, refresh_existing, validate_existing)

ORIGINATOR, PART = "synthetic-message-0", "0:0:3"
THREAD_COLUMNS = ("thread_originator_guid", "thread_originator_part", "reply_to_guid")


def forms_snapshot(*, count=2, replies=None, pointers=None, captions=None, mutate=None, owner_sent=False):
    """The synthetic native snapshot with the three thread columns, inline replies, chain pointers and attachments
    (`captions`: ROWID -> the native `text`, placeholders included) set."""
    def adapt(db):
        for rowid, text in (captions or {}).items():
            db.execute("UPDATE message SET cache_has_attachments=1, text=? WHERE ROWID=?", (text, rowid))
        for column in THREAD_COLUMNS:
            db.execute(f'ALTER TABLE message ADD COLUMN "{column}" TEXT')
        for rowid, (guid, part) in (replies or {}).items():
            db.execute("UPDATE message SET thread_originator_guid=?, thread_originator_part=? WHERE ROWID=?",
                       (guid, part, rowid))
        for rowid, pointer in (pointers or {}).items():
            db.execute("UPDATE message SET reply_to_guid=? WHERE ROWID=?", (pointer, rowid))
        if owner_sent:
            db.execute("UPDATE message SET is_from_me=1")
        if mutate:
            mutate(db)
    return snapshot(count=count, mutate=adapt)


def parse(data, contract=FORMS_CONTRACT, now=NOW):
    return parse_reconciliation_snapshot(data, now=now, reader_contract=contract)


def thread_of(record):
    return record.thread_originator_guid, record.thread_originator_part


# -- G1, G2: what v3 reads ------------------------------------------------------------------------

def test_G1_the_chain_to_the_preceding_message_is_not_a_form_under_v3():
    data = forms_snapshot(pointers={1: ORIGINATOR, 2: "synthetic-message-1"})
    for contract in (CONTRACT, ATTRIBUTED_CONTRACT):
        with pytest.raises(SnapshotRejected, match="snapshot_message_form_unsupported"):
            parse(data, contract)
    records = parse(data)
    assert [record.message_id for record in records] == ["imessage:1", "imessage:2"]
    assert all(thread_of(record) == (None, None) and record.reader_contract == FORMS_CONTRACT for record in records)
    assert all("reply_to_guid" not in repr(record) and ORIGINATOR not in repr(record) for record in records)


def test_G2_an_inline_reply_is_read_with_the_thread_it_answers():
    data = forms_snapshot(replies={1: (ORIGINATOR, PART)})
    with pytest.raises(SnapshotRejected, match="snapshot_message_form_unsupported"):
        parse(data, ATTRIBUTED_CONTRACT)
    reply, plain = parse(data)
    assert thread_of(reply) == (ORIGINATOR, PART) and thread_of(plain) == (None, None)
    assert reply.content == "Synthetic message 1" and reply.is_from_self is True
    # A reply without a part, and empty strings for both: read as a reply without a part, and as no thread.
    [without_part, _] = parse(forms_snapshot(replies={1: (ORIGINATOR, None)}))
    assert thread_of(without_part) == (ORIGINATOR, None)
    [emptied, _] = parse(forms_snapshot(replies={1: ("", "")}))
    assert thread_of(emptied) == (None, None)


@pytest.mark.parametrize("guid,part", [
    (None, PART), ("", PART),                       # a part with no originator
    ("two\nlines", None), (" padded", None), ("x" * 513, None),  # not an identifier
    (ORIGINATOR, "bad\tpart"), (ORIGINATOR, "y" * 513),
])
def test_G2_thread_fields_that_are_not_a_reply_reject_the_whole_snapshot(guid, part):
    with pytest.raises(SnapshotRejected, match="snapshot_message_form_unsupported"):
        parse(forms_snapshot(replies={1: (guid, part)}))
    with pytest.raises(SnapshotRejected, match="snapshot_message_form_unsupported"):
        thread_reply(guid, part)


def test_G2_the_shared_reader_rule_is_the_capture_rule():
    assert thread_reply(None, None) == (None, None) and thread_reply("", "") == (None, None)
    assert thread_reply(ORIGINATOR, PART) == (ORIGINATOR, PART)
    assert thread_reply(ORIGINATOR, "") == (ORIGINATOR, None) and thread_reply(ORIGINATOR, None) == (ORIGINATOR, None)


# -- G3: nothing else widened -----------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "UPDATE message SET associated_message_guid='p:0/synthetic' WHERE ROWID=1",
    "UPDATE message SET associated_message_type=2000 WHERE ROWID=1",
    # An attachment flag other than 0 or 1, and an attachment that is only placeholders (no caption).
    "UPDATE message SET cache_has_attachments=2 WHERE ROWID=1",
    "UPDATE message SET cache_has_attachments=1, text='\ufffc' WHERE ROWID=1",
    "UPDATE message SET subject='Synthetic subject' WHERE ROWID=1",
    "UPDATE message SET item_type=1 WHERE ROWID=1",
    "UPDATE message SET attributedBody=x'0102' WHERE ROWID=1",
])
def test_G3_v3_still_rejects_every_other_unsupported_form(sql):
    with pytest.raises(SnapshotRejected):
        parse(forms_snapshot(mutate=lambda db: db.execute(sql)))


@pytest.mark.parametrize("column,value", [
    ("group_action_type", 1), ("is_forwarded", 1), ("is_forward", 1), ("is_spam", 1), ("is_deleted", 1),
    ("is_system_message", 1), ("is_service_message", 1),
    ("quoted_message_guid", "earlier-message"), ("forwarded_from", "somebody"),
])
def test_G3_v3_keeps_every_other_native_restriction_column(column, value):
    def mutate(db):
        db.execute(f'ALTER TABLE message ADD COLUMN "{column}"')
        db.execute(f'UPDATE message SET "{column}"=? WHERE ROWID=1', (value,))
    with pytest.raises(SnapshotRejected, match="snapshot_message_form_unsupported"):
        parse(forms_snapshot(mutate=mutate))


@pytest.mark.parametrize("contract", ["imessage-existing-comparison/v4", "imessage-owner-snapshot/v1", None, 3, [FORMS_CONTRACT]])
def test_G3_an_unknown_reader_is_refused(contract):
    with pytest.raises(SnapshotRejected, match="snapshot_reader_unsupported"):
        parse(forms_snapshot(), contract)


# -- the comparison -----------------------------------------------------------------------------------

def legacy_event_at(native):
    """The event time the sync stored: Apple's nanoseconds through the old float conversion, at microseconds."""
    converted = datetime.fromtimestamp(float(native.native_event_nanoseconds) / 1_000_000_000.0 + 978307200, tz=timezone.utc)
    return converted.isoformat(timespec="microseconds")


def reply_row(native):
    """The canonical row the sync writes for a native inline reply: the originator as reply_to_message_id and both
    thread fields in the metadata, beside the identity metadata every row carries."""
    row, _ = sample()
    row.update(message_id=native.message_id, source_record_id=native.message_id, conversation_id=native.conversation_id,
               content=native.content, event_at=legacy_event_at(native))
    metadata = {"message_guid": native.message_guid, "chat_guid": native.chat_guid,
                "chat_identifier": native.chat_identifier, "associated_message_type": 0}
    if native.thread_originator_guid is not None:
        row["reply_to_message_id"] = native.thread_originator_guid
        metadata["thread_originator_guid"] = native.thread_originator_guid
        if native.thread_originator_part is not None:
            metadata["thread_originator_part"] = native.thread_originator_part
    row["metadata_json"] = json.dumps(metadata)
    return row


def compare(row, native):
    return compare_existing_message(row, native, dataset_id="dataset-native", owner_id="owner-synthetic")


def with_metadata(row, **changes):
    metadata = json.loads(row["metadata_json"])
    for key, value in changes.items():
        if value is ...:
            metadata.pop(key, None)
        else:
            metadata[key] = value
    return {**row, "metadata_json": json.dumps(metadata)}


def test_G1_an_ordinary_v3_row_matches_exactly_what_v2_matches():
    data = forms_snapshot(pointers={1: ORIGINATOR})
    [native, _] = parse(data)
    row = reply_row(native)
    matched = compare(row, native)
    assert matched.contract == FORMS_CONTRACT and matched.snapshot_sha256 == native.snapshot_sha256
    # The same stored row, under the v2 reader of the same rows without the chain column: the same revision.
    [v2, _] = parse(forms_snapshot(), ATTRIBUTED_CONTRACT)
    assert compare(row, v2).canonical_revision == matched.canonical_revision
    assert compare(row, v2).contract == ATTRIBUTED_CONTRACT


def test_G2_a_reply_matches_a_stored_row_that_names_the_same_thread():
    [native, _] = parse(forms_snapshot(replies={1: (ORIGINATOR, PART)}))
    row = reply_row(native)
    assert row["reply_to_message_id"] == ORIGINATOR
    matched = compare(row, native)
    assert matched.contract == FORMS_CONTRACT and len(matched.canonical_revision) == 64
    [without_part, _] = parse(forms_snapshot(replies={1: (ORIGINATOR, None)}))
    assert compare(reply_row(without_part), without_part).contract == FORMS_CONTRACT


@pytest.mark.parametrize("change", [
    {"reply_to_message_id": None}, {"reply_to_message_id": ""}, {"reply_to_message_id": "another-message"},
    {"reply_to_message_id": 7}, {"reply_to_message_id": ORIGINATOR.upper()},
])
def test_G2_a_stored_row_that_names_another_thread_or_none_refuses(change):
    [native, _] = parse(forms_snapshot(replies={1: (ORIGINATOR, PART)}))
    with pytest.raises(PolicyError, match="reconciliation_message_form"):
        compare({**reply_row(native), **change}, native)


@pytest.mark.parametrize("metadata", [
    {"thread_originator_guid": ...}, {"thread_originator_guid": "another-message"}, {"thread_originator_guid": ""},
    {"thread_originator_part": ...}, {"thread_originator_part": "0:0:4"}, {"thread_originator_part": 3},
    {"associated_message_guid": "p:0/synthetic"}, {"associated_message_type": 2000},
])
def test_G2_stored_thread_metadata_must_agree_exactly(metadata):
    [native, _] = parse(forms_snapshot(replies={1: (ORIGINATOR, PART)}))
    with pytest.raises(PolicyError, match="reconciliation_message_form"):
        compare(with_metadata(reply_row(native), **metadata), native)


def test_G2_a_part_the_native_row_lacks_or_a_reply_the_native_row_is_not_refuses():
    [without_part, _] = parse(forms_snapshot(replies={1: (ORIGINATOR, None)}))
    with pytest.raises(PolicyError, match="reconciliation_message_form"):
        compare(with_metadata(reply_row(without_part), thread_originator_part=PART), without_part)
    [plain, _] = parse(forms_snapshot(pointers={1: ORIGINATOR}))
    [reply, _] = parse(forms_snapshot(replies={1: (ORIGINATOR, PART)}))
    with pytest.raises(PolicyError, match="reconciliation_message_form"):
        compare(reply_row(reply), plain)
    with pytest.raises(PolicyError, match="reconciliation_message_form"):
        compare(with_metadata(reply_row(plain), thread_originator_guid=ORIGINATOR), plain)


@pytest.mark.parametrize("field,value,reason", [
    ("content", "Synthetic message 1 ", "content_mismatch"),
    ("content", "synthetic message 1", "content_mismatch"),
    ("event_at", "2023-03-08T20:26:40.123457+00:00", "time_mismatch"),
    ("is_from_self", 0, "not_owner_sent"),
    ("dataset_id", "another-dataset", "source_binding"),
    ("message_type", "system", "message_form"),
])
def test_G3_the_exact_body_time_sender_and_binding_are_still_required_of_a_reply(field, value, reason):
    [native, _] = parse(forms_snapshot(replies={1: (ORIGINATOR, PART)}))
    with pytest.raises(PolicyError, match="reconciliation_" + reason):
        compare({**reply_row(native), field: value}, native)


def test_G3_v3_verifies_the_native_nanoseconds_as_v2_does():
    from dataclasses import replace
    [native, _] = parse(forms_snapshot(replies={1: (ORIGINATOR, PART)}))
    row = reply_row(native)
    assert compare(row, native)
    with pytest.raises(PolicyError, match="reconciliation_time_mismatch"):
        compare(row, replace(native, native_event_nanoseconds=None))
    with pytest.raises(PolicyError, match="reconciliation_time_mismatch"):
        compare(row, replace(native, native_event_nanoseconds=native.native_event_nanoseconds + 1_000_000_000))


@pytest.mark.parametrize("fields", [
    {"reader_contract": ATTRIBUTED_CONTRACT, "thread_originator_guid": ORIGINATOR},
    {"reader_contract": ATTRIBUTED_CONTRACT, "thread_originator_part": PART},
    {"reader_contract": CONTRACT, "thread_originator_guid": ORIGINATOR},
    {"thread_originator_guid": None, "thread_originator_part": PART},
    {"thread_originator_guid": "two\nlines"}, {"thread_originator_guid": " padded"},
    {"thread_originator_guid": ORIGINATOR, "thread_originator_part": "bad\tpart"},
    {"reader_contract": "imessage-existing-comparison/v4"},
])
def test_G3_a_native_observation_that_misstates_its_reader_or_thread_is_invalid(fields):
    from dataclasses import replace
    [native, _] = parse(forms_snapshot(replies={1: (ORIGINATOR, PART)}))
    with pytest.raises(PolicyError, match="reconciliation_(input_invalid|reader_unsupported)"):
        compare(reply_row(native), replace(native, **fields))


# -- the probe: what the owner's node reads from its Messages database -----------------------------

def canonical_with_thread(files, rowid, guid, part):
    """The stored row of an inline reply, as the sync writes it: the originator in reply_to_message_id and both
    thread fields in the metadata. The fixture's canonical table predates the column, as old nodes' did."""
    _, canonical = files
    with sqlite3.connect(canonical) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(conversation_messages)")}
        if "reply_to_message_id" not in columns:
            db.execute("ALTER TABLE conversation_messages ADD COLUMN reply_to_message_id TEXT")
        [(metadata,)] = db.execute("SELECT metadata_json FROM conversation_messages WHERE message_id=?",
                                   (f"imessage:{rowid}",)).fetchall()
        metadata = json.loads(metadata)
        if guid is None:
            metadata.pop("thread_originator_guid", None)
            metadata.pop("thread_originator_part", None)
        else:
            metadata["thread_originator_guid"] = guid
            if part is not None:
                metadata["thread_originator_part"] = part
        db.execute("UPDATE conversation_messages SET reply_to_message_id=?, metadata_json=? WHERE message_id=?",
                   (guid, json.dumps(metadata), f"imessage:{rowid}"))


def observed(counts):
    return {key: value for key, value in counts.items() if key.startswith("native_observed_")}


def test_G1_the_probe_matches_chained_rows_and_counts_them(files):
    native = full_native(files, count=3)
    canonical_as_ingested(files)
    set_native(native, {1: {"reply_to_guid": ORIGINATOR}, 2: {"reply_to_guid": "synthetic-message-1"}})
    captured = []
    counts = run(files, _on_match=lambda row, chat: captured.append(row))["counts"]
    assert counts["canonical_exact_match"] == 3 and counts["native_text_supported"] == 3
    assert "native_message_form_unsupported" not in counts and form_buckets(counts) == {}
    assert observed(counts) == {"native_observed_reply_pointer": 2, "native_observed_reply_pointer_exact_match": 2}
    # The chain is observed, never captured: no captured row carries it, and no row carries an observed column.
    assert len(captured) == 3 and all("reply_to_guid" not in row and not any(key.startswith("_observed_") for key in row)
                                      for row in captured)
    assert ORIGINATOR not in json.dumps(counts)


def test_G2_the_probe_matches_a_reply_stored_with_its_thread(files):
    native = full_native(files, count=3)
    canonical_as_ingested(files)
    set_native(native, {1: {"thread_originator_guid": ORIGINATOR, "thread_originator_part": PART},
                        2: {"thread_originator_guid": ORIGINATOR, "reply_to_guid": "synthetic-message-1"}})
    canonical_with_thread(files, 1, ORIGINATOR, PART)
    canonical_with_thread(files, 2, ORIGINATOR, None)
    captured = []
    counts = run(files, _on_match=lambda row, chat: captured.append(row))["counts"]
    assert counts["canonical_exact_match"] == 3 and "native_message_form_unsupported" not in counts
    # A row that is both a reply and chained counts as the reply.
    assert observed(counts) == {"native_observed_thread_reply": 2, "native_observed_thread_reply_exact_match": 2}
    by_id = {row["ROWID"]: row for row in captured}
    assert (by_id[1]["thread_originator_guid"], by_id[1]["thread_originator_part"]) == (ORIGINATOR, PART)
    assert (by_id[2]["thread_originator_guid"], by_id[2]["thread_originator_part"]) == (ORIGINATOR, None)
    assert by_id[3]["thread_originator_guid"] is None and "reply_to_guid" not in by_id[2]


@pytest.mark.parametrize("native_thread,stored_thread", [
    ((ORIGINATOR, PART), (None, None)),                 # a reply stored as an ordinary message
    ((None, None), (ORIGINATOR, PART)),                 # an ordinary message stored as a reply
    ((ORIGINATOR, PART), ("synthetic-message-1", PART)),  # a reply to another message
    ((ORIGINATOR, PART), (ORIGINATOR, "0:0:4")),        # another part of the originator
    ((ORIGINATOR, None), (ORIGINATOR, PART)),           # a part the native row lacks
])
def test_G2_the_probe_refuses_a_stored_thread_that_differs_from_the_native_one(files, native_thread, stored_thread):
    native = full_native(files, count=2)
    canonical_as_ingested(files)
    set_native(native, {1: {"thread_originator_guid": native_thread[0], "thread_originator_part": native_thread[1]}})
    canonical_with_thread(files, 1, *stored_thread)
    counts = run(files)["counts"]
    assert counts["reconciliation_message_form"] == 1 and counts["canonical_exact_match"] == 1
    assert "native_observed_thread_reply_exact_match" not in counts


def test_G3_the_probe_still_withholds_a_reaction_that_is_chained(files):
    native = full_native(files, count=2)
    canonical_as_ingested(files)
    set_native(native, {1: {"associated_message_type": 2000, "reply_to_guid": ORIGINATOR}})
    counts = run(files)["counts"]
    assert form_buckets(counts) == {"native_form_reaction": 1} and counts["canonical_exact_match"] == 1
    assert observed(counts) == {}


def test_G7_a_body_stored_without_its_surrounding_whitespace_is_counted_not_matched(files):
    native = full_native(files, count=4)
    canonical_as_ingested(files)
    set_native(native, {1: {"text": "Synthetic message 1 "}, 2: {"text": "\nSynthetic message 2\t"},
                        3: {"text": "changed natively"}})
    counts = run(files)["counts"]
    assert counts["reconciliation_content_mismatch"] == 3 and counts["canonical_exact_match"] == 1
    assert counts["native_observed_content_mismatch_whitespace"] == 2
    assert "Synthetic message" not in json.dumps(counts)


def test_G4_a_capture_keeps_the_thread_and_never_the_chain_and_an_older_reader_refuses_it_whole(files, tmp_path, monkeypatch):
    from topos.permissions_v2 import native_imessage_probe as probe
    native = full_native(files, count=3)
    canonical_as_ingested(files)
    set_native(native, {1: {"thread_originator_guid": ORIGINATOR, "thread_originator_part": PART},
                        2: {"reply_to_guid": "synthetic-message-1"}})
    canonical_with_thread(files, 1, ORIGINATOR, PART)
    root = tmp_path / "private" / "snapshots"
    root.parent.mkdir(mode=0o700)
    root.mkdir(mode=0o700)
    actual = probe.probe_native_messages
    monkeypatch.setattr(probe, "probe_native_messages", lambda conn, **kw: actual(conn, **kw, _native_path=native))
    with sqlite3.connect(files[1]) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        identifier, measured = probe.capture_matching_snapshot(conn, snapshot_root=root, **ARGS)
    data = (root / (identifier + ".db")).read_bytes()
    assert measured["counts"]["canonical_exact_match"] == 3
    records = parse(data, FORMS_CONTRACT, now=ARGS["now"])
    assert [thread_of(record) for record in records] == [(ORIGINATOR, PART), (None, None), (None, None)]
    with sqlite3.connect(root / (identifier + ".db")) as capture_db:
        columns = {row[1] for row in capture_db.execute("PRAGMA table_info(message)")}
    assert "reply_to_guid" not in columns and {"thread_originator_guid", "thread_originator_part"} <= columns
    assert not any(column.startswith("_observed_") for column in columns)
    # The reader before this one rejects a capture that holds a reply: a wheel without v3 withholds, never widens.
    with pytest.raises(SnapshotRejected, match="snapshot_message_form_unsupported"):
        parse(data, ATTRIBUTED_CONTRACT, now=ARGS["now"])


# -- G4, G5: the ledger -------------------------------------------------------------------------------

def native_ns(days_ago, anchor):
    """A native (2001-epoch) nanosecond clock `days_ago` before `anchor` (Unix seconds), microsecond-exact."""
    return (anchor - 978307200 - int(days_ago * DAY)) * 1_000_000_000 + 123_456_000


# One clock reading per store: every capture of a store dates a ROWID identically, so a later capture's rows
# match the rows an earlier one stored even when the second turns between the two.
_ANCHORS: dict[str, int] = {}


def capture_forms(service, name, ids, *, replies=None, pointers=None, captions=None):
    """A private native capture of these owner-sent ROWIDs, dated 14 - ROWID days ago, with replies and chains."""
    keep = ",".join(str(i) for i in ids)
    anchor = _ANCHORS.setdefault(str(service.root), int(time.time()))

    def mutate(db):
        for rowid in ids:
            db.execute("UPDATE message SET date=? WHERE ROWID=?", (native_ns(14 - rowid, anchor), rowid))
        db.execute(f"DELETE FROM message WHERE ROWID NOT IN ({keep})")
        db.execute(f"DELETE FROM chat_message_join WHERE message_id NOT IN ({keep})")
    data = forms_snapshot(count=max(ids), replies=replies, pointers=pointers, captions=captions, mutate=mutate,
                          owner_sent=True)
    path = service.root / (name + ".db")
    path.write_bytes(data)
    path.chmod(0o400)
    return name, data


def add_canonical_forms(conn, data, *, dataset=DATASET):
    """Store each captured row as the sync would, replies with their thread; rows already stored stay as they are."""
    columns = [r[1] for r in conn.execute("PRAGMA table_info(conversation_messages)")]
    for column in ("reply_to_message_id", "message_type", "event_type"):
        if column not in columns:
            conn.execute(f"ALTER TABLE conversation_messages ADD COLUMN {column} TEXT")
            columns.append(column)
    for native in parse(data, FORMS_CONTRACT, now=datetime.now(timezone.utc)):
        if conn.execute("SELECT 1 FROM conversation_messages WHERE message_id=?", (native.message_id,)).fetchone():
            continue
        row = {**reply_row(native), "dataset_id": dataset, "message_type": "message"}
        if native.attachment_caption:
            row["content"] = caption_text(native.content)  # the sync stores the caption alone
        conn.execute("INSERT INTO conversation_messages VALUES(" + ",".join("?" for _ in columns) + ")",
                     [row.get(column) for column in columns])
    conn.commit()


def make_forms_store(ingest_fixture, ids=(1, 2), *, replies=None, pointers=None, captions=None, contract=FORMS_CONTRACT):
    service, conn, _ = ingest_fixture
    conn.execute("CREATE TABLE IF NOT EXISTS ai_chat_messages(message_id TEXT,content TEXT)")
    name, data = capture_forms(service, "capture-a", list(ids), replies=replies, pointers=pointers, captions=captions)
    add_canonical_forms(conn, data)
    with owner():
        desc = service.describe_snapshot(conn, snapshot_id=name, reader_contract=contract)
        enrollment = service.enroll(conn, snapshot_id=name, dataset_id=DATASET, snapshot_sha256=desc["snapshot_sha256"],
                                    owner_attestation=OWNER_ATTESTATION, reader_contract=contract)
    return service, conn, enrollment["enrollment_id"]


def contract_of(conn):
    [(snapshot_json,)] = conn.execute("SELECT snapshot_json FROM ingest_provenance_enrollments").fetchall()
    return json.loads(snapshot_json)["reader_contract"]


REPLIES = {3: (ORIGINATOR, PART)}
POINTERS = {2: "synthetic-message-1"}


def test_G4_a_v3_enrollment_publishes_and_validates_replies_and_chained_rows(ingest_fixture):
    store = make_forms_store(ingest_fixture, ids=(1, 2, 3), replies=REPLIES, pointers=POINTERS)
    service, conn, _ = store
    assert publish(store)["reconciled"] == 3
    assert all(proven(store, f"imessage:{i}") for i in (1, 2, 3)) and contract_of(conn) == FORMS_CONTRACT
    evidence = validate_existing(service, conn, message_id="imessage:3", dataset_id=DATASET, with_classification=True)
    assert set(evidence) == {"_p2b_native_event_nanoseconds", "_p2b_native_classification"}
    # The link pins the thread: a stored row that comes to name another message no longer validates.
    conn.execute("UPDATE conversation_messages SET reply_to_message_id='another-message' WHERE message_id='imessage:3'")
    conn.commit()
    assert not proven(store, "imessage:3") and proven(store, "imessage:2")
    conn.execute("UPDATE conversation_messages SET reply_to_message_id=? WHERE message_id='imessage:3'", (ORIGINATOR,))
    conn.commit()
    assert proven(store, "imessage:3")


def test_G4_a_search_pass_proves_a_v3_enrollment_and_checks_once_at_the_end(ingest_fixture):
    store = make_forms_store(ingest_fixture, ids=(1, 2, 3), replies=REPLIES, pointers=POINTERS)
    service, conn, _ = store
    publish(store)
    reader = sqlite3.connect(service.resolver.path.as_uri() + "?mode=ro", uri=True)
    reader.row_factory = sqlite3.Row
    try:
        reader.execute("BEGIN")
        search = ExistingProvenancePass(reader, canonical_database=service.resolver.path, binding=service.binding)
        for i in (1, 2, 3):
            assert search.validate(reader, message_id=f"imessage:{i}", dataset_id=DATASET) == str(
                parse((service.root / "capture-a.db").read_bytes(), now=datetime.now(timezone.utc))[i - 1].native_event_nanoseconds)
        search.finish()
        with pytest.raises(PolicyError, match="native_owner_provenance_unavailable"):
            search.validate(reader, message_id="imessage:1", dataset_id=DATASET)
    finally:
        reader.close()


def test_G5_a_capture_holding_a_reply_cannot_be_published_as_v2(ingest_fixture):
    store = make_forms_store(ingest_fixture, ids=(1, 2, 3), replies=REPLIES, contract=ATTRIBUTED_CONTRACT)
    service, conn, _ = store
    assert contract_of(conn) == ATTRIBUTED_CONTRACT
    with pytest.raises(SnapshotRejected, match="snapshot_message_form_unsupported"):
        publish(store)
    assert conn.execute("SELECT count(*) FROM ingest_provenance_jobs").fetchone()[0] == 0
    assert links(conn) == [] and not proven(store, "imessage:1")


def test_G5_a_v2_enrollment_without_replies_still_publishes_and_validates(ingest_fixture):
    store = make_forms_store(ingest_fixture, ids=(1, 2), contract=ATTRIBUTED_CONTRACT)
    service, conn, _ = store
    assert publish(store)["reconciled"] == 2 and contract_of(conn) == ATTRIBUTED_CONTRACT
    assert proven(store, "imessage:1") and proven(store, "imessage:2")


def refresh_forms(store, name, **kwargs):
    service, conn, _ = store
    desc = describe(service, conn, name)
    start, end = recent_window()
    with owner():
        return refresh_existing(service, conn, dataset_id=DATASET, snapshot_id=name, snapshot_sha256=desc["snapshot_sha256"],
                                owner_attestation=OWNER_ATTESTATION, window_start_us=start, window_end_us=end, **kwargs)


def test_G4_a_refresh_moves_a_v2_enrollment_to_v3_and_links_the_forms_it_reads(ingest_fixture):
    store = make_forms_store(ingest_fixture, ids=(1, 2), contract=ATTRIBUTED_CONTRACT)
    service, conn, enrollment = store
    publish(store)
    name, data = capture_forms(service, "capture-b", [1, 2, 3, 4], replies=REPLIES, pointers={4: "synthetic-message-3"})
    add_canonical_forms(conn, data)
    assert [proven(store, f"imessage:{i}") for i in (1, 2, 3, 4)] == [True, True, False, False]
    counts = refresh_forms(store, name)
    assert counts == {"linked_new": 2, "previous_capture_removed": 1, "reproven": 2}
    assert contract_of(conn) == FORMS_CONTRACT
    assert links(conn) == [(f"imessage:{i}", 2) for i in (1, 2, 3, 4)]
    assert all(proven(store, f"imessage:{i}") for i in (1, 2, 3, 4))
    # Still one enrollment, the same id, at the next revision, naming the new capture.
    [(found, revision, state)] = conn.execute("SELECT enrollment_id, revision, state FROM ingest_provenance_enrollments").fetchall()
    assert (found, revision, state) == (enrollment, 2, "active")
    assert not (service.root / "capture-a.db").exists() and (service.root / "capture-b.db").exists()
    # A dry run of the next refresh reports and writes nothing, as before.
    again, _ = capture_forms(service, "capture-c", [1, 2, 3, 4], replies=REPLIES)
    assert refresh_forms(store, again, dry_run=True) == {"dry_run": 1, "reproven": 4}
    assert contract_of(conn) == FORMS_CONTRACT and links(conn) == [(f"imessage:{i}", 2) for i in (1, 2, 3, 4)]


def test_G4_the_same_capture_is_no_refresh_whichever_reader_the_enrollment_names(ingest_fixture):
    store = make_forms_store(ingest_fixture, ids=(1, 2), contract=ATTRIBUTED_CONTRACT)
    service, conn, _ = store
    publish(store)
    before = conn.execute("SELECT * FROM ingest_provenance_enrollments").fetchall()
    with pytest.raises(PolicyError, match="reconciliation_refresh_unchanged"):
        refresh_forms(store, "capture-a")
    assert conn.execute("SELECT * FROM ingest_provenance_enrollments").fetchall() == before
    # Once at v3, the same capture again is still no refresh.
    name, _ = capture_forms(service, "capture-b", [1, 2])
    refresh_forms(store, name)
    assert contract_of(conn) == FORMS_CONTRACT
    with pytest.raises(PolicyError, match="reconciliation_refresh_unchanged"):
        refresh_forms(store, name)


def test_G4_revoking_a_v3_enrollment_moves_the_protection_clock_as_v2_does(ingest_fixture):
    store = make_forms_store(ingest_fixture, ids=(1, 2), replies={2: (ORIGINATOR, PART)})
    service, conn, enrollment = store
    publish(store)
    clock = conn.execute("SELECT generation FROM permissions_v2_protection_state").fetchone()[0]
    with owner():
        service.revoke(conn, enrollment_id=enrollment)
    assert conn.execute("SELECT generation FROM permissions_v2_protection_state").fetchone()[0] == clock + 1
    assert not proven(store, "imessage:1") and not proven(store, "imessage:2")


def test_G4_the_two_contract_lists_and_the_lane_table_agree():
    assert ingest_provenance.RECONCILIATION_CONTRACTS == RECONCILIATION_CONTRACTS == (ATTRIBUTED_CONTRACT, FORMS_CONTRACT)
    for contract in RECONCILIATION_CONTRACTS:
        lane = ingest_provenance._lane(contract)
        assert (lane.source_id, lane.table, lane.suffix, lane.attestation) == ("imessage", "conversation_messages", ".db", OWNER_ATTESTATION)
    with pytest.raises(PolicyError, match="ingest_reader_unsupported"):
        ingest_provenance._lane("imessage-existing-comparison/v4")
    assert local_sync.IMESSAGE_ENROLLMENT_CONTRACTS == {IMESSAGE_READER_CONTRACT, *RECONCILIATION_CONTRACTS}
    assert set(imessage_reconciliation._PARSERS) == {CONTRACT, *RECONCILIATION_CONTRACTS}


# -- G6: the sync's enrolled-dataset guard -------------------------------------------------------------

def guard_node(tmp_path, contract, *, state="active"):
    """A node with one iMessage provenance enrollment of the given reader, as the sync's guard reads it."""
    conn = sqlite3.connect(str(tmp_path / "node.db"))
    conn.execute("CREATE TABLE ingest_provenance_enrollments (enrollment_id TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL, "
                 "dataset_id TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL, state TEXT NOT NULL, source_generation INTEGER NOT NULL, "
                 "attestation TEXT NOT NULL, authorized_at INTEGER NOT NULL, channel TEXT NOT NULL)")
    conn.execute("INSERT INTO ingest_provenance_enrollments VALUES (?, ?, ?, 1, ?, 0, 'attested', 0, 'uds')",
                 ("enr-1", json.dumps({"reader_contract": contract, "snapshot_id": "native-synthetic"}), "owner:topos:enrolled", state))
    conn.commit()
    return conn


@pytest.mark.parametrize("contract", [IMESSAGE_READER_CONTRACT, *RECONCILIATION_CONTRACTS])
def test_G6_every_imessage_enrollment_lane_guards_the_dataset(tmp_path, contract):
    conn = guard_node(tmp_path, contract)
    assert local_sync.enrolled_imessage_datasets(conn) == frozenset({"owner:topos:enrolled"})
    assert local_sync.enrolled_dataset_refusal(conn, "owner:topos:enrolled", None) is None
    refusal = local_sync.enrolled_dataset_refusal(conn, "owner:default:other", None)
    assert refusal["status"] == "error" and refusal["code"] == local_sync.DATASET_NOT_ENROLLED
    assert local_sync.enrolled_dataset_refusal(conn, "owner:default:other", {"allow_unenrolled_dataset": True}) is None


@pytest.mark.parametrize("contract,state", [
    ("chatgpt-owner-snapshot/v1", "active"), ("imessage-existing-comparison/v4", "active"), (None, "active"),
    (FORMS_CONTRACT, "revoked"), (ATTRIBUTED_CONTRACT, "revoked"),
])
def test_G6_another_lane_an_unknown_reader_or_a_revoked_enrollment_guards_nothing(tmp_path, contract, state):
    conn = guard_node(tmp_path, contract, state=state)
    assert local_sync.enrolled_imessage_datasets(conn) == frozenset()
    assert local_sync.enrolled_dataset_refusal(conn, "owner:default:other", None) is None


def test_G6_the_guard_reads_a_recovery_enrollment_as_the_live_ledger_writes_it(ingest_fixture):
    """Through the real service: an enrollment the recovery lane writes is one the guard counts."""
    store = make_forms_store(ingest_fixture, ids=(1, 2))
    service, conn, _ = store
    assert local_sync.enrolled_imessage_datasets(conn) == frozenset({DATASET})
    assert local_sync.enrolled_dataset_refusal(conn, "another-dataset", None)["code"] == local_sync.DATASET_NOT_ENROLLED
    assert local_sync.enrolled_dataset_refusal(conn, DATASET, None) is None


# -- the owner door, end to end ---------------------------------------------------------------------------

def door_over_forms(store, tmp_path, monkeypatch, native_ids, *, replies=None, pointers=None):
    """The real refresh route over the real service and a synthetic Messages database holding replies and chains."""
    from contextlib import contextmanager
    from types import SimpleNamespace
    from fastapi import FastAPI
    from topos.api.permissions_native_probe import router
    from topos.permissions_v2 import native_imessage_probe as probe, runtime
    service, conn, _ = store
    native = tmp_path / "native-chat.db"
    _, data = capture_forms(service, "native-source", native_ids, replies=replies, pointers=pointers)
    native.write_bytes(data)
    (service.root / "native-source.db").unlink()
    add_canonical_forms(conn, data)
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


def door_body(**changes):
    now = datetime.now(timezone.utc)
    return {"dataset_id": DATASET,
            "starts_at": datetime.fromtimestamp(now.timestamp() - 20 * DAY, tz=timezone.utc).isoformat(timespec="microseconds"),
            "ends_at": now.isoformat(timespec="microseconds"), "owner_attestation": OWNER_ATTESTATION, **changes}


def post_refresh(app, payload):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    with TestClient(UDSChannelApp(app)) as client:
        return client.post("/v1/permissions-beta/v2/imessage/refresh", json=payload)


def test_G4_the_owner_door_refreshes_a_v2_enrollment_into_v3_with_the_forms_it_reads(ingest_fixture, tmp_path, monkeypatch):
    store = make_forms_store(ingest_fixture, ids=(1, 2), contract=ATTRIBUTED_CONTRACT)
    service, conn, _ = store
    publish(store)
    app, synced = door_over_forms(store, tmp_path, monkeypatch, [1, 2, 3, 4],
                                  replies={3: (ORIGINATOR, PART)}, pointers={4: "synthetic-message-3", 1: ORIGINATOR})
    preview = post_refresh(app, door_body(dry_run=True))
    assert preview.status_code == 200, preview.text
    assert preview.json()["refresh"] == {"dry_run": 1, "linked_new": 2, "reproven": 2}
    assert contract_of(conn) == ATTRIBUTED_CONTRACT and synced == []
    response = post_refresh(app, door_body())
    assert response.status_code == 200, response.text
    payload = response.json()
    counts = payload["counts"]
    assert payload["authority_created"] is True and counts["canonical_exact_match"] == 4
    assert (counts["native_observed_thread_reply"], counts["native_observed_thread_reply_exact_match"]) == (1, 1)
    assert (counts["native_observed_reply_pointer"], counts["native_observed_reply_pointer_exact_match"]) == (2, 2)
    assert "native_message_form_unsupported" not in counts
    assert payload["refresh"] == {"linked_new": 2, "previous_capture_removed": 1, "reproven": 2}
    assert payload["search"] == {"protection_synced": True, "grants": 0, "ready": 0} and synced == ["ledger"]
    assert contract_of(conn) == FORMS_CONTRACT and all(proven(store, f"imessage:{i}") for i in (1, 2, 3, 4))
    assert ORIGINATOR not in response.text and "Synthetic message" not in response.text


def test_G4_publication_refuses_an_enrollment_whose_reader_changed_before_its_transaction(ingest_fixture, monkeypatch):
    """The capture is parsed by the reader its enrollment names; if that name differs inside the transaction, nothing
    is published."""
    store = make_forms_store(ingest_fixture, ids=(1, 2, 3), replies=REPLIES, pointers=POINTERS)
    service, conn, _ = store
    actual, calls = service._enrollment, []

    def relabelled(*args, **kwargs):
        found = actual(*args, **kwargs)
        calls.append(found["lane"].reader_contract)
        return found if len(calls) == 1 else {**found, "lane": ingest_provenance._lane(ATTRIBUTED_CONTRACT)}
    monkeypatch.setattr(service, "_enrollment", relabelled)
    with pytest.raises(PolicyError, match="reconciliation_lane_required"):
        publish(store)
    assert calls[:2] == [FORMS_CONTRACT, FORMS_CONTRACT]
    assert conn.execute("SELECT count(*) FROM ingest_provenance_jobs").fetchone()[0] == 0 and links(conn) == []


def test_G4_the_recovery_door_enrolls_v3_and_proves_replies_and_chained_rows(ingest_fixture, tmp_path, monkeypatch):
    """A first recovery (what a fresh node runs) captures, enrolls and publishes under v3. The model preparation
    is replaced by a fixed plan: this test is about the reader, not about facts."""
    from topos.permissions_v2 import reconciliation_facts
    from topos.permissions_v2.reconciliation_facts import classification
    service, conn, _ = ingest_fixture
    conn.execute("CREATE TABLE IF NOT EXISTS ai_chat_messages(message_id TEXT,content TEXT)")
    seen_rows = []

    async def prepared_plan(rows, **_kwargs):
        seen_rows.extend(row["message_id"] for row in rows)
        label = classification({"domains": ["work"], "sensitivity": "personal"})
        return {row["message_id"]: {"content": row["content"], "classification": label, "facts": []} for row in rows}, {}
    monkeypatch.setattr(reconciliation_facts, "prepare_facts", prepared_plan)
    monkeypatch.setattr(reconciliation_facts, "derive_prepared", lambda conn, rows, prepared: {"facts_written": 0})
    store = (service, conn, None)
    app, _ = door_over_forms(store, tmp_path, monkeypatch, [1, 2, 3, 4],
                             replies={3: (ORIGINATOR, PART)}, pointers={4: "synthetic-message-3", 1: ORIGINATOR})
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post("/v1/permissions-beta/v2/imessage/recover", json=door_body())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["authority_created"] is True and payload["reconciled"] == 4 and payload["boundary_withheld"] == 0
    assert sorted(seen_rows) == [f"imessage:{i}" for i in (1, 2, 3, 4)]
    assert contract_of(conn) == FORMS_CONTRACT
    assert all(proven(store, f"imessage:{i}") for i in (1, 2, 3, 4))
    assert ORIGINATOR not in response.text and "Synthetic message" not in response.text


# -- G8: an attachment's caption ------------------------------------------------------------------------------

CAPTION = "\ufffc Synthetic caption\r\nsecond line "


def test_G8_v3_reads_a_captioned_attachment_and_older_readers_refuse_it():
    data = forms_snapshot(captions={1: CAPTION})
    for contract in (CONTRACT, ATTRIBUTED_CONTRACT):
        with pytest.raises(SnapshotRejected, match="snapshot_message_form_unsupported"):
            parse(data, contract)
    caption, plain = parse(data)
    assert caption.attachment_caption is True and caption.content == CAPTION
    assert plain.attachment_caption is False and plain.content == "Synthetic message 2"


def test_G8_an_archived_caption_is_read_with_its_placeholder_and_the_text_column_must_agree():
    from tests.fixtures.imessage.attributed_body_blobs import ATTRIBUTED_BODY_FIXTURES
    raw, stored = ATTRIBUTED_BODY_FIXTURES["typedstream_mixed"]

    def archived(text):
        return lambda db: db.execute("UPDATE message SET cache_has_attachments=1, text=?, attributedBody=? WHERE ROWID=1",
                                     (text, raw))
    [caption, _] = parse(forms_snapshot(mutate=archived(None)))
    assert caption.attachment_caption is True and "\ufffc" in caption.content and caption_text(caption.content) == stored
    with pytest.raises(SnapshotRejected, match="snapshot_body_representations_disagree"):
        parse(forms_snapshot(mutate=archived("\ufffc")))
    only = ATTRIBUTED_BODY_FIXTURES["typedstream_attachment"][0]
    with pytest.raises(SnapshotRejected, match="snapshot_message_form_unsupported"):
        parse(forms_snapshot(mutate=lambda db: db.execute(
            "UPDATE message SET cache_has_attachments=1, text=NULL, attributedBody=? WHERE ROWID=1", (only,))))


def caption_row(native, content):
    return {**reply_row(native), "content": content}


@pytest.mark.parametrize("stored", [
    "Synthetic caption\nsecond line",      # read from the archive: line ends normalised
    "Synthetic caption\r\nsecond line",    # read from the text column: placeholders and outer whitespace only
])
def test_G8_the_stored_caption_matches_in_either_form_the_sync_stores(stored):
    [native, _] = parse(forms_snapshot(captions={1: CAPTION}))
    assert compare(caption_row(native, stored), native).contract == FORMS_CONTRACT


@pytest.mark.parametrize("stored", [
    "\ufffc Synthetic caption\nsecond line",   # kept the placeholder: released, it would say an attachment was there
    "Synthetic caption\nsecond line\ufffc", CAPTION, "\ufffc",
    "Synthetic caption", "synthetic caption\nsecond line", "", None, 7,
])
def test_G8_a_stored_body_that_is_not_exactly_the_caption_refuses(stored):
    [native, _] = parse(forms_snapshot(captions={1: CAPTION}))
    with pytest.raises(PolicyError, match="reconciliation_content_mismatch"):
        compare(caption_row(native, stored), native)


def test_G8_a_plain_message_is_still_compared_exactly():
    """The caption rule is the attachment's only: an ordinary message's stored body must equal its native body."""
    [_, plain] = parse(forms_snapshot(captions={1: CAPTION}, owner_sent=True))
    assert compare(caption_row(plain, "Synthetic message 2"), plain)
    with pytest.raises(PolicyError, match="reconciliation_content_mismatch"):
        compare(caption_row(plain, " Synthetic message 2"), plain)


@pytest.mark.parametrize("fields", [
    {"reader_contract": ATTRIBUTED_CONTRACT}, {"reader_contract": CONTRACT}, {"attachment_caption": 1},
    {"attachment_caption": "yes"},
])
def test_G8_a_native_observation_that_misstates_its_caption_is_invalid(fields):
    from dataclasses import replace
    [native, _] = parse(forms_snapshot(captions={1: CAPTION}))
    with pytest.raises(PolicyError, match="reconciliation_input_invalid"):
        compare(caption_row(native, caption_text(CAPTION)), replace(native, **fields))


def test_G8_the_probe_captures_a_caption_and_counts_one_stored_with_its_placeholder(files):
    native = full_native(files, count=4)
    canonical_as_ingested(files)
    # The stored bodies are "Synthetic message N"; the native rows become attachments captioned with them, but
    # ROWID 4's caption was changed natively (a mismatch that is not a kept placeholder).
    set_native(native, {1: {"cache_has_attachments": 1, "text": "\ufffc Synthetic message 1"},
                        2: {"cache_has_attachments": 1, "text": "\ufffcSynthetic message 2\ufffc"},
                        3: {"cache_has_attachments": 1, "text": "\ufffc"},
                        4: {"cache_has_attachments": 1, "text": "\ufffc changed natively"}})
    _, canonical = files
    with sqlite3.connect(canonical) as db:
        db.execute("UPDATE conversation_messages SET content=? WHERE message_id='imessage:2'",
                   ("\ufffcSynthetic message 2\ufffc",))
    captured = []
    counts = run(files, _on_match=lambda row, chat: captured.append(row))["counts"]
    assert counts["canonical_exact_match"] == 1 and [row["ROWID"] for row in captured] == [1]
    assert (counts["native_observed_attachment_caption"], counts["native_observed_attachment_caption_exact_match"]) == (3, 1)
    assert counts["reconciliation_content_mismatch"] == 2 and counts["native_observed_caption_placeholder_stored"] == 1
    assert form_buckets(counts) == {"native_form_attachment_only": 1} and counts["native_message_form_unsupported"] == 1
    assert "Synthetic" not in json.dumps(counts)


def test_G8_a_v3_enrollment_proves_a_caption_and_its_stored_body_names_no_attachment(ingest_fixture):
    store = make_forms_store(ingest_fixture, ids=(1, 2, 3), captions={2: CAPTION})
    service, conn, _ = store
    assert publish(store)["reconciled"] == 3
    assert all(proven(store, f"imessage:{i}") for i in (1, 2, 3))
    [(content,)] = conn.execute("SELECT content FROM conversation_messages WHERE message_id='imessage:2'").fetchall()
    assert content == "Synthetic caption\nsecond line" and "\ufffc" not in content
    # The link pins the stored body: a row that comes to hold the placeholder again no longer validates.
    conn.execute("UPDATE conversation_messages SET content=? WHERE message_id='imessage:2'", ("\ufffc" + content,))
    conn.commit()
    assert not proven(store, "imessage:2") and proven(store, "imessage:1")


def test_G8_the_sync_stores_a_caption_without_its_placeholder():
    from topos.ingestion.sources.imessage_reader import _build_content_from_row
    assert _build_content_from_row({"text": "\ufffc Synthetic caption ", "cache_has_attachments": 1}) == "Synthetic caption"
    assert _build_content_from_row({"text": "\ufffc\ufffc", "cache_has_attachments": 1}) == "[attachment]"
    assert _build_content_from_row({"text": " Synthetic message "}) == "Synthetic message"

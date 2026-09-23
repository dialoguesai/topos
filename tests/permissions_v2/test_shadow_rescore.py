"""The node's half of the shadow audit: the released-record index and the re-score handler (C6).

The control plane samples permitted reads and asks about them later. It cannot answer its own question -- the
sample row carries no content by design -- so the node has to, and the node could not: `p2a_receipts` carries
hashes and `p2a_requests` carries an envelope whose intent is itself a hash, so nothing led from a request id to
the records that were released.

Four groups, each written so it fails on `beta/confidence-program-engine` @ 44220969:
  E1  a permitted release is indexed; a refused one is not; the rows carry ids and a sealed pointer, never content
  E2  the index can never be a reason a release fails, and a release it could not index is COUNTED
  E3  the re-score answers `unresolved` for every hole it meets, and never `agree` by default
  E4  the handler is owner-only, its payload is closed, and its answer carries a verdict and nothing else
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from topos.permissions_v2 import shadow_index, shadow_labelers, shadow_rescore
from topos.permissions_v2.canonical import PolicyError
from topos.principal import OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal

pytestmark = [pytest.mark.p0]


class _Labeler:
    def __init__(self, verdict="agree", family="qwen", identifier="local-qwen", raises=False):
        self.id, self.family, self._verdict, self._raises = identifier, family, verdict, raises
        self.seen = []

    def score(self, records):
        if self._raises:
            raise RuntimeError("the model fell over")
        self.seen.append(records)
        return self._verdict


@pytest.fixture(autouse=True)
def _clean():
    shadow_labelers.clear()
    shadow_index.reset_failures()
    yield
    shadow_labelers.clear()
    shadow_index.reset_failures()


@pytest.fixture
def conn(tmp_path):
    db = sqlite3.connect(tmp_path / "ledger.db")
    db.row_factory = sqlite3.Row
    yield db
    db.close()


class _Record:
    def __init__(self, record_id, table="conversation_messages", source="source-A", content="PRIVATE CONTENT"):
        self.record_id, self.canonical_table, self.source_id, self.content = record_id, table, source, content


def _request(**changes):
    return {"version": shadow_rescore.VERSION, "request_id": "req-1", "grant_id": "grant-1",
            "capability": "permissions-beta/p2a-v3", "output_sha256": "a" * 64, "labeler_mode": "local", **changes}


# --------------------------------------------------------------------------- E1


def test_E1_a_release_is_indexed_by_id_and_a_sealed_pointer_never_by_content(conn):
    key = bytes(range(32))
    records = [_Record("r.aaa"), _Record("r.bbb", content="A SECOND PRIVATE THING")]
    assert shadow_index.record_release(conn, request_id="req-1", grant_id="grant-1", records=records,
                                       record_key=key, now=1_000_000) == 2
    rows = shadow_index.released(conn, request_id="req-1")
    assert [row["opaque_record_id"] for row in rows] == ["r.aaa", "r.bbb"]
    assert [row["ordinal"] for row in rows] == [0, 1]
    assert all(row["grant_id"] == "grant-1" and row["canonical_table"] == "conversation_messages" for row in rows)
    # Not one byte of what was released is in the table, or in the file behind it.
    blob = b"".join(bytes(row["sealed_pointer"]) for row in rows)
    assert b"PRIVATE CONTENT" not in blob and b"A SECOND PRIVATE THING" not in blob
    dump = "".join(str(dict(row)) for row in rows)
    assert "PRIVATE CONTENT" not in dump and "A SECOND PRIVATE THING" not in dump


def test_E1b_the_pointer_opens_with_the_grants_own_key_and_with_nothing_else(conn):
    key, other = bytes(range(32)), bytes(range(1, 33))
    shadow_index.record_release(conn, request_id="req-1", grant_id="grant-1", records=[_Record("r.aaa")],
                                record_key=key, now=1_000_000)
    [row] = shadow_index.released(conn, request_id="req-1")
    opened = shadow_index.open_pointer(key, opaque_id="r.aaa", sealed=row["sealed_pointer"])
    assert opened == {"record_id": "r.aaa", "canonical_table": "conversation_messages"}
    assert shadow_index.open_pointer(other, opaque_id="r.aaa", sealed=row["sealed_pointer"]) is None
    # The opaque id is the associated data, so a pointer cannot be moved onto another record's row.
    assert shadow_index.open_pointer(key, opaque_id="r.bbb", sealed=row["sealed_pointer"]) is None


def test_E1c_rows_age_out_and_never_pass_the_cap(conn, monkeypatch):
    old = 1_000_000_000
    for index in range(3):
        shadow_index.record_release(conn, request_id="old-%d" % index, grant_id="grant-1",
                                    records=[_Record("r.%d" % index)], record_key=bytes(range(32)), now=old)
    assert len(shadow_index.released(conn, request_id="old-0")) == 1
    shadow_index.record_release(conn, request_id="new", grant_id="grant-1", records=[_Record("r.new")],
                                record_key=bytes(range(32)),
                                now=old + (shadow_index.RETENTION_DAYS + 1) * 86_400)
    assert shadow_index.released(conn, request_id="old-0") == []
    assert len(shadow_index.released(conn, request_id="new")) == 1
    monkeypatch.setattr(shadow_index, "ROW_CAP", 4)
    for index in range(10):
        shadow_index.record_release(conn, request_id="cap-%d" % index, grant_id="grant-1",
                                    records=[_Record("r.cap%d" % index)], record_key=bytes(range(32)),
                                    now=old + 10 * 86_400 + index)
        assert conn.execute("SELECT COUNT(*) FROM p2a_shadow_released").fetchone()[0] <= 4


def test_E1d_the_index_is_off_unless_the_node_is_told_otherwise(monkeypatch):
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_SHADOW_INDEX_ENABLED", raising=False)
    assert shadow_index.enabled() is False
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_SHADOW_INDEX_ENABLED", "true")
    assert shadow_index.enabled() is True


# --------------------------------------------------------------------------- E2


def test_E2_a_release_the_index_cannot_take_is_still_a_release_and_is_counted(conn):
    """The write is never a reason a recipient's read fails -- and never silently missing either."""
    assert shadow_index.failures() == 0

    class Broken:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")
    assert shadow_index.record_release(Broken(), request_id="req-1", grant_id="grant-1",
                                       records=[_Record("r.aaa")], record_key=bytes(range(32)), now=1) == 0
    assert shadow_index.failures() == 1, "an unauditable read has to be visible to the owner"


def test_E2b_a_release_with_no_grant_key_is_named_rather_than_written_in_the_clear(conn):
    """Without the grant's key the pointer is sealed under one nothing keeps: the row names the release and
    resolves to nothing, which is an honest hole. The alternative is an ordinal id in the clear, in a table whose
    whole purpose is to be read later -- which is the channel p2a-v3's opaque ids exist to close."""
    shadow_index.record_release(conn, request_id="req-1", grant_id="grant-1", records=[_Record("imessage:8142")],
                                record_key=None, now=1_000_000)
    [row] = shadow_index.released(conn, request_id="req-1")
    assert shadow_index.open_pointer(bytes(range(32)), opaque_id="imessage:8142", sealed=row["sealed_pointer"]) is None
    assert b"8142" not in bytes(row["sealed_pointer"])


# --------------------------------------------------------------------------- E3


def test_E3_a_node_with_no_second_labeler_never_agrees(monkeypatch):
    """The property that matters most in this file. Answering `agree` because nothing objected would manufacture
    evidence: the report card counts items that were CHECKED."""
    assert shadow_labelers.registered("local") is None
    result = shadow_rescore.rescore(object(), _request())
    assert result.verdict == "unresolved" and result.reason == "labeler_unavailable"
    assert result.output_sha256 == "a" * 64 and result.request_id == "req-1"


def test_E3b_a_release_the_node_can_no_longer_resolve_is_unresolved(monkeypatch):
    shadow_labelers.register("local", _Labeler())
    monkeypatch.setattr(shadow_rescore, "resolve_records", lambda runtime, request: None)
    result = shadow_rescore.rescore(object(), _request())
    assert result.verdict == "unresolved" and result.reason == "records_unavailable"


def test_E3c_a_labeler_that_fails_or_answers_nonsense_is_unresolved(monkeypatch):
    monkeypatch.setattr(shadow_rescore, "resolve_records", lambda runtime, request: [{"record_id": "r.aaa"}])
    shadow_labelers.register("local", _Labeler(raises=True))
    assert shadow_rescore.rescore(object(), _request()).reason == "labeler_failed"
    shadow_labelers.register("local", _Labeler(verdict="miss"))
    assert shadow_rescore.rescore(object(), _request()).reason == "labeler_verdict_invalid"
    shadow_labelers.register("local", _Labeler(verdict="not_miss"))
    assert shadow_rescore.rescore(object(), _request()).reason == "labeler_verdict_invalid"


@pytest.mark.parametrize("verdict", ["agree", "candidate_miss", "unresolved"])
def test_E3d_a_resolved_release_is_scored_and_the_records_reach_the_labeler(monkeypatch, verdict):
    records = [{"record_id": "r.aaa", "canonical_table": "conversation_messages", "source_id": "source-A"}]
    monkeypatch.setattr(shadow_rescore, "resolve_records", lambda runtime, request: records)
    labeler = _Labeler(verdict=verdict)
    shadow_labelers.register("local", labeler)
    result = shadow_rescore.rescore(object(), _request())
    assert result.verdict == verdict and result.labeler == "local-qwen" and result.family == "qwen"
    assert labeler.seen == [records]
    # Every capability this release serves decides without a primary labeler, so there is no family to differ from.
    assert result.primary_family is None and shadow_rescore.primary_family_of("permissions-beta/p2a-v3") is None


def test_E3e_the_reply_carries_a_verdict_and_never_a_rationale():
    result = shadow_rescore.RescoreResult(request_id="req-1", verdict="agree", labeler="local-qwen",
                                          family="qwen", primary_family=None, output_sha256="a" * 64, reason=None)
    assert set(result.model_dump()) == {"version", "request_id", "verdict", "labeler", "family", "primary_family",
                                        "output_sha256", "reason"}
    for hostile in [{"rationale": "because the frame said so"}, {"records": []}, {"content": "x"},
                    {"scores": [1, 2]}, {"verdict": "miss"}, {"verdict": "not_miss"}]:
        with pytest.raises(PolicyError):
            shadow_rescore.RescoreResult.parse({**result.model_dump(), **hostile})


def test_E3f_a_malformed_question_is_refused_rather_than_guessed():
    for hostile in [{"request_id": ""}, {"output_sha256": "short"}, {"labeler_mode": "whatever"},
                    {"query": "fact:x"}, {"records": []}]:
        with pytest.raises(PolicyError):
            shadow_rescore.rescore(object(), _request(**hostile))


# --------------------------------------------------------------------------- E4


def _call(message):
    """A fresh loop per call: `get_event_loop()` picks up whatever loop the rest of the lane left behind, which
    passes alone and fails in company."""
    from topos.core.handlers.shadow_rescore import handle_permissions_v2_shadow_rescore
    return asyncio.run(handle_permissions_v2_shadow_rescore(message))


@pytest.mark.parametrize("principal", [
    Principal(cls=THIRD_PARTY, channel="cp_relay", acting_user="actor-1"),
    Principal(cls=OWNER_APP, channel="http", acting_user="owner-1"),
    None,
])
def test_E4_the_handler_answers_only_the_owner(principal):
    token = set_principal(principal) if principal is not None else None
    try:
        answer = _call({"id": "m1", "type": shadow_rescore.MESSAGE_TYPE, "payload": {"request": _request()}})
    finally:
        if token is not None:
            reset_principal(token)
    assert answer["status"] == "error" and answer["code"] == 403 and answer["error"] == "owner_mode_required"
    assert "payload" not in answer


def test_E4b_the_payload_is_closed():
    token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user="owner-1"))
    try:
        for payload in ({}, {"request": _request(), "extra": 1}, {"envelope": {}}, None, []):
            answer = _call({"id": "m1", "type": shadow_rescore.MESSAGE_TYPE, "payload": payload})
            assert answer["status"] == "error" and answer["code"] == 400, payload
    finally:
        reset_principal(token)


def test_E4c_the_handler_is_registered_owner_only():
    import topos.core.handlers  # noqa: F401  (registration side effects)
    from topos.core.handlers.registry import HANDLERS, OWNER_ONLY_MESSAGE_TYPES
    assert shadow_rescore.MESSAGE_TYPE in HANDLERS
    assert shadow_rescore.MESSAGE_TYPE in OWNER_ONLY_MESSAGE_TYPES

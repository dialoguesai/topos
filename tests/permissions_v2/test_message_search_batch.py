"""Batched recipient message search on the node (OD-36; design BATCHED_MESSAGE_SEARCH_DESIGN §3.2-3.4, §4, §7).

A batch is 1..6 ordinary signed search envelopes of ONE grant, bound to one frame by position
(`request_id == f"{frame id}:{i}"`). The node verifies once per batch under one snapshot (one
SearchVerification, one index load, one gated recheck, one send-time check_own) and still ranks, walks,
decides, receipts and signs every query on its own. Pinned here:

- every query's released bytes, receipt and signed result equal what the same query gets as a single
  search on the same snapshot (p2c-v1 facts and p2c-v3 direct messages; windowed and unwindowed; k);
- one SearchVerification, one closure, one review digest and the same outermost gate entries per batch
  as per single search;
- the frame binding (position, one authority, no duplicate, closed shape, flags, stamp) refuses before
  any work and writes nothing;
- a batch is refused whole with the single door's one error frame; every item that got past envelope
  verification is spent with its own tombstone and set_refused receipt; a failed item writes nothing;
  the checkpoint claims every item or none;
- a commit between the batch's stages forces the full check exactly as for a single search (an
  unrelated one recomputes and answers the same; a protected one refuses the whole batch), including
  one landing mid-walk and one just after the gated read's snapshot;
- `respond_by` (advisory) stops a batch the CP no longer waits for; the per-grant lock turns a second
  concurrent batch away; the heartbeat advertises batches only with both flags on;
- timing lines carry the batch corr, `n` and `item`, and nothing else new.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
import threading

import pytest

from tests.permissions_v2 import direct_search_twins as dst
from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_entity_boundary_search import node  # noqa: F401 -- the Off-limits fixture
from tests.permissions_v2.test_message_search_refusals import Socket, relay_message, signed
from tests.permissions_v2.test_search_verification import ALIAS, UNRELATED, commit, stale_basis, wal
from topos.permissions_v2 import search_release, search_timing, search_transport
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.permissions_v2.forwarding import verify_node_result
from topos.permissions_v2.search_index import SearchVerification
from topos.permissions_v2.search_release import MessageSearchRelease
from topos.relay_stamp import canonical_signing_payload
from topos.storage.db import write_gate

QUERIES = [{"query": "roadmap review", "k": 5}, {"query": "roadmap", "k": 3}, {"query": "budget sprint", "k": 5},
           {"query": "launch deploy", "k": 2}, {"query": "vendor contract", "k": 5}, {"query": "review", "k": 4}]


def batch_error(batch_id):
    return json.dumps({"code": 403, "error": "permission_denied", "id": batch_id, "status": "error",
                       "type": "permissions_v2_message_search_batch"}, separators=(",", ":"), sort_keys=True)


def batch_message(node, payloads, monkeypatch, *, batch_id="batch-1", respond_by=None, envelopes=None):
    """A CP batch frame: N envelopes signed with request ids `batch_id:i`, one relay stamp over the frame."""
    relay_message(node, signed(node, request_id="unused"), payloads[0], monkeypatch)  # flags, stamp key, runtime, clock
    monkeypatch.setenv(search_transport.BATCH_FLAG, "true")
    envelopes = envelopes or [signed(node, payload=payload, request_id=f"{batch_id}:{number}")
                              for number, payload in enumerate(payloads)]
    message = {"id": batch_id, "type": search_transport.BATCH_MESSAGE_TYPE,
               "payload": {"items": [{"envelope": envelope.model_dump(), "intent": payload}
                                     for envelope, payload in zip(envelopes, payloads)],
                           "respond_by": respond_by if respond_by is not None else (node.now[0] + 60) * 1000}}
    stamp = {"v": 1, "cls": "third_party", "client_id": "client-2", "acting_user": "actor-1", "iat": node.now[0],
             "exp": node.now[0] + 100}
    stamp["sig"] = base64.b64encode(node.cp_key.sign(canonical_signing_payload(stamp, msg_id=message["id"],
                                                                                 msg_type=message["type"]))).decode()
    message["principal_stamp"] = stamp
    return message


async def send_batch(node, payloads, monkeypatch, **options) -> dict:
    message = options.pop("message", None) or batch_message(node, payloads, monkeypatch, **options)
    socket = Socket()
    await search_transport.dispatch_message_search_batch(socket, message)
    [frame] = [json.loads(value) for value in socket.sent]
    return frame


async def send_single(node, payload, monkeypatch, request_id) -> dict:
    message = relay_message(node, signed(node, payload=payload, request_id=request_id), payload, monkeypatch,
                            request_id=request_id)
    socket = Socket()
    await search_transport.dispatch_message_search(socket, message)
    [frame] = [json.loads(value) for value in socket.sent]
    return frame


def ledger_rows(node, request_id):
    with node.ledger._transaction() as db:
        request = db.execute("SELECT status FROM p2a_requests WHERE request_id=?", (request_id,)).fetchone()
        receipt = db.execute("SELECT receipt_json, decision_json FROM p2a_receipts WHERE request_id=?",
                             (request_id,)).fetchone()
    return (request["status"] if request else None,
            (json.loads(receipt["receipt_json"]), json.loads(receipt["decision_json"])) if receipt else None)


def written(node) -> int:
    with node.ledger._transaction() as db:
        return (db.execute("SELECT COUNT(*) FROM p2a_requests").fetchone()[0]
                + db.execute("SELECT COUNT(*) FROM p2a_receipts").fetchone()[0])


def comparable(receipt):
    """A receipt minus what names the request (its id, and the envelope hash, which covers the id)."""
    return {key: value for key, value in receipt.items() if key not in {"request_id", "envelope_hash"}}


def spent_with_refusal(node, request_id) -> bool:
    status, receipt = ledger_rows(node, request_id)
    return status == "refused" and receipt is not None and receipt[1]["reason_code"] == "set_refused" \
        and receipt[0]["verdict"] == "deny" and receipt[0]["output_hash"] is None


def verifications(monkeypatch) -> list:
    made = []
    original = MessageSearchRelease.verification

    def verification(self):
        made.append(original(self))
        return made[-1]
    monkeypatch.setattr(MessageSearchRelease, "verification", verification)
    return made


def outermost_gate_entries(monkeypatch) -> list:
    """Every outermost write-gate entry (a with_db_write that did not already hold the gate on this thread)."""
    entries = []
    original = write_gate._set_holder

    def set_holder(site):
        holder = write_gate._holder
        if holder is None or holder.ident != threading.get_ident():
            entries.append(site)
        original(site)
    monkeypatch.setattr(write_gate, "_set_holder", set_holder)
    return entries


@pytest.fixture
def direct(tmp_path):
    node = dst.build(tmp_path / "direct", members=12, hidden_facts=0, seed=9)
    distinct = list(dict.fromkeys(dst.queries(12, 9, 12)))
    # Six distinct payloads (a batch refuses a duplicate): distinct texts first, then the same texts at k=3.
    node.queries = ([{"query": query, "k": 5} for query in distinct] + [{"query": query, "k": 3} for query in distinct])[:6]
    return node


# -- the same answers as N singles -------------------------------------------------------------------

def windowed(node):
    return {"query": "roadmap review", "k": 5, "window": {"after": node.now[0] - 30 * 86_400, "before": node.now[0]}}


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["node", "direct"])
@pytest.mark.parametrize("size", [1, 3, 6])
async def test_each_item_releases_exactly_what_the_single_search_releases(fixture, size, request, monkeypatch):
    subject = request.getfixturevalue(fixture)
    payloads = (getattr(subject, "queries", None) or QUERIES[:-1] + [windowed(subject)])[:size]
    if size == 6 and fixture == "node":
        assert "window" in payloads[-1]  # a windowed query rides with unwindowed ones
    singles = [await send_single(subject, payload, monkeypatch, f"single-{number}")
               for number, payload in enumerate(payloads)]
    frame = await send_batch(subject, payloads, monkeypatch, batch_id=f"batch-{size}")
    assert frame["status"] == "ok" and set(frame) == {"id", "type", "status", "payload"}
    assert frame["id"] == f"batch-{size}" and set(frame["payload"]) == {"items"}
    items = frame["payload"]["items"]
    assert len(items) == size
    assert sum(len(single["payload"]["output"]["records"]) for single in singles) > 0  # not empty against empty
    trusted = {"node-key": subject.node_key.public_key().public_bytes_raw()}
    for number, (single, item, payload) in enumerate(zip(singles, items, payloads)):
        assert set(item) == {"result", "output"}
        assert json.dumps(item["output"], sort_keys=True) == json.dumps(single["payload"]["output"], sort_keys=True)
        request_id = f"batch-{size}:{number}"
        assert item["result"]["request_id"] == request_id
        # Each item's signed result verifies alone, against its own envelope, exactly as a single's does.
        envelope = signed(subject, payload=payload, request_id=request_id)
        verify_node_result(item["result"], trusted_keys=trusted, envelope=envelope, output=item["output"],
                           now=subject.now[0])
        assert item["result"]["output_hash"] == single["payload"]["result"]["output_hash"]
        assert item["result"]["authority"] == single["payload"]["result"]["authority"]
        # One receipt per query, in today's shape, equal to the single's but for the request it names.
        batch_status, batch_receipt = ledger_rows(subject, request_id)
        single_status, single_receipt = ledger_rows(subject, f"single-{number}")
        assert batch_status == single_status == "checkpointed"
        assert batch_receipt[0]["version"] == "topos-local-receipt/v3"
        assert comparable(batch_receipt[0]) == comparable(single_receipt[0])
        assert batch_receipt[1] == single_receipt[1]


@pytest.mark.asyncio
async def test_a_query_over_its_k_or_its_window_never_borrows_from_another(node, monkeypatch):
    """No candidate, rank or k budget carries over: a k=1 query next to a k=5 one still gets one record."""
    payloads = [{"query": "roadmap review", "k": 5}, {"query": "roadmap review", "k": 1}]
    frame = await send_batch(node, payloads, monkeypatch)
    first, second = (item["output"]["records"] for item in frame["payload"]["items"])
    assert len(first) > 1 and second == first[:1]


# -- one verification pass per batch -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_batch_verifies_once_like_one_search(node, monkeypatch):
    built, made = [], verifications(monkeypatch)
    original = EntityBoundary.__init__
    monkeypatch.setattr(EntityBoundary, "__init__", lambda self, conn: built.append(1) or original(self, conn))
    # Both frames are signed first (signing reads the ledger); only the node's own work is counted.
    single_message = relay_message(node, signed(node, payload=QUERIES[0], request_id="single-gates"), QUERIES[0],
                                   monkeypatch, request_id="single-gates")
    batch = batch_message(node, QUERIES[:6], monkeypatch)
    entries = outermost_gate_entries(monkeypatch)
    socket = Socket()
    await search_transport.dispatch_message_search(socket, single_message)
    assert json.loads(socket.sent[0])["status"] == "ok"
    single_entries, single_built = list(entries), len(built)
    entries.clear()
    built.clear()
    frame = await send_batch(node, QUERIES[:6], monkeypatch, message=batch)
    assert frame["status"] == "ok"
    assert len(made) == 2  # one for the single, one for the whole batch
    verified = made[1]
    assert verified.computed["boundary"] == 1 and verified.reused["boundary"] == 1  # recheck; the send check skips (N5)
    assert len(built) == single_built == 1
    assert len(entries) == len(single_entries)  # gate entries per batch = per search, not N x
    assert verified._probes == {}  # closed with the batch


@pytest.mark.asyncio
async def test_a_direct_batch_reads_the_review_digest_once(direct, monkeypatch):
    made, digests = verifications(monkeypatch), []
    original = direct.corpus.reviews.current_authority_digest
    monkeypatch.setattr(direct.corpus.reviews, "current_authority_digest", lambda: digests.append(1) or original())
    frame = await send_batch(direct, direct.queries, monkeypatch)
    assert frame["status"] == "ok" and any(item["output"]["records"] for item in frame["payload"]["items"])
    [verified] = made
    assert len(digests) == 1 and verified.computed["digest"] == 1 and verified.reused["digest"] == 1  # recheck (N5)


# -- the frame binding refuses before any work -------------------------------------------------------

def _mutate_binding(node, monkeypatch, door):
    payloads = QUERIES[:3]
    batch_id = "batch-1"
    if door == "position":  # two items swapped: each envelope is signed, but not for its place
        envelopes = [signed(node, payload=payload, request_id=f"{batch_id}:{number}")
                     for number, payload in enumerate(payloads)]
        envelopes[0], envelopes[1] = envelopes[1], envelopes[0]
        payloads = [payloads[1], payloads[0], payloads[2]]
        return batch_message(node, payloads, monkeypatch, envelopes=envelopes)
    if door == "other_frame":  # an envelope issued for a different batch
        envelopes = [signed(node, payload=payload, request_id=f"{batch_id}:{number}")
                     for number, payload in enumerate(payloads)]
        envelopes[2] = signed(node, payload=payloads[2], request_id="batch-2:2")
        return batch_message(node, payloads, monkeypatch, envelopes=envelopes)
    if door == "authority":  # the same grant, a different expiry: not one issuance
        envelopes = [signed(node, payload=payload, request_id=f"{batch_id}:{number}")
                     for number, payload in enumerate(payloads)]
        envelopes[1] = signed(node, payload=payloads[1], request_id=f"{batch_id}:1",
                              changes={"expires_at": node.now[0] + 99})
        return batch_message(node, payloads, monkeypatch, envelopes=envelopes)
    if door == "duplicate":
        return batch_message(node, [payloads[0], payloads[0]], monkeypatch)
    if door == "too_many":
        return batch_message(node, (QUERIES + [{"query": "latency", "k": 1}])[:7], monkeypatch)
    message = batch_message(node, payloads, monkeypatch)
    if door == "empty":
        message["payload"]["items"] = []
    elif door == "no_respond_by":
        del message["payload"]["respond_by"]
    elif door == "respond_by_type":
        message["payload"]["respond_by"] = str(message["payload"]["respond_by"])
    elif door == "extra_key":
        message["payload"]["mode"] = "owner"
    elif door == "item_extra_key":
        message["payload"]["items"][1]["grant"] = "other"
    elif door == "changed_query":
        message["payload"]["items"][1]["intent"] = {"query": "other", "k": 3}
    elif door == "batch_flag_off":
        monkeypatch.delenv(search_transport.BATCH_FLAG)
    elif door == "search_flag_off":
        monkeypatch.delenv(search_transport.FLAG)
    elif door == "missing_stamp":
        del message["principal_stamp"]
    elif door == "owner_stamp":
        message["principal_stamp"]["cls"] = "owner_app"
    elif door == "single_type":
        message["type"] = search_transport.MESSAGE_TYPE
    return message


BINDING_DOORS = ["position", "other_frame", "authority", "duplicate", "too_many", "empty", "no_respond_by",
                 "respond_by_type", "extra_key", "item_extra_key", "batch_flag_off", "search_flag_off",
                 "missing_stamp", "owner_stamp", "single_type"]


@pytest.mark.asyncio
@pytest.mark.parametrize("door", BINDING_DOORS)
async def test_a_frame_that_breaks_the_binding_is_refused_and_writes_nothing(node, monkeypatch, door):
    before = written(node)
    message = _mutate_binding(node, monkeypatch, door)
    socket = Socket()
    await search_transport.dispatch_message_search_batch(socket, message)
    assert socket.sent == [batch_error("batch-1")]
    assert written(node) == before


@pytest.mark.asyncio
async def test_a_changed_query_spends_the_verified_items_and_leaves_the_rest_unwritten(node, monkeypatch):
    """Item 1's intent no longer hashes to its signed request_hash: its verification fails and writes nothing."""
    message = _mutate_binding(node, monkeypatch, "changed_query")
    socket = Socket()
    await search_transport.dispatch_message_search_batch(socket, message)
    assert socket.sent == [batch_error("batch-1")]
    assert spent_with_refusal(node, "batch-1:0")
    assert ledger_rows(node, "batch-1:1") == (None, None)
    assert ledger_rows(node, "batch-1:2") == (None, None)


# -- refused whole, spent per item -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_item_over_its_k_refuses_the_whole_batch_and_spends_every_item(node, monkeypatch):
    node.activate({**node.search_raw, "policy_version_id": "policy-k5"} | {"search": {**node.search_raw["search"], "max_k": 5}},
                  generation=2)
    node.rebuild()
    payloads = [{"query": "roadmap", "k": 5}, {"query": "review", "k": 6}, {"query": "budget", "k": 2}]
    frame = await send_batch(node, payloads, monkeypatch)
    assert frame == json.loads(batch_error("batch-1"))
    for number in range(3):
        assert spent_with_refusal(node, f"batch-1:{number}")


@pytest.mark.asyncio
async def test_a_replayed_batch_is_refused_and_writes_nothing_more(node, monkeypatch):
    message = batch_message(node, QUERIES[:3], monkeypatch)
    first = Socket()
    await search_transport.dispatch_message_search_batch(first, json.loads(json.dumps(message)))
    assert json.loads(first.sent[0])["status"] == "ok"
    before = written(node)
    again = Socket()
    await search_transport.dispatch_message_search_batch(again, message)
    assert again.sent == [batch_error("batch-1")]
    assert written(node) == before
    assert all(ledger_rows(node, f"batch-1:{number}")[0] == "checkpointed" for number in range(3))


@pytest.mark.asyncio
async def test_the_checkpoint_claims_every_item_or_none(node, monkeypatch):
    """The third item's checkpoint fails inside the batch transaction: nothing of the first two survives,
    and every id is then spent with its own refusal receipt."""
    ledger = node.ledger
    original, calls = type(ledger)._checkpoint, []

    def checkpoint(self, conn, **kwargs):
        calls.append(kwargs["lease"].request_id)
        if len(calls) == 3:
            raise search_release.PolicyError("decision_binding")
        return original(self, conn, **kwargs)
    monkeypatch.setattr(type(ledger), "_checkpoint", checkpoint)
    frame = await send_batch(node, QUERIES[:3], monkeypatch)
    assert frame == json.loads(batch_error("batch-1"))
    assert calls[:3] == ["batch-1:0", "batch-1:1", "batch-1:2"]
    for number in range(3):
        assert spent_with_refusal(node, f"batch-1:{number}")


@pytest.mark.asyncio
async def test_a_revoke_after_the_checkpoint_refuses_the_whole_batch_with_no_body(node, monkeypatch):
    original = node.search.dispatch_batch

    def dispatch_batch(**kwargs):
        answered = original(**kwargs)
        with owner():
            node.ledger.revoke("grant-search", expected_epoch=node.epoch(), command_id="revoke-mid-batch")
        return answered
    monkeypatch.setattr(node.search, "dispatch_batch", dispatch_batch)
    frame = await send_batch(node, QUERIES[:3], monkeypatch)
    assert frame == json.loads(batch_error("batch-1"))


# -- the shared-snapshot guard, per batch ------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unrelated", "alias"])
@pytest.mark.parametrize("between", ["load_and_recheck", "mid_walk", "checkpoint_and_send"])
async def test_a_commit_between_the_batch_stages_forces_the_full_check(node, monkeypatch, caplog, between, change):
    if between == "mid_walk":
        wal(node)  # only in WAL can a commit land while the gated read is open
    baseline = await send_batch(node, QUERIES[:3], monkeypatch, batch_id="baseline")
    assert baseline["status"] == "ok" and baseline["payload"]["items"][0]["output"]["records"]
    made = verifications(monkeypatch)
    sql = UNRELATED if change == "unrelated" else ALIAS
    if between == "load_and_recheck":
        original, calls = search_release.rank, []

        def rank(*args, **kwargs):
            if not calls:
                calls.append(1)
                commit(node, sql)
            return original(*args, **kwargs)
        monkeypatch.setattr(search_release, "rank", rank)
    elif between == "mid_walk":
        original, calls = MessageSearchRelease._walk, []

        def walk(self, *args, **kwargs):
            value = original(self, *args, **kwargs)
            if not calls:
                calls.append(1)
                commit(node, sql)
            return value
        monkeypatch.setattr(MessageSearchRelease, "_walk", walk)
    else:
        original = node.search.dispatch_batch

        def dispatch_batch(**kwargs):
            answered = original(**kwargs)
            commit(node, sql)
            return answered
        monkeypatch.setattr(node.search, "dispatch_batch", dispatch_batch)
    frame = await send_batch(node, QUERIES[:3], monkeypatch)
    [verified] = made
    if change == "alias":  # the closure moved under the batch: refused whole, whichever stage saw it
        assert frame == json.loads(batch_error("batch-1")) and stale_basis(caplog)
        if between == "load_and_recheck":
            for number in range(3):
                assert spent_with_refusal(node, f"batch-1:{number}")
        return
    assert frame["status"] == "ok"
    assert [item["output"] for item in frame["payload"]["items"]] == \
        [item["output"] for item in baseline["payload"]["items"]]
    # Before the recheck: it recomputes, and the send check, finding nothing moved since, skips (N5). Mid-walk or
    # before the send: the recheck keeps no token (mid-walk) or the token moved, so the send check recomputes.
    reused = 0 if between == "load_and_recheck" else 1
    assert verified.computed["boundary"] == 2 and verified.reused["boundary"] == reused


@pytest.mark.asyncio
async def test_a_commit_just_after_the_gated_reads_snapshot_is_never_trusted_later(node, monkeypatch, caplog):
    wal(node)
    reviews, calls = node.search.reviews, []
    original = reviews._observe_clock

    def observe(conn):  # the batch's gated read established its snapshot just before this
        if not calls:
            calls.append(1)
            commit(node, ALIAS)
        return original(conn)
    monkeypatch.setattr(reviews, "_observe_clock", observe)
    frame = await send_batch(node, QUERIES[:3], monkeypatch)
    assert calls == [1]
    assert frame == json.loads(batch_error("batch-1")) and stale_basis(caplog)


@pytest.mark.asyncio
async def test_a_review_write_between_checkpoint_and_send_refuses_a_direct_batch(direct, monkeypatch, caplog):
    made = verifications(monkeypatch)
    original = direct.search.dispatch_batch

    def dispatch_batch(**kwargs):
        answered = original(**kwargs)
        with owner():
            direct.corpus.reviews.opt_out("fact-outside-the-grant", now=direct.now[0])
        return answered
    monkeypatch.setattr(direct.search, "dispatch_batch", dispatch_batch)
    frame = await send_batch(direct, direct.queries[:3], monkeypatch)
    assert frame == json.loads(batch_error("batch-1")) and stale_basis(caplog)
    [verified] = made
    assert verified.computed["digest"] == 2


# -- respond_by, the per-grant lock, the capability --------------------------------------------------

@pytest.mark.asyncio
async def test_a_batch_past_its_respond_by_is_refused_and_writes_nothing(node, monkeypatch):
    before = written(node)
    frame = await send_batch(node, QUERIES[:3], monkeypatch, respond_by=node.now[0] * 1000)
    assert frame == json.loads(batch_error("batch-1"))
    assert written(node) == before


@pytest.mark.asyncio
async def test_respond_by_passing_between_walks_refuses_and_spends_every_item(node, monkeypatch):
    original, clock = MessageSearchRelease._walk, node.now

    def walk(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        monkeypatch.setattr(search_transport.time, "time", lambda: clock[0] + 120)  # the CP gave up during the first walk
        return value
    monkeypatch.setattr(MessageSearchRelease, "_walk", walk)
    frame = await send_batch(node, QUERIES[:3], monkeypatch, respond_by=(node.now[0] + 60) * 1000)
    assert frame == json.loads(batch_error("batch-1"))
    for number in range(3):
        assert spent_with_refusal(node, f"batch-1:{number}")


@pytest.mark.asyncio
async def test_a_second_batch_for_the_same_grant_waits_then_refuses(node, monkeypatch):
    lock = search_transport._grant_lock("grant-search")
    assert lock.acquire(timeout=1)
    try:
        # The first batch holds the grant; the second may wait only until its respond_by (1 s here).
        monkeypatch.setattr(search_transport, "BATCH_LOCK_MAX_WAIT_SECONDS", 1)
        frame = await send_batch(node, QUERIES[:2], monkeypatch)
    finally:
        lock.release()
    assert frame == json.loads(batch_error("batch-1"))
    # Nothing spent: the lock is taken before any verification.
    assert ledger_rows(node, "batch-1:0") == (None, None)
    frame = await send_batch(node, QUERIES[:2], monkeypatch, batch_id="batch-2")
    assert frame["status"] == "ok"


@pytest.mark.parametrize("search,batch,expected", [(True, True, 1), (True, False, 0), (False, True, 0),
                                                    (False, False, 0)])
def test_the_heartbeat_advertises_batches_only_with_both_flags(monkeypatch, search, batch, expected):
    from topos.engine import registration
    for flag, on in ((search_transport.FLAG, search), (search_transport.BATCH_FLAG, batch)):
        if on:
            monkeypatch.setenv(flag, "true")
        else:
            monkeypatch.delenv(flag, raising=False)
    monkeypatch.setattr(registration, "ollama_is_reachable", lambda: False)
    assert registration.build_engine_capabilities()["permissions_v2_search_batch_version"] == expected
    assert search_transport.batch_capability_version() == expected


def test_the_generic_handler_never_answers_a_batch():
    from topos.core.handlers.permissions_v2_release import handle_permissions_v2_message_search_batch
    reply = asyncio.run(handle_permissions_v2_message_search_batch({"id": "batch-1"}))
    assert reply == {"id": "batch-1", "status": "error", "code": 403, "error": "permission_denied"}


# -- timing ------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timing_lines_carry_the_batch_corr_and_positions_only(node, monkeypatch, caplog):
    monkeypatch.setenv(search_timing.FLAG, "true")

    def message_search():  # the runtime's own hook: the adapter reports to the transport's timing
        timing = search_timing.for_adapter()
        node.search.observe = timing.observe if timing is not None else None
        return node.search
    frame_message = batch_message(node, QUERIES[:3], monkeypatch, batch_id="batch-timed")
    search_transport.get_runtime().message_search = message_search
    caplog.set_level(logging.INFO, logger="topos.permissions_v2.search_timing")
    socket = Socket()
    await search_transport.dispatch_message_search_batch(socket, frame_message)
    node.search.observe = None
    assert json.loads(socket.sent[0])["status"] == "ok"
    lines = [record.getMessage() for record in caplog.records if "permission_search_timing" in record.getMessage()]
    corr = search_timing.correlation_id("batch-timed")
    assert lines and all(f"corr={corr}" in line for line in lines)
    stages = {}
    for line in lines:
        fields = dict(part.split("=", 1) for part in line.split()[1:])
        stages.setdefault(fields["stage"], []).append(fields)
    for stage in ("admit", "index_load", "recheck", "checkpoint"):
        assert [fields.get("n") for fields in stages[stage]] == ["3"]
    for stage in ("embed", "rank", "accept", "sign"):
        assert sorted(fields.get("item") for fields in stages[stage]) == ["0", "1", "2"]
    assert stages["transport_total"][0]["n"] == "3" and stages["transport_total"][0]["outcome"] == "ok"
    joined = "\n".join(lines)
    for payload in QUERIES[:3]:
        assert payload["query"] not in joined
    assert "batch-timed" not in joined and "grant-search" not in joined

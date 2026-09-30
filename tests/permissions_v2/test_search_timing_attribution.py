"""WS3 search timing (contract IF-3): joined by a correlation id, content-free, and inert for the search itself.

Pinned here:
- the correlation-id vector the control plane pins as well (its tests/control_plane/test_search_timing.py);
- timing off: no line, and the sweeper calls sweep() exactly as before;
- timing on: the transport's lines partition transport_total, each gate wait is measured where the gate
  is entered, the sweep's gate hold is timed, and no query, record, grant, actor or request id is logged;
- a refusal is the same bytes with timing on, and the answer is sent with no gate held.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from types import SimpleNamespace

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, fake_embedder
from topos.permissions_v2 import search_timing, search_transport
from topos.permissions_v2.runtime import Runtime
from topos.permissions_v2.search_index import purge
from topos.permissions_v2.signing import parse_envelope, request_digest, sign_envelope
from topos.relay_stamp import canonical_signing_payload
from topos.storage.db import write_gate

FLAG = "TOPOS_PERMISSIONS_V2_SEARCH_TIMINGS"
LOGGER = "topos.permissions_v2.search_timing"
REQUEST_ID = "timing-request-7"
PAYLOAD = {"query": "roadmap review", "k": 5}
LINE = re.compile(r"^permission_search_timing run=([0-9a-f]{32}) stage=([a-z_]+) elapsed_ms=(-?\d+\.\d{3}) "
                  r"corr=([0-9a-f]{16}|-) t_ms=(\d+\.\d{3})((?: [a-z_]+=[A-Za-z0-9_.:-]+)*)$")
FIELDS = {"point", "holder", "site", "hop", "executor_ms", "resume_ms", "outcome", "recv_at", "sent_at", "wait_ms",
          "start_ms", "removed", "open_ms", "protection_ms", "authority_ms", "commit_ms", "check_own_ms",
          # IF-3 v1.3: index_load's check_own/load split, and both check_owns' parts
          "load_ms", "boundary_ms", "digest_ms", "members_ms",
          # IF-3 v1.4: where members_ms goes (dependency loads, their boundary checks, the provenance pass)
          "dependencies_ms", "dependency_boundary_ms", "provenance_setup_ms", "provenance_check_ms",
          "provenance_snapshot_ms"}
ADAPTER = ("runtime_setup", "admit", "index_load", "embed", "rank", "recheck", "checkpoint", "sign")


def parsed(caplog) -> list[dict]:
    """Every timing line, checked against the IF-3 grammar: fixed keys, one token per value."""
    out = []
    for record in caplog.records:
        if record.name != LOGGER:
            continue
        match = LINE.match(record.getMessage())
        assert match, record.getMessage()
        extra = dict(pair.split("=", 1) for pair in match.group(6).split())
        assert set(extra) <= FIELDS, extra
        out.append({"run": match[1], "stage": match[2], "ms": float(match[3]), "corr": match[4],
                    "t_ms": float(match[5]), **extra})
    return out


@pytest.fixture
def node(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=13, counts={name: 1 for name in mc.KINDS} | {"clean_positive_C": 4})
    embed_corpus(corpus)
    node = Node(corpus, tmp_path)
    node.rebuild()
    return node


def envelope_for(node, payload=PAYLOAD, request_id=REQUEST_ID):
    grant_id = node.search_raw["binding"]["grant_id"]
    with node.ledger._transaction() as conn:
        node.protocol._sync_protection(conn)
    from tests.permissions_v2.message_search_harness import owner
    with owner():
        authority = node.ledger.authority_snapshot(grant_id, now=node.now[0])
    body = {**authority.model_dump(), "version": "topos-grantee-envelope/v2", "kid": "cp-key", "request_id": request_id,
            "request_type": "permissions.v2.search", "request_hash": request_digest("permissions.v2.search", payload),
            "issued_at": node.now[0], "expires_at": node.now[0] + 100}
    return sign_envelope(parse_envelope(body, signed=False), node.cp_key)


def relayed(node, monkeypatch, *, payload=PAYLOAD, request_id=REQUEST_ID):
    """The relay frame, with the real Runtime.message_search over the harness's index (fake embedder)."""
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED", "true")
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(node.cp_key.public_key().public_bytes_raw()).decode())
    monkeypatch.setattr(search_transport.time, "time", lambda: node.now[0])
    runtime = Runtime.__new__(Runtime)
    runtime.protocol = node.protocol
    runtime.message_search_index = lambda: node.index

    def message_search():
        adapter = Runtime.message_search(runtime)
        adapter.embedder = fake_embedder
        return adapter
    served = SimpleNamespace(protocol=node.protocol, message_search=message_search)
    monkeypatch.setattr(search_transport, "get_runtime", lambda: served)  # one runtime, as still_current() requires
    message = {"id": request_id, "type": search_transport.MESSAGE_TYPE,
               "payload": {"envelope": envelope_for(node, payload, request_id).model_dump(), "intent": payload}}
    stamp = {"v": 1, "cls": "third_party", "client_id": "client-2", "acting_user": "actor-1", "iat": node.now[0],
             "exp": node.now[0] + 100}
    stamp["sig"] = base64.b64encode(node.cp_key.sign(canonical_signing_payload(
        stamp, msg_id=message["id"], msg_type=message["type"]))).decode()
    message["principal_stamp"] = stamp
    return message


class Socket:
    def __init__(self, on_send=None):
        self.sent, self.on_send = [], on_send

    async def send(self, value):
        if self.on_send:
            self.on_send()
        self.sent.append(value)


REFUSAL = json.dumps({"code": 403, "error": "permission_denied", "id": REQUEST_ID, "status": "error",
                      "type": "permissions_v2_message_search"}, separators=(",", ":"), sort_keys=True)


def hold_gate(seconds: float, *, name: str = "gate-holder"):
    """Hold the write gate on another thread; returns once it is held."""
    held = threading.Event()

    def hold():
        with write_gate.with_db_write():
            held.set()
            time.sleep(seconds)
    thread = threading.Thread(target=hold, name=name, daemon=True)
    thread.start()
    held.wait(5)
    return thread


# -- the join key ------------------------------------------------------------------

def test_correlation_id_is_the_vector_both_services_pin():
    assert search_timing.correlation_id("00000000-0000-4000-8000-000000000000") == "3a23256a6888cdc5"
    assert search_timing.correlation_id(REQUEST_ID) != search_timing.correlation_id(REQUEST_ID + "x")


# -- timing off: nothing happens ---------------------------------------------------

def test_off_emits_nothing_and_the_sweeper_sweeps_as_before(monkeypatch, caplog):
    monkeypatch.delenv(FLAG, raising=False)
    calls = []
    index = SimpleNamespace(sweep=lambda: calls.append(1) or 4)
    with caplog.at_level("INFO", logger=LOGGER):
        assert search_timing.timed_sweep(index) == 4
        assert search_timing.for_adapter() is None
        off = search_timing.transport()
        off.bound(REQUEST_ID)
        with off.active(), off.span("send"):
            off.submitted("adapter"); off.started("adapter"); off.ended("adapter"); off.resumed("adapter")
            off.asking(); off.acquired("send_check")
        off.finish("ok")
    assert calls == [1]
    assert parsed(caplog) == []


# -- the write gate ----------------------------------------------------------------

def test_sweep_hold_times_the_sweepers_own_wait_and_its_hold(monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "true")

    class Index:
        calls = 0

        def sweep(self):
            with write_gate.with_db_write():  # as SearchIndexService.sweep does, first thing
                time.sleep(0.05)
            Index.calls += 1
            return 2
    competitor = hold_gate(0.12)
    with caplog.at_level("INFO", logger=LOGGER):
        assert search_timing.timed_sweep(Index()) == 2
    competitor.join()
    [line] = parsed(caplog)
    assert Index.calls == 1
    assert line["stage"] == "sweep_hold" and line["corr"] == "-" and line["removed"] == "2"
    assert 45 <= line["ms"] < 1000 and float(line["wait_ms"]) >= 60
    assert abs(float(line["start_ms"]) + line["ms"] - line["t_ms"]) < 50
    assert write_gate._WRITE_LOCK.acquire(timeout=1)  # released afterwards
    write_gate._WRITE_LOCK.release()


def test_gate_times_the_wait_of_a_section_that_enters_the_gate_first(monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "true")
    timing = search_timing.SearchTiming(corr="0123456789abcdef")
    competitor = hold_gate(0.1)
    with caplog.at_level("INFO", logger=LOGGER):
        with timing.gate("runtime_setup"):
            with write_gate.with_db_write():  # re-entered at once
                pass
    competitor.join()
    [line] = parsed(caplog)
    assert line["stage"] == "gate_wait" and line["point"] == "runtime_setup" and line["ms"] >= 60


def test_acquired_reads_the_gates_own_stamp_and_logs_only_after_release(monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "true")
    timing = search_timing.SearchTiming(corr="0123456789abcdef")
    with caplog.at_level("INFO", logger=LOGGER):
        timing.asking()
        competitor = hold_gate(0.1)
        with write_gate.with_db_write():
            timing.acquired("recheck")
            assert parsed(caplog) == []  # no log write while the gate is held
        timing.flush()
    competitor.join()
    [line] = parsed(caplog)
    assert line["stage"] == "gate_wait" and line["point"] == "recheck" and line["ms"] >= 60


def test_probe_names_only_the_holders_thread_and_code_site(monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "true")
    timing = search_timing.SearchTiming()
    with caplog.at_level("INFO", logger=LOGGER):
        timing.probe("admit")
        competitor = hold_gate(0.3, name="p2c-index-sweep")
        time.sleep(0.05)
        timing.probe("index_load")
    competitor.join()
    free, held = parsed(caplog)
    assert (free["point"], free["holder"], free["site"], free["ms"]) == ("admit", "none", "-", 0.0)
    assert held["point"] == "index_load" and held["holder"] == "p2c-index-sweep" and held["ms"] >= 30
    assert held["site"].startswith("test_search_timing_attribution.py:") and held["site"].endswith(":hold")


# -- a whole search through the transport -----------------------------------------

def by_stage(lines):
    out = {}
    for line in lines:
        out.setdefault(line["stage"], []).append(line)
    return out


@pytest.mark.asyncio
async def test_a_search_is_attributed_end_to_end_without_logging_what_it_is(node, monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "true")
    message = relayed(node, monkeypatch)
    socket = Socket()
    with caplog.at_level("INFO", logger=LOGGER):
        await search_transport.dispatch_message_search(socket, message)
    [frame] = [json.loads(value) for value in socket.sent]
    assert frame["status"] == "ok" and frame["payload"]["output"]["records"]

    lines = parsed(caplog)
    corr = search_timing.correlation_id(REQUEST_ID)
    assert {line["corr"] for line in lines} == {corr} and len({line["run"] for line in lines}) == 1
    stages = by_stage(lines)
    for stage in ADAPTER + ("pre_adapter", "send_check", "send", "transport_total"):
        assert len(stages[stage]) == 1, stage
    assert sorted(line["hop"] for line in stages["queue_wait"]) == ["adapter", "send_check"]
    assert sorted(line["point"] for line in stages["gate_wait"]) == ["recheck", "runtime_setup", "send_check"]
    assert sorted(line["point"] for line in stages["gate_probe"]) == ["admit", "index_load"]
    [total] = stages["transport_total"]
    assert total["outcome"] == "ok" and float(total["sent_at"]) >= float(total["recv_at"])

    # The partition: nothing counted twice, and next to nothing left over.
    one = {stage: stages[stage][0]["ms"] for stage in stages if len(stages[stage]) == 1}
    parts = (one["pre_adapter"] + sum(line["ms"] for line in stages["queue_wait"]) + sum(one[s] for s in ADAPTER)
             + one["send_check"] + one["send"])
    assert parts <= total["ms"] + 1.0
    assert total["ms"] - parts <= max(25.0, 0.2 * total["ms"])
    waits = {line["point"]: line["ms"] for line in stages["gate_wait"]}
    assert waits["runtime_setup"] <= one["runtime_setup"] + 0.01 and waits["recheck"] <= one["recheck"] + 0.01
    # The send check, split: its gate wait, then the ledger (open, protection, authority, commit), then check_own.
    [check] = stages["send_check"]
    split = [float(check[f"{part}_ms"]) for part in ("open", "protection", "authority", "commit", "check_own")]
    assert min(split) >= 0 and float(check["check_own_ms"]) > 0
    assert waits["send_check"] + sum(split) <= check["ms"] + 0.01
    assert check["ms"] - waits["send_check"] - sum(split) <= max(2.0, 0.1 * check["ms"])

    # Nothing about the search itself: no query term, request id, grant, actor, client, record or content.
    text = " ".join(record.getMessage() for record in caplog.records if record.name == LOGGER)
    canaries = ["roadmap", "review", REQUEST_ID, node.search_raw["binding"]["grant_id"], "actor-1", "client-2"]
    canaries += [record["record_id"] for record in frame["payload"]["output"]["records"]]
    canaries += [record["content"][:12] for record in frame["payload"]["output"]["records"]]
    assert not [canary for canary in canaries if canary in text]


@pytest.mark.asyncio
async def test_timing_changes_neither_the_answer_nor_the_gate_at_send(node, monkeypatch, caplog):
    held_at_send = []

    def probe():
        got = []

        def take():
            got.append(write_gate._WRITE_LOCK.acquire(timeout=1))
            if got[0]:
                write_gate._WRITE_LOCK.release()
        thread = threading.Thread(target=take)
        thread.start()
        thread.join()
        held_at_send.append(not got[0])

    outputs = []
    for flag, request_id in ((None, "timing-off-1"), ("true", "timing-on-1")):
        if flag is None:
            monkeypatch.delenv(FLAG, raising=False)
        else:
            monkeypatch.setenv(FLAG, flag)
        socket = Socket(on_send=probe)
        await search_transport.dispatch_message_search(socket, relayed(node, monkeypatch, request_id=request_id))
        [frame] = [json.loads(value) for value in socket.sent]
        assert frame["status"] == "ok"
        outputs.append(frame["payload"]["output"])
    assert outputs[0] == outputs[1]
    assert held_at_send == [False, False]


def refused_index_missing(node):
    purge(node.index.root, "grant-search")


def refused_index_stale(node):
    node.activate({**node.search_raw, "policy_version_id": "policy-2"}, generation=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("prepare", [refused_index_missing, refused_index_stale], ids=["index_missing", "index_stale"])
async def test_a_refusal_is_the_same_bytes_with_timing_on(node, monkeypatch, caplog, prepare):
    monkeypatch.setenv(FLAG, "true")
    message = relayed(node, monkeypatch)
    prepare(node)
    socket = Socket()
    with caplog.at_level("INFO", logger=LOGGER):
        await search_transport.dispatch_message_search(socket, message)
    assert socket.sent == [REFUSAL]
    [total] = by_stage(parsed(caplog))["transport_total"]
    assert total["outcome"] == "error" and total["corr"] == search_timing.correlation_id(REQUEST_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["disabled", "bad_stamp", "changed_id"])
async def test_a_door_refusal_is_the_same_bytes_and_carries_no_correlation(node, monkeypatch, caplog, door):
    monkeypatch.setenv(FLAG, "true")
    message = relayed(node, monkeypatch)
    if door == "disabled":
        monkeypatch.delenv("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED")
    elif door == "bad_stamp":
        message["principal_stamp"]["sig"] = "invalid"
    else:
        message["payload"]["envelope"]["request_id"] = "other"
    socket = Socket()
    with caplog.at_level("INFO", logger=LOGGER):
        await search_transport.dispatch_message_search(socket, message)
    assert socket.sent == [REFUSAL]
    lines = parsed(caplog)
    assert [line["stage"] for line in lines] == ["transport_total"]
    assert lines[0]["corr"] == "-" and lines[0]["outcome"] == "error"

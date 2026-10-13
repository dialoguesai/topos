"""NR166 R0: attribution of the write gate and of the "nothing changed" fast paths, behind its own switch.

Pinned here:
- off by default, bound or not, and inert without the timing switch: no observer on the write gate, no line;
- with timing alone, a search writes exactly the lines it wrote before;
- with attribution on, both doors release the same bytes, write the same receipts and send a frame with the same
  keys as with everything off;
- every attribution line fits the timing grammar and carries only code locations, closed names, counts and
  durations: no query, id, record, grant, actor or token value;
- a gated read's line names the caller of the read, and a request's own sections contain its nested ones;
- each fast path reports a hit while nothing moves, and on a miss names the token part that moved;
- the process-wide tables see every thread, and are never written from inside the gate.

Invented evidence only (the batch suite's p2c-v3 node and the BL-157 journal share); no model, port or network.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import logging
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from tests.permissions_v2.message_search_harness import owner, recipient
from tests.permissions_v2.test_bl157_basis_keeps_serving import (  # noqa: F401 -- the fixtures
    assess, journal_database, served, sharing,
)
from tests.permissions_v2.test_message_search_batch import QUERIES, comparable, ledger_rows, node, written  # noqa: F401
from tests.permissions_v2.test_message_search_refusals import signed
from tests.permissions_v2.test_search_timing_attribution import FIELDS, FLAG, LINE, LOGGER, Socket, relayed
from tests.permissions_v2.test_search_verification import UNRELATED, commit
from topos.permissions_v2 import search_timing, search_transport, switches
from topos.permissions_v2.canonical import canonical_bytes
from topos.permissions_v2.search_release import MessageSearchRelease
from topos.relay_stamp import canonical_signing_payload
from topos.storage.db import write_gate
from topos.storage.db.write_gate import with_db_write

pytestmark = pytest.mark.public

ATTRIBUTION = "TOPOS_PERMISSIONS_V2_SEARCH_ATTRIBUTION"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2" / "search_timing_attribution.py"
OWN_STAGES = {"gate_section", "fast_path", "gate_site", "fast_path_total", "attribution_dropped", "refusal",
              "refusal_total", "index_stale", "index_removed", "index_rebuild", *search_timing.ANSWER_STAGES}
OWN_FIELDS = {"at", "site", "caller", "nested", "n", "wait_ms", "longest_ms", "longest_wait_ms", "window_ms", "path",
              "outcome", "moved", "end_ms", "door", "reason", "share", "class", "state", "members", "at_ms", "mode"}
LOCATION = re.compile(r"^(?:-|unknown|search_timing\.gate:[a-z_]+|[A-Za-z0-9_]+\.py:\d+:[A-Za-z0-9_<>]+)$")
PATHS = {"n3a_boundary", "n3a_digest", "n5_keep", "n5_send", "bl171_guard", "r1_confirm"}
MOVED = {"-", "not_kept", "unreadable", "identity", "file_replaced", "other", "data_version", "file_main", "file_wal",
         "file_journal", "floor", "generation", "revision", "identity", "directories", "families", "rubric", "marker",
         "snapshots", "index", "keys", "ledger", "canonical", "reviews"}


@pytest.fixture(autouse=True)
def attribution_ends_with_the_test(monkeypatch):
    """Each test starts with both switches unset and leaves no observer on the write gate behind."""
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.delenv(ATTRIBUTION, raising=False)
    search_timing.attributing()
    search_timing.totals(reset=True)
    yield
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.delenv(ATTRIBUTION, raising=False)
    search_timing.attributing()
    search_timing._close_thread()
    search_timing.totals(reset=True)
    assert write_gate._section_observer is None


def on(monkeypatch) -> None:
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setenv(ATTRIBUTION, "true")


def serve(node, monkeypatch) -> None:
    """The transport's collaborators with the real Runtime.message_search, so timing reaches each adapter."""
    relayed(node, monkeypatch, request_id="nr166-setup")
    monkeypatch.setenv(search_transport.BATCH_FLAG, "true")


def stamped(node, message) -> dict:
    stamp = {"v": 1, "cls": "third_party", "client_id": "client-2", "acting_user": "actor-1", "iat": node.now[0],
             "exp": node.now[0] + 100}
    stamp["sig"] = base64.b64encode(node.cp_key.sign(canonical_signing_payload(
        stamp, msg_id=message["id"], msg_type=message["type"]))).decode()
    return {**message, "principal_stamp": stamp}


async def single(node, payload, request_id) -> dict:
    envelope = signed(node, payload=payload, request_id=request_id)
    socket = Socket()
    await search_transport.dispatch_message_search(socket, stamped(node, {
        "id": request_id, "type": search_transport.MESSAGE_TYPE,
        "payload": {"envelope": envelope.model_dump(), "intent": payload}}))
    [frame] = [json.loads(value) for value in socket.sent]
    return frame


async def batch(node, payloads, batch_id) -> dict:
    items = [{"envelope": signed(node, payload=payload, request_id=f"{batch_id}:{number}").model_dump(),
              "intent": payload} for number, payload in enumerate(payloads)]
    socket = Socket()
    await search_transport.dispatch_message_search_batch(socket, stamped(node, {
        "id": batch_id, "type": search_transport.BATCH_MESSAGE_TYPE,
        "payload": {"items": items, "respond_by": (node.now[0] + 60) * 1000}}))
    [frame] = [json.loads(value) for value in socket.sent]
    return frame


def answer(node, question: str):
    """The answers door's retrieval, behind the adapter the runtime hands out."""
    grant_id = node.search_raw["binding"]["grant_id"]
    with node.ledger._transaction() as conn:
        node.protocol._sync_protection(conn)
    with owner():
        authority = node.ledger.authority_snapshot(grant_id, now=node.now[0])
    with recipient():
        adapter = search_transport.get_runtime().message_search()
        _current, _policy, output, decision = adapter.retrieve_for_answer(
            grant_id=grant_id, question=question, admitted_authority=authority)
    return canonical_bytes(output.model_dump()), canonical_bytes(decision.model_dump())


def lines(caplog) -> list[dict]:
    """Every timing line, each checked against the timing grammar and its closed field names."""
    out = []
    for record in caplog.records:
        if record.name != LOGGER:
            continue
        match = LINE.match(record.getMessage())
        assert match, record.getMessage()
        extra = dict(pair.split("=", 1) for pair in match.group(6).split())
        assert set(extra) <= FIELDS | OWN_FIELDS | {"n", "item"}, extra
        out.append({"run": match[1], "stage": match[2], "ms": float(match[3]), "corr": match[4], **extra})
    return out


def load_script():
    spec = importlib.util.spec_from_file_location("search_timing_attribution", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- off by default --------------------------------------------------------------------------------------------------

def test_attribution_is_off_on_every_node_and_does_nothing_without_timing(monkeypatch):
    item = switches.SEARCH_ATTRIBUTION
    assert (item.kind, item.unbound, item.bound) == ("bool", False, False)
    assert switches.on(item, {}) is False
    assert search_timing.attributing() is False and write_gate._section_observer is None
    monkeypatch.setenv(ATTRIBUTION, "true")     # its own switch alone: it rides on timing's lines, so still off
    assert search_timing.attributing() is False and write_gate._section_observer is None
    search_timing.fast_path("n5_send", "hit")
    search_timing.reuse("n3a_boundary", False, ("canonical", 1, ()), ("canonical", 2, ()))
    assert search_timing.totals() == {"sites": {}, "paths": {}, "refusals": {}}
    assert search_timing.emit_totals(every=0) is False
    monkeypatch.setenv(FLAG, "true")
    assert search_timing.attributing() is True and write_gate._section_observer is not None
    monkeypatch.setenv(ATTRIBUTION, "false")    # switched off again: the observer leaves the gate at the next read
    assert search_timing.attributing() is False and write_gate._section_observer is None


@pytest.mark.asyncio
async def test_with_timing_alone_both_doors_write_no_attribution_line(node, monkeypatch, caplog):
    serve(node, monkeypatch)
    monkeypatch.setenv(FLAG, "true")
    with caplog.at_level("INFO", logger=LOGGER):
        assert (await single(node, QUERIES[0], "timing-only-single"))["status"] == "ok"
        assert (await batch(node, QUERIES[:3], "timing-only-batch"))["status"] == "ok"
        answer(node, QUERIES[0]["query"])
        search_timing.timed_sweep(node.index)
    seen = lines(caplog)
    assert {"recheck", "send_check", "transport_total", "sweep_hold"} <= {line["stage"] for line in seen}
    assert not [line for line in seen if line["stage"] in OWN_STAGES]
    assert not [line for line in seen if set(line) & (OWN_FIELDS - FIELDS - {"n"})]
    assert write_gate._section_observer is None
    assert search_timing.totals() == {"sites": {}, "paths": {}, "refusals": {}}


# --- the answer is the same bytes ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_attribution_changes_no_released_byte_no_receipt_and_no_frame_key(node, monkeypatch, caplog):
    serve(node, monkeypatch)

    async def one_pass(tag):
        before = written(node)
        frames = [await single(node, QUERIES[0], f"{tag}-single"), await batch(node, QUERIES[:3], f"{tag}-batch")]
        answers = [answer(node, payload["query"]) for payload in QUERIES[:3]]
        ids = [f"{tag}-single"] + [f"{tag}-batch:{number}" for number in range(3)]
        return frames, answers, [ledger_rows(node, request_id) for request_id in ids], written(node) - before

    off = await one_pass("off")
    on(monkeypatch)
    with caplog.at_level("INFO", logger=LOGGER):
        with_it = await one_pass("on")
    stages = {line["stage"] for line in lines(caplog)}
    assert {"gate_section", "fast_path", "answer_recheck", "answer_walk"} <= stages, "attribution did not run"

    names = {"request_id", "envelope_hash", "signature"}   # what names the request: its id and what covers the id

    def results(frame):
        payload = frame["payload"]
        return [payload] if "result" in payload else payload["items"]
    for before, after in zip(off[0], with_it[0]):
        assert before["status"] == after["status"] == "ok"
        assert sorted(before) == sorted(after) and sorted(before["payload"]) == sorted(after["payload"])
        for one, two in zip(results(before), results(after)):
            assert one["output"]["records"], "nothing was released: the comparison would be empty"
            assert canonical_bytes(one["output"]) == canonical_bytes(two["output"])
            assert sorted(one) == sorted(two) and sorted(one["result"]) == sorted(two["result"])
            assert {key for key in one["result"] if one["result"][key] != two["result"][key]} <= names
    assert off[1] == with_it[1] and all(output for output, _decision in off[1])
    for (status, (receipt, decision)), (status_on, (receipt_on, decision_on)) in zip(off[2], with_it[2]):
        assert status == status_on == "checkpointed"
        assert comparable(receipt) == comparable(receipt_on) and decision == decision_on
        assert sorted(receipt) == sorted(receipt_on)
    assert off[3] == with_it[3] == 8   # four searches, each its request row and its receipt; the retrievals none


# --- what a line may say ---------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_every_attribution_line_is_content_free(node, monkeypatch, caplog):
    serve(node, monkeypatch)
    on(monkeypatch)
    with caplog.at_level("INFO", logger=LOGGER):
        one = await single(node, QUERIES[0], "content-single")
        many = await batch(node, QUERIES[:3], "content-batch")
        answer(node, QUERIES[1]["query"])
        search_timing.timed_sweep(node.index)
        assert search_timing.emit_totals(every=0) is True
    own = [line for line in lines(caplog) if line["stage"] in OWN_STAGES]
    assert {"gate_section", "fast_path", "gate_site", "fast_path_total", "answer_index_load", "answer_rank",
            "answer_recheck", "answer_walk"} <= {line["stage"] for line in own}
    for line in own:
        assert set(line) - {"run", "stage", "ms", "corr"} <= OWN_FIELDS | FIELDS, line
        for key in ("site", "caller"):
            assert key not in line or LOCATION.match(line[key]), line
        assert line.get("path", "n5_send") in PATHS and line.get("outcome", "hit") in {"hit", "miss", "first", "skip"}
        for part in line.get("moved", "-").split(":"):
            assert part.split(".")[-1] in MOVED and part.split(".")[0] in MOVED, line
        assert line.get("nested", "0") in {"0", "1"} and line.get("n", "1").isdigit()
    # No line of the whole run names the search: no query word, request id, grant, actor, client, record or content.
    text = " ".join(record.getMessage() for record in caplog.records if record.name == LOGGER)
    released = one["payload"]["output"]["records"] + [record for item in many["payload"]["items"]
                                                      for record in item["output"]["records"]]
    canaries = ["roadmap", "budget", "content-single", "content-batch", node.search_raw["binding"]["grant_id"],
                "actor-1", "client-2", "owner-1"]
    canaries += [record["record_id"] for record in released] + [record["content"][:12] for record in released]
    assert released and not [canary for canary in canaries if canary in text]


def test_what_moved_is_named_by_part_never_by_value():
    files = (("dev", 1, 10), ("dev", 2, 20), None)
    kept = ("canonical", 41, files)
    assert search_timing._moved(kept, ("canonical", 42, files)) == "data_version"
    assert search_timing._moved(kept, ("canonical", 42, (files[0], ("dev", 2, 99), None))) == "data_version:file_wal"
    reviews = ("reviews", 7, files, "floor-digest", 3, "revision", ("dev", 5))
    assert search_timing._moved(reviews, ("reviews", 7, files, "another-floor", 4, "revision", ("dev", 5))) == "floor:generation"
    send = {"canonical": kept, "reviews": reviews, "index": ("dev", 9), "ledger": ("row", "digest-of-rows")}
    moved = search_timing._moved(send, {**send, "canonical": ("canonical", 42, files), "ledger": ("row", "another")})
    assert moved == "canonical.data_version:ledger"
    assert search_timing._moved(send, {**send, "index": None}) == "index"
    assert search_timing._moved("not-a-token", 3) == "other"
    for text in ("floor-digest", "another-floor", "digest-of-rows", "41", "42"):
        assert text not in moved


# --- the gated read's caller, and a request's own hold ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_gated_read_names_its_caller_and_a_requests_own_sections_hold_its_nested_ones(node, monkeypatch, caplog):
    serve(node, monkeypatch)
    on(monkeypatch)
    held = []

    class HeldAtWrite(logging.Handler):
        def emit(self, record):   # on the writing thread: does it hold the gate as an attribution line is written?
            if record.getMessage().split(" stage=")[1].split()[0] in OWN_STAGES:
                held.append(write_gate._WRITE_LOCK._is_owned())
    watch = HeldAtWrite()
    logging.getLogger(LOGGER).addHandler(watch)
    try:
        with caplog.at_level("INFO", logger=LOGGER):
            answer(node, QUERIES[0]["query"])
            assert (await batch(node, QUERIES[:3], "sections-batch"))["status"] == "ok"
    finally:
        logging.getLogger(LOGGER).removeHandler(watch)
    seen = lines(caplog)
    # Stages are reported from inside the gate too; no attribution line is written until it is released.
    assert len(held) >= 20 and not any(held)
    report = load_script().load_attribution((line["run"], line["stage"], line["ms"], line, None) for line in seen)
    doors = {entry["door"]: entry for entry in report["requests"].values()}
    assert set(doors) == {"answers", "search"}
    for door, function in (("answers", "retrieve_for_answer"), ("search", "_decide_batch")):
        sections = doors[door]["sections"]
        reads = [section for section in sections if section["site"].endswith(":_read")]
        assert reads and all(section["nested"] == 1 for section in reads)
        assert {section["caller"].rsplit(":", 1)[1] for section in reads} == {function}
        assert all(re.match(r"^search_release\.py:\d+:", section["caller"]) for section in reads)
        own = [section for section in sections if not section["nested"] and section["site"].endswith(":" + function)]
        assert own and max(section["hold_ms"] for section in own) >= max(section["hold_ms"] for section in reads)
        assert doors[door]["gate_hold_ms"] == pytest.approx(sum(s["hold_ms"] for s in sections if not s["nested"]))
        assert doors[door]["gate_hold_ms"] > 0 and doors[door]["gate_wait_ms"] >= 0
    # The answers door's four stages are one retrieval's whole time, in order, each with its own end.
    answers = [line for line in seen if line["stage"] in search_timing.ANSWER_STAGES]
    assert [line["stage"] for line in answers] == ["answer_index_load", "answer_rank", "answer_recheck", "answer_walk"]
    assert [float(line["end_ms"]) for line in answers] == sorted(float(line["end_ms"]) for line in answers)
    assert len({line["run"] for line in answers}) == 1 and {line["corr"] for line in answers} == {"-"}
    assert float(answers[2]["members_ms"]) > 0 and doors["answers"]["stages_ms"]["answer_walk"] > 0


# --- the fast paths ----------------------------------------------------------------------------------------------------

def outcomes(seen, path):
    return [(line["at"], line["outcome"], line["moved"]) for line in seen
            if line["stage"] == "fast_path" and line["path"] == path]


@pytest.mark.asyncio
async def test_the_fast_paths_hit_while_nothing_moves(node, monkeypatch, caplog):
    serve(node, monkeypatch)
    on(monkeypatch)
    with caplog.at_level("INFO", logger=LOGGER):
        assert (await batch(node, QUERIES[:3], "quiet-batch"))["status"] == "ok"
    seen = lines(caplog)
    assert outcomes(seen, "n3a_boundary") == [("index_load", "first", "-"), ("recheck", "hit", "-")]
    assert outcomes(seen, "n3a_digest") == [("index_load", "first", "-"), ("recheck", "hit", "-")]
    assert outcomes(seen, "n5_keep") == [("checkpoint", "hit", "-")]
    assert outcomes(seen, "n5_send") == [("send_check", "hit", "-")]
    assert outcomes(seen, "bl171_guard") == [("recheck", "skip", "-")]   # the review digest is the index's own


@pytest.mark.asyncio
async def test_a_commit_before_the_send_check_is_a_miss_that_names_the_canonical_database(node, monkeypatch, caplog):
    serve(node, monkeypatch)
    quiet = await batch(node, QUERIES[:3], "before-commit")
    original = MessageSearchRelease.dispatch_batch

    def then_a_commit(self, **kwargs):
        answered = original(self, **kwargs)
        commit(node, UNRELATED)   # an unrelated row lands after the checkpoint, before the send check
        return answered
    monkeypatch.setattr(MessageSearchRelease, "dispatch_batch", then_a_commit)
    on(monkeypatch)
    with caplog.at_level("INFO", logger=LOGGER):
        frame = await batch(node, QUERIES[:3], "after-commit")
    assert frame["status"] == "ok"
    assert [item["output"] for item in frame["payload"]["items"]] == [item["output"] for item in quiet["payload"]["items"]]
    seen = lines(caplog)
    [(at, outcome, moved)] = outcomes(seen, "n5_send")
    assert (at, outcome) == ("send_check", "miss")
    assert moved.split(":")[0] == "canonical.data_version" and all(part.startswith("canonical.") for part in moved.split(":"))
    assert outcomes(seen, "n5_keep") == [("checkpoint", "hit", "-")]
    # So the send check ran its member loop, and found the boundary it had kept no longer provable either.
    boundary = outcomes(seen, "n3a_boundary")
    assert boundary[:2] == [("index_load", "first", "-"), ("recheck", "hit", "-")]
    assert boundary[2][:2] == ("send_check", "miss") and "data_version" in boundary[2][2].split(":")


def test_the_review_guard_is_first_proven_then_reused_then_missed_when_the_review_store_moves(sharing, monkeypatch):
    journal = sharing
    on(monkeypatch)

    def guard():
        return sorted((outcome, moved) for (path, outcome, moved), count in
                      search_timing.totals(reset=True)["paths"].items() if path == "bl171_guard" for _ in range(count))
    before = served(journal)
    assert guard() == [("skip", "-")]                 # the review digest is the one the index was built on
    assess(journal, "e2")                             # a review outside the index moves the digest
    assert served(journal) == before
    assert guard() == [("first", "-")]                # no verdict kept yet for this file: proven in full
    assert served(journal) == before
    assert guard() == [("hit", "-")]
    assess(journal, "e3", domains=["hobbies"], sensitivity="special")   # the review store moves again
    assert served(journal) == before
    [(outcome, moved)] = guard()
    assert outcome == "miss" and "data_version" in moved.split(":") and set(moved.split(":")) <= MOVED


# --- the process-wide tables ---------------------------------------------------------------------------------------

def test_the_process_tables_see_every_thread_and_are_never_written_inside_the_gate(monkeypatch, caplog, tmp_path):
    on(monkeypatch)
    assert search_timing.attributing()

    def another_writer():
        conn = sqlite3.connect(tmp_path / "other.db")
        with with_db_write():
            conn.execute("CREATE TABLE t(n INTEGER)")
            conn.commit()
        conn.close()
    thread = threading.Thread(target=another_writer, name="some-ingestion-thread")
    thread.start()
    thread.join()
    [(site, caller, nested)] = search_timing.totals()["sites"]
    assert site.startswith("test_nr166_attribution.py:") and site.endswith(" in another_writer") and nested is False
    assert caller.startswith("threading.py:")
    with caplog.at_level("INFO", logger=LOGGER):
        with with_db_write():
            assert search_timing.emit_totals(every=0) is False   # this thread holds the gate: nothing is written
        assert not caplog.records
        assert search_timing.emit_totals(every=0) is True
        assert search_timing.emit_totals(every=3600) is False    # and not again before its interval
    sites = [line for line in lines(caplog) if line["stage"] == "gate_site"]
    assert {line["site"].rsplit(":", 1)[1] for line in sites} == {
        "another_writer", "test_the_process_tables_see_every_thread_and_are_never_written_inside_the_gate"}
    assert all(line["corr"] == "-" and line["n"] == "1" for line in sites)
    assert search_timing.totals()["sites"] == {}                 # written and started again


def test_the_analyzer_adds_up_the_lines_and_reports_a_log_without_them_as_before(tmp_path):
    script = load_script()
    run = "a" * 32

    def line(stage, ms, extra):
        return f"INFO permission_search_timing run={run} stage={stage} elapsed_ms={ms:.3f} corr=- t_ms=1.000 {extra}\n"
    log = tmp_path / "node.log"
    log.write_text(
        line("answer_recheck", 300.0, "members_ms=250.000 end_ms=5.000") + line("answer_walk", 100.0, "end_ms=6.000")
        + line("gate_section", 390.0, "at=answer_walk site=search_release.py:292:retrieve_for_answer caller=x.py:1:f "
                                      "nested=0 n=1 wait_ms=40.000 longest_ms=390.000")
        + line("gate_section", 380.0, "at=answer_walk site=evidence.py:786:_read "
                                      "caller=search_release.py:298:retrieve_for_answer nested=1 n=1 wait_ms=0.000 longest_ms=380.000")
        + line("fast_path", 0.0, "at=answer_recheck path=n3a_boundary outcome=miss moved=data_version:file_wal n=1")
        + line("fast_path_total", 0.0, "path=n5_send outcome=hit moved=- n=3 window_ms=0.000")
        + line("fast_path_total", 0.0, "path=n5_send outcome=miss moved=canonical.data_version n=9 window_ms=0.000")
        + line("gate_site", 25.0, "site=job_store.py:294:claim_next_job caller=y.py:2:g nested=0 n=5 wait_ms=1900.000 "
                                  "longest_ms=10.000 longest_wait_ms=900.000 window_ms=60000.000"))
    found = script.load_attribution(script.timing_lines(log))
    [request] = found["requests"].values()
    assert request["door"] == "answers" and request["gate_hold_ms"] == 390.0 and request["gate_wait_ms"] == 40.0
    assert request["stages_ms"] == {"answer_recheck": 300.0, "answer_walk": 100.0}
    assert request["parts_ms"] == {"answer_recheck.members": 250.0}
    assert found["fast_paths"]["n5_send"]["hit_rate"] == 0.25
    assert found["fast_paths"]["n5_send"]["moved"] == {"canonical.data_version": 9}
    assert found["request_fast_paths"]["n3a_boundary"]["moved"] == {"data_version:file_wal": 1}
    assert found["sites"] == [{"site": "job_store.py:294:claim_next_job", "caller": "y.py:2:g", "nested": 0, "n": 5,
                               "hold_ms": 25.0, "wait_ms": 1900.0, "longest_ms": 10.0, "longest_wait_ms": 900.0}]
    assert [site["nested"] for site in found["request_sites"]] == [0, 1]
    out = tmp_path / "report.json"
    assert script.main(["--node-log", str(log), "--json", str(out)]) == 0
    assert json.loads(out.read_text())["attribution"]["answers_door"] == {
        "retrievals": 1, "stages_ms": {"answer_recheck": 300.0, "answer_walk": 100.0}, "gate_hold_ms": 390.0,
        "gate_wait_ms": 40.0}
    plain = tmp_path / "plain.log"
    plain.write_text(f"INFO permission_search_timing run={run} stage=sweep_hold elapsed_ms=1.000 corr=- t_ms=1.000 "
                     "wait_ms=0.000 start_ms=0.000 removed=0\n")
    assert script.main(["--node-log", str(plain), "--json", str(out)]) == 0
    assert "attribution" not in json.loads(out.read_text())

"""IF-3 v1.3: `index_load` splits into check_own (boundary, review digest, per-member checks) and load; the send
check's check_own splits the same way.

WS0's A2b attribution (205 searches) put ~1 s per search in index_load, as much as the send check's
check_own, with nothing saying which part. Pinned here:
- index_load carries check_own_ms and load_ms, and check_own_ms its boundary_ms, members_ms and (for a
  p2c-v2/v3 grant, whose basis includes the review digest) digest_ms; every part lies inside its whole;
- send_check carries the same three parts inside its own check_own_ms;
- a batch's index_load carries the split once, with n;
- the split is durations only: unknown, negative or non-numeric values are dropped, and what a search
  releases is byte-identical with timing on and off;
- the attribution script reports the parts per search and in total.
"""
from __future__ import annotations

import json

import pytest

from tests.permissions_v2.test_search_timing_attribution import (FLAG, LOGGER, REQUEST_ID, Socket, by_stage,  # noqa: F401
    node, parsed, relayed)
from tests.permissions_v2.test_search_timing_attribution_script import cp_lines, load_script
from topos.permissions_v2 import search_timing, search_transport


def within(part, whole):
    return 0 <= float(part) <= float(whole) + 0.01


@pytest.mark.asyncio
async def test_index_load_and_send_check_carry_the_check_own_split(node, monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "true")
    socket = Socket()
    with caplog.at_level("INFO", logger=LOGGER):
        await search_transport.dispatch_message_search(socket, relayed(node, monkeypatch))
    assert json.loads(socket.sent[0])["status"] == "ok"
    stages = by_stage(parsed(caplog))
    [index_load] = stages["index_load"]
    assert within(index_load["check_own_ms"], index_load["ms"]) and within(index_load["load_ms"], index_load["ms"])
    assert float(index_load["check_own_ms"]) + float(index_load["load_ms"]) <= index_load["ms"] + 0.01
    parts = [float(index_load[f"{part}_ms"]) for part in ("boundary", "members")]
    assert "digest_ms" not in index_load  # a p2c-v1 basis has no review digest
    assert min(parts) >= 0 and sum(parts) <= float(index_load["check_own_ms"]) + 0.01
    assert float(index_load["boundary_ms"]) > 0 and float(index_load["members_ms"]) > 0
    [check] = stages["send_check"]
    assert sum(float(check[f"{part}_ms"]) for part in ("boundary", "members")) <= float(check["check_own_ms"]) + 0.01


@pytest.mark.asyncio
async def test_a_direct_grants_split_names_the_review_digest(monkeypatch, caplog, tmp_path):
    from tests.permissions_v2 import direct_search_twins as dst
    direct = dst.build(tmp_path / "direct", members=8, hidden_facts=0, seed=9)
    monkeypatch.setenv(FLAG, "true")

    def message_search():  # what Runtime.message_search does: report to the transport's timing
        timing = search_timing.for_adapter()
        direct.search.observe = timing.observe if timing is not None else None
        return direct.search
    with caplog.at_level("INFO", logger=LOGGER):
        from tests.permissions_v2.test_message_search_refusals import relay_message, signed
        payload = {"query": dst.queries(8, 9, 1)[0], "k": 5}
        message = relay_message(direct, signed(direct, payload=payload, request_id="digest-split"), payload, monkeypatch,
                                request_id="digest-split")
        search_transport.get_runtime().message_search = message_search
        socket = Socket()
        await search_transport.dispatch_message_search(socket, message)
    direct.search.observe = None
    assert json.loads(socket.sent[0])["status"] == "ok"
    stages = by_stage(parsed(caplog))
    [index_load] = stages["index_load"]
    [check] = stages["send_check"]
    for line in (index_load, check):
        parts = sum(float(line[f"{part}_ms"]) for part in ("boundary", "digest", "members"))
        assert parts <= float(line["check_own_ms"]) + 0.01
    assert float(index_load["digest_ms"]) > 0


@pytest.mark.asyncio
async def test_a_batch_carries_the_split_once(node, monkeypatch, caplog):
    from tests.permissions_v2.test_message_search_batch import QUERIES, batch_message
    monkeypatch.setenv(FLAG, "true")
    message = batch_message(node, QUERIES[:3], monkeypatch, batch_id="batch-split")

    def message_search():
        timing = search_timing.for_adapter()
        node.search.observe = timing.observe if timing is not None else None
        return node.search
    search_transport.get_runtime().message_search = message_search
    socket = Socket()
    with caplog.at_level("INFO", logger=LOGGER):
        await search_transport.dispatch_message_search_batch(socket, message)
    node.search.observe = None
    assert json.loads(socket.sent[0])["status"] == "ok"
    lines = [record.getMessage() for record in caplog.records if record.name == LOGGER]
    index_loads = [dict(part.split("=", 1) for part in line.split()[1:]) for line in lines if "stage=index_load" in line]
    assert len(index_loads) == 1 and index_loads[0]["n"] == "3"
    assert {"check_own_ms", "load_ms", "boundary_ms", "members_ms"} <= set(index_loads[0])


def test_the_split_admits_only_known_non_negative_durations(caplog):
    timing = search_timing.SearchTiming("0" * 16)
    with caplog.at_level("INFO", logger=LOGGER):
        timing.observe("index_load", 0.5, check_own_ms=400.0, load_ms=1.25, boundary_ms=-3.0, members_ms="query text",
                       query_ms=9.0, digest_ms=True)
        timing.observe("embed", 0.1, check_own_ms=5.0)  # only index_load carries the split
    [index_load, embed] = [record.getMessage() for record in caplog.records if record.name == LOGGER]
    extra = dict(part.split("=", 1) for part in index_load.split()[6:])
    assert extra == {"check_own_ms": "400.000", "load_ms": "1.250"}
    assert "check_own_ms" not in embed


@pytest.mark.asyncio
async def test_timing_on_releases_exactly_what_timing_off_releases(node, monkeypatch):
    socket_off = Socket()
    await search_transport.dispatch_message_search(socket_off, relayed(node, monkeypatch, request_id="split-off"))
    monkeypatch.setenv(FLAG, "true")
    socket_on = Socket()
    await search_transport.dispatch_message_search(socket_on, relayed(node, monkeypatch, request_id="split-on"))
    off, on = (json.loads(socket.sent[0]) for socket in (socket_off, socket_on))
    assert off["status"] == on["status"] == "ok" and off["payload"]["output"]["records"]
    assert off["payload"]["output"] == on["payload"]["output"]


@pytest.mark.asyncio
async def test_the_attribution_script_reports_the_split(node, monkeypatch, caplog, tmp_path):
    monkeypatch.setenv(FLAG, "true")
    with caplog.at_level("INFO", logger=LOGGER):
        await search_transport.dispatch_message_search(Socket(), relayed(node, monkeypatch))
    records = [record for record in caplog.records if record.name == LOGGER]
    node_log = tmp_path / "node.log"
    node_log.write_text("".join(json.dumps({"level": "INFO", "logger": LOGGER, "message": record.getMessage(),
                                            "timestamp": record.created}) + "\n" for record in records))
    transport = next(float(r.getMessage().split("elapsed_ms=")[1].split()[0]) for r in records
                     if "stage=transport_total" in r.getMessage())
    cp_log = tmp_path / "cp.log"
    cp_log.write_text("".join(cp_lines(search_timing.correlation_id(REQUEST_ID), transport + 40.0)))
    out = tmp_path / "report.json"
    assert load_script().main(["--node-log", str(node_log), "--cp-log", str(cp_log), "--json", str(out)]) == 0
    report = json.loads(out.read_text())
    [row] = report["per_search"]
    assert set(row["index_load_parts_ms"]) == {"check_own", "boundary", "members", "load"}
    assert {"check_own.boundary", "check_own.members"} <= set(row["send_check_parts_ms"])
    totals = report["totals"]["node_stages_ms"]
    assert totals["index_load.check_own"] == pytest.approx(row["index_load_parts_ms"]["check_own"])

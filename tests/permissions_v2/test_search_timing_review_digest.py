"""The review digest's gate wait inside check_own is timed exactly (IF-3 `gate_wait point=*_digest`).

`check_own` reads the review store's authority digest for p2c-v2/v3 grants, and that read enters the
node write gate (evidence.py `_db`) outside every timed section. A1a found one search that spent
14.5 s in index_load that way with no line naming it. Pinned here:
- the wait is measured where the gate is entered, and written only after the gate is released;
- no line when timing is off, when no search's timing is active, or when the gate is already held;
- a relayed p2c-v3 search reports both digest waits, inside their stages, and nothing about itself.
"""
from __future__ import annotations

import json
import logging

import pytest

from tests.permissions_v2 import direct_search_twins as dst
from tests.permissions_v2.test_search_timing_attribution import (FLAG, LOGGER, Socket, by_stage, hold_gate, parsed,
                                                                 relayed)
from topos.permissions_v2 import search_timing, search_transport
from topos.storage.db import write_gate


def test_gate_wait_is_exact_and_written_after_release(monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "true")
    timing = search_timing.TransportTiming()
    holder = hold_gate(0.2)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with timing.active():
            with search_timing.gate_wait("index_load_digest"):
                assert write_gate._WRITE_LOCK._is_owned()
                with write_gate.with_db_write():  # the digest's own entry re-enters at once
                    pass
                assert not caplog.records  # nothing is written while the gate is held
    holder.join()
    [line] = parsed(caplog)
    assert line["stage"] == "gate_wait" and line["point"] == "index_load_digest"
    assert 150.0 <= line["ms"] < 5000.0


def test_gate_wait_is_silent_off_inactive_or_already_held(monkeypatch, caplog):
    timing = search_timing.TransportTiming()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        monkeypatch.delenv(FLAG, raising=False)
        with timing.active(), search_timing.gate_wait("index_load_digest"):
            assert not write_gate._WRITE_LOCK._is_owned()  # off: the old path, the gate untouched
        monkeypatch.setenv(FLAG, "true")
        with search_timing.gate_wait("index_load_digest"):
            assert not write_gate._WRITE_LOCK._is_owned()  # no active search on this thread
        with timing.active(), search_timing.gate_wait(None):
            pass
        with write_gate.with_db_write(), timing.active(), search_timing.gate_wait("send_check_digest"):
            pass  # already held: no wait is possible, and no line inside the gate
    assert parsed(caplog) == []


@pytest.fixture
def direct_node(tmp_path):
    return dst.build(tmp_path / "direct", members=4, hidden_facts=0, seed=5)


@pytest.mark.asyncio
async def test_a_direct_search_reports_both_digest_waits_inside_their_stages(direct_node, monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "true")
    payload = {"query": dst.queries(4, 5, 1)[0], "k": 5}
    message = relayed(direct_node, monkeypatch, payload=payload, request_id="digest-timing-1")
    socket = Socket()
    with caplog.at_level("INFO", logger=LOGGER):
        await search_transport.dispatch_message_search(socket, message)
    [frame] = [json.loads(value) for value in socket.sent]
    assert frame["status"] == "ok"

    stages = by_stage(parsed(caplog))
    waits = {line["point"]: line["ms"] for line in stages["gate_wait"]}
    assert sorted(waits) == ["index_load_digest", "recheck", "runtime_setup", "send_check", "send_check_digest"]
    [index_load] = stages["index_load"]
    [check] = stages["send_check"]
    assert waits["index_load_digest"] <= index_load["ms"] + 0.01
    assert waits["send_check_digest"] <= float(check["check_own_ms"]) + 0.01
    text = " ".join(record.getMessage() for record in caplog.records if record.name == LOGGER)
    canaries = ["digest-timing-1", direct_node.search_raw["binding"]["grant_id"], "actor-1", "client-2"]
    canaries += payload["query"].split()
    canaries += [record["record_id"] for record in frame["payload"]["output"]["records"]]
    assert not [canary for canary in canaries if canary in text]

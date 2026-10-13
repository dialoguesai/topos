"""NR166: the node's own log says why it refused a request, and what happened to a share's index.

On 13 Oct three own-AI asks were refused in one to three seconds and nothing in the log said why: a share with no
index, a paused share and a wrong mode read the same, and finding the cause took reading the index folder and the
ledger. With the attribution switch on (it is off by default) the node now writes:

- one `refusal` line per refused request on the search doors and the answers door, with the one bounded machine
  word the refusal was raised with;
- `index_stale` (with its class), `index_removed` and `index_rebuild` (how long, how it ended, how many members)
  for a share's index, the share named by eight hex characters that are neither its grant id nor its file name.

Pinned here: the wire is unchanged (every refusal is the same bytes with the lines on and off), the word is a
closed machine word and never data, nothing is written with the switch off, and no line is written by a thread
that holds the write gate.

Invented evidence only (the batch suite's p2c-v3 node).
"""
from __future__ import annotations

import json
import logging

import pytest

from tests.permissions_v2.test_message_search_batch import QUERIES, batch_error, node  # noqa: F401
from tests.permissions_v2.test_nr166_attribution import (  # noqa: F401 -- the autouse fixture resets attribution
    ATTRIBUTION, LOCATION, attribution_ends_with_the_test, answer, batch, lines, load_script, on, serve, single,
)
from tests.permissions_v2.test_nr166_decide_outside_gate import owner_only_mark
from tests.permissions_v2.test_search_timing_attribution import FLAG, LOGGER, REFUSAL
from topos.permissions_v2 import search_timing, search_transport
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.search_index import index_path, purge
from topos.permissions_v2.search_release import _denied
from topos.storage.db import write_gate

pytestmark = pytest.mark.public
GRANT = "grant-search"


def refusals(seen) -> list:
    return [(line["door"], line["reason"]) for line in seen if line["stage"] == "refusal"]


def uniform(request_id) -> dict:
    return {**json.loads(REFUSAL), "id": request_id}


@pytest.mark.asyncio
async def test_a_refusal_is_the_same_bytes_and_the_log_names_its_reason(node, monkeypatch, caplog):
    serve(node, monkeypatch)
    on(monkeypatch)
    with caplog.at_level("INFO", logger=LOGGER):
        over_k = await batch(node, [{"query": "roadmap", "k": 20}], "over-k")       # more than the share allows
        purge(node.index.root, GRANT)
        missing = await single(node, QUERIES[0], "no-index")
        missing_batch = await batch(node, QUERIES[:2], "no-index-batch")
        with pytest.raises(PolicyError) as retrieval:
            answer(node, QUERIES[0]["query"])
        monkeypatch.setenv("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED", "false")
        closed = await single(node, QUERIES[0], "door-closed")
    # The wire: one frame for every refusal, whatever the cause.
    assert missing == uniform("no-index") and closed == uniform("door-closed")
    assert over_k == json.loads(batch_error("over-k")) and missing_batch == json.loads(batch_error("no-index-batch"))
    seen = lines(caplog)
    assert retrieval.value.code == "search_index_stale"       # the index check's word for a share with no index
    assert refusals(seen) == [("search_batch", "search_k_above_grant"), ("search", "search_index_stale"),
                              ("search_batch", "search_index_stale"), ("answer_retrieval", "search_index_stale"),
                              ("search", "message_search_disabled")]
    # A search's line joins its other lines: the same run and the same correlation id.
    [line] = [line for line in seen if line["stage"] == "refusal" and line["reason"] == "search_index_stale"
              and line["door"] == "search"]
    assert line["corr"] == search_timing.correlation_id("no-index")
    assert line["run"] in {other["run"] for other in seen if other["stage"] == "transport_total"}
    assert search_timing.totals()["refusals"][("search", "search_index_stale")] == 1


@pytest.mark.asyncio
async def test_without_the_switch_nothing_is_written_and_the_frame_is_the_same(node, monkeypatch, caplog):
    serve(node, monkeypatch)
    monkeypatch.setenv(FLAG, "true")           # timing alone
    purge(node.index.root, GRANT)
    with caplog.at_level("INFO", logger=LOGGER):
        frame = await single(node, QUERIES[0], "timing-only")
    assert frame == uniform("timing-only")
    assert not [line for line in lines(caplog) if line["stage"].startswith(("refusal", "index_"))]
    assert search_timing.totals()["refusals"] == {}


def test_the_reason_is_a_bounded_machine_word_and_never_what_an_exception_says(monkeypatch, caplog):
    on(monkeypatch)
    denied = _denied(PolicyError("authority_stale"))
    assert denied.code == "permission_denied" and denied.reason == "authority_stale" and str(denied) == "permission_denied"
    assert _denied(RuntimeError("a sentence with a name in it")).reason == "other" and _denied().reason is None
    with caplog.at_level("INFO", logger=LOGGER):
        search_timing.refusal("search", denied)
        search_timing.refusal("search", PolicyError("Not A Word, with data"))
        search_timing.refusal("search", RuntimeError("a sentence with a name in it"))
        search_timing.refusal("a door with spaces", PolicyError("grant_inactive"))
    assert refusals(lines(caplog)) == [("search", "authority_stale"), ("search", "other"), ("search", "other"),
                                       ("other", "grant_inactive")]
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "sentence" not in text and "data" not in text


@pytest.mark.asyncio
async def test_a_shares_index_going_stale_being_removed_and_rebuilt_is_logged_by_a_label(node, monkeypatch, caplog):
    serve(node, monkeypatch)
    on(monkeypatch)
    path = index_path(node.index.root, GRANT)
    label = search_timing.share_label(path)
    assert len(label) == 8 and label not in path.name and label not in GRANT and GRANT not in label
    before = node.index._open(path)["count"]
    held = []

    class HeldAtWrite(logging.Handler):
        def emit(self, record):
            if " stage=index_" in record.getMessage():
                held.append(write_gate._WRITE_LOCK._is_owned())
    watch = HeldAtWrite()
    logging.getLogger(LOGGER).addHandler(watch)
    try:
        with caplog.at_level("INFO", logger=LOGGER):
            owner_only_mark(node)                              # a member becomes the owner's alone
            assert await single(node, QUERIES[0], "now-stale") == uniform("now-stale")
            assert not path.exists()
            assert node.rebuild() == {GRANT: "ready"}
            search_timing.write_index_events()
    finally:
        logging.getLogger(LOGGER).removeHandler(watch)
    seen = [line for line in lines(caplog) if line["stage"].startswith("index_")]
    assert [line["stage"] for line in seen] == ["index_stale", "index_removed", "index_rebuild"]
    assert {line["share"] for line in seen} == {label} and held == [False, False, False]
    stale, removed, rebuilt = seen
    assert stale["class"] == "basis"     # an owner-only mark moves the protection state the index was built on
    count = node.index._open(path)["count"]
    assert rebuilt["state"] == "ready" and int(rebuilt["members"]) == count and rebuilt["ms"] > 0
    # Beside the label, the two facts that tell shares apart: the mode the last request saw, and the member count
    # (the index's as it was last read; on the rebuild line, the count that build ended with: the marked one is out).
    assert {line["mode"] for line in seen} == {"records"}
    assert int(stale["members"]) == int(removed["members"]) == before == count + 1
    assert float(stale["at_ms"]) <= float(removed["at_ms"]) <= float(rebuilt["at_ms"])
    assert refusals(lines(caplog)) == [("search", "search_index_stale")]
    text = " ".join(record.getMessage() for record in caplog.records if record.name == LOGGER)
    assert GRANT not in text and path.name not in text and path.stem.split("-", 1)[1] not in text


def test_the_analyzer_reports_refusals_and_each_shares_index(tmp_path):
    script = load_script()
    run, corr = "b" * 32, "c" * 16

    def line(stage, ms, extra, corr_value="-"):
        return (f"INFO permission_search_timing run={run} stage={stage} elapsed_ms={ms:.3f} corr={corr_value} "
                f"t_ms=1.000 {extra}\n")
    log = tmp_path / "node.log"
    log.write_text(
        line("transport_total", 900.0, "outcome=error recv_at=1.000 sent_at=2.000", corr)
        + line("refusal", 0.0, "door=search reason=search_index_missing", corr)
        + line("refusal", 0.0, "door=answer_retrieval reason=release_snapshot_moved")
        + line("index_stale", 0.0, "share=0a1b2c3d mode=records members=561 at_ms=5.000 class=basis")
        + line("index_removed", 0.0, "share=0a1b2c3d mode=records members=561 at_ms=6.000")
        + line("index_rebuild", 640000.0, "share=0a1b2c3d mode=unknown members=568 at_ms=700.000 state=ready"))
    found = script.load_attribution(script.timing_lines(log))
    assert found["refusals"] == [{"door": "answer_retrieval", "reason": "release_snapshot_moved", "n": 1},
                                 {"door": "search", "reason": "search_index_missing", "n": 1}]
    assert found["shares"] == {"0a1b2c3d": {"stale": {"basis": 1}, "removed": 1, "mode": "records", "members": 568,
                                            "rebuilds": [{"ms": 640000.0, "state": "ready", "members": 568}]}}
    out = tmp_path / "report.json"
    assert script.main(["--node-log", str(log), "--json", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["attribution"]["refusals"] == found["refusals"]
    [row] = report["per_search"]
    assert row["refusal_reason"] == "search_index_missing" and row["outcome"] == "error"

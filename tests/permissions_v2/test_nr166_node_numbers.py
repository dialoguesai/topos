"""NR166: one command reads a run's numbers out of a node log.

After each run on a real node, five figures are recorded by hand from its log: requests dropped, asks answered
busy, gate sections of 5 s or more, the longest gate hold by a sharing request, and whether a share lost its index.
`search_timing_attribution.py --node-log <log> --since <epoch> --until <epoch> --numbers` now prints them, with
what each event loop was in when it stalled.

Pinned here: each figure against a log written for the purpose; lines outside the window are not counted; and
nothing of the log but numbers and code locations reaches the output.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from tests.permissions_v2.test_nr166_attribution import load_script

pytestmark = pytest.mark.public
RUN, OTHER = "b" * 32, "d" * 32


def at(clock: str) -> float:
    return datetime.strptime(f"2026-10-13 {clock}", "%Y-%m-%d %H:%M:%S").timestamp()


def timing(clock: str, stage: str, ms: float, extra: str, run: str = RUN) -> str:
    return (f"2026-10-13 {clock} INFO permission_search_timing run={run} stage={stage} elapsed_ms={ms:.3f} corr=- "
            f"t_ms=1.000 {extra}\n")


def plain(clock: str, text: str) -> str:
    return f"2026-10-13 {clock} WARNING {text}\n"


SHED = ("control_plane_inbound_shed window_s=60 aged={aged} gone={gone} oldest_s={oldest} aged_search=1 "
        "aged_search_batch=0 aged_answer_submit=0 aged_answer_fetch=0 gone_search=0 gone_search_batch=0 "
        "gone_answer_submit=0 gone_answer_fetch=0")
LOG = (
    # Before the window: none of this is counted.
    plain("09:59:00", "Dropping inbound request due to saturation pending=128")
    + plain("09:59:01", "[WRITE_GATE] slow section at evidence.py:786 in _read: waited=0.0s held=99.0s — other writers")
    + timing("09:59:02", "refusal", 0.0, "door=answers reason=answer_busy")
    # The window.
    + plain("10:00:01", "Dropping inbound request due to saturation pending=128")
    + plain("10:00:02", "Dropping inbound request due to saturation pending=129")
    + plain("10:00:03", SHED.format(aged=3, gone=1, oldest="41.2"))
    + plain("10:01:03", SHED.format(aged=1, gone=0, oldest="27.0"))
    + plain("10:00:04", "[WRITE_GATE] slow section at evidence.py:786 in _read: waited=0.2s held=12.5s — other writers")
    + plain("10:00:05", "[WRITE_GATE] slow section at ledger.py:210 in _transaction: waited=6.5s held=0.1s — other")
    + plain("10:00:06", "[WRITE_GATE] slow section: waited=0.0s held=5.0s — other writers were blocked")
    + timing("10:00:07", "refusal", 0.0, "door=answers reason=answer_busy")
    + timing("10:00:08", "refusal", 0.0, "door=answers reason=answer_busy")
    + timing("10:00:09", "refusal", 0.0, "door=answers reason=permission_denied")
    + timing("10:00:10", "gate_section", 2600.0, "at=recheck site=evidence.py:786:_read "
             "caller=search_release.py:670:_decide_batch nested=0 n=2 wait_ms=10.000 longest_ms=2400.000")
    + timing("10:00:11", "gate_section", 9000.0, "at=recheck site=evidence.py:1707:_db "
             "caller=evidence.py:900:current nested=1 n=1 wait_ms=0.000 longest_ms=9000.000")
    + timing("10:00:12", "answer_recheck", 3100.0, "end_ms=5.000", OTHER)
    + timing("10:00:12", "gate_section", 3100.0, "at=answer_recheck site=evidence.py:786:_read "
             "caller=search_release.py:440:retrieve_for_answer nested=0 n=1 wait_ms=1.000 longest_ms=3100.000", OTHER)
    + timing("10:00:13", "index_stale", 0.0, "share=0a1b2c3d mode=only members=561 at_ms=5.000 class=basis")
    + timing("10:00:14", "index_removed", 0.0, "share=0a1b2c3d mode=only members=561 at_ms=6.000")
    + timing("10:00:15", "index_rebuild", 64000.0, "share=1f2e3d4c mode=records members=40 at_ms=7.000 state=ready")
    + timing("10:00:16", "loop_lag", 1250.0, "loop=app stalls=2 longest_ms=4100.000 frames=2")
    + timing("10:00:16", "loop_lag_frame", 0.0, "loop=app depth=0 site=write_gate.py:560:batched_writes")
    + timing("10:00:16", "loop_lag_frame", 0.0, "loop=app depth=1 site=record_progress.py:105:_mark_records_processed")
    + timing("10:00:22", "loop_lag_end", 7300.0, "loop=app")
    # After the window.
    + plain("10:05:00", "Dropping inbound request due to saturation pending=128")
    + timing("10:05:01", "index_removed", 0.0, "share=1f2e3d4c mode=records members=40 at_ms=9.000")
)


def test_the_five_numbers_of_a_window(tmp_path):
    script = load_script()
    log = tmp_path / "node.log"
    log.write_text(LOG)
    found = script.node_numbers(log, at("10:00:00"), at("10:02:00"))
    assert found["requests_dropped"] == {"total": 7, "queue_full": 2, "waited_too_long": 4, "connection_gone": 1,
                                         "longest_wait_for_a_slot_s": 41.2}
    assert found["asks_answered_busy"] == 2
    # Held 5 s or more: two of the three lines. The third waited 6.5 s and held 0.1 s.
    assert found["gate_sections_5s"] == {"n": 2, "longest_held_s": 12.5, "at": "evidence.py:786 in _read",
                                         "longest_wait_s": 6.5}
    # A sharing request's own longest single hold: the answers retrieval's, not the nested section inside another.
    assert found["longest_sharing_gate_hold"] == {"ms": 3100.0, "site": "evidence.py:786:_read",
                                                  "caller": "search_release.py:440:retrieve_for_answer",
                                                  "door": "answers"}
    assert found["shares_that_lost_their_index"] == {"n": 1, "shares": [
        {"share": "0a1b2c3d", "mode": "only", "members": 561, "removed": 1, "stale": {"basis": 1}, "rebuilds": 0}]}
    assert found["loops"] == {"app": {"reports": 1, "quiet_stalls": 2, "longest_ms": 7300.0,
                                      "in": {"write_gate.py:560:batched_writes": 1}}}


def test_the_one_command_prints_them_and_nothing_else_of_the_log(tmp_path, capsys):
    script = load_script()
    log = tmp_path / "node.log"
    log.write_text(LOG + plain("10:00:30", "a line about something-private that is no business of this script"))
    assert script.main(["--node-log", str(log), "--since", str(at("10:00:00")), "--until", str(at("10:02:00")),
                        "--numbers"]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["requests_dropped"]["total"] == 7
    assert "something-private" not in printed and "pending=" not in printed
    # The whole report carries the same block.
    out = tmp_path / "report.json"
    assert script.main(["--node-log", str(log), "--since", str(at("10:00:00")), "--until", str(at("10:02:00")),
                        "--json", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["node_numbers"] == json.loads(printed)
    assert report["attribution"]["loops"]["app"]["longest_ms"] == 7300.0


def test_a_log_with_none_of_it_reads_zero(tmp_path):
    script = load_script()
    log = tmp_path / "node.log"
    log.write_text("2026-10-13 10:00:01 INFO nothing to see\n")
    found = script.node_numbers(log)
    assert found["requests_dropped"]["total"] == 0 and found["asks_answered_busy"] == 0
    assert found["gate_sections_5s"]["n"] == 0 and found["longest_sharing_gate_hold"]["ms"] == 0.0
    assert found["shares_that_lost_their_index"] == {"n": 0, "shares": []} and found["loops"] == {}

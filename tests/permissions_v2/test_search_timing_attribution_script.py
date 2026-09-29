"""scripts/permissions_v2/search_timing_attribution.py: joins what the timers really emit, and leaks no id.

The node lines come from a real timed search (so emitter and parser cannot drift apart); the control
plane lines follow control_plane/permissions_v2/search_timing.py's grammar for the same correlation id.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from tests.permissions_v2.test_search_timing_attribution import (FLAG, LOGGER, REQUEST_ID, Socket, node,  # noqa: F401
                                                                 relayed)
from topos.permissions_v2 import search_timing, search_transport

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2" / "search_timing_attribution.py"


def load_script():
    spec = importlib.util.spec_from_file_location("search_timing_attribution", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cp_lines(corr: str, relay_ms: float, *, run="c" * 32, send_at=1_800_000_000_000.0):
    """The control plane's lines for one search, in its logger's format."""
    def line(stage, ms, corr_value, end_at, extra=""):
        return (f"INFO permission_search_timing run={run} stage={stage} elapsed_ms={ms:.3f} corr={corr_value} "
                f"end_at={end_at:.3f}{extra}\n")
    return [line("authentication", 40.0, "-", send_at - 700),
            line("issuance", 20.0, corr, send_at - 600),
            line("consent_before", 230.0, corr, send_at - 350),
            line("routing", 210.0, corr, send_at - 5),
            line("relay", relay_ms, corr, send_at + relay_ms,
                 f" relay_send_at={send_at:.3f} relay_sent_at={send_at + 3:.3f} relay_recv_at={send_at + relay_ms:.3f}"),
            line("revalidation", 48.0, corr, send_at + relay_ms + 48)]


@pytest.mark.asyncio
async def test_real_node_lines_join_the_control_plane_and_attribute_the_remainder(node, monkeypatch, caplog, tmp_path):
    monkeypatch.setenv(FLAG, "true")
    with caplog.at_level("INFO", logger=LOGGER):
        await search_transport.dispatch_message_search(Socket(), relayed(node, monkeypatch))
    records = [record for record in caplog.records if record.name == LOGGER]
    node_log = tmp_path / "node.log"
    node_log.write_text("".join(json.dumps({"level": "INFO", "logger": LOGGER, "message": record.getMessage(),
                                            "timestamp": record.created}) + "\n" for record in records)
                        + "2026-09-28 20:56:37.254 | WARNING | other: a line that is not timing\n")
    transport = next(float(r.getMessage().split("elapsed_ms=")[1].split()[0]) for r in records
                     if "stage=transport_total" in r.getMessage())
    corr = search_timing.correlation_id(REQUEST_ID)
    cp_log = tmp_path / "cp.log"
    cp_log.write_text("".join(cp_lines(corr, transport + 40.0)) + "INFO unrelated line\n")
    out = tmp_path / "report.json"

    assert load_script().main(["--node-log", str(node_log), "--cp-log", str(cp_log), "--json", str(out)]) == 0
    report = json.loads(out.read_text())
    assert (report["node_searches"], report["cp_searches"], report["joined"]) == (1, 1, 1)
    [row] = report["per_search"]
    assert row["network_queue_ms"] == pytest.approx(40.0, abs=0.01)
    assert row["cp_send_ms"] == pytest.approx(3.0) and row["outside_ms"] == pytest.approx(37.0, abs=0.01)
    assert set(row["gate_wait_exact_ms"]) == {"runtime_setup", "recheck", "send_check"}
    assert None not in row["gate_wait_exact_ms"].values()
    assert 0 <= row["node_other_ms"] <= max(25.0, 0.2 * row["transport_total_ms"])
    assert row["send_check_parts_ms"]["check_own"] > 0
    text = out.read_text()
    assert corr not in text and REQUEST_ID not in text and "c" * 32 not in text
    assert not any(record.getMessage().split()[1].split("=")[1] in text for record in records)  # no run id


def node_search(corr, total, *, admit=10.0, probe=None, t_end=100_000.0):
    run = corr * 2
    stages = {"runtime_setup": 1.0, "admit": admit, "index_load": 1000.0, "embed": 4.0, "rank": 1.0,
              "recheck": 900.0, "checkpoint": 5.0, "sign": 1.0}
    lines = [f"permission_search_timing run={run} stage=pre_adapter elapsed_ms=1.000 corr={corr} t_ms={t_end - total:.3f}"]
    for stage, ms in stages.items():
        lines.append(f"permission_search_timing run={run} stage={stage} elapsed_ms={ms:.3f} corr={corr} t_ms=0.000")
    if probe:
        holder, at = probe
        lines.append(f"permission_search_timing run={run} stage=gate_probe elapsed_ms=500.000 corr={corr} "
                     f"t_ms={at:.3f} point=admit holder={holder} site=search_index.py:672:sweep")
    lines.append(f"permission_search_timing run={run} stage=transport_total elapsed_ms={total:.3f} corr={corr} "
                 f"t_ms={t_end:.3f} outcome=ok recv_at=1.000 sent_at=2.000")
    return [json.dumps({"message": line, "timestamp": 1.0}) + "\n" for line in lines]


def test_the_band_test_bounds_a_sweep_wait_by_the_sweepers_own_hold(tmp_path):
    sweep = ("permission_search_timing run=" + "5" * 32 + " stage=sweep_hold elapsed_ms=5000.000 corr=- "
             "t_ms=60000.000 wait_ms=0.000 start_ms=55000.000 removed=0")
    lines = [json.dumps({"message": sweep, "timestamp": 1.0}) + "\n"]
    lines += node_search("a" * 16, 2000.0, t_end=20_000.0)
    lines += node_search("b" * 16, 2100.0, t_end=30_000.0)
    # Two searches that met the sweep at admission: 3900 ms of its hold left at the probe.
    lines += node_search("c" * 16, 6000.0, admit=4000.0, probe=("p2c-index-sweep", 56_100.0), t_end=61_000.0)
    lines += node_search("d" * 16, 5900.0, admit=4000.0, probe=("p2c-index-sweep", 56_100.0), t_end=61_000.0)
    node_log = tmp_path / "node.log"
    node_log.write_text("".join(lines))
    out = tmp_path / "report.json"
    assert load_script().main(["--node-log", str(node_log), "--json", str(out)]) == 0
    report = json.loads(out.read_text())
    high = [row for row in report["per_search"] if row["transport_total_ms"] > 5000]
    assert [row["gate_wait_sweep_bounded_ms"]["admit"] for row in high] == [3900.0, 3900.0]
    assert all(row["gate_holders"] == {"admit": "p2c-index-sweep"} for row in high)
    bands = report["bands"]
    assert (bands["low"]["n"], bands["high"]["n"]) == (2, 2)
    assert bands["high"]["holders_seen"] == {"p2c-index-sweep": 2}
    assert bands["explained_by_gate_waits"] == pytest.approx(3900 / 3900, rel=0.01)
    assert report["sweeps"] == {"n": 1, "hold_ms_p50": 5000.0, "hold_ms_max": 5000.0}



def test_coloured_text_lines_are_read_and_windowed_by_their_own_stamp(tmp_path):
    """The node's console format: ANSI colours around a local-time stamp and the message, no JSON."""
    def coloured(stamp, message):
        return (f"\x1b[38;5;28m{stamp}\x1b[0m \x1b[38;5;244m|\x1b[0m \x1b[38;5;220mINFO\x1b[0m \x1b[38;5;244m|\x1b[0m "
                f"\x1b[38;5;75mtopos.permissions_v2.search_timing\x1b[0m: \x1b[38;5;26m{message}\x1b[0m\n")
    hold = ("permission_search_timing run=" + "6" * 32 + " stage=sweep_hold elapsed_ms={ms}.000 corr=- "
            "t_ms={end}.000 wait_ms=0.004 start_ms={start}.000 removed=0")
    node_log = tmp_path / "node.log"
    node_log.write_text(coloured("2026-09-29 04:10:00.000", hold.format(ms=9000, end=19000, start=10000))
                        + coloured("2026-09-29 04:46:51.256", hold.format(ms=4832, end=40000, start=35168)))
    since = datetime(2026, 9, 29, 4, 30).timestamp()
    out = tmp_path / "report.json"
    assert load_script().main(["--node-log", str(node_log), "--since", str(since), "--json", str(out)]) == 0
    assert json.loads(out.read_text())["sweeps"] == {"n": 1, "hold_ms_p50": 4832.0, "hold_ms_max": 4832.0}


def test_an_if2_v2_report_splits_each_search_into_before_node_and_after(tmp_path):
    """The harness and the node share a clock, so each search span splits exactly at the node's receive and send."""
    def transport(corr, total, recv, sent):
        return json.dumps({"message": f"permission_search_timing run={'7' * 32} stage=transport_total elapsed_ms={total:.3f} "
                                      f"corr={corr} t_ms=5000.000 outcome=ok recv_at={recv:.3f} sent_at={sent:.3f}",
                           "timestamp": 1.0}) + "\n"
    node_log = tmp_path / "node.log"
    node_log.write_text(transport("a" * 16, 6670.0, 1_000_002_450.0, 1_000_009_120.0)
                        + transport("b" * 16, 7810.0, 1_000_014_690.0, 1_000_022_500.0))
    report = {"schema": "IF-2/v2", "cases": [{
        "spans": [{"stage": "grant", "startMs": 0, "durationMs": 100.0},
                  {"stage": "search", "startMs": 200, "durationMs": 11740.0},
                  {"stage": "search", "startMs": 12000, "durationMs": 13450.0}],
        "per_search": [{"status": 200, "sent_at_ms": 1_000_000_000}, {"status": 200, "sent_at_ms": 1_000_012_000}]}]}
    harness = tmp_path / "report.json"
    harness.write_text(json.dumps(report))
    out = tmp_path / "attribution.json"
    assert load_script().main(["--node-log", str(node_log), "--harness", str(harness), "--json", str(out)]) == 0
    result = json.loads(out.read_text())
    assert result["harness"]["paired"] == 2 and result["harness"]["ambiguous"] == 0
    rows = sorted(result["per_search"], key=lambda row: row["transport_total_ms"])
    assert [(row["before_node_ms"], row["after_node_ms"]) for row in rows] == [(2450.0, 2620.0), (2690.0, 2950.0)]
    for row in rows:
        assert row["before_node_ms"] + row["transport_total_ms"] + row["after_node_ms"] == pytest.approx(row["harness_span_ms"])


def test_ids_join_harness_cp_and_node_and_the_cp_legs_land_on_the_harness_clock(tmp_path):
    """One search, CP clock 1,000 ms behind the node's; the analyzer recovers the offset from the relay marks."""
    corr, stray, run = "c0ffee0123456789", "5" * 16, "8" * 32
    def node_line(stage, ms, extra="", corr_value=corr):
        return json.dumps({"message": f"permission_search_timing run={run} stage={stage} elapsed_ms={ms:.3f} "
                                      f"corr={corr_value} t_ms=0.000{extra}", "timestamp": 1.0}) + "\n"
    sweep_start = 50_000.0
    lines = [json.dumps({"message": f"permission_search_timing run={'9' * 32} stage=sweep_hold elapsed_ms=4990.000 corr=- "
                                    f"t_ms={sweep_start + 4990:.3f} wait_ms=0.000 start_ms={sweep_start:.3f} removed=0",
                         "timestamp": 1.0}) + "\n"]
    lines += [node_line("pre_adapter", 1.0), node_line("gate_wait", 3990.0, f" point=runtime_setup start_ms={sweep_start + 1000:.3f}"),
              node_line("runtime_setup", 4000.0), node_line("admit", 10.0), node_line("index_load", 900.0),
              node_line("embed", 5.0), node_line("rank", 1.0), node_line("recheck", 1000.0), node_line("checkpoint", 5.0),
              node_line("sign", 2.0), node_line("queue_wait", 1.0, " hop=adapter executor_ms=0.5 resume_ms=0.5"),
              node_line("send_check", 950.0, " open_ms=0.1 protection_ms=6.0 authority_ms=0.6 commit_ms=0.0 check_own_ms=943.0"),
              node_line("send", 1.0),
              node_line("transport_total", 6900.0, " outcome=ok recv_at=1000000573.000 sent_at=1000007473.000"),
              node_line("transport_total", 999.0, " outcome=ok recv_at=1000020000.000 sent_at=1000020999.000", stray)]
    node_log = tmp_path / "node.log"
    node_log.write_text("".join(lines))
    def cp_line(stage, ms, end_at, extra=""):
        return json.dumps({"message": f"permission_search_timing run={'a' * 32} stage={stage} elapsed_ms={ms:.3f} "
                                      f"corr={corr if stage != 'authentication' else '-'} end_at={end_at:.3f}{extra}"}) + "\n"
    cp_log = tmp_path / "cp.log"
    cp_log.write_text("".join([
        cp_line("authentication", 40, 999_999_090), cp_line("issuance", 20, 999_999_110),
        cp_line("consent_before", 230, 999_999_340), cp_line("routing", 210, 999_999_550),
        cp_line("relay", 6943, 1_000_006_493, " relay_send_at=999999550.000 relay_sent_at=999999553.000 relay_recv_at=1000006493.000"),
        cp_line("revalidation", 50, 1_000_006_543), cp_line("consent_send", 230, 1_000_006_773),
        cp_line("consent_send", 230, 1_000_007_003)]))
    report = {"schema": "IF-2/v2", "cases": [{"spans": [{"stage": "search", "startMs": 0, "durationMs": 8063.0,
                                                        "correlation_id": corr}],
                                             "per_search": [{"status": 200, "sent_at_ms": 1_000_000_000}]}]}
    harness = tmp_path / "report.json"
    harness.write_text(json.dumps(report))
    out = tmp_path / "attribution.json"
    assert load_script().main(["--node-log", str(node_log), "--cp-log", str(cp_log), "--harness", str(harness),
                               "--json", str(out)]) == 0
    result = json.loads(out.read_text())
    assert result["node_searches"] == 1 and result["harness"]["paired_by_id"] == 1  # the stray search is not this run's
    [row] = result["per_search"]
    assert row["clock_offset_ms"] == pytest.approx(1000.0)
    assert row["client_uplink_ms"] == pytest.approx(50.0) and row["client_downlink_ms"] == pytest.approx(60.0)
    assert row["cp_untimed_ms"] == pytest.approx(0.0) and row["cp_stages_ms"] == pytest.approx(1010.0)
    assert row["relay_remainder_ms"] == pytest.approx(6943.0 - 5923.0)
    assert row["gate_wait_holder"] == "sweep" and row["gate_wait_sweep_covered_ms"] == pytest.approx(3990.0)
    h1 = result["totals"]["h1"]
    assert h1["send_check_share"] == pytest.approx(950.0 / 1020.0) and h1["verdict"] == "supported"
    assert corr not in out.read_text() and stray not in out.read_text()


def test_the_replacement_h2_rule_is_the_registered_one():
    """Registered 29 Sep before A2: supported at >= 0.80, rejected below 0.50, >= 10 per group, 100 ms threshold."""
    h2 = load_script().h2_overlap
    def rows(overlap, node, n):
        return [{"outcome": "ok", "sweep_overlap_ms": overlap, "transport_total_ms": node} for _ in range(n)]
    clear = rows(0.0, 5000.0, 12)
    assert h2(rows(3000.0, 7400.0, 12) + clear, resamples=50)["verdict"] == "supported"      # 2400 / 3000 = 0.80
    assert h2(rows(3000.0, 6000.0, 12) + clear, resamples=50)["verdict"] == "rejected"       # 1000 / 3000 = 0.33
    assert h2(rows(3000.0, 6800.0, 12) + clear, resamples=50)["verdict"] == "inconclusive"   # 0.60: between the bounds
    assert h2(rows(3000.0, 9000.0, 9) + clear, resamples=50)["verdict"] == "inconclusive"    # too few overlapping
    small = h2(rows(3000.0, 7400.0, 12) + clear + rows(50.0, 5100.0, 3)
               + [{"outcome": "error", "sweep_overlap_ms": 0.0, "transport_total_ms": 90.0}], resamples=50)
    assert (small["n_excluded_small_overlap"], small["n_not_answered"], small["verdict"]) == (3, 1, "supported")


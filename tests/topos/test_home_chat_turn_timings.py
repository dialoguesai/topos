"""HT lane: one content-free timing row per owner home-chat turn, kept on the owner's node.

Until now the owner's home-chat latency was recorded nowhere durable: the app kept its timing lines in browser
localStorage and dropped them when a turn finished, so no fix could be measured before and after. These tests pin
the three promises the owner was given (15 Oct 2026):

* the row holds only numbers, epoch-millisecond times, closed-set words, random ids, a model tag and a build stamp.
  A field is closed because the validator closes it, not because the app is careful, so every string field is
  probed with a sentence and every number with values the app could not have produced;
* the node stamps its own version and clock, and a resent turn is stored once;
* what is read back is aggregates only, and only the owner's frames reach either type.
"""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from uuid import uuid4

import pytest

import topos.core.handlers as hub
from topos.__version__ import __version__
from topos.core.handlers import dispatch_relay_message, handle_control_plane_request
from topos.home_chat import turn_timings
from topos.principal import OWNER_APP, RELAY_PRINCIPAL, Principal
from topos.storage.db import migrations
from topos.storage.db.migrations.registry import MIGRATIONS, max_migration_order

OWNER = Principal(cls=OWNER_APP, channel="uds")
#: 2026-10-15T00:13:20Z. Every record is dated against this, so no test depends on the machine's clock.
NOW_MS = 1_792_023_200_000
DAY_MS = 86_400_000
#: The probe. If any field passed its value through instead of checking it, this is what would be stored.
PROBE = "the owner asked about a clinic visit next week"


def record(**overrides):
    """A complete, valid turn: a local model, one scope query, an answer of five sentences."""
    base = {
        "schema_version": 1,
        "turn_id": str(uuid4()),
        "session_id": str(uuid4()),
        "submitted_at": NOW_MS - 60_000,
        "ttft_ms": 2_100,
        "total_ms": 9_300,
        "outcome": "ok",
        "error_class": None,
        "model": "qwen3.5:9b-mlx",
        "provider": "ollama",
        "route": "local",
        "llm_calls": 2,
        "llm_ms": 8_000,
        "llm": [
            {"purpose": "decide", "provider": "ollama", "model": "qwen3.5:9b-mlx", "ms": 3_000, "ttft_ms": 900,
             "load_ms": 0, "prefill_ms": 700, "decode_ms": 2_100, "prompt_tokens": 1_200, "completion_tokens": 80,
             "cold": False},
            {"purpose": "synthesize", "provider": "ollama", "model": "qwen3.5:9b-mlx", "ms": 5_000,
             "ttft_ms": 1_000, "load_ms": None, "prefill_ms": 800, "decode_ms": 4_000, "prompt_tokens": 2_400,
             "completion_tokens": 310, "cold": None},
        ],
        "tool_calls": 1,
        "tool_ms": 1_200,
        "tools": [{"name": "query_scope", "ms": 1_200, "ok": True}],
        "scope_queries": 1,
        "scope_query_ms_total": 1_200,
        "scope_query_list": [{"ms": 1_200, "rows": 12, "mode": "summary", "ok": True}],
        "answer_chars": 640,
        "answer_sentences": 5,
        "app_build": "2f097745",
    }
    base.update(overrides)
    return base


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "node.sqlite"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def stored(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='home_chat_turn_timings'").fetchone():
        return []
    return [dict(r) for r in conn.execute("SELECT * FROM home_chat_turn_timings ORDER BY submitted_at")]


def put(conn, *records, now_ms=NOW_MS):
    return turn_timings.record_batch(conn, list(records), node_version="9.9.9-test", now_ms=now_ms)


# --- what is stored -----------------------------------------------------------------------------------------------


def test_a_complete_record_is_stored_with_the_nodes_version_and_clock(conn):
    turn = record()
    assert put(conn, turn, now_ms=NOW_MS + 5) == {"stored": 1, "duplicate": 0, "refused": 0, "refused_fields": []}
    [row] = stored(conn)
    assert row["turn_id"] == turn["turn_id"] and row["session_id"] == turn["session_id"]
    assert (row["ttft_ms"], row["total_ms"], row["outcome"], row["route"]) == (2_100, 9_300, "ok", "local")
    assert row["node_version"] == "9.9.9-test"          # the node's own version, never the client's
    assert row["recorded_at"] == NOW_MS + 5              # the node's clock, beside the client's submitted_at
    assert row["node_gate_wait_ms"] is None              # M5: the node fills this, the client cannot
    assert json.loads(row["llm_json"])[1]["prefill_ms"] == 800
    assert json.loads(row["tools_json"]) == [{"name": "query_scope", "ms": 1_200, "ok": True}]
    assert json.loads(row["scope_queries_json"])[0]["rows"] == 12
    assert (row["answer_chars"], row["answer_sentences"]) == (640, 5)


def test_a_resent_turn_is_stored_once(conn):
    """The app's outbox resends after a network failure; the turn id makes that safe."""
    turn = record()
    put(conn, turn)
    assert put(conn, turn, deepcopy(turn)) == {"stored": 0, "duplicate": 2, "refused": 0, "refused_fields": []}
    assert len(stored(conn)) == 1


def test_optional_measures_may_be_absent_but_never_invented(conn):
    """A turn that produced no token has no TTFT. Null stays null; it is not written as zero."""
    turn = record(ttft_ms=None, outcome="timeout", error_class="engine_timeout", llm=[], tools=[],
                  scope_query_list=[], answer_chars=None, answer_sentences=None, model=None, provider=None,
                  route=None, app_build=None, llm_ms=None, tool_ms=None, scope_query_ms_total=None)
    assert put(conn, turn)["stored"] == 1
    [row] = stored(conn)
    assert row["ttft_ms"] is None and row["answer_chars"] is None and row["error_class"] == "engine_timeout"


# --- what is refused, and that a refusal never repeats what it refused ---------------------------------------------

STRING_FIELDS = [
    ("turn_id",), ("session_id",), ("outcome",), ("error_class",), ("model",), ("provider",), ("route",),
    ("app_build",), ("llm", 0, "purpose"), ("llm", 0, "provider"), ("llm", 0, "model"), ("tools", 0, "name"),
    ("scope_query_list", 0, "mode"),
]


def _set(target, path, value):
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize("path", STRING_FIELDS, ids=lambda p: ".".join(map(str, p)))
def test_a_sentence_cannot_enter_any_string_field(conn, path):
    turn = record(outcome="error") if path == ("error_class",) else record()
    _set(turn, path, PROBE)
    result = put(conn, turn)
    assert result["stored"] == 0 and result["refused"] == 1
    assert result["refused_fields"] == [path[0]]          # the field's NAME, from this module's own vocabulary
    assert PROBE not in json.dumps(result)
    assert stored(conn) == []


def test_a_model_tag_with_spaces_is_refused(conn):
    """The model field is the one open-ended string; its charset is what keeps a sentence out of it."""
    assert put(conn, record(model="my model about the visit"))["refused"] == 1


def test_an_unknown_key_is_refused_without_repeating_it(conn):
    turn = record()
    turn[PROBE] = 1
    result = put(conn, turn)
    assert result["refused"] == 1 and result["refused_fields"] == ["unknown_key"]
    assert PROBE not in json.dumps(result)


def test_an_unknown_key_inside_a_list_item_is_refused(conn):
    turn = record()
    turn["tools"][0]["arguments"] = PROBE
    assert put(conn, turn)["refused_fields"] == ["tools"]


@pytest.mark.parametrize("value", [-1, 1.5, True, "1200", 10**12], ids=repr)
def test_numbers_must_be_whole_non_negative_and_bounded(conn, value):
    assert put(conn, record(total_ms=value))["refused_fields"] == ["total_ms"]


@pytest.mark.parametrize("field", ["node_version", "node_gate_wait_ms", "recorded_at"])
def test_the_client_cannot_supply_what_the_node_stamps(conn, field):
    assert put(conn, record(**{field: 5}))["refused_fields"] == ["unknown_key"]


def test_lists_are_capped_and_never_longer_than_their_count(conn):
    call = record()["llm"][0]
    assert put(conn, record(llm=[call] * 17, llm_calls=17))["refused_fields"] == ["llm"]
    assert put(conn, record(tool_calls=0))["refused_fields"] == ["tools"]


def test_cross_field_rules(conn):
    assert put(conn, record(ttft_ms=10_000, total_ms=9_000))["refused_fields"] == ["ttft_ms"]
    assert put(conn, record(outcome="ok", error_class="network"))["refused_fields"] == ["error_class"]
    assert put(conn, record(submitted_at=NOW_MS + 2 * DAY_MS))["refused_fields"] == ["submitted_at"]
    assert put(conn, record(schema_version=2))["refused_fields"] == ["schema_version"]


def test_one_bad_record_does_not_sink_the_batch(conn):
    result = put(conn, record(), record(outcome=PROBE), record())
    assert (result["stored"], result["refused"]) == (2, 1)


@pytest.mark.parametrize("records", [None, "a string", {"a": 1}, [record()] * 21], ids=["none", "str", "dict", "21"])
def test_a_batch_that_is_not_a_short_list_is_refused_whole(conn, records):
    with pytest.raises(ValueError, match="INVALID_TIMING_BATCH"):
        turn_timings.record_batch(conn, records, node_version="9.9.9-test", now_ms=NOW_MS)
    assert stored(conn) == []


# --- what is read back --------------------------------------------------------------------------------------------


def _seed_two_days(conn):
    day1, day2 = NOW_MS - 2 * DAY_MS, NOW_MS - DAY_MS
    turns = [record(submitted_at=day1 + i, total_ms=t, ttft_ms=t // 4) for i, t in
             enumerate([10_000, 20_000, 30_000, 40_000, 50_000])]
    turns += [record(submitted_at=day2 + i, total_ms=t, ttft_ms=None if t > 90_000 else 1_000,
                     outcome="timeout" if t > 90_000 else "ok", error_class="engine_timeout" if t > 90_000 else None)
              for i, t in enumerate([5_000, 6_000, 95_000])]
    turns.append(record(submitted_at=day2 + 9, total_ms=12_000, model="llama-3.3-70b-instruct",
                        provider="redpill", route="hosted"))
    put(conn, *turns)
    return turns


def test_the_summary_is_nearest_rank_percentiles_per_day_model_and_route(conn):
    turns = _seed_two_days(conn)
    summary = turn_timings.summarize(conn, days=7, now_ms=NOW_MS)
    assert summary["turns"] == 9 and summary["window_days"] == 7
    by_key = {(g["day"], g["model"], g["route"]): g for g in summary["groups"]}
    local_day1 = by_key[("2026-10-13", "qwen3.5:9b-mlx", "local")]
    assert (local_day1["n"], local_day1["total_p50_ms"], local_day1["total_p90_ms"]) == (5, 30_000, 50_000)
    assert (local_day1["ttft_p50_ms"], local_day1["ttft_p90_ms"]) == (7_500, 12_500)
    local_day2 = by_key[("2026-10-14", "qwen3.5:9b-mlx", "local")]
    assert local_day2["outcomes"] == {"ok": 2, "fallback": 0, "timeout": 1, "abort": 0, "error": 0}
    assert local_day2["timeout_rate"] == pytest.approx(0.333, abs=1e-3)
    assert local_day2["ttft_n"] == 2                      # the timed-out turn has no TTFT and is not counted as fast
    hosted = by_key[("2026-10-14", "llama-3.3-70b-instruct", "hosted")]
    assert hosted["n"] == 1 and hosted["total_p50_ms"] == 12_000
    totals = {(t["model"], t["route"]): t for t in summary["totals"]}
    assert totals[("qwen3.5:9b-mlx", "local")]["n"] == 8
    # Aggregates only: no id of any turn or session leaves the node through the summary.
    text = json.dumps(summary)
    assert not any(t["turn_id"] in text or t["session_id"] in text for t in turns)


def test_the_window_excludes_older_turns(conn):
    _seed_two_days(conn)
    assert turn_timings.summarize(conn, days=1, now_ms=NOW_MS)["turns"] == 4


def test_a_node_without_the_table_summarizes_to_nothing(conn):
    assert turn_timings.summarize(conn, days=30, now_ms=NOW_MS) == {
        "window_days": 30, "turns": 0, "groups": [], "totals": []}


# --- the two message types ----------------------------------------------------------------------------------------


@pytest.fixture
def node_db(conn, monkeypatch):
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    return conn


def _frame(msg_type, payload, req_id="req-1"):
    return {"id": req_id, "type": msg_type, "payload": payload}


@pytest.mark.asyncio
async def test_the_relay_records_a_batch_and_reads_the_summary_back(node_db):
    """The control plane's frame carries no stamp and names the signed-in owner, as its home-chat routes do."""
    turn = record(submitted_at=NOW_MS - DAY_MS)
    response = await dispatch_relay_message(
        _frame("record_home_chat_turn_timings", {"user_id": "owner-account", "records": [turn]}))
    assert response["status"] == "ok" and response["payload"]["stored"] == 1
    [row] = stored(node_db)
    assert row["node_version"] == __version__
    summary = await dispatch_relay_message(
        _frame("get_home_chat_turn_timings_summary", {"user_id": "owner-account", "days": 30}, "req-2"))
    assert summary["status"] == "ok" and summary["payload"]["turns"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", ["record_home_chat_turn_timings", "get_home_chat_turn_timings_summary"])
async def test_a_frame_that_names_nobody_is_refused(node_db, msg_type):
    """Both types require the caller's id, so the relay's owner rule always has a name to check."""
    response = await handle_control_plane_request(_frame(msg_type, {"records": [record()], "days": 7}),
                                                  principal=RELAY_PRINCIPAL)
    assert response["status"] == "error" and response["error_code"] == "USER_ID_REQUIRED"
    assert stored(node_db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", ["record_home_chat_turn_timings", "get_home_chat_turn_timings_summary"])
async def test_on_a_bound_node_a_frame_naming_someone_else_is_refused(node_db, monkeypatch, msg_type):
    monkeypatch.setattr("topos.permissions_v2.switches.is_bound", lambda: True)
    monkeypatch.setattr(hub, "_bound_owner_id", lambda: "owner-account")
    # Rule (b) runs at the relay's entry point, the function the app's relay wiring calls.
    response = await dispatch_relay_message(
        _frame(msg_type, {"user_id": "someone-else", "records": [record()], "days": 7}))
    assert response == {"id": "req-1", "status": "error", "code": 403, "error": "owner_mode_required"}
    assert stored(node_db) == []
    owners = await dispatch_relay_message(
        _frame(msg_type, {"user_id": "owner-account", "records": [record()], "days": 7}, "req-9"))
    assert owners["status"] == "ok"


@pytest.mark.asyncio
async def test_a_refused_batch_answers_with_a_code_and_no_values(node_db):
    response = await handle_control_plane_request(
        _frame("record_home_chat_turn_timings", {"user_id": "owner-account", "records": PROBE}), principal=OWNER)
    assert response["status"] == "error" and response["error_code"] == "INVALID_TIMING_BATCH"
    assert PROBE not in json.dumps(response)


@pytest.mark.asyncio
async def test_the_summary_window_is_bounded(node_db):
    response = await handle_control_plane_request(
        _frame("get_home_chat_turn_timings_summary", {"user_id": "owner-account", "days": 10_000}), principal=OWNER)
    assert response["status"] == "ok" and response["payload"]["window_days"] == turn_timings.SUMMARY_MAX_DAYS


# --- schema, retention ----------------------------------------------------------------------------------------------


def test_the_table_is_a_registered_ledger_step():
    spec = next(s for s in MIGRATIONS if s.id == "home_chat_turn_timings_v1")
    assert spec.order == 86 and not spec.always_run
    assert max_migration_order() >= spec.order


def test_upgrade_backs_up_then_creates_the_table_and_refuses_an_older_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOS_BACKUP_DIR", str(tmp_path / "backups"))
    with sqlite3.connect(tmp_path / "database.db") as db:
        migrations.apply_all_migrations(db)
        db.execute("DROP TABLE home_chat_turn_timings")
        db.execute("DELETE FROM wiki_schema_migrations WHERE migration_id='home_chat_turn_timings_v1'")
        db.execute("PRAGMA user_version=85")
        db.commit()
        migrations.reset_ensured_connections()
        backup = migrations.ensure_migrations_applied(db)
        assert backup
        with sqlite3.connect(backup) as before:
            assert not before.execute("SELECT 1 FROM sqlite_master WHERE name='home_chat_turn_timings'").fetchone()
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='home_chat_turn_timings'").fetchone()
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='idx_home_chat_turn_timings_submitted'").fetchone()
        migrations.reset_ensured_connections()
        assert migrations.ensure_migrations_applied(db) is None
        monkeypatch.setattr(migrations, "max_migration_order", lambda: 85)
        with pytest.raises(migrations.DowngradeGuardError):
            migrations.ensure_migrations_applied(db, skip_backup=True)


def test_a_node_whose_ledger_never_ran_still_records(conn):
    """The first write creates the table (one read to check, the gate only when it is missing): a fresh install is
    right without a manual step."""
    assert put(conn, record())["stored"] == 1 and len(stored(conn)) == 1


def test_gc_trims_rows_the_node_recorded_more_than_the_retention_ago(conn):
    from topos.features.lifecycle.gc import apply_audit_retention

    put(conn, record(submitted_at=NOW_MS - DAY_MS))
    real_now = turn_timings.now_ms()
    put(conn, record(submitted_at=real_now - 120 * DAY_MS), now_ms=real_now - 120 * DAY_MS)
    put(conn, record(submitted_at=real_now - 5 * DAY_MS), now_ms=real_now - 5 * DAY_MS)
    before = len(stored(conn))
    removed = apply_audit_retention(conn, days=90)
    assert removed["home_chat_turn_timings"] == 1 and len(stored(conn)) == before - 1
    assert apply_audit_retention(conn, days=0)["home_chat_turn_timings"] == 0


# --- WS0's reader on a census copy ------------------------------------------------------------------------------------


def _load_percentiles_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "query_latency_percentiles.py"
    spec = importlib.util.spec_from_file_location("query_latency_percentiles", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_the_census_script_reports_the_same_percentiles_as_the_node(conn, monkeypatch):
    """Two readers of one table: the node's summary handler and the stdlib-only script WS0 runs on a copy. They
    must agree, or a before/after comparison would compare the readers, not the releases."""
    script = _load_percentiles_script()
    _seed_two_days(conn)
    node = turn_timings.summarize(conn, days=7, now_ms=NOW_MS)
    report = script.build_report(conn, days=7, stale_minutes=60, now_ms=NOW_MS)
    turns = report["home_chat_turns"]
    assert turns["samples"] == node["turns"] == 9
    node_groups = {(g["day"], g["model"], g["route"]): g for g in node["groups"]}
    script_groups = {(g["day"], g["model"], g["route"]): g for g in turns["groups"]}
    assert set(node_groups) == set(script_groups)
    for key, g in node_groups.items():
        s = script_groups[key]
        for field in ("n", "ttft_p50_ms", "ttft_p90_ms", "total_p50_ms", "total_p90_ms", "timeout_rate"):
            assert s[field] == g[field], (key, field)


def test_the_census_script_reports_zero_turns_on_a_copy_without_the_table(conn):
    script = _load_percentiles_script()
    report = script.build_report(conn, days=7, stale_minutes=60, now_ms=NOW_MS)
    assert report["home_chat_turns"]["samples"] == 0 and report["home_chat_turns"]["groups"] == []

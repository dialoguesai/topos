"""The inbound truth lane (VERIFY_CLAIM_WEEKLY_PROGRAM.md §2a): settings, lazy tables, the hook, the job, the pace.

Synthetic only: a migrated temp database with invented rows, and verify_claim v2 replaced by a recorder, so no
model, no network, and never the live node.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests.truth_lane_support import add_contact, add_journal, add_message, add_segment, ago, bind, build_node, connect
from topos.config import truth_inbound as cfg
from topos.ingestion import truth_inbound as lane
from topos.query import truth_checks as store


@pytest.fixture
def node(tmp_path, tmp_path_factory, monkeypatch):
    path = build_node(tmp_path, tmp_path_factory.getbasetemp())
    bind(monkeypatch, path)
    conn = connect(path)
    yield SimpleNamespace(path=path, conn=conn)
    conn.close()


SHIPPED_OWNER_CLAIMS_ONLY = lane.OWNER_CLAIMS_ONLY  # read before any fixture changes it


@pytest.fixture(autouse=True)
def every_speaker(monkeypatch):
    """Most tests here describe the lane's machinery with contacts' rows as fixtures, so they run with the step 1
    owner-only rule off; the tests under "Calibrate step 1" turn it back on."""
    monkeypatch.setattr(lane, "OWNER_CLAIMS_ONLY", False)


def turn_on(conn, **over):
    return cfg.save(conn, {"enabled": True, **over})


class Recorder:
    """Stands in for verify_claim v2: records each ClaimRequest and answers what the test planted."""

    def __init__(self, answers=None):
        self.requests, self.answers = [], answers or {}

    async def __call__(self, request, chat):
        self.requests.append(request)
        return self.answers.get(request.statement, supported())


def supported(claim="The owner finished the garden shed.", cited=(("journal_entries", "j-old", "authored", "2026-10-01"),)):
    evidence = [{"n": i + 1, "ref": f"{t}:{r}", "source": t, "date": d, "role": role, "snippet": "x", "cited": True}
                for i, (t, r, role, d) in enumerate(cited)]
    evidence.append({"n": 99, "ref": "journal_entries:j-not-cited", "source": "journal_entries", "date": "2026-10-02",
                     "role": "authored", "snippet": "y", "cited": False})
    return {"verdict": "supported", "reason": None, "category": "did", "claim": {"text": claim, "kind": "did_or_made"},
            "evidence": evidence, "timings_ms": {"total": 1234.5}}


class CountingPacer:
    def __init__(self):
        self.waits = []

    async def wait(self, rate):
        self.waits.append(rate)


def run_job(payload, pacer=None):
    return asyncio.run(lane.execute_truth_check(payload, pacer=pacer or CountingPacer()))


def checks(conn):
    if not store.tables_present(conn):
        return []
    cursor = conn.execute("SELECT * FROM truth_checks ORDER BY record_id")
    return [dict(zip([d[0] for d in cursor.description], row)) for row in cursor.fetchall()]


# --- settings ---------------------------------------------------------------------------------------------------------
def test_settings_default_off_with_every_source_the_local_model_and_six_a_minute(node):
    assert cfg.load(node.conn) == {"enabled": False, "sources": ["imessage", "signal", "transcripts", "journal"],
                                   "model": "local", "rate_per_minute": 6}
    assert cfg.public_view(node.conn)["paused_until"] is None


def test_settings_persist_in_engine_config_and_merge_a_partial_change(node):
    cfg.save(node.conn, {"enabled": True, "sources": ["signal", "imessage"]})
    stored = json.loads(node.conn.execute("SELECT value FROM engine_config WHERE key='truth_inbound'").fetchone()[0])
    assert stored["enabled"] is True and stored["sources"] == ["imessage", "signal"]
    assert cfg.save(node.conn, {"rate_per_minute": 12}) == {"enabled": True, "sources": ["imessage", "signal"],
                                                            "model": "local", "rate_per_minute": 12}


@pytest.mark.parametrize("patch", [{}, {"enabled": "yes"}, {"enabled": 1}, {"sources": ["imessage", "email"]},
                                   {"sources": ["signal", "signal"]}, {"sources": "signal"}, {"model": "gpt"},
                                   {"rate_per_minute": 0}, {"rate_per_minute": 61}, {"rate_per_minute": True},
                                   {"rate_per_minute": "6"}, {"enabled": True, "other": 1}, None, ["enabled"]])
def test_a_malformed_change_is_refused_and_stores_nothing(node, patch):
    with pytest.raises(cfg.InvalidTruthSettings) as raised:
        cfg.save(node.conn, patch)
    assert str(raised.value) == "invalid_truth_settings"
    assert cfg.load(node.conn)["enabled"] is False


def test_a_damaged_stored_value_never_switches_the_lane_on_or_widens_it():
    assert cfg.normalize({"enabled": "true", "sources": ["imessage", "email"], "model": "x", "rate_per_minute": 600}) == cfg.defaults()
    assert cfg.normalize("not a mapping") == cfg.defaults()


# --- lazy tables ------------------------------------------------------------------------------------------------------
def test_tables_are_created_on_first_write_without_a_migration(node):
    assert not store.tables_present(node.conn)
    record = {"table": "journal_entries", "record_id": "j1", "source": "journal", "owner": True}
    row = lane.check_row(record, supported(), model="local:test")
    assert store.record_check(node.conn, row)
    assert store.tables_present(node.conn)
    assert store.record_check(node.conn, row) is None  # one check per record
    assert not node.conn.execute("SELECT 1 FROM wiki_schema_migrations WHERE migration_id LIKE '%truth%'").fetchone()


# --- the hook ---------------------------------------------------------------------------------------------------------
JOURNAL = SimpleNamespace(source_id="demo_journal_file", canonical_group_id="journal")
IMESSAGE = SimpleNamespace(source_id="imessage", canonical_group_id="conversations")


@pytest.mark.parametrize("definition, expected", [
    (IMESSAGE, "imessage"), (SimpleNamespace(source_id="signal", canonical_group_id="conversations"), "signal"),
    (SimpleNamespace(source_id="voxterm_transcripts", canonical_group_id="conversations"), "transcripts"),
    (JOURNAL, "journal"), (SimpleNamespace(source_id="installed_journal", canonical_group_id="journal"), "journal"),
    (SimpleNamespace(source_id="youtube_transcripts", canonical_group_id="transcripts"), "transcripts"),
    (SimpleNamespace(source_id="chatgpt_file_ingestion", canonical_group_id="ai_chat"), None),
    (SimpleNamespace(source_id="demo_messenger_file", canonical_group_id="conversations"), None),
])
def test_which_batches_the_lane_watches(definition, expected):
    assert lane.truth_source_for_definition(definition) == expected


def test_with_the_lane_off_the_hook_queues_nothing_and_creates_nothing(node):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    assert lane.after_rows_stored(JOURNAL) is None
    assert not store.tables_present(node.conn)
    assert not node.conn.execute("SELECT 1 FROM sqlite_master WHERE name='pipeline_jobs'").fetchone() or \
        not node.conn.execute("SELECT 1 FROM pipeline_jobs WHERE kind=?", (lane.KIND,)).fetchone()


def test_the_hook_queues_record_ids_only_for_the_chosen_sources_newest_first(node):
    add_contact(node.conn, "+15550000001", "Robin Example")
    add_message(node.conn, "m-new", "Robin says the shed roof is already finished.", at=ago(minutes=5))
    add_message(node.conn, "m-mid", "I painted the shed doors blue yesterday afternoon.", at=ago(hours=5), from_self=True)
    add_message(node.conn, "m-old", "This one is from long before the window opened.", at=ago(days=10))
    add_message(node.conn, "m-empty", "   ", at=ago(hours=1))
    add_message(node.conn, "s-1", "A Signal line that is not a chosen source here.", source_id="signal")
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.", at=ago(hours=2))
    add_segment(node.conn, "seg-heard", "A caption line the owner only overheard.", actor_role="ambient")
    add_segment(node.conn, "seg-said", "I told the group the shed would be done by Friday.", actor_role="authored")
    node.conn.commit()
    turn_on(node.conn, sources=["imessage", "journal", "transcripts"])

    job_id = lane.after_rows_stored(IMESSAGE)
    assert job_id
    payload = json.loads(node.conn.execute("SELECT payload_json FROM pipeline_jobs WHERE job_id=?", (job_id,)).fetchone()[0])
    assert payload == {"records": [["conversation_messages", "m-new"], ["journal_entries", "j1"],
                                   ["transcript_segments", "seg-said"], ["conversation_messages", "m-mid"]]}
    assert "shed" not in json.dumps(payload)  # ids only, never text
    assert lane.after_rows_stored(IMESSAGE) is None  # one batch in flight at a time


def test_a_source_the_owner_left_out_queues_nothing(node):
    add_message(node.conn, "s-1", "A Signal line about the finished shed roof.", source_id="signal")
    node.conn.commit()
    turn_on(node.conn, sources=["imessage"])
    assert lane.after_rows_stored(SimpleNamespace(source_id="signal", canonical_group_id="conversations")) is None


def test_a_checked_row_is_not_owed_again(node):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    turn_on(node.conn)
    store.record_check(node.conn, lane.check_row({"table": "journal_entries", "record_id": "j1", "source": "journal",
                                                  "owner": True}, supported(), model="local:test"))
    assert lane.owed(node.conn, ["journal"], 10) == []


def test_a_paused_hosted_lane_queues_nothing_until_the_pause_ends(node):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    turn_on(node.conn, model="hosted")
    cfg.set_paused_until(node.conn, datetime.now(timezone.utc) + timedelta(minutes=5))
    assert lane.enqueue_truth_checks(node.conn) is None
    assert lane.enqueue_truth_checks(node.conn, now=datetime.now(timezone.utc) + timedelta(minutes=6))


def test_run_post_canonical_pipeline_calls_the_hook_after_storing(node, monkeypatch):
    from topos.ingestion import canonical_pipeline

    seen = []

    def hook(source_def):
        seen.append(source_def.source_id)
        return "job-1"

    async def no_privacy(*_a, **_k):
        return {}

    monkeypatch.setattr(lane, "after_rows_stored", hook)
    monkeypatch.setattr("topos.disclosure.privacy_layer.run_privacy_disclosure_layer", no_privacy)
    monkeypatch.setattr(canonical_pipeline, "_record_episode", lambda **_k: None)
    monkeypatch.setattr("topos.engine.pipeline_memory.flush_engine_model_cache_after_pipeline", lambda: None)
    monkeypatch.setattr("topos.features.entities.graph_refresh.mark_graph_dirty", lambda: None)
    monkeypatch.setattr("topos.features.entities.graph_refresh.mark_gc_due", lambda: None)
    from topos.sources.registry import REGISTRY

    outcome = asyncio.run(canonical_pipeline.run_post_canonical_pipeline(
        source_def=REGISTRY["demo_journal_file"], canonical_records=[{"entry_id": "j1", "content": "x"}],
        sync_batch_id="b1", tables_manager=object(), run_signal=False, run_enrichment=False, fill_graph=False))
    assert seen == ["demo_journal_file"] and outcome["truth_inbound"] == {"queued_job": "job-1"}


def test_the_job_runs_on_the_catch_up_lane_and_a_finished_batch_or_sync_queues_the_next():
    import inspect

    from topos.pipeline import job_runner

    assert lane.KIND in job_runner.EXECUTORS and lane.KIND in job_runner._catchup_kinds()
    assert lane.KIND not in job_runner._executable_kinds() and lane.KIND not in job_runner._long_running_kinds()
    source = inspect.getsource(job_runner.process_job)
    assert "if kind in (TRUTH_INBOUND_KIND, LOCAL_SYNC_KIND):" in source


# --- one job ----------------------------------------------------------------------------------------------------------
def test_a_check_builds_the_claim_request_the_design_names(node, monkeypatch):
    add_contact(node.conn, "+15550000001", "Robin Example")
    for i in range(8):
        add_message(node.conn, f"m{i}", f"Earlier line {i} about the shed project plans.", at=ago(hours=10 - i),
                    from_self=i % 2 == 0)
    long_text = "Robin says the shed roof is finished and painted. " * 30
    add_message(node.conn, "m-target", long_text, at=ago(hours=1))
    node.conn.commit()
    turn_on(node.conn)
    recorder = Recorder()
    monkeypatch.setattr(lane, "_verify", recorder)

    out = run_job({"records": [["conversation_messages", "m-target"]]})
    assert out["checked"] == 1
    request = recorder.requests[0]
    assert request.statement == long_text[:1000] and len(request.statement) == 1000
    assert [t["text"] for t in request.context] == [f"Earlier line {i} about the shed project plans." for i in range(2, 8)]
    assert [t["speaker"] for t in request.context] == ["owner", "Robin Example"] * 3
    assert request.speaker == "Robin Example" and request.about_others is True
    assert request.effort == "low" and request.deadline_s == 30.0
    assert request.read_timeout_s == 20.0 and request.source_kind == "message"
    said = datetime.fromisoformat(request.said_at)
    assert said.utcoffset() is not None and abs((datetime.now(timezone.utc) - said) - timedelta(hours=1)) < timedelta(minutes=2)


def test_the_owners_row_is_spoken_by_the_owner_and_an_unknown_sender_is_a_contact_not_a_number(node, monkeypatch):
    add_message(node.conn, "m-me", "I painted the shed doors blue yesterday afternoon.", from_self=True)
    add_message(node.conn, "m-them", "The shed doors look great in that blue colour.", sender_id="+15559990000")
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    turn_on(node.conn)
    recorder = Recorder()
    monkeypatch.setattr(lane, "_verify", recorder)
    run_job({"records": [["conversation_messages", "m-me"], ["conversation_messages", "m-them"], ["journal_entries", "j1"]]})
    by_statement = {r.statement: r for r in recorder.requests}
    assert by_statement["I painted the shed doors blue yesterday afternoon."].speaker == "owner"
    assert by_statement["I painted the shed doors blue yesterday afternoon."].about_others is False
    them = by_statement["The shed doors look great in that blue colour."]
    assert them.speaker == "a contact" and them.about_others is True and "+1555" not in json.dumps(them.__dict__)
    assert by_statement["I finished building the garden shed this morning."].context == []
    kinds = {c["record_id"]: c["speaker_kind"] for c in checks(node.conn)}
    assert kinds == {"m-me": "owner", "m-them": "contact", "j1": "owner"}


def test_the_stored_row_has_the_designs_columns_and_cited_refs_without_text(node, monkeypatch):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    turn_on(node.conn)
    monkeypatch.setattr(lane, "_verify", Recorder())
    run_job({"records": [["journal_entries", "j1"]]})
    [row] = checks(node.conn)
    assert set(row) == {"check_id", "record_table", "record_id", "conversation_id", "source", "speaker_kind", "week",
                        "category", "claim_text", "verdict", "reason", "cited_refs_json", "effort", "model",
                        "latency_ms", "created_at", "attempts", "read_timeout_s", "checked_at", "engine_version",
                        "engine_sha16", "contradicted", "reading_json", "polarity", "claim_key", "said_at",
                        "group_override", "owner_labels_shown", "due_at"}
    assert row["reading_json"] is None and row["polarity"] is None and row["claim_key"] is None  # the stub gave no reading
    assert row["said_at"] and datetime.fromisoformat(row["said_at"]).utcoffset() is not None and row["group_override"] is None
    assert row["attempts"] == 1 and row["read_timeout_s"] == lane.LOCAL_READ_TIMEOUT_S and row["checked_at"] == row["created_at"]
    assert (row["engine_version"], row["engine_sha16"]) == ENGINE  # the stub answered without a version: VERSION
    assert row["verdict"] == "supported" and row["claim_text"] == "The owner finished the garden shed."
    assert row["source"] == "journal" and row["category"] == "did" and row["effort"] == "low"
    assert row["model"].startswith("local:") and row["latency_ms"] == 1234.5
    assert row["week"] == store.iso_week() and row["week"][4:6] == "-W"
    assert json.loads(row["cited_refs_json"]) == [{"table": "journal_entries", "record_id": "j-old", "role": "authored",
                                                   "date": "2026-10-01"}]
    assert datetime.fromisoformat(row["created_at"]).tzinfo is not None


def test_a_row_the_gate_refuses_costs_no_model_call_and_no_pacing_slot(node, monkeypatch):
    add_message(node.conn, "m-short", "ok sounds good")
    add_message(node.conn, "m-question", "Did you finish the shed?")
    add_message(node.conn, "m-claim", "I finished the shed roof on Sunday morning.", from_self=True)
    node.conn.commit()
    turn_on(node.conn)
    recorder, pacer = Recorder(), CountingPacer()
    monkeypatch.setattr(lane, "_verify", recorder)
    out = run_job({"records": [["conversation_messages", "m-short"], ["conversation_messages", "m-question"],
                               ["conversation_messages", "m-claim"]]}, pacer)
    assert [r.statement for r in recorder.requests] == ["I finished the shed roof on Sunday morning."]
    assert pacer.waits == [6] and out["not_checkable_by_gate"] == 2
    rows = {r["record_id"]: r for r in checks(node.conn)}
    assert rows["m-short"]["verdict"] == "not_checkable" and rows["m-short"]["reason"] == "not_checkable:fragment"
    assert rows["m-question"]["reason"] == "not_checkable:question" and rows["m-question"]["model"] is None
    assert rows["m-short"]["claim_text"] is None
    assert (rows["m-short"]["engine_version"], rows["m-short"]["engine_sha16"]) == ENGINE  # the engine's own gate


def test_a_reader_refusal_is_not_checkable_and_counts_its_model_call(node, monkeypatch):
    add_message(node.conn, "m1", "Everyone knows that water is wet, obviously.")
    node.conn.commit()
    turn_on(node.conn)
    answer = {"verdict": "unable", "reason": "not_checkable:not_about_the_owner", "category": "world", "claim": None,
              "evidence": [], "timings_ms": {"total": 800.0}}
    monkeypatch.setattr(lane, "_verify", Recorder({"Everyone knows that water is wet, obviously.": answer}))
    run_job({"records": [["conversation_messages", "m1"]]})
    [row] = checks(node.conn)
    assert row["verdict"] == "not_checkable" and row["category"] == "world" and row["model"] is not None


def test_a_row_deselected_or_gone_since_it_was_queued_is_skipped(node, monkeypatch):
    add_message(node.conn, "m1", "I finished the shed roof on Sunday morning.", from_self=True)
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    turn_on(node.conn, sources=["journal"])
    monkeypatch.setattr(lane, "_verify", Recorder())
    out = run_job({"records": [["conversation_messages", "m1"], ["journal_entries", "missing"], ["journal_entries", "j1"]]})
    assert out["skipped"] == 2 and [r["record_id"] for r in checks(node.conn)] == ["j1"]


def test_a_job_does_nothing_once_the_lane_is_off(node, monkeypatch):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    recorder = Recorder()
    monkeypatch.setattr(lane, "_verify", recorder)
    assert run_job({"records": [["journal_entries", "j1"]]}) == {"status": "ok", "skipped": "truth lane is off"}
    assert recorder.requests == [] and checks(node.conn) == []


@pytest.mark.parametrize("payload", [{}, {"records": []}, {"records": [["sqlite_master", "x"]]},
                                     {"records": [["journal_entries"]]}, {"records": [["journal_entries", 3]]},
                                     {"records": [["journal_entries", "j"]] * (lane.MAX_BATCH_SIZE + 1)}])
def test_a_malformed_batch_is_refused(payload):
    assert run_job(payload)["status"] == "error"


def test_a_job_hands_the_lane_back_after_its_budget(node, monkeypatch):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    turn_on(node.conn)
    monkeypatch.setattr(lane, "_verify", Recorder())
    monkeypatch.setattr(lane, "JOB_BUDGET_S", 0.0)
    out = run_job({"records": [["journal_entries", "j1"]]})
    assert out["deferred"] == 1 and checks(node.conn) == []
    assert lane.owed(node.conn, ["journal"], 10) == [["journal_entries", "j1"]]  # still owed: the next batch takes it


# --- the pace ---------------------------------------------------------------------------------------------------------
def test_the_pacer_spaces_checks_by_the_owners_rate():
    clock = {"t": 100.0}
    slept = []

    async def sleep(seconds):
        slept.append(round(seconds, 3))
        clock["t"] += seconds

    pacer = lane.Pacer(clock=lambda: clock["t"], sleep=sleep)

    async def go():
        await pacer.wait(6)       # first: no wait
        clock["t"] += 4.0         # the check took 4 s
        await pacer.wait(6)       # 10 s apart at 6 a minute: 6 s more
        clock["t"] += 15.0        # a slow check: no wait after it
        await pacer.wait(6)
        await pacer.wait(60)      # 1 s apart at 60 a minute
    asyncio.run(go())
    assert slept == [6.0, 1.0]


# --- the pause --------------------------------------------------------------------------------------------------------
def _hosted_402():
    return {"verdict": "unable", "reason": "model_unavailable:http_402", "category": None, "claim": None,
            "evidence": [], "timings_ms": {"total": 300.0}}


def test_a_hosted_402_pauses_the_lane_for_ten_minutes_and_stores_nothing(node, monkeypatch):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    add_journal(node.conn, "j2", "I bought new hinges for the shed doors today.", at=ago(hours=3))
    node.conn.commit()
    turn_on(node.conn, model="hosted")
    recorder = Recorder({"I finished building the garden shed this morning.": _hosted_402()})
    monkeypatch.setattr(lane, "_verify", recorder)
    monkeypatch.setattr(lane, "_chat_for", lambda model: SimpleNamespace(name=model, model="test"))
    out = run_job({"records": [["journal_entries", "j1"], ["journal_entries", "j2"]]})
    assert out["paused"] is True and out["deferred"] == 2 and len(recorder.requests) == 1
    assert checks(node.conn) == []
    until = cfg.paused_until(node.conn)
    assert timedelta(minutes=9) < until - datetime.now(timezone.utc) <= timedelta(minutes=10)
    assert cfg.public_view(node.conn)["paused_until"] is not None
    # While paused, a job does nothing and no model is called.
    assert run_job({"records": [["journal_entries", "j2"]]})["skipped"] == "paused" and len(recorder.requests) == 1


def test_after_the_pause_a_failed_probe_renews_it_and_a_passing_probe_resumes(node, monkeypatch):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    turn_on(node.conn, model="hosted")
    cfg.set_paused_until(node.conn, datetime.now(timezone.utc) - timedelta(seconds=1))
    recorder = Recorder()
    monkeypatch.setattr(lane, "_verify", recorder)
    monkeypatch.setattr(lane, "_chat_for", lambda model: SimpleNamespace(name=model, model="test"))
    monkeypatch.setattr(lane, "_probe_hosted", lambda: False)
    assert run_job({"records": [["journal_entries", "j1"]]})["skipped"] == "paused"
    assert cfg.paused_until(node.conn) > datetime.now(timezone.utc) + timedelta(minutes=9)
    assert recorder.requests == []

    cfg.set_paused_until(node.conn, datetime.now(timezone.utc) - timedelta(seconds=1))
    monkeypatch.setattr(lane, "_probe_hosted", lambda: True)
    assert run_job({"records": [["journal_entries", "j1"]]})["checked"] == 1
    assert cfg.paused_until(node.conn) is None


def test_an_unreachable_local_model_pauses_instead_of_marking_rows_unable(node, monkeypatch):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    turn_on(node.conn)
    down = {"verdict": "unable", "reason": "model_unavailable:URLError", "claim": None, "evidence": [], "timings_ms": {}}
    monkeypatch.setattr(lane, "_verify", Recorder({"I finished building the garden shed this morning.": down}))
    assert run_job({"records": [["journal_entries", "j1"]]})["paused"] is True
    assert checks(node.conn) == []


# --- VERIFY_V2_WARM_LOCAL (tracker round r2) ---------------------------------------------------------------------------
class WarmChat:
    """A local chat stand-in: says whether its model is loaded and records each load."""

    name, model = "local", "qwen3.5:9b-mlx"

    def __init__(self, loaded=False, load_fails=False, ps_fails=False):
        self.loaded, self.load_fails, self.ps_fails = loaded, load_fails, ps_fails
        self.ps_calls, self.loads = 0, []

    def resident(self):
        self.ps_calls += 1
        if self.ps_fails:
            raise RuntimeError("no answer")
        return self.loaded

    def warm(self, timeout):
        self.loads.append(timeout)
        if self.load_fails:
            raise RuntimeError("load timed out")
        self.loaded = True


def _two_journal_rows(conn):
    add_journal(conn, "j1", "I finished building the garden shed this morning.")
    add_journal(conn, "j2", "I bought new hinges for the shed doors today.", at=ago(hours=3))
    conn.commit()


def test_warm_local_loads_a_cold_model_before_the_check_outside_its_caps(node, monkeypatch):
    _two_journal_rows(node.conn)
    turn_on(node.conn)
    monkeypatch.setenv(lane.WARM_LOCAL_ENV, "1")
    chat, recorder = WarmChat(loaded=False), Recorder()
    monkeypatch.setattr(lane, "_chat_for", lambda model: chat)
    monkeypatch.setattr(lane, "_verify", recorder)
    out = run_job({"records": [["journal_entries", "j1"], ["journal_entries", "j2"]]})
    # One load, before the first check, with the lane's own load cap (not the row's deadline or reader cap).
    assert chat.loads == [lane.WARM_TIMEOUT_S] and out["warmed"] == 1 and out["warm_failed"] == 0
    assert out["checked"] == 2 and len(recorder.requests) == 2 and chat.ps_calls == 2
    assert all(r.deadline_s == lane.DEADLINE_S and r.read_timeout_s == lane.LOCAL_READ_TIMEOUT_S for r in recorder.requests)
    assert [row["attempts"] for row in checks(node.conn)] == [1, 1]


def test_warm_local_does_not_load_a_model_that_is_already_loaded(node, monkeypatch):
    _two_journal_rows(node.conn)
    turn_on(node.conn)
    monkeypatch.setenv(lane.WARM_LOCAL_ENV, "1")
    chat = WarmChat(loaded=True)
    monkeypatch.setattr(lane, "_chat_for", lambda model: chat)
    monkeypatch.setattr(lane, "_verify", Recorder())
    out = run_job({"records": [["journal_entries", "j1"], ["journal_entries", "j2"]]})
    assert chat.loads == [] and out["warmed"] == 0 and out["warm_s"] == 0.0 and out["checked"] == 2


def test_warm_local_loads_anyway_when_the_server_cannot_say(node, monkeypatch):
    _two_journal_rows(node.conn)
    turn_on(node.conn)
    monkeypatch.setenv(lane.WARM_LOCAL_ENV, "1")
    chat = WarmChat(ps_fails=True)
    monkeypatch.setattr(lane, "_chat_for", lambda model: chat)
    monkeypatch.setattr(lane, "_verify", Recorder())
    assert run_job({"records": [["journal_entries", "j1"]]})["checked"] == 1 and len(chat.loads) == 1


def test_a_failed_load_defers_the_batch_stores_nothing_and_spends_no_attempt(node, monkeypatch):
    _two_journal_rows(node.conn)
    turn_on(node.conn)
    monkeypatch.setenv(lane.WARM_LOCAL_ENV, "1")
    chat, recorder = WarmChat(load_fails=True), Recorder()
    monkeypatch.setattr(lane, "_chat_for", lambda model: chat)
    monkeypatch.setattr(lane, "_verify", recorder)
    out = run_job({"records": [["journal_entries", "j1"], ["journal_entries", "j2"]]})
    assert out["warm_failed"] == 1 and out["deferred"] == 2 and out["checked"] == 0 and out["paused"] is False
    assert recorder.requests == [] and checks(node.conn) == []
    assert cfg.paused_until(node.conn) is None
    # Still owed: the next sweep queues both rows again.
    assert sorted(r for _, r in lane.owed(node.conn, ["journal"], 10)) == ["j1", "j2"]


def test_warm_local_off_never_asks_the_server_and_keeps_the_jobs_counts(node, monkeypatch):
    _two_journal_rows(node.conn)
    turn_on(node.conn)
    monkeypatch.delenv(lane.WARM_LOCAL_ENV, raising=False)
    chat = WarmChat(loaded=False)
    monkeypatch.setattr(lane, "_chat_for", lambda model: chat)
    monkeypatch.setattr(lane, "_verify", Recorder())
    out = run_job({"records": [["journal_entries", "j1"]]})
    assert chat.ps_calls == 0 and chat.loads == []
    assert set(out) == {"status", "checked", "not_checkable_by_gate", "skipped", "deferred", "paused", "rechecked"}


def test_warm_local_never_touches_the_hosted_side(node, monkeypatch):
    _two_journal_rows(node.conn)
    turn_on(node.conn, model="hosted")
    monkeypatch.setenv(lane.WARM_LOCAL_ENV, "1")
    chat = WarmChat(loaded=False)
    monkeypatch.setattr(lane, "_chat_for", lambda model: chat)
    monkeypatch.setattr(lane, "_verify", Recorder())
    out = run_job({"records": [["journal_entries", "j1"]]})
    assert chat.ps_calls == 0 and chat.loads == [] and "warmed" not in out and out["checked"] == 1


def test_the_local_chat_reads_loaded_models_and_loads_with_an_empty_generate(monkeypatch):
    from topos.query import verify_v2_models as models

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(self.body).encode()

    seen = []

    def urlopen(request, timeout):
        url = request if isinstance(request, str) else request.full_url
        seen.append((url, None if isinstance(request, str) else json.loads(request.data)))
        if url.endswith("/api/ps"):
            return Resp({"models": [{"name": "qwen3.5:9b-mlx", "model": "qwen3.5:9b-mlx"}, {"name": "llama3.2:latest"}]})
        return Resp({"done": True})

    monkeypatch.setattr(models.urllib.request, "urlopen", urlopen)
    base = "http://127.0.0.1:11434"
    assert models.LocalChat("qwen3.5:9b-mlx", base).resident() is True
    assert models.LocalChat("llama3.2", base).resident() is True  # an untagged name means :latest
    assert models.LocalChat("smtek/Qwen3.8-27B:IQ2_M", base).resident() is False
    models.LocalChat("qwen3.5:9b-mlx", base).warm(120.0)
    assert seen[-1] == (base + "/api/generate", {"model": "qwen3.5:9b-mlx", "keep_alive": "10m"})

    def down(request, timeout):
        raise models.urllib.error.URLError("refused")

    monkeypatch.setattr(models.urllib.request, "urlopen", down)
    with pytest.raises(models.ModelUnavailable):
        models.LocalChat("qwen3.5:9b-mlx", base).resident()
    with pytest.raises(models.ModelUnavailable):
        models.LocalChat("qwen3.5:9b-mlx", base).warm(1.0)


def test_a_deadline_is_stored_as_unable(node, monkeypatch):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    turn_on(node.conn)
    late = {"verdict": "unable", "reason": "deadline", "claim": {"text": "The owner built a shed."}, "evidence": [],
            "timings_ms": {"total": 31000.0}}
    monkeypatch.setattr(lane, "_verify", Recorder({"I finished building the garden shed this morning.": late}))
    run_job({"records": [["journal_entries", "j1"]]})
    [row] = checks(node.conn)
    assert row["verdict"] == "unable" and row["reason"] == "deadline"


def test_the_real_verify_call_uses_the_owner_engine_with_a_deadline(monkeypatch):
    """``_verify`` hands verify_owner_claim the node's DB worker and threads, and turns a late answer into unable."""
    from topos.query import verify_v2

    async def slow(request, **kwargs):
        assert kwargs["gather_in_worker"].__name__ == "run_db_read" and kwargs["to_thread"] is asyncio.to_thread
        await asyncio.sleep(5)

    monkeypatch.setattr(verify_v2, "verify_owner_claim", slow)
    request = verify_v2.ClaimRequest(statement="I finished the shed roof on Sunday.", deadline_s=0.5)
    request.deadline_s = -1.9  # wait_for gets 0.1 s
    out = asyncio.run(lane._verify(request, chat=None))
    assert out["verdict"] == "unable" and out["reason"] == "deadline"


# --- the reader's timeout and the source kind -------------------------------------------------------------------------
class ReaderChat:
    """A local-model stand-in for the real engine: records what the reader gets and refuses the row as "meta", so no
    retrieval runs."""

    name, model = "local", "stand-in"

    def __init__(self):
        self.reads = []

    def __call__(self, system, user, schema, timeout, usage):
        self.reads.append({"system": system, "timeout": timeout})
        usage.add(1, 1)
        return {"category": "meta", "claims": [], "not_checkable_because": ""}


@pytest.mark.parametrize("model, expected", [("local", 20.0), ("hosted", 8.0)])
def test_the_lanes_read_timeout_follows_its_model_setting(node, monkeypatch, model, expected):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    turn_on(node.conn, model=model)
    recorder = Recorder()
    monkeypatch.setattr(lane, "_verify", recorder)
    monkeypatch.setattr(lane, "_chat_for", lambda m: SimpleNamespace(name=m, model="test"))
    run_job({"records": [["journal_entries", "j1"]]})
    [request] = recorder.requests
    assert request.read_timeout_s == expected and request.deadline_s == lane.DEADLINE_S
    assert lane.read_timeout_for(model) == expected


def test_the_lanes_read_timeout_and_source_kind_reach_the_reader(node, monkeypatch):
    """Through the real verify_owner_claim: the reader call gets the lane's 20 s (not the engine's 8 s) and the
    prompt for a chat message."""
    from topos.query import verify_v2

    add_message(node.conn, "m1", "Robin says the shed roof was finished on Sunday.")
    node.conn.commit()
    turn_on(node.conn)
    chat = ReaderChat()
    monkeypatch.setattr(lane, "_chat_for", lambda m: chat)
    out = run_job({"records": [["conversation_messages", "m1"]]})
    assert out["checked"] == 1
    [read] = chat.reads
    assert 19.0 < read["timeout"] <= 20.0
    assert read["system"] == verify_v2.read_system(True, "message") and read["system"] != verify_v2.READ_SYSTEM_WITH_OTHERS
    [row] = checks(node.conn)
    assert row["verdict"] == "not_checkable" and row["reason"] == "not_checkable:about_the_call_itself"
    assert row["read_timeout_s"] == 20.0


def test_each_source_tells_the_reader_its_kind(node, monkeypatch):
    add_message(node.conn, "m-imessage", "I painted the shed doors blue yesterday afternoon.", from_self=True)
    add_message(node.conn, "m-signal", "I bought new hinges for the shed doors today.", source_id="signal", from_self=True)
    add_message(node.conn, "m-vox", "I told everyone the shed would be done by Friday.", source_id="voxterm_transcripts",
                from_self=True, conversation_id="vox-1")
    add_segment(node.conn, "seg-1", "I said the shed roof is finished and painted.")
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    turn_on(node.conn)
    recorder = Recorder()
    monkeypatch.setattr(lane, "_verify", recorder)
    run_job({"records": [["conversation_messages", "m-imessage"], ["conversation_messages", "m-signal"],
                         ["conversation_messages", "m-vox"], ["transcript_segments", "seg-1"], ["journal_entries", "j1"]]})
    kinds = {r.statement.split()[1]: r.source_kind for r in recorder.requests}
    assert kinds == {"painted": "message", "bought": "message", "told": "transcript", "said": "transcript",
                     "finished": "journal"}


# --- re-checks of unable rows -----------------------------------------------------------------------------------------
def _engine():
    from topos.query import verify_v2

    return verify_v2.VERSION, verify_v2.engine_sha16()


#: The stamp every row this build stores carries: the engine's VERSION and the manifest digest of its four files
#: (verify_v2.ENGINE_MANIFEST; tests/query/test_verify_v2.py pins the recipe).
ENGINE = _engine()


def test_a_row_carries_the_answers_engine_version_when_it_names_one(node, monkeypatch):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    turn_on(node.conn)
    monkeypatch.setattr(lane, "_verify", Recorder({"I finished building the garden shed this morning.":
                                                   {**supported(), "version": "verify-claim/v2.0-test"}}))
    run_job({"records": [["journal_entries", "j1"]]})
    [row] = checks(node.conn)
    assert (row["engine_version"], row["engine_sha16"]) == ("verify-claim/v2.0-test", ENGINE[1])


#: What the lane's checks on the local model run with (the label the lane records: the environment may name the model).
LOCAL = ("local:" + __import__("topos.query.verify_v2_models", fromlist=["x"]).LocalChat().model, "low", 20.0)

#: The table as 1.5.19 created it, before attempts / read_timeout_s / checked_at.
DDL_1519 = """CREATE TABLE truth_checks (check_id TEXT PRIMARY KEY, record_table TEXT NOT NULL, record_id TEXT NOT NULL,
    conversation_id TEXT, source TEXT NOT NULL, speaker_kind TEXT NOT NULL CHECK (speaker_kind IN ('owner', 'contact')),
    week TEXT NOT NULL, category TEXT, claim_text TEXT,
    verdict TEXT NOT NULL CHECK (verdict IN ('supported', 'no_evidence', 'unable', 'not_checkable')), reason TEXT,
    cited_refs_json TEXT NOT NULL DEFAULT '[]', effort TEXT, model TEXT, latency_ms REAL, created_at TEXT NOT NULL)"""


def _unable(conn, record_id="j1", *, model=LOCAL[0], effort="low", read_timeout=20.0, attempts=1, checked_at=None,
            table="journal_entries", source="journal"):
    """Store an unable row as a check that ran with the given model, effort and reader timeout."""
    store.record_check(conn, lane.check_row({"table": table, "record_id": record_id, "source": source, "owner": True},
                                            {"verdict": "unable", "reason": "model_unavailable:TimeoutError",
                                             "category": "did", "claim": None, "timings_ms": {"total": 8500.0}},
                                            model=model, read_timeout_s=read_timeout))
    conn.execute("UPDATE truth_checks SET effort=?, attempts=?, checked_at=COALESCE(?, checked_at) WHERE record_id=?",
                 (effort, attempts, checked_at, record_id))
    conn.commit()


def _hours_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.mark.parametrize("over, trigger", [
    ({"model": "local:some-other-model"}, "model"),
    ({"model": "hosted:" + "meta-llama/llama-3.3-70b-instruct"}, "model"),
    ({"effort": "high"}, "effort"),
    ({"read_timeout": 8.0}, "read_timeout"),
    ({"read_timeout": None}, "read_timeout"),        # a row stored before the timeout was recorded
    ({"checked_at": _hours_ago(7)}, "cooldown"),
    ({"checked_at": _hours_ago(1)}, None),           # same model, effort and timeout, inside the cooldown
    ({"attempts": 3, "model": "local:some-other-model"}, None),  # the cap holds whatever changed
    ({"attempts": 3, "checked_at": _hours_ago(48)}, None),
])
def test_an_unable_row_is_due_again_on_each_trigger_and_never_past_the_cap(node, over, trigger):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    _unable(node.conn, **over)
    state = store.check_state(node.conn, "journal_entries", "j1")
    assert store.recheck_reason(state, LOCAL, cooldown_s=lane.RECHECK_COOLDOWN_S) == trigger
    due = lane.recheck_due(node.conn, ["journal"], LOCAL, 10)
    assert due == ([["journal_entries", "j1"]] if trigger else [])


def test_only_unable_and_unlabelled_rows_are_checked_again(node):
    for i, verdict in enumerate(("supported", "no_evidence", "not_checkable")):
        add_journal(node.conn, f"j{i}", f"I finished building garden shed number {i} this morning.")
        node.conn.commit()
        result = {"verdict": "unable" if verdict == "not_checkable" else verdict,
                  "reason": "not_checkable:fragment" if verdict == "not_checkable" else None,
                  "claim": {"text": "The owner built a shed."}, "timings_ms": {"total": 1.0}}
        store.record_check(node.conn, lane.check_row({"table": "journal_entries", "record_id": f"j{i}", "source": "journal",
                                                      "owner": True}, result, model="local:old", read_timeout_s=None))
    add_journal(node.conn, "j-labelled", "I painted both shed doors a deep blue today.")
    node.conn.commit()
    store.record_check(node.conn, lane.check_row(
        {"table": "journal_entries", "record_id": "j-labelled", "source": "journal", "owner": True},
        {"verdict": "unable", "reason": "deadline", "claim": {"text": "The owner painted the shed doors."},
         "timings_ms": {"total": 31000.0}}, model="local:old"))
    check_id = store.check_state(node.conn, "journal_entries", "j-labelled")["check_id"]
    store.label_claim(node.conn, check_id, "true")
    assert lane.recheck_due(node.conn, ["journal"], LOCAL, 10) == []


def test_a_recheck_replaces_the_result_in_place_and_counts_the_attempt(node, monkeypatch):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    _unable(node.conn, read_timeout=8.0, checked_at=_hours_ago(1))
    [before] = checks(node.conn)
    turn_on(node.conn)
    recorder = Recorder()
    monkeypatch.setattr(lane, "_verify", recorder)
    job_id = lane.enqueue_truth_checks(node.conn)
    payload = json.loads(node.conn.execute("SELECT payload_json FROM pipeline_jobs WHERE job_id=?", (job_id,)).fetchone()[0])
    assert payload == {"records": [["journal_entries", "j1"]]}
    out = run_job(payload)
    assert out["checked"] == 1 and out["rechecked"] == 1
    [after] = checks(node.conn)
    assert after["check_id"] == before["check_id"] and after["week"] == before["week"]
    assert after["created_at"] == before["created_at"] and after["checked_at"] > before["checked_at"]
    assert after["verdict"] == "supported" and after["claim_text"] == "The owner finished the garden shed."
    assert after["attempts"] == 2 and after["read_timeout_s"] == 20.0 and after["model"] == LOCAL[0]
    assert (after["engine_version"], after["engine_sha16"]) == ENGINE
    assert recorder.requests[0].read_timeout_s == 20.0
    assert node.conn.execute("SELECT COUNT(*) FROM truth_checks").fetchone()[0] == 1  # still one row per record


def test_a_row_still_unable_after_its_third_check_is_left_alone(node, monkeypatch):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    _unable(node.conn, attempts=2, checked_at=_hours_ago(7))
    turn_on(node.conn)
    late = {"verdict": "unable", "reason": "deadline", "claim": None, "evidence": [], "timings_ms": {"total": 31000.0}}
    monkeypatch.setattr(lane, "_verify", Recorder({"I finished building the garden shed this morning.": late}))
    assert run_job({"records": [["journal_entries", "j1"]]})["rechecked"] == 1
    [row] = checks(node.conn)
    assert row["verdict"] == "unable" and row["attempts"] == store.MAX_ATTEMPTS == 3
    later = datetime.now(timezone.utc) + timedelta(days=2)
    assert lane.recheck_due(node.conn, ["journal"], ("hosted:another", "high", 8.0), 10, now=later) == []
    assert lane.enqueue_truth_checks(node.conn, now=later) is None
    week = store.weekly_stats(node.conn, 1)["weeks"][0]
    assert week["rechecked"] == 1 and week["attempts_exhausted"] == 1 and week["unable"] == 1


def test_a_due_row_the_job_would_skip_is_never_queued(node, monkeypatch):
    """A re-check of a gone record or a deselected source would be skipped by the job, and each finished batch queues
    the next: queuing it would loop."""
    add_journal(node.conn, "j-kept", "I finished building the garden shed this morning.")
    add_message(node.conn, "m-signal", "I bought new hinges for the shed doors today.", source_id="signal", from_self=True)
    node.conn.commit()
    _unable(node.conn, "j-gone", read_timeout=None)
    _unable(node.conn, "m-signal", read_timeout=None, table="conversation_messages", source="signal")
    _unable(node.conn, "j-kept", read_timeout=None)
    turn_on(node.conn, sources=["journal", "imessage"])
    assert lane.recheck_due(node.conn, ["journal", "imessage"], LOCAL, 10) == [["journal_entries", "j-kept"]]
    node.conn.execute("UPDATE journal_entries SET content='  ' WHERE entry_id='j-kept'")
    node.conn.commit()
    assert lane.recheck_due(node.conn, ["journal", "imessage"], LOCAL, 10) == []
    assert lane.enqueue_truth_checks(node.conn) is None


def test_new_rows_go_first_and_re_checks_fill_the_batch(node, monkeypatch):
    add_journal(node.conn, "j-old", "I finished building the garden shed this morning.", at=ago(days=3))
    for i in range(3):
        add_journal(node.conn, f"j-new{i}", f"I painted shed door number {i} a deep blue today.", at=ago(hours=i + 1))
    node.conn.commit()
    _unable(node.conn, "j-old", read_timeout=None)
    turn_on(node.conn)
    monkeypatch.setattr(lane, "BATCH_SIZE", 3)
    job_id = lane.enqueue_truth_checks(node.conn)
    payload = json.loads(node.conn.execute("SELECT payload_json FROM pipeline_jobs WHERE job_id=?", (job_id,)).fetchone()[0])
    assert payload["records"] == [["journal_entries", "j-new0"], ["journal_entries", "j-new1"], ["journal_entries", "j-new2"]]
    node.conn.execute("DELETE FROM pipeline_jobs")
    monkeypatch.setattr(lane, "BATCH_SIZE", 5)
    job_id = lane.enqueue_truth_checks(node.conn)
    payload = json.loads(node.conn.execute("SELECT payload_json FROM pipeline_jobs WHERE job_id=?", (job_id,)).fetchone()[0])
    assert payload["records"][-1] == ["journal_entries", "j-old"] and len(payload["records"]) == 4


def test_a_table_the_1519_lane_created_gains_the_columns_and_its_timed_out_rows_are_due(node, monkeypatch):
    """The owner's node holds the table without the new columns, and its timed-out rows recorded no reader timeout:
    the first enqueue adds the columns and queues those rows (attempts 1, a timeout that differs from the lane's)."""
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.execute(DDL_1519)
    node.conn.execute("CREATE UNIQUE INDEX idx_truth_checks_record ON truth_checks(record_table, record_id)")
    node.conn.execute("CREATE TABLE truth_labels (label_id TEXT PRIMARY KEY, check_id TEXT NOT NULL UNIQUE, label TEXT NOT NULL, "
                      "note TEXT, labelled_at TEXT NOT NULL)")
    node.conn.execute("INSERT INTO truth_checks (check_id, record_table, record_id, source, speaker_kind, week, verdict, reason, "
                      "effort, model, latency_ms, created_at) VALUES ('c1', 'journal_entries', 'j1', 'journal', 'owner', "
                      "'2026-W43', 'unable', 'model_unavailable:TimeoutError', 'low', ?, 8500.0, ?)",
                      (LOCAL[0], _hours_ago(1)))
    node.conn.commit()
    assert store.weekly_stats(node.conn, 1)["weeks"][0]["rechecked"] == 0  # reads the old table as it is
    turn_on(node.conn)
    job_id = lane.enqueue_truth_checks(node.conn)
    assert job_id
    columns = [r[1] for r in node.conn.execute("PRAGMA table_info(truth_checks)").fetchall()]
    assert columns[-13:] == ["attempts", "read_timeout_s", "checked_at", "engine_version", "engine_sha16", "contradicted",
                             "reading_json", "polarity", "claim_key", "said_at", "group_override", "owner_labels_shown", "due_at"]
    [row] = checks(node.conn)
    assert row["attempts"] == 1 and row["read_timeout_s"] is None and row["checked_at"] is None
    assert row["engine_version"] is None and row["engine_sha16"] is None  # 1.5.19's rows: unstamped
    monkeypatch.setattr(lane, "_verify", Recorder())
    out = run_job({"records": [["journal_entries", "j1"]]})
    assert out["rechecked"] == 1
    [row] = checks(node.conn)
    assert row["check_id"] == "c1" and row["attempts"] == 2 and row["verdict"] == "supported" and row["week"] == "2026-W43"
    assert (row["engine_version"], row["engine_sha16"]) == ENGINE  # the re-check carries this build's stamp


def test_a_recheck_that_lost_a_race_stores_nothing(node):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    _unable(node.conn, attempts=2)
    row = lane.check_row({"table": "journal_entries", "record_id": "j1", "source": "journal", "owner": True}, supported(),
                         model=LOCAL[0], read_timeout_s=20.0)
    assert store.recheck(node.conn, row, 2) is None  # the row has two checks already: attempt 2 was someone else's
    assert store.recheck(node.conn, row, 3)
    assert store.recheck(node.conn, row, 3) is None  # no longer unable
    with pytest.raises(store.TruthRequestError):
        store.recheck(node.conn, row, 4)


# --- Calibrate step 1: the owner's own claims only --------------------------------------------------------------------
def test_owner_claims_only_is_the_default():
    assert SHIPPED_OWNER_CLAIMS_ONLY is True


def test_with_owner_claims_only_a_contacts_message_is_never_queued_or_checked(node, monkeypatch):
    monkeypatch.setattr(lane, "OWNER_CLAIMS_ONLY", True)
    add_message(node.conn, "m-contact", "I finished building the garden shed this morning.")
    add_message(node.conn, "m-owner", "I finished painting the garden shed this morning.", from_self=True, at=ago(minutes=5))
    add_journal(node.conn, "j1", "I bought new hinges for the shed doors today.", at=ago(hours=1))
    node.conn.commit()
    turn_on(node.conn)
    assert sorted(r for _, r in lane.owed(node.conn, list(cfg.SOURCES), 10)) == ["j1", "m-owner"]
    recorder = Recorder()
    monkeypatch.setattr(lane, "_verify", recorder)
    out = run_job({"records": [["conversation_messages", "m-contact"], ["conversation_messages", "m-owner"]]})
    assert out["skipped"] == 1 and out["checked"] == 1 and [r.statement for r in recorder.requests] == [
        "I finished painting the garden shed this morning."]


def test_the_request_names_its_record_its_thread_and_whether_the_owner_spoke(node, monkeypatch):
    monkeypatch.setattr(lane, "OWNER_CLAIMS_ONLY", True)
    add_message(node.conn, "m-owner", "I finished painting the garden shed this morning.", from_self=True,
                conversation_id="thread-9")
    add_journal(node.conn, "j1", "I bought new hinges for the shed doors today.", at=ago(hours=1))
    node.conn.commit()
    turn_on(node.conn)
    recorder = Recorder()
    monkeypatch.setattr(lane, "_verify", recorder)
    run_job({"records": [["conversation_messages", "m-owner"], ["journal_entries", "j1"]]})
    message, journal = recorder.requests
    assert (message.speaker_is_owner, message.source_ref, message.conversation_id) == (
        True, "conversation_messages:m-owner", "thread-9")
    assert (journal.speaker_is_owner, journal.source_ref, journal.conversation_id) == (True, "journal_entries:j1", None)


# --- the lane's denominator (owner, 27 Oct 2026) ----------------------------------------------------------------------
def _daily(conn):
    return {r[0]: dict(zip(store.LANE_COUNTS, r[1:])) for r in conn.execute(
        f"SELECT source, {', '.join(store.LANE_COUNTS)} FROM truth_lane_daily ORDER BY source")}


def test_each_batch_adds_what_it_did_to_the_days_counts_by_source(node, monkeypatch):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    add_journal(node.conn, "j2", "Yeah.", at=ago(hours=2))  # the gate refuses a fragment
    add_message(node.conn, "m1", "I painted the shed doors blue this afternoon.", from_self=True, at=ago(hours=3))
    node.conn.commit()
    turn_on(node.conn)
    monkeypatch.setattr(lane, "_verify", Recorder())
    run_job({"records": [["journal_entries", "j1"], ["journal_entries", "j2"], ["conversation_messages", "m1"],
                         ["journal_entries", "gone"]]})
    daily = _daily(node.conn)
    assert daily["journal"] == {"queued": 2, "gated": 1, "checked": 1, "deferred": 0, "skipped": 0, "paused": 0}
    assert daily["imessage"]["checked"] == 1 and daily["unresolved"]["skipped"] == 1
    run_job({"records": [["journal_entries", "j1"]]})  # already checked: skipped, and the day's row grows
    assert _daily(node.conn)["unresolved"]["skipped"] == 2
    [week] = store.weekly_stats(node.conn, 1)["weeks"]
    assert week["lane"] == {"queued": 3, "gated": 1, "checked": 2, "deferred": 0, "skipped": 2, "paused": 0}


def test_a_pause_is_counted_with_what_it_deferred(node, monkeypatch):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    add_journal(node.conn, "j2", "I bought new hinges for the shed doors today.", at=ago(hours=3))
    node.conn.commit()
    turn_on(node.conn, model="hosted")
    monkeypatch.setattr(lane, "_verify", Recorder({"I finished building the garden shed this morning.": _hosted_402()}))
    monkeypatch.setattr(lane, "_chat_for", lambda model: SimpleNamespace(name=model, model="test"))
    run_job({"records": [["journal_entries", "j1"], ["journal_entries", "j2"]]})
    assert _daily(node.conn)["journal"]["paused"] == 1 and _daily(node.conn)["journal"]["deferred"] == 2


def test_the_sources_last_sync_is_a_category_never_the_error_text(node):
    from topos.pipeline.job_store import ensure_pipeline_jobs_schema

    ensure_pipeline_jobs_schema(node.conn)
    rows = [("j-ok", "done", "2026-10-26 08:01:02", '{"status": "ok"}'),
            ("j-bad", "failed", "2026-10-27 14:32:13", '{"error": "Cannot copy chat.db: [Errno 1] Operation not permitted: /x. Full Disk Access may be required."}')]
    for job_id, status, at, detail in rows:
        node.conn.execute("INSERT INTO pipeline_jobs (job_id, kind, status, payload_json, detail_json, created_at, updated_at) "
                          "VALUES (?, 'local_sync', ?, '{\"source_id\": \"imessage\"}', ?, ?, ?)", (job_id, status, detail, at, at))
    node.conn.commit()
    health = store.weekly_stats(node.conn, 1)["sources"]
    assert health == {"imessage": {"last_sync_at": "2026-10-27 14:32:13", "last_sync_ok": False, "last_ok_at": "2026-10-26 08:01:02",
                                   "failure": "full_disk_access"}}
    assert "chat.db" not in json.dumps(health)


def test_a_contradiction_is_stored_as_nothing_found_with_its_flag(node, monkeypatch):
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    turn_on(node.conn)
    against = {"verdict": "contradicted", "reason": None, "category": "did", "claim": {"text": "The owner built a shed."},
               "evidence": [{"n": 1, "ref": "journal_entries:j-old", "source": "journal_entries", "date": "2026-10-01",
                             "role": "authored", "snippet": "x", "cited": True, "contradicts": True}], "timings_ms": {"total": 900.0}}
    monkeypatch.setattr(lane, "_verify", Recorder({"I finished building the garden shed this morning.": against}))
    run_job({"records": [["journal_entries", "j1"]]})
    [row] = checks(node.conn)
    assert (row["verdict"], row["reason"], row["contradicted"]) == ("no_evidence", "contradicted", 1)
    assert json.loads(row["cited_refs_json"])[0]["record_id"] == "j-old"
    [card] = store.claim_cards(node.conn, 10, True)["cards"]
    assert card["contradicted"] is True and store.weekly_stats(node.conn, 1)["weeks"][0]["contradicted"] == 1


# --- the engine writes no answer row (lean v3 increment A, 27 Oct 2026) -------------------------------------------------
def test_a_full_lane_run_with_no_owner_answer_writes_no_label_dismissal_skip_or_object(node, monkeypatch):
    """First checks, then a re-check of the unable row: the lane writes truth_checks and truth_lane_daily only. The
    owner's three answer tables and signal_objects are byte-identical across the re-check and signal_objects across
    both runs, and an answered verdict is never rewritten (only an ``unable`` row is checked again)."""
    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    add_journal(node.conn, "j2", "I painted both shed doors a deep blue today.")
    node.conn.commit()
    turn_on(node.conn)
    objects_before = node.conn.execute("SELECT * FROM signal_objects ORDER BY object_id").fetchall()
    late = {"verdict": "unable", "reason": "deadline", "claim": {"text": "The owner painted the shed doors."}, "evidence": [],
            "timings_ms": {"total": 31000.0}}
    monkeypatch.setattr(lane, "_verify", Recorder({"I painted both shed doors a deep blue today.": late}))
    assert run_job({"records": [["journal_entries", "j1"], ["journal_entries", "j2"]]})["checked"] == 2
    first = {c["record_id"]: (c["verdict"], c["cited_refs_json"]) for c in checks(node.conn)}
    assert first["j1"][0] == "supported" and first["j2"][0] == "unable"

    def answers():
        return [node.conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                for t in ("truth_labels", "truth_dismissals", "truth_skips", "signal_objects")]

    after_first = answers()
    assert after_first[:3] == [[], [], []] and after_first[3] == objects_before
    node.conn.execute("UPDATE truth_checks SET checked_at=? WHERE record_id='j2'", (_hours_ago(7),))
    node.conn.commit()
    monkeypatch.setattr(lane, "_verify", Recorder())  # the re-check answers supported
    out = run_job({"records": [["journal_entries", "j2"]]})
    assert out["checked"] == 1 and out["rechecked"] == 1
    after = {c["record_id"]: (c["verdict"], c["cited_refs_json"]) for c in checks(node.conn)}
    assert after["j1"] == first["j1"] and after["j2"][0] == "supported"
    assert answers() == after_first
    assert node.conn.execute("SELECT COUNT(*) FROM truth_checks").fetchone()[0] == 2


# --- the stored reading (lean v3 increment B, 27 Oct 2026) ----------------------------------------------------------------
def test_the_row_stores_the_reading_its_polarity_key_and_time(node, monkeypatch):
    add_journal(node.conn, "j1", "I never use tabs in Python.", at="2026-10-22T09:30:00")
    node.conn.commit()
    turn_on(node.conn)
    reading = {"category": "state", "claims": [{"type": "setup", "subject": "owner", "reported_speech": False,
                                                "claim": "The owner uses tabs in Python.", "names": ["Python", "tabs"],
                                                "date_from": "", "date_to": "", "as_written": "", "polarity": "denies"}]}
    answer = {**supported(claim="The owner uses tabs in Python."), "category": "state", "reading": reading,
              "stages": {"retrieve": {"owner_labels": 0, "candidates": 3}}}
    monkeypatch.setattr(lane, "_verify", Recorder({"I never use tabs in Python.": answer}))
    run_job({"records": [["journal_entries", "j1"]]})
    [row] = checks(node.conn)
    assert json.loads(row["reading_json"]) == reading and row["polarity"] == -1
    assert row["claim_key"] == "state|setup|none|python tabs" and row["owner_labels_shown"] == 0
    assert row["said_at"] == store.with_offset("2026-10-22T09:30:00")  # the record's own time, local, with its offset
    state = store.check_state(node.conn, "journal_entries", "j1")
    assert json.loads(state["reading_json"]) == reading and state["category"] == "state"


def test_a_recheck_runs_from_the_stored_reading_with_no_reader_call(node, monkeypatch):
    """An unable row that already carries its reading is checked again through the real engine: retrieval and the
    judge only. The chat is never called for a read (here never at all: no records, so no judge either), the reading
    survives the in-place rewrite, and the row is answered."""
    from topos.query import verify_v2

    add_journal(node.conn, "j1", "I finished building the garden shed this morning.")
    node.conn.commit()
    _unable(node.conn, checked_at=_hours_ago(7))
    reading = {"category": "did", "claims": [{"type": "did_or_made", "subject": "owner", "reported_speech": False,
                                              "claim": "The owner finished building the garden shed.", "names": ["shed"],
                                              "date_from": "", "date_to": "", "as_written": "", "polarity": "affirms"}]}
    node.conn.execute("UPDATE truth_checks SET reading_json=?, claim_key=?, polarity=1 WHERE record_id='j1'",
                      (json.dumps(reading), "did|did_or_made|none|building finished garden shed"))
    node.conn.commit()
    turn_on(node.conn)

    class NoModel:
        name, model = "local", "stand-in"

        def __call__(self, system, user, schema, timeout, usage):
            raise AssertionError("a re-check from a stored reading makes no reader call")

    class NoRecords:
        def __init__(self, conn):
            pass

        def gather(self, query, needles, now):
            return [], {"candidates": 0}

    monkeypatch.setattr(lane, "_chat_for", lambda m: NoModel())
    monkeypatch.setattr(verify_v2, "NodeEvidence", NoRecords)
    out = run_job({"records": [["journal_entries", "j1"]]})
    assert out["checked"] == 1 and out["rechecked"] == 1
    [row] = checks(node.conn)
    assert row["verdict"] == "no_evidence" and row["attempts"] == 2
    assert json.loads(row["reading_json"]) == reading and row["claim_key"] == "did|did_or_made|none|building finished garden shed" and row["polarity"] == 1
    assert row["claim_text"] == "The owner finished building the garden shed."


# --- plans (lean v3 increment C, 27 Oct 2026) ------------------------------------------------------------------------------
PLAN_READING = {"category": "plan", "claims": [{"type": "intention", "subject": "owner", "reported_speech": False,
                                                "claim": "The owner will finish the shed roof by Wednesday.", "names": ["shed"],
                                                "date_from": "2026-10-08", "date_to": "2026-10-08", "as_written": "", "polarity": "affirms"}]}
PLAN_SAID = "2026-10-07T19:47:36-05:00"
PLAN_READY = "2026-10-16T04:59:59+00:00"  # the end of 2026-10-08 at -05:00, plus seven days


def _plan_row(conn, record_id="j-plan", *, verdict="unable", reason=f"waiting_until:{PLAN_READY}", label=None, thing="shed roof"):
    add_journal(conn, record_id, f"I'll finish the {thing} by Wednesday.", at=PLAN_SAID)
    conn.commit()
    claim = {**PLAN_READING["claims"][0], "claim": f"The owner will finish the {thing} by Wednesday.", "names": [thing.split()[0]]}
    reading = {"category": "plan", "claims": [claim]}
    result = {"verdict": verdict, "reason": reason, "category": "plan", "claim": {"text": claim["claim"]},
              "evidence": [], "timings_ms": {"total": 900.0}, "reading": reading,
              **({"stages": {"retrieve": "waiting"}} if reason and reason.startswith("waiting_until:") else {})}
    check_id = store.record_check(conn, lane.check_row({"table": "journal_entries", "record_id": record_id, "source": "journal", "owner": True},
                                                       result, model=LOCAL[0], read_timeout_s=20.0, said_at=PLAN_SAID))
    if label:
        store.label_claim(conn, check_id, label)
    return check_id


def test_a_plan_row_waits_for_its_due_moment_and_is_counted_as_a_plan_not_as_unable(node):
    check_id = _plan_row(node.conn)
    [row] = checks(node.conn)
    assert row["verdict"] == "unable" and row["reason"] == f"waiting_until:{PLAN_READY}" and row["due_at"] == "2026-10-09T04:59:59+00:00"
    state = store.check_state(node.conn, "journal_entries", "j-plan")
    before, after = datetime(2026, 10, 16, 4, 59, tzinfo=timezone.utc), datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc)
    assert store.recheck_reason(state, LOCAL, now=before, cooldown_s=0) is None  # never before its moment, whatever changed
    assert store.recheck_reason(state, ("local:another-model", "high", 8.0), now=before, cooldown_s=0) is None
    assert store.recheck_reason(state, LOCAL, now=after, cooldown_s=lane.RECHECK_COOLDOWN_S) == "due"
    assert lane.recheck_due(node.conn, ["journal"], LOCAL, 10, now=before) == []
    [card] = store.claim_cards(node.conn, 10, True)["cards"]
    assert card["check_id"] == check_id and card["kind"] == "intention" and card["plan"] == "waiting" and card["due_at"] == row["due_at"]
    [entry] = store.weekly_stats(node.conn, 1)["weeks"]
    assert entry["plans"] == {"waiting": 1, "kept": 0, "nothing_by_due": 0, "answered": 0}
    assert entry["unable"] == 0 and entry["to_confirm"] == 0 and entry["checked"] == 1 and entry["by_source"]["journal"] == 1


@pytest.fixture
def node_zone():
    """The zone the plan fixtures were written in (their said time is -05:00). Plan days and due moments are read in
    the node's LOCAL zone (``truth_checks.with_offset`` gives ``said_at`` the node's offset; ``intention_period`` takes
    the said day locally), so a test that pins a due instant pins that zone too, instead of inheriting the host's:
    on 27 Oct 2026 the two plan tests passed on the owner's Mac and failed on CI, which runs in UTC."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Chicago"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_a_due_plan_goes_first_and_is_checked_as_of_its_moment_from_the_stored_reading(node, monkeypatch, node_zone):
    _plan_row(node.conn)
    add_journal(node.conn, "j-late", "I finished building the garden shed this morning.")
    node.conn.commit()
    after = datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc)
    _unable(node.conn, "j-late", checked_at=(after - timedelta(hours=7)).isoformat())  # an ordinary unable row, newer, due by cooldown
    assert lane.recheck_due(node.conn, ["journal"], LOCAL, 10, now=after) == [["journal_entries", "j-plan"], ["journal_entries", "j-late"]]
    turn_on(node.conn)
    recorder = Recorder({"I'll finish the shed roof by Wednesday.": {**supported(claim=PLAN_READING["claims"][0]["claim"]), "category": "plan",
                                                                        "reading": PLAN_READING}})
    monkeypatch.setattr(lane, "_verify", recorder)
    monkeypatch.setattr(store, "now_utc", lambda: after)  # the lane's clock, past the plan's moment
    out = run_job({"records": [["journal_entries", "j-plan"]]})
    assert out["checked"] == 1 and out["rechecked"] == 1
    [request] = recorder.requests
    assert request.as_of == PLAN_READY and request.reading == PLAN_READING and request.said_at == store.with_offset(PLAN_SAID)
    row = next(c for c in checks(node.conn) if c["record_id"] == "j-plan")
    assert row["verdict"] == "supported" and row["attempts"] == 2 and row["due_at"] == "2026-10-09T04:59:59+00:00"
    [card] = [c for c in store.claim_cards(node.conn, 10, True)["cards"] if c["check_id"] == row["check_id"]]
    assert card["plan"] == "kept"
    [entry] = store.weekly_stats(node.conn, 1, when=datetime.now(timezone.utc))["weeks"]  # the rows' week is the real one
    assert entry["plans"]["kept"] == 1 and entry["plans"]["waiting"] == 0 and entry["backed"] == 1


def test_plans_the_owner_answered_or_that_came_to_nothing_are_counted_so(node):
    _plan_row(node.conn, "j-kept", verdict="supported", reason=None, thing="shed roof")
    _plan_row(node.conn, "j-nothing", verdict="no_evidence", reason="no_records", thing="garden fence")
    _plan_row(node.conn, "j-said", verdict="no_evidence", reason="no_records", label="true", thing="back gate")
    cards = {c["check_id"]: c["plan"] for c in store.claim_cards(node.conn, 10, False)["cards"]}
    assert sorted(cards.values()) == ["answered", "kept", "nothing_by_due"]
    [entry] = store.weekly_stats(node.conn, 1)["weeks"]
    assert entry["plans"] == {"waiting": 0, "kept": 1, "nothing_by_due": 1, "answered": 1} and entry["labelled"] == 1

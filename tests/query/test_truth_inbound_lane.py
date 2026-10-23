"""The inbound truth lane (VERIFY_CLAIM_WEEKLY_PROGRAM.md §2a): settings, lazy tables, the hook, the job, the pace.

Synthetic only: a migrated temp database with invented rows, and verify_claim v2 replaced by a recorder, so no
model, no network, and never the live node.
"""
from __future__ import annotations

import asyncio
import json
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
                        "latency_ms", "created_at"}
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

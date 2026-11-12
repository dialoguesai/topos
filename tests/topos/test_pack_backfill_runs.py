"""One owner-confirmed fact-pack backfill run (`features/derivation/backfill_runs`, the run routes in `api/signal`).

protects: the owner's yes on a terminal authorises ONE run (these packs, back to this date, read by the extractor
the owner has picked for facts) that the node works through itself, pack after pack and batch after batch, and nothing
else can start one. A pack Topos Facts knows is read by it and never by the LLM when the owner picked it; a pick that
cannot load defers with nothing marked done; extraction and verification time are reported apart. On the owner socket the start route is refused 403
``owner_confirmation_required`` without the app or a live confirmation token, the token is good for one start, and
the route refuses over TCP and with the socket gate switched off too. A run never overlaps another run or one of the
app's single batches; it stops between records, keeps every finished record, and a rerun resumes from
``derivation_progress``; a model that is down ends it instead of spinning. The count shown before the yes is the
number of records a backfill then reaches. No write transaction is open during a model call.

The model is a stub everywhere (``OllamaAdapter._generate``); databases are scratch files under ``tmp_path``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from topos import owner_door_routes as census, uds
from topos.features.derivation import backfill_runs as runs
from topos.features.derivation import surfaces
from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
from topos.features.signal.derived_index import index_derived_objects as _REAL_INDEXER
from topos.storage.db.migrations import apply_all_migrations

JOY = "moments.joy"
EVENTS = "life.events"
OFF = "values.motivation"
BASE = "/v1/signal/derivation/backfill"
REFUSAL = {"error": "owner_confirmation_required", "message": "This change needs the Topos app."}

#: Passes the moments.joy prefilter (an exemplar phrase) and gives the stub words to ground a fact in.
HIT = "It was the best day at the lake, I felt so alive out there (entry {n})"
#: Passes no pack's prefilter: no lexicon word, no exemplar.
MISS = "zzqx vvkp wwrt ggnh qqqq xxxx (nnn {n})"
HITS = (1, 2, 4, 5, 7, 8)

VERDICT = json.dumps({"supported": True, "about": "owner", "fields_ok": True, "reason": "stated"})
COUNT_KEYS = {"processed", "assertions", "accepted", "written", "quarantined", "llm_errors"}
TIME_KEYS = {"extract_s", "verify_s"}
BATCH_KEYS = COUNT_KEYS | TIME_KEYS | {"index", "elapsed_s"}


def _open(path) -> sqlite3.Connection:
    return sqlite3.connect(path, check_same_thread=False)


@pytest.fixture
def node(tmp_path):
    """A scratch node: an owner, moments.joy and life.events switched on, and ten journal entries, six that the
    moments.joy prefilter passes and four that no prefilter passes (the newest one among them)."""
    path = tmp_path / "node.db"
    conn = _open(path)
    apply_all_migrations(conn)
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_owner','person','Owner','owner','[]',1)")
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)")
    seed_pack_registry(conn, bundled_pack_dir())
    conn.execute("UPDATE pack_registry SET enabled=0")
    conn.execute("UPDATE pack_registry SET enabled=1 WHERE pack_id IN (?, ?)", (JOY, EVENTS))
    for n in range(10):
        conn.execute("INSERT INTO journal_entries (entry_id, entry_at, content, source_id, source_record_id)"
                     " VALUES (?, ?, ?, 'grow_journal', ?)",
                     (f"j{n}", f"2026-10-{10 + n:02d}T09:00:00Z", (HIT if n in HITS else MISS).format(n=n), f"r{n}"))
    conn.commit()
    yield SimpleNamespace(conn=conn, path=path)
    conn.close()


class _Model:
    """The local model, stubbed: every extraction prompt is kept, a verifier call always supports."""

    def __init__(self) -> None:
        self.extract: List[str] = []
        self.verify: List[str] = []
        self.down = False
        self.before: Optional[Callable[[], None]] = None

    def entries(self) -> List[int]:
        return [int(re.search(r"\(entry (\d+)\)", p).group(1)) for p in self.extract]

    def __call__(self, _adapter, _model, prompt, **_kw) -> Dict[str, Any]:
        if self.before is not None:
            self.before()
        if "strict fact-checker" in prompt:
            self.verify.append(prompt)
            return {"text": VERDICT}
        self.extract.append(prompt)
        if self.down:
            raise RuntimeError("Ollama request failed: connection refused")
        n = int(re.search(r"\(entry (\d+)\)", prompt).group(1))
        return {"text": json.dumps({"assertions": [{
            "predicate": "moment.peak", "value": {"kind": "alive", "description": f"lake day number {n}"},
            "about": "owner", "occurrence_date": None, "confidence": 0.9, "quote": "I felt so alive out there"}]})}


@pytest.fixture
def model(monkeypatch):
    from topos.engine.backends import ollama

    stub = _Model()
    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", lambda self, m, p, **kw: stub(self, m, p, **kw))
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c=None: "stub-model")
    monkeypatch.setattr("topos.features.signal.derived_index.index_derived_objects", lambda *a, **kw: {"embedded": 0})
    return stub


@pytest.fixture(autouse=True)
def _one_run_state():
    """Each test starts with no run, and no run's thread outlives its test."""
    def reset():
        run = runs._active
        if run is not None:
            run.request_stop()
            if run.thread is not None:
                run.thread.join(timeout=10)
        runs._active = None
        runs._last = None
        runs._single_batches = 0

    reset()
    yield
    reset()


def _wait(run: runs.BackfillRun) -> Dict[str, Any]:
    run.thread.join(timeout=20)
    assert not run.thread.is_alive(), "the run did not end"
    return run.snapshot()


def _until(check: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + 10
    while not check():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.005)


# ----------------------------------------------------------------------------- one batch (surfaces.run_pack_backfill)


def test_no_write_transaction_is_open_during_a_model_call(node, model) -> None:
    """The newest record is one the prefilter passes over, so its progress mark is written before the first
    extraction call; the call's own measurement row is written before the verifier call. Neither may leave SQLite's
    write lock held while the model works (topos#271 fixed this for the ingest job; a backfill batch still did it)."""
    held: List[bool] = []
    model.before = lambda: held.append(node.conn.in_transaction)
    stats = surfaces.run_pack_backfill(node.conn, JOY, limit=10)
    assert stats["processed"] == 6 and len(model.verify) >= 1
    assert len(held) == len(model.extract) + len(model.verify) and not any(held), held


def test_no_write_transaction_is_open_while_the_embedding_model_works(node, model, monkeypatch) -> None:
    """The batch's tail stamps the run and then embeds what was written, with the real indexer here and a stub
    encoder. The stamp was a bare UPDATE left uncommitted, so the encoder ran with SQLite's write lock held and the
    indexer then asked for the write gate: on a live node (1.5.47, 10 Nov 2026) every other writer waited 312 to
    343 s at 5 of 9 batch ends."""
    held: List[bool] = []
    stamps: List[Optional[str]] = []

    class Encoder:
        def run_inference(self, payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
            held.append(node.conn.in_transaction)
            other = _open(node.path)
            try:
                stamps.append(other.execute("SELECT last_run_at FROM pack_registry WHERE pack_id=?",
                                            (JOY,)).fetchone()[0])
            finally:
                other.close()
            return {"vectors": [[0.1] * 8 for _ in payload["texts"]], "dims": 8, "provider": "stub"}

    monkeypatch.setattr("topos.features.signal.derived_index.index_derived_objects", _REAL_INDEXER)
    monkeypatch.setattr("topos.engine.backends.huggingface.HuggingFaceAdapter", Encoder)
    stats = surfaces.run_pack_backfill(node.conn, JOY, limit=10)
    assert stats["written"] >= 1 and stats["indexed"] != -1, stats
    assert held, "the batch wrote facts, so the encoder was asked"
    assert not any(held), "the encoder ran inside the backfill's open write transaction"
    assert all(stamps), "the run stamp was not committed before the encoder ran"
    assert not node.conn.in_transaction


def test_no_write_transaction_is_open_when_an_assertion_reaches_the_writer(node, model, monkeypatch) -> None:
    """The writer queues a conflict through the write gate (`FactStore._queue_conflict`). The batch's own counters
    and the verifier call's measurement row were still uncommitted when it was called, so that path asked for the
    gate while holding SQLite's write lock: the same inversion as the tail, without a model call in it."""
    from topos.features.derivation.writer import DerivationWriter

    real = DerivationWriter.assert_pack_fact
    held: List[bool] = []

    def assert_pack_fact(self, **kw):
        held.append(self.conn.in_transaction)
        return real(self, **kw)

    monkeypatch.setattr(DerivationWriter, "assert_pack_fact", assert_pack_fact)
    stats = surfaces.run_pack_backfill(node.conn, JOY, limit=10)
    assert stats["accepted"] == 6 and len(held) == 6
    assert not any(held), held


def test_a_stop_ends_the_batch_between_records_and_a_rerun_resumes(node, model) -> None:
    seen: List[Dict[str, Any]] = []
    first = surfaces.run_pack_backfill(node.conn, JOY, limit=10, on_record=seen.append,
                                       should_stop=lambda: len(seen) >= 2)
    assert first["stopped"] is True and first["processed"] == 2
    assert [s["processed"] for s in seen] == [1, 2] and set(seen[-1]) >= COUNT_KEYS | TIME_KEYS
    assert model.entries() == [8, 7], "newest first, and nothing past the stop"
    marked = {r[0] for r in node.conn.execute("SELECT key FROM derivation_progress WHERE key LIKE ?", (f"{JOY}@%",))}
    assert {k.rsplit(":", 1)[1] for k in marked} == {"j9", "j8", "j7"}, "the finished records, and the one passed over"

    second = surfaces.run_pack_backfill(node.conn, JOY, limit=10)
    assert "stopped" not in second and second["processed"] == 4
    assert sorted(model.entries()) == sorted(HITS), "each waiting record was asked exactly once across both runs"
    assert surfaces.run_pack_backfill(node.conn, JOY, limit=10)["processed"] == 0


def test_a_failed_extraction_call_is_counted_and_the_record_is_asked_again(node, model) -> None:
    model.down = True
    seen: List[Dict[str, Any]] = []
    held: List[bool] = []

    def on_record(counts: Dict[str, Any]) -> None:
        seen.append(counts)
        held.append(node.conn.in_transaction)

    stats = surfaces.run_pack_backfill(node.conn, JOY, limit=10, on_record=on_record)
    assert stats["processed"] == 6 and stats["llm_errors"] == 6 and stats["written"] == 0
    assert [s["llm_errors"] for s in seen] == [1, 2, 3, 4, 5, 6]
    assert not any(held), "the callback may wait for the owner's requests: never inside a write transaction"
    model.down = False
    model.extract.clear()
    again = surfaces.run_pack_backfill(node.conn, JOY, limit=10)
    assert again["processed"] == 6 and again["llm_errors"] == 0 and sorted(model.entries()) == sorted(HITS)


def test_a_batch_writes_the_facts_the_stub_states(node, model) -> None:
    stats = surfaces.run_pack_backfill(node.conn, JOY, limit=10)
    facts = node.conn.execute("SELECT COUNT(*) FROM signal_objects WHERE object_type='fact' AND ontology_id=?"
                              " AND valid_to IS NULL", (JOY,)).fetchone()[0]
    assert stats["assertions"] == 6 and stats["accepted"] == 6
    assert stats["written"] >= 1 and facts >= 1, stats


# ----------------------------------------------------------------------------- the count before the yes


def test_the_count_before_the_yes_is_what_a_backfill_then_reaches(node, model) -> None:
    before = node.conn.total_changes
    plan = runs.backfill_plan(node.conn, [JOY, EVENTS, OFF, "no.such_pack", JOY])
    assert node.conn.total_changes == before and not node.conn.in_transaction, "the count writes nothing"
    assert model.extract == [] and model.verify == [], "and asks no model"
    rows = {row["pack_id"]: row for row in plan["packs"]}
    assert list(rows) == [JOY, EVENTS, OFF, "no.such_pack"], "in the order asked, without repeats"
    assert rows[JOY] == {"pack_id": JOY, "title": rows[JOY]["title"], "known": True, "enabled": True, "waiting": 6,
                         "extract_route": "llm", "extract_model": "stub-model"}
    assert rows[OFF]["known"] is True and rows[OFF]["enabled"] is False and rows[OFF]["waiting"] is None
    assert (rows["no.such_pack"]["known"], rows["no.such_pack"]["enabled"], rows["no.such_pack"]["waiting"]) == (
        False, False, None)
    assert plan["since"] is None and plan["history_truncated"] is False
    assert plan["history_window"] == surfaces.BACKFILL_HISTORY_WINDOW and plan["history_records"] == 10
    assert plan["total_waiting"] == rows[JOY]["waiting"] + rows[EVENTS]["waiting"]

    for pack_id in (JOY, EVENTS):
        assert surfaces.run_pack_backfill(node.conn, pack_id, limit=50)["processed"] == rows[pack_id]["waiting"]
    after = {row["pack_id"]: row["waiting"] for row in runs.backfill_plan(node.conn, [JOY, EVENTS])["packs"]}
    assert after == {JOY: 0, EVENTS: 0}


def test_the_count_and_the_batch_use_the_same_prefilter_rule() -> None:
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.prefilter import PackPrefilter, _tokens

    prefilter = PackPrefilter(load_packs(bundled_pack_dir(), only=[JOY])[JOY])
    for text in (HIT.format(n=1), MISS.format(n=1), "", "We finally got the keys", "thank you for driving out"):
        low = text.lower()
        assert prefilter.matches(low, _tokens(low)) is prefilter.passes(text), text
    assert prefilter.hits + prefilter.misses == 5, "matches() counts nothing; passes() counts each call"


# ----------------------------------------------------------------------------- the run (a scripted batch runner)


class _Script:
    """A batch runner over a made-up history: `waiting[pack]` records, one `on_record` per record."""

    def __init__(self, waiting: Dict[str, int]) -> None:
        self.waiting = dict(waiting)
        self.calls: List[tuple] = []
        self.since: List[Optional[str]] = []
        self.inflight = 0
        self.most = 0
        self.hold = threading.Event()
        self.hold.set()
        self.lock = threading.Lock()

    def __call__(self, pack_id, limit, should_stop, on_record, since=None) -> Dict[str, Any]:
        with self.lock:
            self.inflight += 1
            self.most = max(self.most, self.inflight)
            self.calls.append((pack_id, limit))
            self.since.append(since)
        try:
            stats = {"processed": 0, "assertions": 0, "accepted": 0, "written": 0, "quarantined": 0, "llm_errors": 0,
                     "extract_s": 0.0, "verify_s": 0.0, "extract_model": f"reader-of-{pack_id}",
                     "verify_model": "stub-verifier"}
            while stats["processed"] < limit and self.waiting[pack_id] > 0:
                while not self.hold.is_set() and not should_stop():
                    time.sleep(0.002)
                if should_stop():
                    return {**stats, "stopped": True}
                self.waiting[pack_id] -= 1
                stats["processed"] += 1
                stats["assertions"] += 2
                stats["accepted"] += 1
                stats["written"] += 1
                stats["extract_s"] += 0.5
                stats["verify_s"] += 2.0
                on_record(dict(stats))
            return stats
        finally:
            with self.lock:
                self.inflight -= 1


def test_packs_run_one_after_another_batch_after_batch_until_each_is_done() -> None:
    script = _Script({"a.one": 5, "b.two": 4})
    snap = _wait(runs.start_run(["a.one", "b.two"], 2, True, run_batch=script))
    assert script.calls == [("a.one", 2)] * 3 + [("b.two", 2)] * 3, "a short (or empty) batch is what ends a pack"
    assert script.most == 1, "never two batches at once"
    assert snap["state"] == "done" and snap["reason"] is None and snap["finished_at"]
    assert [(p["pack_id"], p["state"], [b["processed"] for b in p["batches"]]) for p in snap["packs"]] == [
        ("a.one", "done", [2, 2, 1]), ("b.two", "done", [2, 2, 0])]
    assert [b["index"] for b in snap["packs"][0]["batches"]] == [1, 2, 3]
    assert snap["totals"] == {"processed": 9, "assertions": 18, "accepted": 9, "written": 9, "quarantined": 0,
                              "llm_errors": 0, "extract_s": 4.5, "verify_s": 18.0}, "the two times, apart"
    assert [(b["extract_s"], b["verify_s"]) for b in snap["packs"][0]["batches"]] == [(1.0, 4.0), (1.0, 4.0), (0.5, 2.0)]
    assert [(p["extract_model"], p["verify_model"]) for p in snap["packs"]] == [
        ("reader-of-a.one", "stub-verifier"), ("reader-of-b.two", "stub-verifier")]
    assert snap["since"] is None and set(script.since) == {None}
    assert runs.current() == snap, "the last run stays readable after it ends"


def test_without_until_done_each_pack_gets_one_batch() -> None:
    script = _Script({"a.one": 5, "b.two": 4})
    snap = _wait(runs.start_run(["a.one", "b.two"], 2, False, run_batch=script))
    assert script.calls == [("a.one", 2), ("b.two", 2)] and snap["state"] == "done"
    assert script.waiting == {"a.one": 3, "b.two": 2}


def test_the_status_carries_states_counts_and_stamps_only() -> None:
    script = _Script({"a.one": 3})
    script.hold.clear()
    run = runs.start_run(["a.one"], 2, True, run_batch=script)
    _until(lambda: run.snapshot()["packs"][0]["current"] is not None, "the first batch")
    live = run.snapshot()
    assert set(live) == {"run_id", "state", "reason", "origin", "waiting_for", "pack_ids", "limit", "until_done",
                         "since", "started_at", "finished_at", "elapsed_s", "packs", "totals"}
    assert (live["origin"], live["waiting_for"]) == ("owner", None), "the owner's yes started this one"
    assert set(live["packs"][0]) == {"pack_id", "state", "error", "batches", "current", "extract_model",
                                     "verify_model"}
    assert set(live["packs"][0]["current"]) == BATCH_KEYS
    assert live["state"] == "running" and live["packs"][0]["state"] == "running"
    script.hold.set()
    done = _wait(run)
    assert all(set(b) == BATCH_KEYS for b in done["packs"][0]["batches"])


def test_a_stop_ends_the_run_between_records_and_leaves_the_rest_waiting() -> None:
    script = _Script({"a.one": 5, "b.two": 4})
    script.hold.clear()
    run = runs.start_run(["a.one", "b.two"], 2, True, run_batch=script)
    _until(lambda: script.calls, "the first batch")
    stopping = runs.stop()
    assert stopping["state"] == "stopping"
    snap = _wait(run)
    assert snap["state"] == "stopped" and snap["reason"] == "stopped"
    assert [(p["pack_id"], p["state"]) for p in snap["packs"]] == [("a.one", "stopped"), ("b.two", "waiting")]
    assert script.calls == [("a.one", 2)] and script.waiting == {"a.one": 5, "b.two": 4}
    assert runs.stop() is None, "nothing is active any more"


def test_one_run_at_a_time_and_never_beside_a_single_batch() -> None:
    script = _Script({"a.one": 1})
    script.hold.clear()
    run = runs.start_run(["a.one"], 2, False, run_batch=script)
    with pytest.raises(runs.BackfillBusy):
        runs.start_run(["b.two"], 2, False, run_batch=script)
    with pytest.raises(runs.BackfillBusy):
        with runs.single_batch():
            raise AssertionError("a single batch started beside a run")
    script.hold.set()
    _wait(run)

    with runs.single_batch():
        with runs.single_batch():
            pass                                     # two of the app's batches may overlap, as before
        with pytest.raises(runs.BackfillBusy):
            runs.start_run(["a.one"], 2, False, run_batch=script)
    assert runs.current()["run_id"] == run.run_id, "the refused start left no run behind"
    _wait(runs.start_run(["a.one"], 2, False, run_batch=script))   # and the hold is released


def test_a_model_that_is_down_ends_the_run_instead_of_spinning() -> None:
    asked: List[str] = []

    def down(pack_id, limit, should_stop, on_record, since=None):
        errors = 0
        while errors < limit:
            if should_stop():
                return {"processed": errors, "llm_errors": errors, "stopped": True}
            errors += 1
            asked.append(pack_id)
            on_record({"processed": errors, "llm_errors": errors})
        return {"processed": errors, "llm_errors": errors}

    snap = _wait(runs.start_run(["a.one", "b.two"], 200, True, run_batch=down))
    assert asked == ["a.one"] * runs.MODEL_ERROR_STREAK, "it gave up after the streak, in the first batch"
    assert snap["state"] == "failed" and snap["reason"] == "model_unavailable"
    assert [(p["state"], p["error"]) for p in snap["packs"]] == [("failed", "model_unavailable"), ("waiting", None)]
    assert snap["packs"][0]["batches"][0]["llm_errors"] == runs.MODEL_ERROR_STREAK


def test_scattered_model_errors_do_not_end_the_run() -> None:
    def flaky(pack_id, limit, should_stop, on_record, since=None):
        errors = 0
        for n in range(1, 13):
            errors += n % 2                          # every other record fails; never two in a row
            on_record({"processed": n, "llm_errors": errors})
        return {"processed": 12, "llm_errors": errors}

    snap = _wait(runs.start_run(["a.one"], 200, True, run_batch=flaky))
    assert snap["state"] == "done" and snap["totals"]["llm_errors"] == 6


def test_the_run_yields_to_the_owners_requests_between_records(monkeypatch) -> None:
    from topos.core import owner_requests

    yields: List[str] = []
    monkeypatch.setattr(owner_requests, "pace", lambda: yields.append(threading.current_thread().name) or 0.0)
    snap = _wait(runs.start_run(["a.one"], 2, True, run_batch=_Script({"a.one": 3})))
    assert snap["totals"]["processed"] == 3 and yields == [runs.THREAD_NAME] * 3


def test_a_pack_switched_off_after_the_yes_is_skipped_and_the_rest_run() -> None:
    script = _Script({"b.two": 1})

    def run_batch(pack_id, limit, should_stop, on_record, since=None):
        if pack_id == "a.one":
            raise ValueError("pack a.one is not enabled")
        return script(pack_id, limit, should_stop, on_record)

    snap = _wait(runs.start_run(["a.one", "b.two"], 2, True, run_batch=run_batch))
    assert snap["state"] == "done"
    assert [(p["state"], p["error"]) for p in snap["packs"]] == [("failed", "pack_not_enabled"), ("done", None)]


def test_an_unexpected_batch_error_fails_the_run_and_its_text_stays_in_the_log() -> None:
    def boom(pack_id, limit, should_stop, on_record, since=None):
        raise RuntimeError("database is locked while reading a private sentence")

    snap = _wait(runs.start_run(["a.one", "b.two"], 2, True, run_batch=boom))
    assert snap["state"] == "failed" and snap["reason"] == "batch_failed"
    assert [(p["state"], p["error"]) for p in snap["packs"]] == [("failed", "batch_failed"), ("waiting", None)]
    assert "private sentence" not in json.dumps(snap)


def test_a_node_that_is_going_down_stops_the_run() -> None:
    from topos import runtime_shutdown

    script = _Script({"a.one": 5})
    script.hold.clear()
    run = runs.start_run(["a.one"], 2, True, run_batch=script)
    _until(lambda: script.calls, "the first batch")
    try:
        runtime_shutdown.request_shutdown("sigterm")
        snap = _wait(run)
    finally:
        runtime_shutdown.clear_shutdown()
    assert snap["state"] == "stopped" and snap["reason"] == "node_stopping"


def test_a_run_over_a_real_history_with_the_stub_model(node, model) -> None:
    """The default shape end to end: each batch is `run_pack_backfill` on the worker thread's own connection."""
    def run_batch(pack_id, limit, should_stop, on_record, since=None):
        conn = _open(node.path)
        try:
            return surfaces.run_pack_backfill(conn, pack_id, limit, should_stop=should_stop, on_record=on_record,
                                              since=since)
        finally:
            conn.close()

    waiting = {row["pack_id"]: row["waiting"] for row in runs.backfill_plan(node.conn, [JOY, EVENTS])["packs"]}
    snap = _wait(runs.start_run([JOY, EVENTS], 4, True, run_batch=run_batch))
    assert snap["state"] == "done", snap
    joy, events = snap["packs"]
    assert [b["processed"] for b in joy["batches"]] == [4, 2] and joy["state"] == "done"
    assert sum(b["processed"] for b in events["batches"]) == waiting[EVENTS] and events["state"] == "done"
    assert snap["totals"]["processed"] == waiting[JOY] + waiting[EVENTS]
    assert snap["totals"]["written"] >= 1
    assert len(model.extract) == waiting[JOY] + waiting[EVENTS], "one extraction call per waiting record, no repeats"
    again = _wait(runs.start_run([JOY, EVENTS], 4, True, run_batch=run_batch))
    assert again["totals"]["processed"] == 0, "a rerun finds nothing left"


def test_the_default_batch_runner_resolves_the_worker_threads_own_connection(node, model, monkeypatch) -> None:
    import topos.core.state as state

    handed: List[tuple] = []

    def resolver():
        conn = _open(node.path)
        handed.append((threading.current_thread().name, conn))
        return conn

    monkeypatch.setattr(state, "get_db_connection", resolver)
    snap = _wait(runs.start_run([JOY], 4, False))
    assert snap["state"] == "done" and snap["totals"]["processed"] == 4
    assert [name for name, _conn in handed] == [runs.THREAD_NAME]
    for _name, conn in handed:
        conn.close()


# ----------------------------------------------------------------------------- the plan check at the door


def test_a_plan_names_enabled_packs_and_a_sane_batch_size(node) -> None:
    assert runs.validate_plan(node.conn, [JOY, " " + EVENTS, JOY], 200) == [JOY, EVENTS]
    for pack_ids, limit, code in (
        ([OFF], 200, "pack_not_enabled"), (["no.such_pack"], 200, "unknown_pack"),
        ([], 200, "backfill_plan_invalid"), ("moments.joy", 200, "backfill_plan_invalid"),
        ([JOY, 7], 200, "backfill_plan_invalid"), ([" "], 200, "backfill_plan_invalid"),
        ([JOY], 0, "backfill_plan_invalid"), ([JOY], 1001, "backfill_plan_invalid"),
        ([JOY], True, "backfill_plan_invalid"), ([JOY], "200", "backfill_plan_invalid"),
    ):
        with pytest.raises(runs.BackfillRefused) as refused:
            runs.validate_plan(node.conn, pack_ids, limit)
        assert refused.value.code == code, (pack_ids, limit)


# ----------------------------------------------------------------------------- the routes, on the owner socket


def _mounted(node, monkeypatch):
    """The signal router as the node mounts it, over the scratch node; batches go to a scripted runner."""
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "1")
    for name in ("TOPOS_OWNER_DOOR_TEAM_IDS", "TOPOS_UDS_TEAM_IDS", "TOPOS_DEV_PROXY_ALLOWED"):
        monkeypatch.delenv(name, raising=False)
    from topos.api import signal

    monkeypatch.setattr(signal, "_entities_conn", lambda: node.conn)
    script = _Script({JOY: 3, EVENTS: 1})
    monkeypatch.setattr(runs, "_run_batch", script)
    app = FastAPI()
    app.include_router(signal.router, prefix="/v1")
    return app, script


@pytest.fixture
def door(node, tmp_path, monkeypatch):
    """The mounted routes behind the owner socket's wrapper, in process."""
    monkeypatch.setenv("TOPOS_UDS_PATH", str(tmp_path / "engine.sock"))
    app, script = _mounted(node, monkeypatch)
    return SimpleNamespace(app=app, script=script, socket=lambda: TestClient(uds.UDSChannelApp(app)))


def _yes() -> Dict[str, str]:
    return {"X-Topos-Owner-Confirm": uds.write_owner_confirmation()}


def _settle() -> Dict[str, Any]:
    run = runs._active
    if run is not None:
        run.thread.join(timeout=20)
    return runs.current()


def test_the_routes_are_in_the_census_with_the_verdicts_the_gate_reads() -> None:
    assert census.lookup("POST", BASE + "/runs").verdict == census.DANGEROUS
    assert census.lookup("POST", BASE + "/runs/current/stop").verdict == census.OPEN
    assert census.lookup("GET", BASE + "/runs/current").verdict == census.READ
    assert census.lookup("GET", BASE + "/plan").verdict == census.READ
    for method, path in (("POST", "/runs"), ("POST", "/runs/current/stop"), ("GET", "/runs/current"), ("GET", "/plan")):
        assert not census.owner_agent_route_allowed(method, BASE + path), "a coding agent's label reaches none of them"
        assert not census.app_agent_route_allowed(method, BASE + path)


def test_the_start_is_refused_without_the_owners_yes(door) -> None:
    with door.socket() as client:
        refused = client.post(BASE + "/runs", json={"pack_ids": [JOY], "limit": 2, "until_done": True})
        wrong = client.post(BASE + "/runs", json={"pack_ids": [JOY]}, headers={"X-Topos-Owner-Confirm": "a" * 64})
    for response in (refused, wrong):
        assert response.status_code == 403 and response.json() == REFUSAL
    assert runs.current() is None and door.script.calls == []


def test_a_live_confirmation_starts_one_run_and_is_spent(door) -> None:
    header = _yes()
    with door.socket() as client:
        started = client.post(BASE + "/runs", json={"pack_ids": [JOY, EVENTS], "limit": 2, "until_done": True},
                              headers=header)
        assert started.status_code == 202, started.text
        run = started.json()["run"]
        assert run["pack_ids"] == [JOY, EVENTS] and run["limit"] == 2 and run["until_done"] is True
        assert not uds.owner_confirmation_path().exists(), "the confirmation is consumed by the start"
        final = _settle()
        assert final["run_id"] == run["run_id"] and final["state"] == "done"
        assert door.script.calls == [(JOY, 2), (JOY, 2), (EVENTS, 2)]
        status = client.get(BASE + "/runs/current")
        assert status.status_code == 200 and status.json() == {"run": final}
        again = client.post(BASE + "/runs", json={"pack_ids": [JOY]}, headers=header)
        assert again.status_code == 403 and again.json() == REFUSAL, "one yes, one run"
    assert runs.current()["run_id"] == run["run_id"]


def test_the_start_is_refused_over_tcp_and_with_the_socket_gate_switched_off(door, monkeypatch) -> None:
    from topos.auth import require_api_key, resolve_request_principal
    from topos.principal import OWNER_APP, Principal

    # Over TCP the socket gate never runs; the owner's key alone must not start a run.
    door.app.dependency_overrides[require_api_key] = lambda: "key"
    door.app.dependency_overrides[resolve_request_principal] = lambda: Principal(OWNER_APP, "local_http", acting_user="o")
    try:
        with TestClient(door.app) as tcp:
            _yes()                                   # a live token on disk changes nothing for a TCP caller
            token = json.loads(uds.owner_confirmation_path().read_text())["token"]
            response = tcp.post(BASE + "/runs", json={"pack_ids": [JOY]}, headers={"X-Topos-Owner-Confirm": token})
            assert response.status_code == 403 and response.json() == REFUSAL
            assert tcp.get(BASE + "/runs/current").json() == {"run": None}
    finally:
        door.app.dependency_overrides.clear()
    uds.clear_owner_confirmation()

    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "0")
    with door.socket() as client:
        bare = client.post(BASE + "/runs", json={"pack_ids": [JOY]})
        assert bare.status_code == 403 and bare.json() == REFUSAL, "the route asks for the yes itself"
        assert runs.current() is None
        confirmed = client.post(BASE + "/runs", json={"pack_ids": [JOY]}, headers=_yes())
        assert confirmed.status_code == 202
    _settle()


def test_a_plan_that_cannot_run_is_refused_and_starts_nothing(door) -> None:
    cases = (
        ({"pack_ids": ["no.such_pack"]}, 404, "unknown_pack"),
        ({"pack_ids": [OFF]}, 409, "pack_not_enabled"),
        ({"pack_ids": []}, 400, "backfill_plan_invalid"),
        ({"pack_ids": [JOY], "limit": 0}, 400, "backfill_plan_invalid"),
        ({"pack_ids": [JOY], "limit": 1001}, 400, "backfill_plan_invalid"),
        ({"pack_ids": [JOY], "until_done": "yes"}, 400, "backfill_plan_invalid"),
        ({"pack_ids": [JOY], "records": [{"text": "planted"}]}, 400, "backfill_plan_invalid"),
        ({"limit": 5}, 400, "backfill_plan_invalid"),
    )
    with door.socket() as client:
        for body, status, code in cases:
            response = client.post(BASE + "/runs", json=body, headers=_yes())
            assert response.status_code == status and response.json()["error"] == code, (body, response.text)
    assert runs.current() is None and door.script.calls == []


def test_a_second_start_and_a_single_batch_are_409_while_a_run_works_and_stop_needs_no_yes(door, model) -> None:
    door.script.hold.clear()
    with door.socket() as client:
        first = client.post(BASE + "/runs", json={"pack_ids": [JOY, EVENTS], "limit": 2, "until_done": True},
                            headers=_yes())
        assert first.status_code == 202
        _until(lambda: door.script.calls, "the first batch")
        second = client.post(BASE + "/runs", json={"pack_ids": [EVENTS]}, headers=_yes())
        assert second.status_code == 409 and second.json()["error"] == "backfill_already_running"
        single = client.post(f"/v1/signal/derivation/packs/{EVENTS}/backfill", json={"limit": 5}, headers=_yes())
        assert single.status_code == 409 and single.json()["detail"]["error"] == "backfill_already_running"
        assert model.extract == [], "the app's single batch did not run beside the run"

        stop = client.post(BASE + "/runs/current/stop")
        assert stop.status_code == 200 and stop.json()["stopping"] is True
        assert stop.json()["run"]["state"] == "stopping"
        final = _settle()
        assert final["state"] == "stopped" and [p["state"] for p in final["packs"]] == ["stopped", "waiting"]
        idle = client.post(BASE + "/runs/current/stop")
        assert idle.status_code == 200 and idle.json() == {"stopping": False, "run": final}

        # With no run working, the app's single batch is served as before.
        after = client.post(f"/v1/signal/derivation/packs/{JOY}/backfill", json={"limit": 2}, headers=_yes())
        assert after.status_code == 200 and after.json()["processed"] == 2


def test_the_plan_and_the_status_are_reads(door, model) -> None:
    with door.socket() as client:
        plan = client.get(BASE + "/plan", params=[("pack_id", JOY), ("pack_id", OFF), ("pack_id", "no.such_pack")])
        assert plan.status_code == 200, plan.text
        rows = {row["pack_id"]: row["waiting"] for row in plan.json()["packs"]}
        assert rows == {JOY: 6, OFF: None, "no.such_pack": None} and plan.json()["total_waiting"] == 6
        assert client.get(BASE + "/plan").json()["packs"] == []
        assert client.get(BASE + "/runs/current").json() == {"run": None}
    assert model.extract == [] and runs.current() is None


def test_the_dispatcher_door_refuses_a_single_batch_beside_a_run(node, monkeypatch) -> None:
    import asyncio

    from topos.core.handlers import derivation as handler

    monkeypatch.setattr(handler.hub, "get_db_connection", lambda: node.conn)
    script = _Script({"a.one": 1})
    script.hold.clear()
    run = runs.start_run(["a.one"], 2, False, run_batch=script)
    try:
        reply = asyncio.run(handler.handle_run_pack_backfill(
            {"id": "r1", "payload": {"pack_id": JOY, "limit": 5}}))
    finally:
        script.hold.set()
        _wait(run)
    assert reply == {"id": "r1", "status": "error", "error": "backfill_already_running"}


# ----------------------------------------------------------------------------- the command, over a real socket


def test_the_command_end_to_end_over_a_real_owner_socket(node, model, monkeypatch) -> None:
    """`topos-node packs backfill` against the mounted routes behind the socket wrapper on a real unix socket: the
    real client, the real confirmation file, the real gate. Only the person (a patched terminal and prompt) and the
    batches (the scripted runner) are stand-ins."""
    import os
    import sys
    from pathlib import Path

    import click
    from click.testing import CliRunner

    from tests.topos.fake_node_socket import serve_fake_node
    from topos.cli import packs_cmd
    from topos.mcp_server import NodeRefused, NodeSocket

    app, script = _mounted(node, monkeypatch)
    socket_path = Path(os.environ["TOPOS_UDS_PATH"])
    uds.clear_owner_confirmation()
    prompts: List[str] = []
    terminal = SimpleNamespace(isatty=lambda: True)
    pipe = SimpleNamespace(isatty=lambda: False)
    monkeypatch.setattr(click, "confirm", lambda text, **kw: prompts.append(text) or True)
    monkeypatch.setattr(packs_cmd, "POLL_S", 0.02)
    monkeypatch.setenv("TOPOS_DERIVATION_VERIFIER_MODEL", "stub-verifier")
    args = ["backfill", "--pack", JOY, "--pack", EVENTS, "--limit", "2", "--until-done", "--since", "2026-10-15"]

    with serve_fake_node(SimpleNamespace(app=uds.UDSChannelApp(app)), socket_path):
        # What a coding agent with a shell can do: call the route, or run the command without a terminal.
        with pytest.raises(NodeRefused) as refused:
            NodeSocket().request_sync("POST", BASE + "/runs", json_body={"pack_ids": [JOY], "limit": 2})
        assert (refused.value.status, refused.value.code) == (403, "owner_confirmation_required")
        monkeypatch.setattr(packs_cmd, "sys", SimpleNamespace(stdin=pipe, stdout=pipe, exit=sys.exit))
        piped = CliRunner().invoke(packs_cmd.packs_group, args, catch_exceptions=False)
        assert piped.exit_code == packs_cmd.EXIT_NEEDS_HUMAN and prompts == []
        assert runs.current() is None and script.calls == [] and not uds.owner_confirmation_path().exists()

        # The owner, on a terminal.
        monkeypatch.setattr(packs_cmd, "sys", SimpleNamespace(stdin=terminal, stdout=terminal, exit=sys.exit))
        result = CliRunner().invoke(packs_cmd.packs_group, args, catch_exceptions=False)
        listing = CliRunner().invoke(packs_cmd.packs_group, ["list"], catch_exceptions=False)
        status = CliRunner().invoke(packs_cmd.packs_group, ["status"], catch_exceptions=False)

    assert result.exit_code == packs_cmd.EXIT_OK, result.output
    assert prompts == ["Run it?"]
    assert "Fact-pack backfill, records dated 2026-10-15 or later" in result.output
    assert re.search(rf"^  {re.escape(JOY)} +3 records waiting   read by stub-model$", result.output, re.M), result.output
    assert "Extraction: the local model stub-model reads every record, one call each." in result.output
    assert "Verification: every fact found is checked by stub-verifier" in result.output
    assert result.output.index("Verification:") < result.output.index(f"{JOY}:"), "the models are named before the yes"
    assert script.calls == [(JOY, 2), (JOY, 2), (EVENTS, 2)] and set(script.since) == {"2026-10-15"}
    assert result.output.count("  batch 1: 2 processed, 4 assertions, 2 accepted, 2 written, 0 quarantined") == 1
    assert "(extraction 1s, verification 4s)" in result.output
    assert "Done: 4 processed, 8 assertions, 4 accepted, 4 written, 0 quarantined" in result.output
    assert "(extraction 2s, verification 8s)." in result.output
    assert not uds.owner_confirmation_path().exists(), "the one confirmation was spent on the one start"
    assert listing.exit_code == 0 and f"{JOY}" in listing.output and "2 of " in listing.output
    assert status.exit_code == 0 and ": done" in status.output


# ----------------------------------------------------------------------------- how far back (--since)


def test_since_walks_only_records_dated_on_or_after_it_and_leaves_the_rest_for_later(node, model) -> None:
    """The entries are dated 10 to 19 October; `since` the 15th keeps j5..j9, three of which the prefilter passes."""
    stats = surfaces.run_pack_backfill(node.conn, JOY, limit=10, since="2026-10-15")
    assert stats["processed"] == 3 and sorted(model.entries()) == [5, 7, 8]
    marked = {r[0].rsplit(":", 1)[1] for r in node.conn.execute(
        "SELECT key FROM derivation_progress WHERE key LIKE ?", (f"{JOY}@%",))}
    assert marked == {"j5", "j6", "j7", "j8", "j9"}, "nothing older than the date was read or marked"
    assert surfaces.run_pack_backfill(node.conn, JOY, limit=10, since="2026-10-15")["processed"] == 0
    model.extract.clear()
    assert surfaces.run_pack_backfill(node.conn, JOY, limit=10)["processed"] == 3
    assert sorted(model.entries()) == [1, 2, 4], "a later backfill without the date reaches the older records"


def test_the_count_uses_the_same_date(node, model) -> None:
    plan = runs.backfill_plan(node.conn, [JOY], since="2026-10-15")
    assert plan["since"] == "2026-10-15" and plan["packs"][0]["waiting"] == 3 and plan["history_records"] == 5
    assert runs.backfill_plan(node.conn, [JOY])["packs"][0]["waiting"] == 6
    assert runs.backfill_plan(node.conn, [JOY], since="2027-01-01")["total_waiting"] == 0
    for bad in ("April", "2026-4-1", "2026-13-01", "2026-04-01T00:00:00", 20260401):
        with pytest.raises(runs.BackfillRefused) as refused:
            runs.backfill_plan(node.conn, [JOY], since=bad)
        assert refused.value.code == "backfill_plan_invalid", bad
    assert runs.validate_since(None) is None and runs.validate_since("") is None
    assert runs.validate_since("2026-04-01") == "2026-04-01"


def test_the_date_applies_to_messages_too_and_a_bad_one_never_reaches_sql(node) -> None:
    from topos.enrichment.jobs.canonical.derivation_job import _iter_history

    node.conn.execute("CREATE TABLE IF NOT EXISTS conversation_messages (message_id TEXT PRIMARY KEY,"
                      " conversation_id TEXT, content TEXT, event_at TEXT, actor_role TEXT, is_from_self INTEGER,"
                      " sender_id TEXT, sender_type TEXT, source_id TEXT, dataset_id TEXT)")
    for mid, when in (("old", "2026-03-31 23:59:59"), ("edge", "2026-04-01T00:00:00Z"), ("new", "2026-06-02T08:00:00Z")):
        node.conn.execute("INSERT INTO conversation_messages VALUES (?, 'c1', ?, ?, 'authored', 1, 'self', 'human',"
                          " 'imessage', 'd')", (mid, f"a message long enough to be read ({mid})", when))
    node.conn.commit()
    since = {r["record_id"] for r in _iter_history(node.conn, limit=50, since="2026-04-01")}
    assert {"edge", "new"} <= since and "old" not in since
    assert {r["record_id"] for r in _iter_history(node.conn, limit=50)} >= {"old", "edge", "new", "j0"}
    with pytest.raises(ValueError):
        _iter_history(node.conn, limit=50, since="2026-04-01' OR '1'='1")


def test_a_run_carries_its_date_to_every_batch() -> None:
    script = _Script({"a.one": 3, "b.two": 1})
    snap = _wait(runs.start_run(["a.one", "b.two"], 2, True, since="2026-04-01", run_batch=script))
    assert snap["state"] == "done" and snap["since"] == "2026-04-01"
    assert script.since == ["2026-04-01"] * 3


def test_more_history_than_the_window_is_said_not_hidden(node, model, monkeypatch) -> None:
    monkeypatch.setattr(surfaces, "BACKFILL_HISTORY_WINDOW", 4)
    plan = runs.backfill_plan(node.conn, [JOY])
    assert plan["history_truncated"] is True and plan["history_window"] == 4 and plan["history_records"] == 4
    assert runs.backfill_plan(node.conn, [JOY], since="2026-10-17")["history_truncated"] is False


# ----------------------------------------------------------------------------- the extractor the owner picked


class _Classifier:
    """Topos Facts, stubbed (no weights, no network): it knows the packs it is told it knows."""

    def __init__(self, knows=(JOY,), conn: Optional[sqlite3.Connection] = None, delay: float = 0.0) -> None:
        self._knows, self.calls, self.held = set(knows), [], []
        self.conn, self.delay = conn, delay

    def knows(self, pack_id: str) -> bool:
        return pack_id in self._knows

    def extract(self, pack_id: str, text: str) -> List[Dict[str, Any]]:
        self.calls.append(pack_id)
        if self.conn is not None:
            self.held.append(self.conn.in_transaction)
        if self.delay:
            time.sleep(self.delay)
        n = int(re.search(r"\(entry (\d+)\)", text).group(1))
        return [{"predicate": "moment.peak", "value": {"kind": "alive", "description": f"lake day number {n}"},
                 "confidence": 0.97, "about": "owner"}]


def _pick_topos_facts(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('facts_llm_provider', 'topos_facts')")
    conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('facts_llm_model', 'v0.1')")
    conn.commit()


def _extractors(conn: sqlite3.Connection) -> set:
    return {json.loads(r[0])["extractor"]["model"] for r in conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact' AND ontology_id IS NOT NULL")}


def test_a_pack_topos_facts_knows_is_read_by_it_and_never_by_the_llm(node, model, monkeypatch) -> None:
    """Before this the backfill never looked at the owner's pick: it always called the local extraction model."""
    stub = _Classifier(conn=node.conn, delay=0.01)
    loads: List[int] = []
    monkeypatch.setattr("topos.features.derivation.topos_facts.load", lambda: loads.append(1) or stub)
    monkeypatch.setenv("TOPOS_DERIVATION_VERIFIER_MODEL", "stub-verifier")
    _pick_topos_facts(node.conn)
    seen: List[Dict[str, Any]] = []
    stats = surfaces.run_pack_backfill(node.conn, JOY, limit=10, on_record=seen.append)
    assert stub.calls == [JOY] * 6 and model.extract == [], "six records, six classifier reads, no LLM extraction"
    assert not any(stub.held), "no write transaction is open while the classifier reads"
    assert len(model.verify) == stats["assertions"] == 6, "every assertion still goes to the verifier"
    assert stats["extract_model"] == "topos-facts-v0.1" and stats["verify_model"] == "stub-verifier"
    assert stats["extract_s"] >= 0.05 and stats["verify_s"] >= 0.0 and isinstance(stats["verify_s"], float)
    assert seen[-1]["extract_model"] == "topos-facts-v0.1" and seen[-1]["extract_s"] > 0
    assert stats["written"] >= 1 and _extractors(node.conn) == {"topos-facts-v0.1"}
    hits, llm_calls = node.conn.execute(
        "SELECT SUM(prefilter_hits), SUM(llm_calls) FROM pack_yield WHERE pack_id=?", (JOY,)).fetchone()
    assert (hits, llm_calls or 0) == (6, 0), "a classifier read is a prefilter hit and not an LLM call"
    assert len(loads) >= 6, "the encoders are touched per record, so a long batch does not let them go idle"


def test_a_pack_outside_its_spec_and_the_one_kept_on_the_llm_stay_on_the_llm(node, model, monkeypatch) -> None:
    stub = _Classifier(knows=("aspirations.goals",))
    monkeypatch.setattr("topos.features.derivation.topos_facts.load", lambda: stub)
    _pick_topos_facts(node.conn)
    stats = surfaces.run_pack_backfill(node.conn, JOY, limit=10)
    assert stub.calls == [] and len(model.extract) == 6 and stats["extract_model"] == "stub-model"
    assert _extractors(node.conn) == {"stub-model"}
    node.conn.execute("UPDATE pack_registry SET enabled=1 WHERE pack_id='aspirations.goals'")
    node.conn.commit()
    goals = surfaces.run_pack_backfill(node.conn, "aspirations.goals", limit=10)
    assert goals["extract_model"] == "stub-model" and stub.calls == [], "ROUTED_TO_LLM, whatever the spec lists"


def test_nothing_picked_means_the_classifier_is_never_loaded(node, model, monkeypatch) -> None:
    def boom():
        raise AssertionError("Topos Facts must not load unless the owner picked it")

    monkeypatch.setattr("topos.features.derivation.topos_facts.load", boom)
    stats = surfaces.run_pack_backfill(node.conn, JOY, limit=10)
    assert stats["processed"] == 6 and stats["extract_model"] == "stub-model" and len(model.extract) == 6


def test_a_pick_that_cannot_load_defers_the_batch_and_marks_nothing(node, model, monkeypatch) -> None:
    from topos.config.facts_llm import derivation_deferral, facts_classifier_version

    def offline():
        raise OSError("no network and no cached weights")

    monkeypatch.setattr("topos.features.derivation.topos_facts.load", offline)
    _pick_topos_facts(node.conn)
    with pytest.raises(surfaces.BackfillDeferred) as deferred:
        surfaces.run_pack_backfill(node.conn, JOY, limit=10)
    assert deferred.value.code == "topos_facts_unavailable"
    assert model.extract == [] and model.verify == [], "it does not fall back to the LLM behind the owner's pick"
    assert node.conn.execute("SELECT COUNT(*) FROM derivation_progress").fetchone()[0] == 0
    assert derivation_deferral(node.conn)["reason"].startswith("topos_facts_unavailable")
    assert facts_classifier_version(node.conn) == "v0.1", "the pick itself is the owner's and is left alone"


def test_a_run_ends_when_the_pick_cannot_load(node, model, monkeypatch) -> None:
    import topos.core.state as state

    def offline():
        raise OSError("no network and no cached weights")

    monkeypatch.setattr("topos.features.derivation.topos_facts.load", offline)
    monkeypatch.setattr(state, "get_db_connection", lambda: node.conn)
    _pick_topos_facts(node.conn)
    snap = _wait(runs.start_run([JOY, EVENTS], 4, True))
    assert snap["state"] == "failed" and snap["reason"] == "topos_facts_unavailable"
    assert [(p["state"], p["error"]) for p in snap["packs"]] == [("failed", "topos_facts_unavailable"),
                                                                 ("waiting", None)]
    assert model.extract == [] and snap["totals"]["processed"] == 0


def test_the_plan_names_the_model_that_would_read_each_pack_without_loading_one(node, model, monkeypatch) -> None:
    from topos.features.derivation import topos_facts

    def boom():
        raise AssertionError("a plan loads no model")

    monkeypatch.setattr(topos_facts, "load", boom)
    monkeypatch.setenv("TOPOS_DERIVATION_VERIFIER_MODEL", "stub-verifier")
    node.conn.execute("UPDATE pack_registry SET enabled=1 WHERE pack_id='aspirations.goals'")
    node.conn.commit()
    asked = [JOY, EVENTS, "aspirations.goals"]

    def routes(plan):
        return {row["pack_id"]: (row["extract_route"], row["extract_model"]) for row in plan["packs"]}

    plain = runs.backfill_plan(node.conn, asked)
    assert set(routes(plain).values()) == {("llm", "stub-model")} and plain["verify_model"] == "stub-verifier"
    assert plain["extraction"] == {"provider": "llm", "llm_model": "stub-model", "classifier_model": None,
                                   "classifier_spec_on_disk": None}

    _pick_topos_facts(node.conn)
    monkeypatch.setattr(topos_facts, "known_packs_offline", lambda: frozenset({EVENTS, "aspirations.goals"}))
    picked = runs.backfill_plan(node.conn, asked)
    assert routes(picked) == {JOY: ("llm", "stub-model"), EVENTS: ("classifier", "topos-facts-v0.1"),
                              "aspirations.goals": ("llm", "stub-model")}
    assert picked["extraction"] == {"provider": "topos_facts", "llm_model": "stub-model",
                                    "classifier_model": "topos-facts-v0.1", "classifier_spec_on_disk": True}

    monkeypatch.setattr(topos_facts, "known_packs_offline", lambda: None)
    unread = runs.backfill_plan(node.conn, asked)
    assert routes(unread) == {JOY: ("unknown", None), EVENTS: ("unknown", None),
                              "aspirations.goals": ("llm", "stub-model")}
    assert unread["extraction"]["classifier_spec_on_disk"] is False

    monkeypatch.setenv("TOPOS_DERIVATION_VERIFY", "off")
    assert runs.backfill_plan(node.conn, asked)["verify_model"] is None


def test_the_spec_is_read_from_disk_alone(tmp_path, monkeypatch) -> None:
    from topos.features.derivation import topos_facts

    monkeypatch.setenv(topos_facts.ENV_GATE, str(tmp_path))
    assert topos_facts.known_packs_offline() is None, "no spec file on this computer"
    (tmp_path / topos_facts.SPEC_FILE).write_text(json.dumps({"packs": {"life.events": {}, "work.career": {}}}))
    assert topos_facts.known_packs_offline() == frozenset({"life.events", "work.career"})
    (tmp_path / topos_facts.SPEC_FILE).write_text("not json")
    assert topos_facts.known_packs_offline() is None


# ----------------------------------------------------------------------------- the routes carry both


def test_the_routes_take_the_date_and_refuse_a_bad_one(door, model) -> None:
    with door.socket() as client:
        plan = client.get(BASE + "/plan", params=[("pack_id", JOY), ("since", "2026-10-15")])
        assert plan.status_code == 200 and plan.json()["packs"][0]["waiting"] == 3 and plan.json()["since"] == "2026-10-15"
        assert plan.json()["packs"][0]["extract_model"] == "stub-model"
        bad = client.get(BASE + "/plan", params=[("pack_id", JOY), ("since", "April")])
        assert bad.status_code == 400 and bad.json()["error"] == "backfill_plan_invalid"
        refused = client.post(BASE + "/runs", json={"pack_ids": [JOY], "since": "last spring"}, headers=_yes())
        assert refused.status_code == 400 and refused.json()["error"] == "backfill_plan_invalid"
        assert runs.current() is None
        started = client.post(BASE + "/runs", json={"pack_ids": [JOY], "limit": 2, "until_done": True,
                                                    "since": "2026-10-15"}, headers=_yes())
        assert started.status_code == 202 and started.json()["run"]["since"] == "2026-10-15"
    assert _settle()["state"] == "done" and set(door.script.since) == {"2026-10-15"}


def test_the_apps_single_batch_says_so_when_the_pick_cannot_load(door, node, model, monkeypatch) -> None:
    def offline():
        raise OSError("no network and no cached weights")

    monkeypatch.setattr("topos.features.derivation.topos_facts.load", offline)
    _pick_topos_facts(node.conn)
    with door.socket() as client:
        response = client.post(f"/v1/signal/derivation/packs/{JOY}/backfill", json={"limit": 2}, headers=_yes())
    assert response.status_code == 503 and response.json()["detail"]["error"] == "topos_facts_unavailable"
    assert model.extract == [] and node.conn.execute("SELECT COUNT(*) FROM derivation_progress").fetchone()[0] == 0

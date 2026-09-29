"""Node-side search refresh (refresh_loop.py): N7 restore after a drift drop, RD2 catch-up."""
import asyncio
import json
import stat
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_automatic_message_review import setup, answer
from tests.permissions_v2.test_reconciliation_provenance import legacy
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from tests.permissions_v2.test_knowledge_search import node_for
from topos.permissions_v2 import automatic_review_worker as worker_module
from topos.permissions_v2.automatic_message_review import prepare, publish
from topos.permissions_v2.automatic_review_worker import AutomaticReviewWorker
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.message_review_contract import AutomaticReviewRequest, AutomaticReviewStatus
from topos.permissions_v2.refresh_loop import STATE_FILE, RefreshLoop, RefreshSettings


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def settings(**overrides):
    values = dict(restore=True, debounce=0, min_interval=0, max_defer=0, backoff=10, max_backoff=40, max_attempts=3)
    values.update(overrides)
    return RefreshSettings(**values)


def loop_for(node, clock, **overrides):
    return RefreshLoop(ledger=node.ledger, root=node.index.root, index=lambda: node.index, worker=None,
                       settings=settings(**overrides), clock=clock)


def reassess(node, identity):
    """A second machine assessment of the same message: the review digest moves, the member stays."""
    resolver, reviews = node.corpus.resolver, node.corpus.reviews
    with owner():
        prepared = prepare(resolver, reviews, identity)
        publish(resolver, reviews, prepared, answer(prepared), now=node.now[0])


def receipts(node):
    with node.ledger._transaction() as db:
        return [json.loads(row["receipt_json"]) for row in
                db.execute("SELECT receipt_json FROM p2a_system_actions ORDER BY recorded_at, action_id")]


# -- N7 on the real p2c-v3 index -------------------------------------------------------------

def test_restore_rebuilds_an_index_that_a_review_change_dropped(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    assert node.rebuild() == {"grant-search": "ready"}
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)                                  # the published set
    reassess(node, identity)
    assert node.index.sweep(now=node.now[0]) == 1            # today's drift drop
    output, refused = node.search_request("Synthetic message", k=10)
    assert output is None and refused is not None            # dark until something rebuilds

    loop.observe(node.index)
    receipt = loop.run_pending()

    assert receipt.cause_classes == ["review_changed"]
    assert [(g.grant_id, g.state, g.member_count) for g in receipt.grants] == [("grant-search", "ready", 1)]
    output, refused = node.search_request("Synthetic message", k=10)
    assert refused is None and len(output["records"]) == 1
    stored = receipts(node)
    assert [r["action"] for r in stored] == ["search_index_restore"]
    assert stored[0]["actor"] == "node_system" and stored[0]["grants"][0]["policy_hash"]
    assert "Synthetic" not in json.dumps(stored)              # counts and ids only, never content


def test_never_builds_an_index_the_owner_never_published(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)
    loop.observe(node.index)
    assert loop.run_pending() is None
    assert not list(node.index.root.glob("grant-*.db"))
    assert receipts(node) == []


def test_a_drop_across_a_restart_is_still_restored(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    loop_for(node, Clock(node.now[0])).observe(node.index)
    state = node.index.root / STATE_FILE
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    reassess(node, identity)
    node.index.sweep(now=node.now[0])

    restarted = loop_for(node, Clock(node.now[0]))
    restarted.observe(node.index)
    receipt = restarted.run_pending()

    assert receipt.cause_classes == ["restart_gap"]
    assert receipt.grants[0].state == "ready"


def test_a_revoked_grant_is_never_restored(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)
    with owner():
        node.ledger.revoke("grant-search", expected_epoch=node.epoch(), command_id="revoke-search")
    node.index.sweep(now=node.now[0])
    loop.observe(node.index)
    assert loop.run_pending() is None
    assert not list(node.index.root.glob("grant-*.db"))


def test_restore_off_by_default_does_nothing(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    loop = RefreshLoop(ledger=node.ledger, root=node.index.root, index=lambda: node.index, worker=None,
                       settings=RefreshSettings.from_env({}))
    loop.observe(node.index)
    reassess(node, identity)
    node.index.sweep(now=node.now[0])
    loop.observe(node.index)
    assert loop.run_pending() is None
    assert not (node.index.root / STATE_FILE).exists()


def test_system_action_receipts_need_the_owner_process(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    with owner(actor="recipient"), pytest.raises(PolicyError):
        node.ledger.record_system_action({"version": "x"}, now=1)


# -- scheduling, with the ledger and index replaced -------------------------------------------

class FakeIndex:
    def __init__(self, root, results):
        self.root, self.results, self.calls = root, list(results), []

    def rebuild(self, grant_id, *, now):
        self.calls.append(now)
        state = self.results.pop(0)
        if state == "ready":
            (self.root / f"grant-{grant_id}.db").write_bytes(b"")
        return {"state": state, "member_count": 1 if state == "ready" else 0}


class ScheduledLoop(RefreshLoop):
    """Scheduling only: one active grant, fixed signals, receipts kept in memory."""

    def __init__(self, root, index, clock, worker=None, **overrides):
        ledger = SimpleNamespace(identity=SimpleNamespace(owner_id="owner-1"))
        super().__init__(ledger=ledger, root=root, index=lambda: index, worker=worker,
                         settings=settings(**overrides), clock=clock)
        self.recorded = []

    def _dropped_grants(self, now, names):
        return [name[len("grant-"):-len(".db")] for name in sorted(names)]

    def _policy_hash(self, grant_id, now):
        return "a" * 64

    def _current_signals(self, service):
        return ("digest", ("clock", 1))

    def _record(self, receipt):
        self.recorded.append(receipt)


def drop(root, loop, index, grant="g1"):
    path = root / f"grant-{grant}.db"
    path.write_bytes(b"")
    loop.observe(index)
    path.unlink()
    loop.observe(index)


def test_drops_are_coalesced_and_rate_limited(tmp_path):
    clock = Clock(1000)
    index = FakeIndex(tmp_path, ["ready", "ready", "ready"])
    loop = ScheduledLoop(tmp_path, index, clock, debounce=30, min_interval=300)
    drop(tmp_path, loop, index, "g1")
    clock.now = 1010
    drop(tmp_path, loop, index, "g2")
    clock.now = 1025
    assert loop.run_pending() is None                         # still inside the debounce
    clock.now = 1031
    receipt = loop.run_pending()
    assert [g.grant_id for g in receipt.grants] == ["g1", "g2"] and len(index.calls) == 2
    loop.observe(index)                                       # the next sweep sees both republished
    (tmp_path / "grant-g1.db").unlink()
    loop.observe(index)
    clock.now = 1200
    assert loop.run_pending() is None                         # inside min_interval of the last pass
    clock.now = 1331
    assert [g.grant_id for g in loop.run_pending().grants] == ["g1"]


def test_failed_restore_backs_off_then_gives_up(tmp_path):
    clock = Clock(1000)
    index = FakeIndex(tmp_path, ["failed", "stale", "failed"])
    loop = ScheduledLoop(tmp_path, index, clock, backoff=10, max_attempts=3)
    drop(tmp_path, loop, index)
    assert loop.run_pending().grants[0].state == "failed"
    clock.now = 1009
    assert loop.run_pending() is None
    clock.now = 1010
    assert loop.run_pending().grants[0].state == "stale"
    clock.now = 1029
    assert loop.run_pending() is None                         # backoff doubled to 20 s
    clock.now = 1030
    assert loop.run_pending().grants[0].state == "failed"
    clock.now = 5000
    assert loop.run_pending() is None and len(index.calls) == 3   # dark until the owner acts, as before


class FakeWorker:
    def __init__(self):
        self.is_running, self.started, self.status_value = False, [], None

    def running(self):
        return self.is_running

    def start_node_pass(self, request, *, now, ingested_after, max_assessed):
        self.started.append(dict(after=request.after, before=request.before, ingested_after=ingested_after,
                                 max_assessed=max_assessed))
        self.is_running = True

    def status(self):
        return self.status_value


def test_a_running_assessment_defers_a_restore_up_to_the_limit(tmp_path):
    clock = Clock(1000)
    index, worker = FakeIndex(tmp_path, ["ready"]), FakeWorker()
    loop = ScheduledLoop(tmp_path, index, clock, worker=lambda: worker, max_defer=600)
    drop(tmp_path, loop, index)
    worker.is_running = True
    clock.now = 1300
    assert loop.run_pending() is None
    clock.now = 1600
    assert loop.run_pending().grants[0].state == "ready"


class CatchUpLoop(ScheduledLoop):
    def __init__(self, *args, window=30 * 86400, new_rows=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.window, self.new_rows = window, new_rows

    def _window_seconds(self, now):
        return self.window

    def _new_ingest(self, path, high_water):
        return self.new_rows


def finish(worker, **counts):
    worker.is_running = False
    worker.status_value = AutomaticReviewStatus(state="complete", **counts)


def test_catch_up_runs_a_full_pass_then_only_changed_conversations(tmp_path):
    T = 1_790_000_000
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, max_assessed=50,
                       catchup_interval=300, full_interval=86_400)
    loop.run_catchup()
    assert worker.started[-1] == dict(after=T - 30 * 86400, before=T, ingested_after=None, max_assessed=50)
    finish(worker, scanned=10, assessed=4, current=6)
    clock.now = T + 60
    receipt = loop.run_catchup()
    assert (receipt.cause_class, receipt.scope, receipt.assessed, receipt.budget_exhausted) == \
        ("startup_backlog", "full_window", 4, False)
    state = json.loads((tmp_path / STATE_FILE).read_text())
    assert state["last_full_pass_at"] == T and state["ingest_high_water"] == T

    clock.now = T + 400
    loop.run_catchup()                                        # nothing new: no pass
    assert len(worker.started) == 1
    loop.new_rows = True
    clock.now = T + 800
    loop.run_catchup()
    assert worker.started[-1]["ingested_after"] == T
    finish(worker, scanned=2, assessed=2)
    clock.now = T + 810
    assert loop.run_catchup().scope == "changed_conversations"

    clock.now = T + 86_400
    loop.run_catchup()
    assert worker.started[-1]["ingested_after"] is None      # the daily full reconciliation
    worker.is_running = False
    worker.status_value = AutomaticReviewStatus(state="complete", assessed=50)
    clock.now += 10
    receipt = loop.run_catchup()
    assert receipt.cause_class == "daily_reconciliation" and receipt.budget_exhausted
    assert json.loads((tmp_path / STATE_FILE).read_text())["last_full_pass_at"] == T   # not advanced


def test_quiet_ticks_read_nothing_between_intervals(tmp_path):
    T = 1_790_000_000
    clock, worker = Clock(T), FakeWorker()
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, catchup_interval=300)
    reads = []
    loop._window_seconds = lambda now: reads.append(now) or 30 * 86400
    loop.run_catchup()
    finish(worker)
    for offset in (5, 60, 150, 299):                          # the 5 s ticks inside one interval
        clock.now = T + offset
        loop.run_catchup()
    assert reads == [T]                                       # the ledger was read once, not per tick
    clock.now = T + 310
    loop.run_catchup()
    assert reads == [T, T + 310]


def test_catch_up_never_interferes_with_the_owners_pass(tmp_path):
    clock, worker = Clock(100_000), FakeWorker()
    worker.is_running = True
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True)
    assert loop.run_catchup() is None and worker.started == []


def test_catch_up_needs_restore_and_is_off_by_default():
    assert not RefreshSettings.from_env({}).enabled
    only_catchup = RefreshSettings.from_env({"TOPOS_PERMISSIONS_V2_ASSESSMENT_CATCHUP_ENABLED": "true"})
    assert not only_catchup.catchup and not only_catchup.enabled
    both = RefreshSettings.from_env({"TOPOS_PERMISSIONS_V2_INDEX_RESTORE_ENABLED": "true",
                                     "TOPOS_PERMISSIONS_V2_ASSESSMENT_CATCHUP_ENABLED": "true",
                                     "TOPOS_PERMISSIONS_V2_INDEX_RESTORE_MIN_INTERVAL_SECONDS": "5"})
    assert both.catchup and both.min_interval == 60           # floored


def test_after_sweep_never_raises(tmp_path):
    loop = ScheduledLoop(tmp_path, SimpleNamespace(), Clock(1))

    def broken(service):
        raise RuntimeError("ledger unavailable")

    loop.observe = broken
    loop.after_sweep(SimpleNamespace())                       # an exception would end the sweep thread


# -- the worker's node pass ------------------------------------------------------------------

def request_for(legacy):
    from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
    now = canonical_utc_microseconds(legacy[1].execute("SELECT event_at FROM conversation_messages").fetchone()[0]) // 1000000 + 1
    return AutomaticReviewRequest(after=now - 86400, before=now)


def test_node_pass_only_rechecks_conversations_with_new_rows_and_never_refreshes(legacy):
    conn = legacy[1]
    conn.execute("ALTER TABLE conversation_messages ADD COLUMN ingested_at TEXT")
    conn.execute("UPDATE conversation_messages SET ingested_at='2026-09-20T12:00:00Z'")
    conn.commit()
    resolver, reviews, _, _ = setup(legacy)
    refreshed = []

    async def classify(prepared):
        return answer(prepared)

    worker = AutomaticReviewWorker(resolver, reviews, classifier=classify, refresh=lambda: refreshed.append(1))
    import calendar
    ingested = calendar.timegm((2026, 9, 20, 12, 0, 0))
    with owner():
        asyncio.run(worker._process(request_for(legacy), refresh=False, ingested_after=ingested))
        assert worker.status().scanned == 0                  # nothing ingested after the high-water mark
        asyncio.run(worker._process(request_for(legacy), refresh=False, ingested_after=ingested - 60))
        assert worker.status().scanned == 1 and worker.status().assessed == 1
    assert refreshed == []


def test_node_pass_stops_at_its_model_budget(monkeypatch):
    rows = [("m1", "s", "d"), ("m2", "s", "d"), ("m3", "s", "d")]
    calls = []

    class Reviews:
        def _db(self):
            import contextlib
            return contextlib.nullcontext(None)

        def _current_in(self, db, key):
            return None

    resolver = SimpleNamespace(binding=None, _identity=lambda *a: a[1])
    worker = AutomaticReviewWorker(resolver, Reviews(), classifier=lambda p: asyncio.sleep(0, result="labels"))
    monkeypatch.setattr(worker, "_page", lambda table, after, request, ingested_after=None:
                        [row for row in rows if row[0] > after] if table == "conversation_messages" else [])
    monkeypatch.setattr(worker_module, "prepare", lambda resolver, reviews, identity: {"id": identity})
    monkeypatch.setattr(worker_module, "is_current", lambda previous, prepared: False)
    monkeypatch.setattr(worker_module, "machine_key", lambda identity: identity)
    monkeypatch.setattr(worker_module, "publish", lambda *a, **k: calls.append(a[2]["id"]))
    asyncio.run(worker._process(AutomaticReviewRequest(after=0, before=1), refresh=False, max_assessed=2))
    assert calls == ["m1", "m2"]


def test_node_pass_rejects_a_bad_budget(legacy):
    resolver, reviews, _, _ = setup(legacy)
    worker = AutomaticReviewWorker(resolver, reviews)
    with owner(), pytest.raises(PolicyError, match="message_review_budget_invalid"):
        worker.start_node_pass(request_for(legacy), max_assessed=0)
